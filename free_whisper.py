#!/usr/bin/env python3
"""FreeWhisper — Lightweight macOS dictation powered by Gladia."""

import json
import os
import sys
import time
import threading
import subprocess
import logging
import wave
import io
import shlex
import fcntl
import ctypes
import ctypes.util

from app_paths import ensure_user_data_file, resource_path, user_log_path, user_support_path

_log_file = user_log_path("debug.log")
logging.basicConfig(
    handlers=[
        logging.FileHandler(_log_file),
        logging.StreamHandler(sys.stderr),
    ],
    level=logging.DEBUG,
    format="%(asctime)s.%(msecs)03d %(message)s",
    datefmt="%H:%M:%S",
    force=True,
)
log = logging.getLogger("freewhisper")

import AppKit
import rumps
import rumps.rumps as rumps_runtime
import objc
import sounddevice as sd
import requests
import websocket as ws_lib
import ssl
import certifi
import Quartz
import ApplicationServices as AS
from PyObjCTools import AppHelper
from app_runtime import canonical_app_bundle_path, launch_program_arguments
from overlay import OverlayWindow
from settings_window import SettingsWindow
from app_paths import BUNDLE_ID
CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = ensure_user_data_file("config.json")
LOCK_FILE = user_support_path(".freewhisper.lock")

DEFAULT_CONFIG = {
    "api_key": "",
    "cohere_api_key": "",
    "provider": "gladia",
    "language": "fr",
    "hotkey": "alt_r",
    "hold_to_record": True,
    "sample_rate": 16000,
    "model": "solaria-1",
    "code_switching": False,
    "show_notifications": True,
    "launch_at_startup": False,
    "show_menu_bar_icon": True,
}

# Modifier keys: (keyCode, device-independent flag mask)
MODIFIER_KEYS = {
    "alt_r":   (61, 0x00080000),
    "alt_l":   (58, 0x00080000),
    "cmd_r":   (54, 0x00100000),
    "cmd_l":   (55, 0x00100000),
    "shift_r": (60, 0x00020000),
    "shift_l": (56, 0x00020000),
    "ctrl_r":  (62, 0x00040000),
    "ctrl_l":  (59, 0x00040000),
    "fn":      (63, 0x00800000),
}

# Regular keys: keyCode only
REGULAR_KEYS = {
    "`": 42,
}
MODIFIER_KEYCODE_FLAGS = {keycode: flag for keycode, flag in MODIFIER_KEYS.values()}


def _fourcc(text):
    return int.from_bytes(text.encode("ascii"), "big")


class EventTypeSpec(ctypes.Structure):
    _fields_ = [
        ("eventClass", ctypes.c_uint32),
        ("eventKind", ctypes.c_uint32),
    ]


class EventHotKeyID(ctypes.Structure):
    _fields_ = [
        ("signature", ctypes.c_uint32),
        ("id", ctypes.c_uint32),
    ]


_CARBON_LIB = None
_CARBON_HANDLER_PROC = ctypes.CFUNCTYPE(
    ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
)
_EVENT_CLASS_KEYBOARD = _fourcc("keyb")
_EVENT_HOTKEY_PRESSED = 5
_EVENT_HOTKEY_RELEASED = 6
_EVENT_PARAM_DIRECT_OBJECT = _fourcc("----")
_TYPE_EVENT_HOTKEY_ID = _fourcc("hkid")
_HOTKEY_SIGNATURE = _fourcc("FWHK")
_HOTKEY_ID_MAIN = 1
_HOTKEY_ID_CANCEL = 2
_CARBON_MOD_COMMAND = 1 << 8
_CARBON_MOD_SHIFT = 1 << 9
_CARBON_MOD_OPTION = 1 << 11
_CARBON_MOD_CONTROL = 1 << 12
_CARBON_MOD_FN = 1 << 17
_SHOW_SETTINGS_NOTIFICATION = f"{BUNDLE_ID}.show-settings"

# ── Module-level CGEvent tap callback (avoids GC issues) ──
_app_ref = None
_target_keycode = 0
_target_flag = 0            # 0 = regular key, >0 = modifier flag mask
_required_mod_flags = 0     # additional modifier flags for combos
_hold_to_record = True
_hotkey_pressed = False
_cancel_keycode = 53        # Escape by default
_cancel_mod_flags = 0       # modifier flags for cancel combo
_last_toggle = 0.0
_hotkey_events_suppressed = False
_HOTKEY_FLAG_MASK = 0x009E0000
_MIN_HOLD_TO_TRANSCRIBE = 0.25
_REMOTE_SESSION_GRACE = 0.25
_TEXT_INPUT_ROLES = {
    "AXComboBox",
    "AXSearchField",
    "AXTextArea",
    "AXTextField",
}
_BROWSER_APP_NAMES = {
    "Arc",
    "Brave Browser",
    "Codex",
    "Firefox",
    "Google Chrome",
    "Microsoft Edge",
    "Opera",
    "Safari",
}
_PLACEHOLDER_STRING_ATTRS = (
    "AXPlaceholderValue",
    "AXPlaceholderText",
    "AXDescription",
    "AXHelp",
    "AXTitle",
)


def _load_carbon_hotkey_lib():
    global _CARBON_LIB

    if _CARBON_LIB is not None:
        return _CARBON_LIB

    path = ctypes.util.find_library("Carbon")
    if not path:
        return None

    carbon = ctypes.cdll.LoadLibrary(path)
    carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
    carbon.InstallEventHandler.argtypes = [
        ctypes.c_void_p,
        _CARBON_HANDLER_PROC,
        ctypes.c_uint32,
        ctypes.POINTER(EventTypeSpec),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    carbon.InstallEventHandler.restype = ctypes.c_int32
    carbon.RemoveEventHandler.argtypes = [ctypes.c_void_p]
    carbon.RemoveEventHandler.restype = ctypes.c_int32
    carbon.RegisterEventHotKey.argtypes = [
        ctypes.c_uint32,
        ctypes.c_uint32,
        EventHotKeyID,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    carbon.RegisterEventHotKey.restype = ctypes.c_int32
    carbon.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
    carbon.UnregisterEventHotKey.restype = ctypes.c_int32
    carbon.GetEventClass.argtypes = [ctypes.c_void_p]
    carbon.GetEventClass.restype = ctypes.c_uint32
    carbon.GetEventKind.argtypes = [ctypes.c_void_p]
    carbon.GetEventKind.restype = ctypes.c_uint32
    carbon.GetEventParameter.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_void_p,
    ]
    carbon.GetEventParameter.restype = ctypes.c_int32

    _CARBON_LIB = carbon
    return carbon


def _masked_hotkey_flags(flags):
    return flags & _HOTKEY_FLAG_MASK


def _is_keycode_pressed(keycode):
    try:
        return bool(Quartz.CGEventSourceKeyState(
            Quartz.kCGEventSourceStateCombinedSessionState, keycode
        ))
    except Exception:
        return False


def _modifier_hotkey_is_active(flags):
    required = _target_flag | _required_mod_flags
    if (_masked_hotkey_flags(flags) & required) != required:
        return False
    # Use key state so left/right modifiers keep working even when other
    # modifiers also change while the hotkey is held.
    return _is_keycode_pressed(_target_keycode)


def _toggle_hotkey_action(source, keycode, flags):
    global _last_toggle

    now = time.time()
    dt = now - _last_toggle
    if dt < 0.4:
        log.debug(f"{source} debounce skip dt={dt:.3f}")
        return
    _last_toggle = now

    if _app_ref:
        rec = _app_ref.is_recording
        log.debug(f"{source} TOGGLE is_recording={rec} keycode={keycode} flags=0x{flags:08x}")
        if rec:
            _app_ref._on_hotkey_up()
        else:
            _app_ref._on_hotkey_down()


def _handle_hotkey_event(kind, keycode, flags, source):
    global _hotkey_pressed

    mod_flags = _masked_hotkey_flags(flags)
    suppress = (not _target_flag and keycode == _target_keycode
                and kind in ("down", "up"))

    if kind == "down" and keycode == _cancel_keycode:
        if mod_flags == _cancel_mod_flags and _app_ref and _app_ref.is_recording:
            log.debug(f"{source} global cancel -> CANCEL")
            _app_ref._do_cancel()
            return True

    if _hold_to_record:
        if _target_flag:
            if kind != "flags":
                return suppress

            active = _modifier_hotkey_is_active(flags)
            if active and not _hotkey_pressed:
                _hotkey_pressed = True
                log.debug(f"{source} HOTKEY DOWN keycode={keycode} flags=0x{flags:08x}")
                if _app_ref:
                    _app_ref._on_hotkey_down()
            elif _hotkey_pressed and not active:
                _hotkey_pressed = False
                log.debug(f"{source} HOTKEY UP keycode={keycode} flags=0x{flags:08x}")
                if _app_ref:
                    _app_ref._on_hotkey_up()
            return suppress

        if kind == "down" and keycode == _target_keycode:
            if mod_flags == _required_mod_flags and not _hotkey_pressed:
                _hotkey_pressed = True
                log.debug(f"{source} HOTKEY DOWN keycode={keycode} flags=0x{flags:08x}")
                if _app_ref:
                    _app_ref._on_hotkey_down()
            return suppress

        if kind == "up" and keycode == _target_keycode:
            if _hotkey_pressed:
                # Some non-modifier keys can emit a spurious key-up while they
                # are still physically held. Trust key state over the raw event.
                if _is_keycode_pressed(_target_keycode):
                    log.debug(f"{source} HOTKEY UP ignored (key still pressed) keycode={keycode} flags=0x{flags:08x}")
                else:
                    _hotkey_pressed = False
                    log.debug(f"{source} HOTKEY UP keycode={keycode} flags=0x{flags:08x}")
                    if _app_ref:
                        _app_ref._on_hotkey_up()
            return suppress

        if kind == "flags" and _hotkey_pressed:
            if (mod_flags & _required_mod_flags) != _required_mod_flags:
                _hotkey_pressed = False
                log.debug(f"{source} HOTKEY UP keycode={keycode} flags=0x{flags:08x}")
                if _app_ref:
                    _app_ref._on_hotkey_up()

        return suppress

    if _target_flag:
        if kind != "flags":
            return suppress

        active = _modifier_hotkey_is_active(flags)
        if active and not _hotkey_pressed:
            _hotkey_pressed = True
            _toggle_hotkey_action(source, keycode, flags)
        elif _hotkey_pressed and not active:
            _hotkey_pressed = False
        return suppress

    if kind == "down" and keycode == _target_keycode:
        if mod_flags == _required_mod_flags and not _hotkey_pressed:
            _hotkey_pressed = True
            _toggle_hotkey_action(source, keycode, flags)
        return suppress

    if kind == "up" and keycode == _target_keycode:
        _hotkey_pressed = False
        return suppress

    if kind == "flags" and _hotkey_pressed:
        if (mod_flags & _required_mod_flags) != _required_mod_flags:
            _hotkey_pressed = False

    return suppress


def _carbon_modifiers_for_hotkey(keycode, extra_flags):
    flags = int(extra_flags or 0) | MODIFIER_KEYCODE_FLAGS.get(int(keycode), 0)
    carbon_mods = 0
    if flags & 0x00100000:
        carbon_mods |= _CARBON_MOD_COMMAND
    if flags & 0x00020000:
        carbon_mods |= _CARBON_MOD_SHIFT
    if flags & 0x00080000:
        carbon_mods |= _CARBON_MOD_OPTION
    if flags & 0x00040000:
        carbon_mods |= _CARBON_MOD_CONTROL
    if flags & 0x00800000:
        carbon_mods |= _CARBON_MOD_FN
    return carbon_mods


def _carbon_hotkey_callback(call_ref, event_ref, user_data):
    carbon = _load_carbon_hotkey_lib()
    if carbon is None or _app_ref is None:
        return 0

    try:
        if carbon.GetEventClass(event_ref) != _EVENT_CLASS_KEYBOARD:
            return 0

        event_kind = carbon.GetEventKind(event_ref)
        if event_kind not in (_EVENT_HOTKEY_PRESSED, _EVENT_HOTKEY_RELEASED):
            return 0

        hotkey_id = EventHotKeyID()
        status = carbon.GetEventParameter(
            event_ref,
            _EVENT_PARAM_DIRECT_OBJECT,
            _TYPE_EVENT_HOTKEY_ID,
            None,
            ctypes.sizeof(hotkey_id),
            None,
            ctypes.byref(hotkey_id),
        )
        if status != 0:
            log.debug(f"CARBON GetEventParameter FAILED status={status}")
            return 0

        _app_ref._handle_carbon_hotkey_event(
            hotkey_id.id, event_kind == _EVENT_HOTKEY_PRESSED
        )
    except Exception:
        log.exception("CARBON hotkey callback FAILED")
    return 0


def request_existing_instance_show_settings():
    try:
        AppKit.NSDistributedNotificationCenter.defaultCenter().postNotificationName_object_userInfo_deliverImmediately_(
            _SHOW_SETTINGS_NOTIFICATION,
            None,
            None,
            True,
        )
        log.debug("Posted distributed show-settings request")
    except Exception as e:
        log.debug(f"Distributed show-settings request FAILED: {e}")


class _AppControlBridge(AppKit.NSObject):
    app_ref = objc.ivar()

    def showSettingsRequest_(self, notification):
        app = self.app_ref
        if app is not None:
            AppHelper.callAfter(app._handle_external_show_settings_request)


class FreeWhisperNSApp(rumps_runtime.NSApp):
    def applicationShouldHandleReopen_hasVisibleWindows_(self, ns_app, has_visible_windows):
        app = getattr(rumps_runtime.App, "*app_instance", None)
        if app is not None and hasattr(app, "_handle_external_show_settings_request"):
            AppHelper.callAfter(app._handle_external_show_settings_request)
        return True


rumps_runtime.NSApp = FreeWhisperNSApp


def _global_tap_callback(proxy, event_type, event, refcon):
    keycode = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
    flags = Quartz.CGEventGetFlags(event)

    if event_type == Quartz.kCGEventKeyDown:
        suppress = _handle_hotkey_event("down", keycode, flags, "TAP")
    elif event_type == Quartz.kCGEventKeyUp:
        suppress = _handle_hotkey_event("up", keycode, flags, "TAP")
    elif event_type == Quartz.kCGEventFlagsChanged:
        suppress = _handle_hotkey_event("flags", keycode, flags, "TAP")
    else:
        suppress = False

    return None if suppress else event


def load_config():
    cfg = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            cfg.update(json.load(f))
    return cfg


def paste_at_cursor(text, restore_clipboard=True, prefer_applescript=False, target_pid=None):
    """Paste text at cursor via clipboard and the most suitable paste method."""
    log.debug(f"paste_at_cursor start len={len(text)}")
    saved = None
    if restore_clipboard:
        saved = subprocess.run(["pbpaste"], capture_output=True, check=False).stdout
    if not copy_text_to_clipboard(text):
        log.debug("paste_at_cursor aborted (clipboard copy failed)")
        return False

    def _post_key(keycode, is_down, flags=0):
        event = Quartz.CGEventCreateKeyboardEvent(None, keycode, is_down)
        Quartz.CGEventSetFlags(event, flags)
        if target_pid:
            Quartz.CGEventPostToPid(int(target_pid), event)
        else:
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def _send_quartz_paste():
        try:
            cmd_flag = getattr(Quartz, "kCGEventFlagMaskCommand", 0x00100000)
            keycode_command = 55
            keycode_v = 9

            time.sleep(0.10)
            _post_key(keycode_command, True)
            _post_key(keycode_v, True, cmd_flag)
            _post_key(keycode_v, False, cmd_flag)
            _post_key(keycode_command, False)
            log.debug(
                "paste_at_cursor sent synthetic Cmd+V target_pid=%s"
                % (target_pid if target_pid else "(session)")
            )
            return True
        except Exception as e:
            log.debug(f"paste_at_cursor Quartz paste FAILED: {e}")
            return False

    def _send_applescript_paste():
        osa = subprocess.run(
            [
                "osascript", "-e",
                'tell application "System Events" to keystroke "v" using command down',
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        log.debug(
            "paste_at_cursor AppleScript rc=%s stderr=%s"
            % (osa.returncode, (osa.stderr or "").strip()[:200])
        )
        return osa.returncode == 0

    pasted = False
    if prefer_applescript:
        pasted = _send_applescript_paste()
        if not pasted:
            pasted = _send_quartz_paste()
    else:
        pasted = _send_quartz_paste()
        if not pasted:
            pasted = _send_applescript_paste()

    if restore_clipboard and saved is not None:
        def _restore_clipboard():
            time.sleep(0.75)
            restore_res = subprocess.run(["pbcopy"], input=saved, check=False)
            if restore_res.returncode != 0:
                log.debug(f"paste_at_cursor clipboard restore FAILED rc={restore_res.returncode}")
            else:
                log.debug("paste_at_cursor clipboard restored")

        threading.Thread(target=_restore_clipboard, daemon=True).start()
    else:
        log.debug("paste_at_cursor leaving transcript in clipboard")
    return pasted


def insert_text_at_cursor(text, target_pid=None):
    """Type text directly at cursor using Unicode key events."""
    log.debug(
        "insert_text_at_cursor start len=%s target_pid=%s"
        % (len(text), target_pid if target_pid else "(session)")
    )
    try:
        for ch in text:
            key_down = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
            Quartz.CGEventKeyboardSetUnicodeString(key_down, len(ch), ch)
            if target_pid:
                Quartz.CGEventPostToPid(int(target_pid), key_down)
            else:
                tap = getattr(Quartz, "kCGAnnotatedSessionEventTap", Quartz.kCGHIDEventTap)
                Quartz.CGEventPost(tap, key_down)

            key_up = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
            if target_pid:
                Quartz.CGEventPostToPid(int(target_pid), key_up)
            else:
                tap = getattr(Quartz, "kCGAnnotatedSessionEventTap", Quartz.kCGHIDEventTap)
                Quartz.CGEventPost(tap, key_up)
            time.sleep(0.002)
        log.debug("insert_text_at_cursor sent unicode events")
        return True
    except Exception as e:
        log.debug(f"insert_text_at_cursor FAILED: {e}")
        return False


def copy_text_to_clipboard(text):
    log.debug(f"copy_text_to_clipboard start len={len(text)}")
    copy_res = subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=False)
    if copy_res.returncode != 0:
        log.debug(f"copy_text_to_clipboard FAILED rc={copy_res.returncode}")
        return False
    log.debug("copy_text_to_clipboard OK")
    return True


def _is_regular_unmodified_hotkey(cfg):
    hotkey_name = cfg.get("hotkey")
    return hotkey_name in REGULAR_KEYS and not cfg.get("hotkey_mod_flags", 0)


def _hotkey_text_artifacts_possible(cfg):
    return _is_regular_unmodified_hotkey(cfg) and not _hotkey_events_suppressed


def _post_key_press(keycode, flags=0, tap=None):
    if tap is None:
        tap = Quartz.kCGHIDEventTap
    key_down = Quartz.CGEventCreateKeyboardEvent(None, keycode, True)
    Quartz.CGEventSetFlags(key_down, flags)
    Quartz.CGEventPost(tap, key_down)
    key_up = Quartz.CGEventCreateKeyboardEvent(None, keycode, False)
    Quartz.CGEventSetFlags(key_up, flags)
    Quartz.CGEventPost(tap, key_up)


_SYSTEM_SOUNDS = {"start": "Purr", "stop": "Bottle"}
_sound_cache = {}

def play_sound(name):
    sys_name = _SYSTEM_SOUNDS.get(name)
    if sys_name:
        snd = _sound_cache.get(name)
        if not snd:
            path = f"/System/Library/Sounds/{sys_name}.aiff"
            snd = AppKit.NSSound.alloc().initWithContentsOfFile_byReference_(path, True)
            _sound_cache[name] = snd
        snd.stop()
        snd.play()


def acquire_single_instance_lock():
    lock_handle = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_handle.close()
        return None
    lock_handle.write(str(os.getpid()))
    lock_handle.flush()
    return lock_handle


class FreeWhisperApp(rumps.App):
    def __init__(self, instance_lock):
        log.debug("===== FreeWhisper starting =====")
        self.cfg = load_config()
        log.debug(f"Config loaded: provider={self.cfg.get('provider')}, hotkey={self.cfg.get('hotkey')}")
        log.debug(f"Canonical app bundle: {canonical_app_bundle_path() or '(none)'}")
        icon_path = resource_path("iconTemplate.png")
        super().__init__("FreeWhisper", title=None, icon=icon_path, template=True)
        self._instance_lock = instance_lock

        self.is_recording = False
        self.ws = None
        self.ws_connected = False
        self.ws_done = threading.Event()
        self.audio_stream = None
        self._audio_buffer = []
        self._texts = []
        self._lock = threading.Lock()
        self._cooldown = 0.0
        self._record_started_at = 0.0
        self._stop_requested = False
        self._cancel_requested = False
        self._target_app_pid = None
        self._target_app_name = None
        self._target_ax_element = None
        self._target_ax_role = None
        self._target_selection_location = None
        self._target_selection_length = 0
        self._overlay = OverlayWindow()
        self._overlay.set_callbacks(cancel_cb=self._do_cancel, stop_cb=self._do_stop)
        self._settings = SettingsWindow(self)
        self._tap = None
        self._tap_source = None
        self._tap_timer = None
        self._key_state_timer = None
        self._global_key_monitor = None
        self._settings_timer = None
        self._carbon_handler_ref = None
        self._carbon_handler_proc = None
        self._carbon_hotkey_refs = {}
        self._control_bridge = _AppControlBridge.alloc().init()
        self._control_bridge.app_ref = self
        AppKit.NSDistributedNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self._control_bridge,
            b"showSettingsRequest:",
            _SHOW_SETTINGS_NOTIFICATION,
            None,
        )

        # Menu

        self.menu = [
            rumps.MenuItem("Settings", callback=self._open_settings),
        ]

        self._start_listener()
        AppHelper.callAfter(self._apply_menu_bar_icon_visibility)

    # ── Hotkey listener (Quartz CGEvent tap) ────────────────

    def _start_listener(self):
        global _app_ref, _target_keycode, _target_flag, _required_mod_flags
        global _hold_to_record, _hotkey_pressed
        global _cancel_keycode, _cancel_mod_flags, _hotkey_events_suppressed

        hotkey_name = self.cfg["hotkey"]
        _cancel_keycode = self.cfg.get("cancel_keycode", 53)
        _cancel_mod_flags = self.cfg.get("cancel_mod_flags", 0)
        _required_mod_flags = self.cfg.get("hotkey_mod_flags", 0)
        _hold_to_record = self.cfg.get("hold_to_record", True)
        _hotkey_pressed = False

        # Check modifier keys first, then regular keys
        key_info = MODIFIER_KEYS.get(hotkey_name)
        reg_keycode = REGULAR_KEYS.get(hotkey_name)

        if key_info:
            _target_keycode, _target_flag = key_info
        elif reg_keycode is not None:
            _target_keycode = reg_keycode
            _target_flag = 0
        elif hotkey_name.isdigit():
            # Raw keycode from settings key capture
            _target_keycode = int(hotkey_name)
            from settings_window import MODIFIER_FLAG_MAP
            _target_flag = MODIFIER_FLAG_MAP.get(_target_keycode, 0)
        else:
            all_keys = list(MODIFIER_KEYS.keys()) + list(REGULAR_KEYS.keys())
            rumps.notification("FreeWhisper", "Config Error",
                             f"Unsupported hotkey '{hotkey_name}'. Use: {', '.join(all_keys)}")
            return

        mask = (Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
                | Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
                | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp))
        _hotkey_events_suppressed = not _target_flag
        tap_option = (
            Quartz.kCGEventTapOptionDefault
            if _hotkey_events_suppressed
            else Quartz.kCGEventTapOptionListenOnly
        )

        _app_ref = self

        self._stop_quartz_listener()
        self._stop_carbon_hotkeys()
        self._stop_global_listener_fallback()

        if self._start_carbon_hotkeys(hotkey_name):
            return

        self._tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            tap_option,
            mask,
            _global_tap_callback,
            None,
        )
        if self._tap is not None:
            self._tap_source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
            Quartz.CFRunLoopAddSource(
                Quartz.CFRunLoopGetMain(),
                self._tap_source,
                Quartz.kCFRunLoopCommonModes,
            )
            Quartz.CGEventTapEnable(self._tap, True)
            log.info(
                "Event tap active — hotkey: %s (keycode=%s, suppress=%s)"
                % (hotkey_name, _target_keycode, _hotkey_events_suppressed)
            )

            # Re-enable tap if macOS disables it (happens after inactivity)
            self._tap_timer = rumps.Timer(self._check_tap, 5)
            self._tap_timer.start()
            if _hold_to_record and not _target_flag and not _hotkey_events_suppressed:
                self._key_state_timer = rumps.Timer(self._sync_regular_hotkey_state, 0.03)
                self._key_state_timer.start()
            return

        self._tap = None
        self._tap_source = None
        log.error("Cannot create event tap — hotkey listener unavailable")
        self._start_global_listener_fallback()

    def _check_tap(self, _):
        if not self._tap:
            return
        if not Quartz.CGEventTapIsEnabled(self._tap):
            log.debug("TAP was disabled by macOS — re-enabling")
            Quartz.CGEventTapEnable(self._tap, True)

    def _stop_quartz_listener(self):
        if self._tap_timer is not None:
            self._tap_timer.stop()
            self._tap_timer = None
        if self._key_state_timer is not None:
            self._key_state_timer.stop()
            self._key_state_timer = None
        if self._tap_source is not None:
            try:
                Quartz.CFRunLoopRemoveSource(
                    Quartz.CFRunLoopGetMain(),
                    self._tap_source,
                    Quartz.kCFRunLoopCommonModes,
                )
            except Exception:
                pass
            self._tap_source = None
        if self._tap is not None:
            try:
                Quartz.CFMachPortInvalidate(self._tap)
            except Exception:
                pass
            self._tap = None

    def _start_carbon_hotkeys(self, hotkey_name):
        carbon = _load_carbon_hotkey_lib()
        if carbon is None:
            log.debug("CARBON hotkeys unavailable (Carbon.framework not found)")
            return False

        if self._carbon_handler_proc is None:
            self._carbon_handler_proc = _CARBON_HANDLER_PROC(_carbon_hotkey_callback)

        specs = (EventTypeSpec * 2)(
            EventTypeSpec(_EVENT_CLASS_KEYBOARD, _EVENT_HOTKEY_PRESSED),
            EventTypeSpec(_EVENT_CLASS_KEYBOARD, _EVENT_HOTKEY_RELEASED),
        )
        handler_ref = ctypes.c_void_p()
        status = carbon.InstallEventHandler(
            carbon.GetApplicationEventTarget(),
            self._carbon_handler_proc,
            2,
            specs,
            None,
            ctypes.byref(handler_ref),
        )
        if status != 0:
            log.debug(f"CARBON InstallEventHandler FAILED status={status}")
            return False

        self._carbon_handler_ref = handler_ref

        main_mods = _carbon_modifiers_for_hotkey(_target_keycode, _required_mod_flags)
        main_ref = ctypes.c_void_p()
        status = carbon.RegisterEventHotKey(
            _target_keycode,
            main_mods,
            EventHotKeyID(_HOTKEY_SIGNATURE, _HOTKEY_ID_MAIN),
            carbon.GetApplicationEventTarget(),
            0,
            ctypes.byref(main_ref),
        )
        if status != 0:
            log.debug(
                "CARBON RegisterEventHotKey FAILED status=%s keycode=%s mods=0x%08x"
                % (status, _target_keycode, main_mods)
            )
            self._stop_carbon_hotkeys()
            return False

        self._carbon_hotkey_refs[_HOTKEY_ID_MAIN] = main_ref

        cancel_mods = _carbon_modifiers_for_hotkey(_cancel_keycode, _cancel_mod_flags)
        if (_cancel_keycode, cancel_mods) != (_target_keycode, main_mods):
            cancel_ref = ctypes.c_void_p()
            status = carbon.RegisterEventHotKey(
                _cancel_keycode,
                cancel_mods,
                EventHotKeyID(_HOTKEY_SIGNATURE, _HOTKEY_ID_CANCEL),
                carbon.GetApplicationEventTarget(),
                0,
                ctypes.byref(cancel_ref),
            )
            if status == 0:
                self._carbon_hotkey_refs[_HOTKEY_ID_CANCEL] = cancel_ref
            else:
                log.debug(
                    "CARBON cancel hotkey register skipped status=%s keycode=%s mods=0x%08x"
                    % (status, _cancel_keycode, cancel_mods)
                )

        log.info(
            "Carbon hotkey active — hotkey: %s (keycode=%s, mods=0x%08x)"
            % (hotkey_name, _target_keycode, main_mods)
        )
        return True

    def _stop_carbon_hotkeys(self):
        carbon = _load_carbon_hotkey_lib()
        if carbon is None:
            self._carbon_hotkey_refs.clear()
            self._carbon_handler_ref = None
            return

        for hotkey_ref in self._carbon_hotkey_refs.values():
            if hotkey_ref is None:
                continue
            try:
                carbon.UnregisterEventHotKey(hotkey_ref)
            except Exception:
                pass
        self._carbon_hotkey_refs.clear()

        if self._carbon_handler_ref is not None:
            try:
                carbon.RemoveEventHandler(self._carbon_handler_ref)
            except Exception:
                pass
            self._carbon_handler_ref = None

    def _handle_carbon_hotkey_event(self, hotkey_id, is_pressed):
        global _hotkey_pressed

        if hotkey_id == _HOTKEY_ID_CANCEL:
            if is_pressed and self.is_recording:
                log.debug("CARBON global cancel -> CANCEL")
                AppHelper.callAfter(self._do_cancel)
            return

        if hotkey_id != _HOTKEY_ID_MAIN:
            return

        if _hold_to_record:
            if is_pressed and not _hotkey_pressed:
                _hotkey_pressed = True
                log.debug(f"CARBON HOTKEY DOWN keycode={_target_keycode}")
                AppHelper.callAfter(self._on_hotkey_down)
            elif not is_pressed and _hotkey_pressed:
                _hotkey_pressed = False
                log.debug(f"CARBON HOTKEY UP keycode={_target_keycode}")
                AppHelper.callAfter(self._on_hotkey_up)
            return

        if is_pressed and not _hotkey_pressed:
            _hotkey_pressed = True
            AppHelper.callAfter(
                _toggle_hotkey_action,
                "CARBON",
                _target_keycode,
                _required_mod_flags | MODIFIER_KEYCODE_FLAGS.get(_target_keycode, 0),
            )
        elif not is_pressed:
            _hotkey_pressed = False

    def _start_global_listener_fallback(self):
        if self._global_key_monitor is not None:
            return

        mask = (
            AppKit.NSEventMaskKeyDown
            | AppKit.NSEventMaskKeyUp
            | AppKit.NSEventMaskFlagsChanged
        )
        self._global_key_monitor = AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            mask, self._on_global_key_event
        )
        log.info("NSEvent global monitor fallback active")

    def _stop_global_listener_fallback(self):
        if self._global_key_monitor is not None:
            AppKit.NSEvent.removeMonitor_(self._global_key_monitor)
            self._global_key_monitor = None

    def _handle_nsevent_hotkey_main_thread(self, kind, keycode, flags):
        _handle_hotkey_event(kind, keycode, flags, "NSEVENT")

    def _on_global_key_event(self, event):
        try:
            event_type = int(event.type())
            keycode = int(event.keyCode())
            flags = int(event.modifierFlags())
        except Exception as e:
            log.debug(f"NSEVENT decode FAILED: {e}")
            return

        if event_type == AppKit.NSEventTypeKeyDown:
            kind = "down"
        elif event_type == AppKit.NSEventTypeKeyUp:
            kind = "up"
        elif event_type == AppKit.NSEventTypeFlagsChanged:
            kind = "flags"
        else:
            return

        AppHelper.callAfter(self._handle_nsevent_hotkey_main_thread, kind, keycode, flags)

    def _sync_regular_hotkey_state(self, _):
        global _hotkey_pressed

        if not _hold_to_record or _target_flag or not _hotkey_pressed:
            return
        if _is_keycode_pressed(_target_keycode):
            return

        _hotkey_pressed = False
        log.debug(f"TIMER HOTKEY UP keycode={_target_keycode} (state poll)")
        self._on_hotkey_up()

    def _on_hotkey_down(self):
        self._do_start()

    def _on_hotkey_up(self):
        self._do_stop()

    def _refresh_target_context(self, label):
        try:
            front_app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
            if front_app is not None:
                self._target_app_pid = int(front_app.processIdentifier())
                self._target_app_name = str(front_app.localizedName() or "")
                log.debug(
                    f"{label} frontmost app pid={self._target_app_pid} name={self._target_app_name}"
                )
        except Exception as e:
            log.debug(f"{label} frontmost app lookup FAILED: {e}")

        try:
            system_wide = AS.AXUIElementCreateSystemWide()
            err, element = AS.AXUIElementCopyAttributeValue(
                system_wide, AS.kAXFocusedUIElementAttribute, None
            )
            if err == 0 and element is not None:
                self._target_ax_element = element
                role_err, role = AS.AXUIElementCopyAttributeValue(element, "AXRole", None)
                self._target_ax_role = str(role) if role_err == 0 and role is not None else None
                self._capture_target_selection(element, label)
                log.debug(
                    f"{label} focused AX element captured role={self._target_ax_role or '(unknown)'}"
                )
                return True

            self._target_ax_element = None
            self._target_ax_role = None
            self._target_selection_location = None
            self._target_selection_length = 0
            log.debug(f"{label} focused AX element lookup FAILED err={err}")
        except Exception as e:
            self._target_ax_element = None
            self._target_ax_role = None
            self._target_selection_location = None
            self._target_selection_length = 0
            log.debug(f"{label} focused AX element lookup FAILED: {e}")
        return False

    def _refresh_target_context_retry(self, label):
        if self._target_ax_element is not None:
            return
        self._refresh_target_context(label)

    def _apply_menu_bar_icon_visibility(self):
        should_show = bool(self.cfg.get("show_menu_bar_icon", True))
        nsapp_delegate = getattr(self, "_nsapp", None)
        status_item = getattr(nsapp_delegate, "nsstatusitem", None)
        if status_item is None:
            AppHelper.callLater(0.1, self._apply_menu_bar_icon_visibility)
            return

        try:
            if hasattr(status_item, "setVisible_"):
                status_item.setVisible_(should_show)
            elif not should_show:
                AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(status_item)
                if nsapp_delegate is not None:
                    nsapp_delegate.nsstatusitem = None
            log.debug(
                "_apply_menu_bar_icon_visibility show=%s"
                % ("yes" if should_show else "no")
            )
        except Exception as e:
            log.debug(f"_apply_menu_bar_icon_visibility FAILED: {e}")

    def _handle_external_show_settings_request(self):
        log.debug("Received external show-settings request")
        self._schedule_settings_presentation(0.0)

    def _present_settings_window(self):
        log.debug("_present_settings_window start")
        try:
            AppKit.NSApp.activateIgnoringOtherApps_(True)
        except Exception as e:
            log.debug(f"_present_settings_window activate FAILED: {e}")
        try:
            self._settings.show()
            log.debug("_present_settings_window done")
        except Exception:
            log.exception("_present_settings_window FAILED")

    def _schedule_settings_presentation(self, delay=0.1):
        log.debug(f"Scheduling Settings presentation in {delay:.2f}s")
        existing_timer = getattr(self, "_settings_timer", None)
        if existing_timer is not None:
            try:
                existing_timer.cancel()
            except Exception:
                pass
            self._settings_timer = None

        def _fire():
            log.debug("Settings presentation timer fired")
            AppHelper.callAfter(self._present_settings_window)

        if delay <= 0:
            _fire()
            return

        timer = threading.Timer(delay, _fire)
        timer.daemon = True
        self._settings_timer = timer
        timer.start()

    # ── Recording lifecycle ─────────────────────────────────

    def _capture_target_selection(self, element, label):
        self._target_selection_location = None
        self._target_selection_length = 0
        if element is None:
            return
        try:
            range_err, selected_range = AS.AXUIElementCopyAttributeValue(
                element, AS.kAXSelectedTextRangeAttribute, None
            )
            if range_err != 0 or selected_range is None:
                log.debug(f"{label} selection lookup FAILED err={range_err}")
                return
            ok, cf_range = AS.AXValueGetValue(
                selected_range, AS.kAXValueTypeCFRange, None
            )
            if not ok:
                log.debug(f"{label} selection decode FAILED")
                return
            location, length = cf_range
            self._target_selection_location = max(0, int(location))
            self._target_selection_length = max(0, int(length))
            log.debug(
                f"{label} selection captured loc={self._target_selection_location} len={self._target_selection_length}"
            )
        except Exception as e:
            log.debug(f"{label} selection capture FAILED: {e}")

    def _do_start(self):
        cd = time.time() - self._cooldown
        if cd < 0.6:
            log.debug(f"_do_start BLOCKED by cooldown ({cd:.3f}s)")
            return
        with self._lock:
            if self.is_recording:
                log.debug("_do_start BLOCKED already recording")
                return
            self.is_recording = True
        log.debug("_do_start OK -> recording")

        self._audio_buffer = []
        self._texts = []
        self.ws_connected = False
        self.ws_done.clear()
        self._record_started_at = time.time()
        self._stop_requested = False
        self._cancel_requested = False
        try:
            front_app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
            if front_app is not None:
                self._target_app_pid = int(front_app.processIdentifier())
                self._target_app_name = str(front_app.localizedName() or "")
                log.debug(
                    f"_do_start frontmost app pid={self._target_app_pid} name={self._target_app_name}"
                )
        except Exception as e:
            log.debug(f"_do_start frontmost app lookup FAILED: {e}")
            self._target_app_pid = None
            self._target_app_name = None

        if not self._refresh_target_context("_do_start"):
            AppHelper.callLater(0.05, self._refresh_target_context_retry, "_do_start retry")
            AppHelper.callLater(0.15, self._refresh_target_context_retry, "_do_start retry2")

        # Start mic FIRST so the overlay waveform reacts instantly
        try:
            input_device = self.cfg.get("input_device")
            if input_device is not None:
                input_device = int(input_device)
            self.audio_stream = sd.InputStream(
                device=input_device,
                samplerate=self.cfg["sample_rate"],
                channels=1,
                dtype="int16",
                blocksize=int(self.cfg["sample_rate"] * 0.1),
                callback=self._audio_cb,
            )
            self.audio_stream.start()
        except Exception as e:
            log.debug(f"_do_start mic FAILED: {e}")
            with self._lock:
                self.is_recording = False
            return

        play_sound("start")
        self._overlay.set_state("recording")
        from settings_window import keycode_to_name
        cancel_label = self.cfg.get("cancel_display",
                                    keycode_to_name(self.cfg.get("cancel_keycode", 53)))
        self._overlay.set_cancel_label(cancel_label)
        self._overlay.show()
        if (not self.cfg.get("hold_to_record", True)
                and _hotkey_text_artifacts_possible(self.cfg)):
            AppHelper.callLater(0.04, self._remove_toggle_start_hotkey_artifact)

        threading.Thread(target=self._worker_record, daemon=True).start()

    def _do_cancel(self):
        """Cancel recording without transcribing."""
        with self._lock:
            # Cancel if recording OR waiting for websocket
            if not self.is_recording and not getattr(self, 'ws_connected', False):
                return
            self.is_recording = False
            self._cancel_requested = True
            self._stop_requested = True
        log.debug("_do_cancel -> discarding")
        
        # Audio confirmation for cancel as requested by user
        play_sound("stop")
        
        self._overlay.hide()
        self._overlay.set_state("recording")
        AppHelper.callAfter(self._finish_without_insertion_main_thread)
        self._texts = [] # clear buffer
        if hasattr(self, 'ws_done'): self.ws_done.set()
        self._cleanup()

    def _do_stop(self):
        quick_tap = False
        with self._lock:
            if not self.is_recording:
                log.debug("_do_stop BLOCKED not recording")
                return
            self.is_recording = False
            held_for = time.time() - self._record_started_at
            self._stop_requested = True
            if held_for < _MIN_HOLD_TO_TRANSCRIBE:
                self._cancel_requested = True
                quick_tap = True
        if quick_tap:
            log.debug(f"_do_stop QUICK TAP ({held_for:.3f}s) -> cancel")
            AppHelper.callAfter(self._finish_without_insertion_main_thread)
            self._texts = []
            self._cleanup()
            return
        log.debug("_do_stop OK -> stopping")

        play_sound("stop")
        
        # Trigger the calm breathing processing animation for standard completion!
        self._overlay.set_state("waiting")
        if (not self.cfg.get("hold_to_record", True)
                and _hotkey_text_artifacts_possible(self.cfg)):
            AppHelper.callLater(0.04, self._remove_toggle_stop_hotkey_artifact)

        threading.Thread(target=self._worker_finalize, daemon=True).start()

    def _restore_target_app_focus(self):
        if not self._target_app_pid:
            log.debug("_restore_target_app_focus skipped (no target app)")
            return
        try:
            app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(
                self._target_app_pid
            )
            if app is None:
                log.debug(
                    f"_restore_target_app_focus missing app pid={self._target_app_pid}"
                )
                return
            app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
            log.debug(
                f"_restore_target_app_focus activated pid={self._target_app_pid} name={self._target_app_name or '(unknown)'}"
            )
        except Exception as e:
            log.debug(f"_restore_target_app_focus FAILED: {e}")

    def _log_frontmost_app(self, label):
        try:
            front_app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
            if front_app is None:
                log.debug(f"{label} frontmost app missing")
                return
            log.debug(
                f"{label} frontmost app pid={int(front_app.processIdentifier())} "
                f"name={str(front_app.localizedName() or '')}"
            )
        except Exception as e:
            log.debug(f"{label} frontmost app lookup FAILED: {e}")

    def _set_ax_caret_position(self, element, position, label):
        try:
            new_range = AS.AXValueCreate(
                AS.kAXValueTypeCFRange,
                AS.CFRangeMake(max(0, int(position)), 0),
            )
            sel_err = AS.AXUIElementSetAttributeValue(
                element, AS.kAXSelectedTextRangeAttribute, new_range
            )
            log.debug(f"{label} caret set err={sel_err} pos={position}")
            return sel_err == 0
        except Exception as e:
            log.debug(f"{label} caret set FAILED: {e}")
            return False

    def _reassert_ax_caret_position(self, position):
        try:
            system_wide = AS.AXUIElementCreateSystemWide()
            err, element = AS.AXUIElementCopyAttributeValue(
                system_wide, AS.kAXFocusedUIElementAttribute, None
            )
            if err == 0 and element is not None:
                self._set_ax_caret_position(element, position, "_reassert_ax_caret_position")
            elif self._target_ax_element is not None:
                self._set_ax_caret_position(
                    self._target_ax_element,
                    position,
                    "_reassert_ax_caret_position target",
                )
            else:
                log.debug(f"_reassert_ax_caret_position skipped err={err}")
        except Exception as e:
            log.debug(f"_reassert_ax_caret_position FAILED: {e}")

    def _clear_dead_key_state(self):
        if not _hotkey_text_artifacts_possible(self.cfg):
            return
        if self.cfg.get("hotkey") != "`":
            return

        try:
            # On layouts where ` is a dead key, a neutral "space + delete"
            # sequence flushes the pending accent without leaving visible text.
            time.sleep(0.03)
            _post_key_press(49)  # Space
            time.sleep(0.01)
            _post_key_press(51)  # Delete / Backspace
            log.debug("_clear_dead_key_state sent space+delete neutralizer")
        except Exception as e:
            log.debug(f"_clear_dead_key_state FAILED: {e}")

    def _should_skip_dead_key_neutralizer(self):
        if not _hotkey_text_artifacts_possible(self.cfg):
            return True
        if self.cfg.get("hold_to_record", True):
            return False
        return self._target_ax_element is not None

    def _get_focused_ax_target(self):
        try:
            system_wide = AS.AXUIElementCreateSystemWide()
            err, element = AS.AXUIElementCopyAttributeValue(
                system_wide, AS.kAXFocusedUIElementAttribute, None
            )
            if err != 0 or element is None:
                log.debug(f"_get_focused_ax_target FAILED err={err}")
                return None, None
            role_err, role = AS.AXUIElementCopyAttributeValue(element, "AXRole", None)
            role_name = str(role) if role_err == 0 and role is not None else None
            return element, role_name
        except Exception as e:
            log.debug(f"_get_focused_ax_target FAILED: {e}")
            return None, None

    def _ax_string_attribute(self, element, attr_name):
        try:
            err, value = AS.AXUIElementCopyAttributeValue(element, attr_name, None)
            if err != 0 or value is None:
                return None
            value = str(value).strip()
            return value or None
        except Exception:
            return None

    def _placeholder_backed_ax_value(self, element, current_text, location, length):
        text = (current_text or "").strip()
        if not text:
            return None
        if location != 0 or length != 0:
            return None
        if self._target_selection_location not in (None, 0):
            return None
        if int(self._target_selection_length or 0) != 0:
            return None

        for attr_name in _PLACEHOLDER_STRING_ATTRS:
            attr_value = self._ax_string_attribute(element, attr_name)
            if attr_value and attr_value.strip() == text:
                return attr_name, attr_value
        return None

    def _remove_hotkey_artifact_from_element(
        self,
        element,
        role,
        label,
        preferred_location=None,
        allow_current_selection_fallback=True,
        update_anchor=False,
    ):
        if element is None:
            log.debug(f"{label} skipped (no AX element)")
            return False
        if not _hotkey_text_artifacts_possible(self.cfg):
            return False
        if self.cfg.get("hotkey") != "`":
            return False

        try:
            value_err, current_value = AS.AXUIElementCopyAttributeValue(
                element, AS.kAXValueAttribute, None
            )
            if value_err != 0 or current_value is None:
                log.debug(f"{label} value lookup FAILED err={value_err}")
                return False

            current_text = str(current_value)
            candidate_indexes = []
            if preferred_location is not None:
                preferred_location = max(0, int(preferred_location))
                if preferred_location < len(current_text):
                    if current_text[preferred_location] in "`´^~¨":
                        candidate_indexes.append(preferred_location)
                if preferred_location > 0:
                    prev_idx = preferred_location - 1
                    if prev_idx < len(current_text) and current_text[prev_idx] in "`´^~¨":
                        candidate_indexes.append(prev_idx)

            if allow_current_selection_fallback:
                range_err, selected_range = AS.AXUIElementCopyAttributeValue(
                    element, AS.kAXSelectedTextRangeAttribute, None
                )
                if range_err == 0 and selected_range is not None:
                    ok, cf_range = AS.AXValueGetValue(
                        selected_range, AS.kAXValueTypeCFRange, None
                    )
                    if ok:
                        location, length = cf_range
                        if length == 0 and location > 0:
                            prev_idx = location - 1
                            if prev_idx < len(current_text) and current_text[prev_idx] in "`´^~¨":
                                candidate_indexes.append(prev_idx)
                    else:
                        log.debug(f"{label} range decode FAILED")
                else:
                    log.debug(f"{label} range lookup FAILED err={range_err}")

            candidate_indexes = list(dict.fromkeys(candidate_indexes))
            if not candidate_indexes:
                return False

            artifact_index = candidate_indexes[0]
            prev_char = current_text[artifact_index]
            new_text = current_text[:artifact_index] + current_text[artifact_index + 1:]
            set_err = AS.AXUIElementSetAttributeValue(
                element, AS.kAXValueAttribute, new_text
            )
            if set_err != 0:
                log.debug(f"{label} value set FAILED err={set_err} role={role or '(unknown)'}")
                return False

            self._set_ax_caret_position(element, artifact_index, label)
            if update_anchor:
                self._target_selection_location = artifact_index
                self._target_selection_length = 0
            log.debug(
                f"{label} removed hotkey artifact char={prev_char!r} role={role or '(unknown)'} pos={artifact_index}"
            )
            return True
        except Exception as e:
            log.debug(f"{label} FAILED: {e}")
            return False

    def _remove_toggle_stop_hotkey_artifact(self):
        element, role = self._get_focused_ax_target()
        if self._remove_hotkey_artifact_from_element(
            element,
            role,
            "_remove_toggle_stop_hotkey_artifact",
            self._target_selection_location,
            allow_current_selection_fallback=False,
        ):
            self._target_ax_element = element
            self._target_ax_role = role
            return
        self._remove_hotkey_artifact_from_element(
            self._target_ax_element,
            self._target_ax_role,
            "_remove_toggle_stop_hotkey_artifact target",
            self._target_selection_location,
            allow_current_selection_fallback=False,
        )

    def _remove_toggle_start_hotkey_artifact(self):
        element, role = self._get_focused_ax_target()
        if self._remove_hotkey_artifact_from_element(
            element,
            role,
            "_remove_toggle_start_hotkey_artifact",
            self._target_selection_location,
            allow_current_selection_fallback=False,
            update_anchor=True,
        ):
            self._target_ax_element = element
            self._target_ax_role = role
            return
        self._remove_hotkey_artifact_from_element(
            self._target_ax_element,
            self._target_ax_role,
            "_remove_toggle_start_hotkey_artifact target",
            self._target_selection_location,
            allow_current_selection_fallback=False,
            update_anchor=True,
        )

    def _target_text_input_capability(self):
        element = self._target_ax_element
        role = self._target_ax_role
        if element is None:
            log.debug("_target_text_input_capability -> none (no AX element)")
            return "none"
        if role in _TEXT_INPUT_ROLES:
            log.debug(f"_target_text_input_capability -> strong role={role}")
            return "strong"

        try:
            range_err, selected_range = AS.AXUIElementCopyAttributeValue(
                element, AS.kAXSelectedTextRangeAttribute, None
            )
            if range_err == 0 and selected_range is not None:
                log.debug(
                    f"_target_text_input_capability -> weak role={role or '(unknown)'}"
                )
                return "weak"
            log.debug(
                f"_target_text_input_capability -> none role={role or '(unknown)'} err={range_err}"
            )
        except Exception as e:
            log.debug(f"_target_text_input_capability lookup FAILED: {e}")
        return "none"

    def _target_prefers_keyboard_paste(self):
        app_name = (self._target_app_name or "").strip()
        prefers = app_name in _BROWSER_APP_NAMES
        if prefers:
            log.debug(
                f"_target_prefers_keyboard_paste -> yes app={app_name} role={self._target_ax_role or '(unknown)'}"
            )
        return prefers

    def _insert_text_via_accessibility(self, text):
        element = self._target_ax_element
        if element is None:
            log.debug("_insert_text_via_accessibility skipped (no AX element)")
            return False

        try:
            value_err, current_value = AS.AXUIElementCopyAttributeValue(
                element, AS.kAXValueAttribute, None
            )
            if value_err != 0 or current_value is None:
                log.debug(
                    f"_insert_text_via_accessibility value lookup FAILED err={value_err}"
                )
                return False

            current_text = str(current_value)
            range_err, selected_range = AS.AXUIElementCopyAttributeValue(
                element, AS.kAXSelectedTextRangeAttribute, None
            )
            current_location = len(current_text)
            current_length = 0
            if range_err == 0 and selected_range is not None:
                ok, cf_range = AS.AXValueGetValue(
                    selected_range, AS.kAXValueTypeCFRange, None
                )
                if not ok:
                    log.debug("_insert_text_via_accessibility range decode FAILED")
                    return False
                current_location, current_length = cf_range

            placeholder_match = self._placeholder_backed_ax_value(
                element, current_text, current_location, current_length
            )
            if placeholder_match is not None:
                attr_name, attr_value = placeholder_match
                log.debug(
                    "_insert_text_via_accessibility treating AXValue as placeholder "
                    f"attr={attr_name} value={attr_value!r}"
                )
                current_text = ""
                current_location = 0
                current_length = 0

            location = current_location
            length = current_length
            if self._target_selection_location is not None:
                location = max(0, min(int(self._target_selection_location), len(current_text)))
                max_replace = max(0, len(current_text) - location)
                length = max(0, min(int(self._target_selection_length), max_replace))
                log.debug(
                    "_insert_text_via_accessibility using captured selection "
                    f"loc={location} len={length} current_loc={current_location} current_len={current_length}"
                )

            if _hotkey_text_artifacts_possible(self.cfg) and length == 0:
                artifact_index = None
                if location < len(current_text) and current_text[location] in "`´^~¨":
                    artifact_index = location
                elif location > 0 and current_text[location - 1] in "`´^~¨":
                    artifact_index = location - 1

                if artifact_index is not None:
                    prev_char = current_text[artifact_index]
                    current_text = current_text[:artifact_index] + current_text[artifact_index + 1:]
                    if artifact_index < location:
                        location -= 1
                    log.debug(
                        f"_insert_text_via_accessibility removed hotkey artifact char={prev_char!r} pos={artifact_index}"
                    )

            new_text = (
                current_text[:location] + text + current_text[location + length:]
            )
            set_err = AS.AXUIElementSetAttributeValue(
                element, AS.kAXValueAttribute, new_text
            )
            if set_err != 0:
                log.debug(
                    f"_insert_text_via_accessibility value set FAILED err={set_err} role={self._target_ax_role or '(unknown)'}"
                )
                return False

            caret_position = location + len(text)
            self._set_ax_caret_position(
                element,
                caret_position,
                "_insert_text_via_accessibility",
            )
            # Some web textareas briefly reset the caret after value changes.
            AppHelper.callLater(0.05, self._reassert_ax_caret_position, caret_position)
            AppHelper.callLater(0.15, self._reassert_ax_caret_position, caret_position)
            log.debug(
                f"_insert_text_via_accessibility OK role={self._target_ax_role or '(unknown)'} caret={caret_position}"
            )
            self._target_selection_location = caret_position
            self._target_selection_length = 0
            return True
        except Exception as e:
            log.debug(f"_insert_text_via_accessibility FAILED: {e}")
            return False

    def _deliver_result_main_thread(self, final):
        self._restore_target_app_focus()
        if self._target_ax_element is None:
            self._refresh_target_context("_deliver_result_main_thread")
            if self._target_ax_element is None:
                time.sleep(0.05)
                self._refresh_target_context("_deliver_result_main_thread retry")
        if self._should_skip_dead_key_neutralizer():
            log.debug("_deliver_result_main_thread skipped dead-key neutralizer")
        else:
            self._clear_dead_key_state()
        self._log_frontmost_app("_paste_result")

        inserted = False
        copied_only = False
        target_capability = self._target_text_input_capability()
        if target_capability == "none":
            copy_text_to_clipboard(final)
            inserted = paste_at_cursor(
                final,
                restore_clipboard=False,
                prefer_applescript=False,
                target_pid=self._target_app_pid,
            )
            if inserted:
                log.debug("_deliver_result_main_thread used pid-targeted keyboard paste fallback")
            else:
                inserted = insert_text_at_cursor(final, target_pid=self._target_app_pid)
                if inserted:
                    log.debug("_deliver_result_main_thread used direct unicode typing fallback")
        else:
            inserted = paste_at_cursor(
                final,
                restore_clipboard=False,
                prefer_applescript=False,
                target_pid=self._target_app_pid,
            )
            if inserted:
                log.debug("_deliver_result_main_thread used global keyboard paste method=quartz-first")

        if not inserted and target_capability == "strong":
            inserted = self._insert_text_via_accessibility(final)
            if not inserted:
                inserted = insert_text_at_cursor(final, target_pid=self._target_app_pid)
        elif not inserted and target_capability == "weak":
            log.debug(
                "_deliver_result_main_thread weak text target -> AX insert + keep clipboard backup"
            )
            inserted = self._insert_text_via_accessibility(final)
        elif not inserted:
            log.debug("_deliver_result_main_thread no text target -> clipboard only")

        if not inserted:
            copied_only = copy_text_to_clipboard(final)
            if copied_only:
                log.debug("_deliver_result_main_thread kept transcript in clipboard")

        if self.cfg.get("show_notifications"):
            preview = (final[:77] + "\u2026") if len(final) > 80 else final
            title = "\u2713 Copied to clipboard" if copied_only else "\u2713 Transcribed"
            rumps.notification("FreeWhisper", title, preview)

    def _finish_without_insertion_main_thread(self):
        self._restore_target_app_focus()
        if self._should_skip_dead_key_neutralizer():
            log.debug("_finish_without_insertion_main_thread skipped dead-key neutralizer")
        else:
            self._clear_dead_key_state()

    # ── Background workers ──────────────────────────────────

    def _worker_record(self):
        cfg = self.cfg
        provider = cfg.get("provider", "gladia")

        api_key = cfg["api_key"] if provider == "gladia" else cfg.get("cohere_api_key", "")
        if not api_key:
            log.debug(f"WORKER no api_key for {provider}")
            self._cleanup()
            return

        if self._cancel_requested:
            log.debug("WORKER cancelled before provider setup")
            return

        # Mic already started in _do_start()

        # Cohere: just buffer audio, finalize will upload the file
        if provider == "cohere":
            log.debug("WORKER cohere mode — buffering audio")
            return

        # Avoid creating remote sessions for accidental taps. We already buffer
        # local audio immediately, so waiting here doesn't lose speech.
        grace_deadline = self._record_started_at + _REMOTE_SESSION_GRACE
        while time.time() < grace_deadline:
            if self._cancel_requested:
                log.debug("WORKER cancelled before Gladia session creation")
                return
            time.sleep(0.01)

        # 2) Init Gladia live session
        try:
            lang_cfg = {"languages": [cfg["language"]], "code_switching": cfg.get("code_switching", False)}
            if cfg["language"] == "auto":
                lang_cfg = {"languages": [], "code_switching": True}

            resp = requests.post(
                "https://api.gladia.io/v2/live",
                headers={"Content-Type": "application/json", "x-gladia-key": cfg["api_key"]},
                json={
                    "encoding": "wav/pcm",
                    "sample_rate": cfg["sample_rate"],
                    "bit_depth": 16,
                    "channels": 1,
                    "model": cfg.get("model", "solaria-1"),
                    "endpointing": 3,
                    "maximum_duration_without_endpointing": 60,
                    "language_config": lang_cfg,
                    "pre_processing": {
                        "audio_enhancer": True,
                    },
                    "realtime_processing": {
                        "named_entity_recognition": True,
                        "sentiment_analysis": True,
                    },
                    "messages_config": {"receive_partial_transcripts": False},
                },
                timeout=10,
            )
            if not resp.ok:
                log.debug(f"WORKER gladia API {resp.status_code}: {resp.text}")
            resp.raise_for_status()
            ws_url = resp.json()["url"]
            log.debug(f"WORKER gladia session OK url={ws_url[:60]}...")
            if self._cancel_requested:
                log.debug("WORKER cancelled after Gladia session creation — closing session")
        except Exception as e:
            log.debug(f"WORKER gladia API FAILED: {e}")
            self._cleanup()
            return

        # 3) Connect WebSocket
        try:
            self.ws = ws_lib.WebSocket(
                sslopt={"ca_certs": certifi.where(), "cert_reqs": ssl.CERT_REQUIRED}
            )
            self.ws.settimeout(30)
            self.ws.connect(ws_url)
            log.debug("WORKER websocket connected")
            if self._cancel_requested:
                log.debug("WORKER cancelled after websocket connect — sending stop_recording")
                try:
                    self.ws.send(json.dumps({"type": "stop_recording"}))
                except Exception:
                    pass
                try:
                    self.ws.close()
                except Exception:
                    pass
                self.ws = None
                self.ws_connected = False
                self.ws_done.set()
                return
        except Exception as e:
            log.debug(f"WORKER websocket FAILED: {e}")
            self._cleanup()
            return

        # 4) Start WS reader thread
        threading.Thread(target=self._ws_reader, daemon=True).start()

        # 5) Flush buffered audio then stream live
        self.ws_connected = True
        n = len(self._audio_buffer)
        for chunk in self._audio_buffer:
            try:
                self.ws.send(chunk, opcode=ws_lib.ABNF.OPCODE_BINARY)
            except Exception:
                break
        self._audio_buffer.clear()
        log.debug(f"WORKER streaming live (flushed {n} buffered chunks)")
        if self._stop_requested and self.ws:
            log.debug("WORKER stop was already requested — sending stop_recording now")
            try:
                self.ws.send(json.dumps({"type": "stop_recording"}))
            except Exception:
                pass

    def _audio_cb(self, indata, frames, time_info, status):
        raw = indata.tobytes()
        self._overlay.push_audio(raw)
        if self.ws_connected and self.ws:
            try:
                self.ws.send(raw, opcode=ws_lib.ABNF.OPCODE_BINARY)
            except Exception:
                pass
        elif self.is_recording:
            self._audio_buffer.append(raw)

    def _ws_reader(self):
        while True:
            try:
                msg = self.ws.recv()
                if not msg:
                    log.debug("WORKER ws recv empty — closing")
                    break
                data = json.loads(msg)
                msg_type = data.get("type")
                if msg_type in ("transcript", "post_transcript"):
                    td = data.get("data", {})
                    text = td.get("utterance", {}).get("text", "").strip()
                    is_final = td.get("is_final")
                    log.debug(f"WORKER transcript is_final={is_final} text={text[:80] if text else '(empty)'}")
                    if is_final and text:
                        self._texts.append(text)
            except Exception as e:
                log.debug(f"WORKER ws recv exception: {e}")
                break
        self.ws_done.set()

    def _worker_finalize(self):
        # Stop mic
        if self.audio_stream:
            try:
                self.audio_stream.stop()
                self.audio_stream.close()
            except Exception:
                pass
            self.audio_stream = None

        provider = self.cfg.get("provider", "gladia")
        if provider == "cohere":
            self._finalize_cohere()
        else:
            self._finalize_gladia()

    def _finalize_gladia(self):
        # Signal end of recording to Gladia
        if self.ws:
            try:
                self.ws.send(json.dumps({"type": "stop_recording"}))
            except Exception:
                pass

        # Wait for all final transcripts (max 15s)
        self.ws_done.wait(timeout=15)

        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None
        self.ws_connected = False

        final = " ".join(self._texts).strip()
        log.debug(f"WORKER gladia final: '{final[:100] if final else '(empty)'}' ({len(self._texts)} segments)")
        self._paste_result(final)

    def _finalize_cohere(self):
        cfg = self.cfg
        audio_data = b"".join(self._audio_buffer)

        if not audio_data:
            log.debug("COHERE no audio data")
            self._paste_result("")
            return

        # Create WAV in memory
        wav_buf = io.BytesIO()
        with wave.open(wav_buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(cfg["sample_rate"])
            wf.writeframes(audio_data)
        wav_buf.seek(0)

        # Upload to Cohere Transcribe API
        try:
            lang = cfg.get("language", "fr")
            if lang == "auto":
                lang = "fr"  # Cohere requires explicit language
            resp = requests.post(
                "https://api.cohere.com/v2/audio/transcriptions",
                headers={"Authorization": f"Bearer {cfg['cohere_api_key']}"},
                files={"file": ("recording.wav", wav_buf, "audio/wav")},
                data={
                    "model": "cohere-transcribe-03-2026",
                    "language": lang,
                },
                timeout=60,
            )
            if not resp.ok:
                log.debug(f"COHERE API {resp.status_code}: {resp.text}")
            resp.raise_for_status()
            final = resp.json().get("text", "").strip()
            log.debug(f"COHERE transcription: {final[:80]}")
            from settings_window import increment_cohere_usage
            increment_cohere_usage()
        except Exception as e:
            log.debug(f"COHERE API FAILED: {e}")
            final = ""

        self._paste_result(final)

    def _paste_result(self, final):
        self._overlay.hide()
        self._overlay.set_state("recording")

        if final:
            AppHelper.callAfter(self._deliver_result_main_thread, final)
        else:
            AppHelper.callAfter(self._finish_without_insertion_main_thread)
            if self.cfg.get("show_notifications"):
                rumps.notification("FreeWhisper", "", "No speech detected")

        self._cooldown = time.time()

    def _cleanup(self):
        log.debug("_cleanup called")
        self.is_recording = False
        self.ws_connected = False
        self._overlay.hide()
        if self.audio_stream:
            try:
                self.audio_stream.stop()
                self.audio_stream.close()
            except Exception:
                pass
            self.audio_stream = None
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None
        self.title = None
        self._cooldown = time.time()

    # ── Menu actions ────────────────────────────────────────

    def _open_settings(self, _):
        log.debug("Menu requested Settings")
        self._schedule_settings_presentation(0.1)

    def _restart(self, _):
        # Launch new process after a short delay so old one can quit cleanly
        launch_args = launch_program_arguments(force_new_instance=True)
        cmd = " ".join(shlex.quote(a) for a in launch_args)
        log.debug(f"_restart scheduling relaunch via: {launch_args}")
        subprocess.Popen(
            ["bash", "-lc", f"sleep 1 && exec {cmd}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        rumps.quit_application()


if __name__ == "__main__":
    # Hide from Dock (agent/accessory app — menu bar only)
    info = AppKit.NSBundle.mainBundle().infoDictionary()
    info["LSUIElement"] = "1"

    try:
        instance_lock = acquire_single_instance_lock()
        if instance_lock is None:
            log.warning("Another FreeWhisper instance is already running — exiting")
            request_existing_instance_show_settings()
            sys.exit(0)
        FreeWhisperApp(instance_lock).run()
    except Exception:
        log.exception("FATAL unhandled exception")
