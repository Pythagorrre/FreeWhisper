"""Native macOS Settings window for FreeWhisper — dark themed."""

import json
import os
import subprocess
import threading
import ctypes
import ctypes.util
import logging
import objc
import AppKit
from app_runtime import (
    check_for_update,
    download_and_apply_update,
    launch_program_arguments,
    open_latest_release_page,
)
from app_paths import ensure_user_data_file, resource_path, user_support_path
from AppKit import (
    NSWindow, NSView, NSTextField, NSImageView,
    NSPopUpButton, NSButton, NSBox, NSColor, NSFont, NSImage,
    NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSBackingStoreBuffered, NSScreen,
    NSBezelStyleRounded,
    NSTextAlignmentLeft,
    NSMutableAttributedString,
    NSForegroundColorAttributeName,
    NSFontAttributeName,
    NSMenu, NSMenuItem,
    NSBezierPath,
)

try:
    _NSSwitch = AppKit.NSSwitch
except AttributeError:
    _NSSwitch = None

CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = ensure_user_data_file("config.json")
WORKING_DIRECTORY = user_support_path()
ACCESSIBILITY_SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
log = logging.getLogger("freewhisper")

KEYCODE_NAMES = {
    61: "Right Option", 58: "Left Option",
    54: "Right Cmd", 55: "Left Cmd",
    60: "Right Shift", 56: "Left Shift",
    62: "Right Ctrl", 59: "Left Ctrl",
    63: "Fn",
    53: "Escape", 51: "Delete", 36: "Return", 48: "Tab", 49: "Space",
    122: "F1", 120: "F2", 99: "F3", 118: "F4", 96: "F5",
    97: "F6", 98: "F7", 100: "F8", 101: "F9", 109: "F10",
    103: "F11", 111: "F12",
    42: "`", 50: "§", 10: "<",
    0: "A", 1: "S", 2: "D", 3: "F", 4: "H", 5: "G",
    6: "Z", 7: "X", 8: "C", 9: "V", 11: "B", 12: "Q", 13: "W",
    14: "E", 15: "R", 16: "Y", 17: "T",
}

MODIFIER_FLAG_MAP = {
    61: 0x00080000, 58: 0x00080000,
    54: 0x00100000, 55: 0x00100000,
    60: 0x00020000, 56: 0x00020000,
    62: 0x00040000, 59: 0x00040000,
    63: 0x00800000,
}

MODIFIER_ORDER = {
    59: 0, 62: 1, 56: 2, 60: 3, 58: 4, 61: 5, 55: 6, 54: 7, 63: 8,
}

HOTKEY_MODIFIER_KEYS = {
    "alt_r": (61, 0x00080000),
    "alt_l": (58, 0x00080000),
    "cmd_r": (54, 0x00100000),
    "cmd_l": (55, 0x00100000),
    "shift_r": (60, 0x00020000),
    "shift_l": (56, 0x00020000),
    "ctrl_r": (62, 0x00040000),
    "ctrl_l": (59, 0x00040000),
    "fn": (63, 0x00800000),
}

HOTKEY_REGULAR_KEYS = {
    "`": 42,
}

LANGUAGES = [
    ("fr", "Français"), ("en", "English"), ("es", "Español"),
    ("de", "Deutsch"), ("it", "Italiano"), ("pt", "Português"),
    ("nl", "Nederlands"), ("ar", "العربية"), ("zh", "中文"),
    ("ja", "日本語"), ("ko", "한국어"), ("auto", "Auto-detect"),
]

FLAGS = {
    "fr": "🇫🇷", "en": "🇬🇧", "es": "🇪🇸", "de": "🇩🇪", "it": "🇮🇹",
    "pt": "🇵🇹", "nl": "🇳🇱", "ar": "🇸🇦", "zh": "🇨🇳", "ja": "🇯🇵",
    "ko": "🇰🇷", "auto": "🌐",
}

def _app_icon():
    """Return the FreeWhisper app icon as an NSImage, or None."""
    from app_paths import CONTENTS_DIR
    if CONTENTS_DIR:
        icon_path = os.path.join(CONTENTS_DIR, "Resources", "FreeWhisper.icns")
        if os.path.exists(icon_path):
            return NSImage.alloc().initWithContentsOfFile_(icon_path)
    return None


# ── Layout constants ──────────────────────────────────────
WIN_W = 700
WIN_H = 690
PAD = 20
ROW_H = 32
LABEL_W = 180
FIELD_W = WIN_W - LABEL_W - PAD * 3
CARD_H = 85
CARD_PAD = 15

# ── Launch at startup helpers ─────────────────────────────

LAUNCHAGENT_PATH = os.path.expanduser("~/Library/LaunchAgents/com.freewhisper.app.plist")
OLD_LAUNCHAGENT_PATH = os.path.expanduser("~/Library/LaunchAgents/com.gladiamic.app.plist")


def _is_launch_at_startup():
    return os.path.exists(LAUNCHAGENT_PATH) or os.path.exists(OLD_LAUNCHAGENT_PATH)


def _set_launch_at_startup(enabled):
    if os.path.exists(OLD_LAUNCHAGENT_PATH):
        subprocess.run(["launchctl", "unload", OLD_LAUNCHAGENT_PATH], capture_output=True)
        try:
            os.remove(OLD_LAUNCHAGENT_PATH)
        except OSError:
            pass
    if enabled:
        program_arguments = launch_program_arguments(force_new_instance=True)
        program_args_xml = "\n".join(
            f"        <string>{arg}</string>" for arg in program_arguments
        )
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.freewhisper.app</string>
    <key>ProgramArguments</key>
    <array>
{program_args_xml}
    </array>
    <key>WorkingDirectory</key>
    <string>{WORKING_DIRECTORY}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>"""
        os.makedirs(os.path.dirname(LAUNCHAGENT_PATH), exist_ok=True)
        with open(LAUNCHAGENT_PATH, "w") as f:
            f.write(plist)
    else:
        if os.path.exists(LAUNCHAGENT_PATH):
            subprocess.run(["launchctl", "unload", LAUNCHAGENT_PATH], capture_output=True)
            try:
                os.remove(LAUNCHAGENT_PATH)
            except OSError:
                pass


# ── Cohere usage tracking (local counter) ────────────────

COHERE_USAGE_FILE = user_support_path("cohere_usage.json")


def get_cohere_usage_count():
    from datetime import datetime
    month = datetime.now().strftime("%Y-%m")
    try:
        with open(COHERE_USAGE_FILE) as f:
            data = json.load(f)
        if data.get("month") == month:
            return data.get("count", 0)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return 0


def increment_cohere_usage():
    from datetime import datetime
    month = datetime.now().strftime("%Y-%m")
    count = get_cohere_usage_count() + 1
    with open(COHERE_USAGE_FILE, "w") as f:
        json.dump({"month": month, "count": count}, f)
    return count


# ── UI helpers ────────────────────────────────────────────

def _make_label(text, y):
    label = NSTextField.alloc().initWithFrame_(((PAD, y), (LABEL_W, ROW_H)))
    label.setStringValue_(text)
    label.setBezeled_(False)
    label.setDrawsBackground_(False)
    label.setEditable_(False)
    label.setSelectable_(False)
    label.setFont_(NSFont.systemFontOfSize_(13))
    label.setAlignment_(NSTextAlignmentLeft)
    return label


def _make_section_header(text, y):
    lbl = NSTextField.alloc().initWithFrame_(((PAD, y), (400, 20)))
    lbl.setStringValue_(text)
    lbl.setBezeled_(False)
    lbl.setDrawsBackground_(False)
    lbl.setEditable_(False)
    lbl.setSelectable_(False)
    lbl.setFont_(NSFont.systemFontOfSize_weight_(13, 0.4))
    return lbl


def _sf_image(name, size=14):
    """Load an SF Symbol as NSImage."""
    try:
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
        if img:
            config = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(
                float(size), 0.0)
            return img.imageWithSymbolConfiguration_(config)
    except Exception:
        pass
    return None


def _make_progress_ring(size, progress, color):
    """Create a semi-circular gauge with blue-teal gradient as NSImage."""
    img = NSImage.alloc().initWithSize_((size, size))
    img.lockFocus()

    cx = size / 2
    ring_w = 7.0
    arc_r = size / 2 - ring_w / 2 - 4
    cy_arc = size * 0.50  # center of arc, text sits inside below

    # Arc spans 220° (from 200° to -20°), extending slightly below horizontal
    arc_start = 200
    arc_end = -20
    arc_span = arc_start - arc_end  # 220

    # ── Background track (dark gray arc) ──
    base = NSColor.colorWithWhite_alpha_(0.20, 1.0)
    trk = NSBezierPath.bezierPath()
    trk.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
        (cx, cy_arc), arc_r, arc_start, arc_end, True)
    trk.setLineWidth_(ring_w)
    trk.setLineCapStyle_(1)
    base.set()
    trk.stroke()

    # ── Progress arc with gradient segments ──
    if progress > 0.005:
        angle = arc_span * min(progress, 1.0)
        n_seg = max(int(angle / 3), 1)
        for i in range(n_seg):
            seg_start = arc_start - (angle * i / n_seg)
            seg_end = arc_start - (angle * (i + 1) / n_seg)
            t = i / max(n_seg - 1, 1)
            # Vivid blue → bright teal gradient
            r = 0.05 + t * 0.10
            g = 0.35 + t * 0.55
            b = 0.70 - t * 0.05
            seg_c = NSColor.colorWithRed_green_blue_alpha_(r, g, b, 1.0)
            seg = NSBezierPath.bezierPath()
            seg.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                (cx, cy_arc), arc_r, seg_start, seg_end, True)
            seg.setLineWidth_(ring_w)
            seg.setLineCapStyle_(1)
            seg_c.set()
            seg.stroke()

    # ── Percentage text centered inside the semi-circle ──
    pct_val = f"{int(progress * 100)}"
    pct_text = f"{pct_val}%"
    attrs = {
        NSFontAttributeName: NSFont.monospacedDigitSystemFontOfSize_weight_(13, 0.0),
        NSForegroundColorAttributeName: NSColor.labelColor(),
    }
    
    astr = NSMutableAttributedString.alloc().initWithString_attributes_(pct_text, attrs)
    ts = astr.size()
    text_y = cy_arc - ts.height / 2 + 1
    astr.drawAtPoint_((cx - ts.width / 2, text_y))

    img.unlockFocus()
    return img


# ── Keyboard layout translation ──────────────────────────

_carbon_lib = None
_cf_lib = None


def _keycode_to_layout_char(keycode):
    global _carbon_lib, _cf_lib
    try:
        if _carbon_lib is None:
            path = ctypes.util.find_library("Carbon")
            if not path:
                return None
            _carbon_lib = ctypes.cdll.LoadLibrary(path)
        if _cf_lib is None:
            path = ctypes.util.find_library("CoreFoundation")
            if not path:
                return None
            _cf_lib = ctypes.cdll.LoadLibrary(path)

        carbon, cf = _carbon_lib, _cf_lib
        carbon.TISCopyCurrentKeyboardInputSource.restype = ctypes.c_void_p
        carbon.TISGetInputSourceProperty.restype = ctypes.c_void_p
        carbon.TISGetInputSourceProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        kTISProp = ctypes.c_void_p.in_dll(carbon, "kTISPropertyUnicodeKeyLayoutData")
        source = carbon.TISCopyCurrentKeyboardInputSource()
        if not source:
            return None
        layout_ref = carbon.TISGetInputSourceProperty(source, kTISProp)
        if not layout_ref:
            return None

        cf.CFDataGetBytePtr.restype = ctypes.c_void_p
        cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
        layout_ptr = cf.CFDataGetBytePtr(layout_ref)
        if not layout_ptr:
            return None

        carbon.UCKeyTranslate.restype = ctypes.c_int32
        carbon.UCKeyTranslate.argtypes = [
            ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
        ]

        dead = ctypes.c_uint32(0)
        length = ctypes.c_uint32(0)
        buf = (ctypes.c_uint16 * 4)()

        status = carbon.UCKeyTranslate(
            layout_ptr, keycode, 0, 0, 0, 1,
            ctypes.byref(dead), 4, ctypes.byref(length), buf,
        )

        if status == 0 and length.value > 0:
            char = "".join(chr(buf[i]) for i in range(length.value))
            if char and char.isprintable():
                return char.upper() if char.isalpha() else char
    except Exception:
        pass
    return None


def keycode_to_name(kc):
    name = KEYCODE_NAMES.get(kc)
    if name:
        return name
    char = _keycode_to_layout_char(kc)
    if char:
        return char
    return f"Key {kc}"


def _char_from_event(event):
    for getter in (event.charactersIgnoringModifiers, event.characters):
        try:
            raw = getter()
            if not raw:
                continue
            chars = str(raw)
            if chars and chars.isprintable():
                return chars.upper() if chars.isalpha() else chars
        except Exception:
            continue
    return _keycode_to_layout_char(event.keyCode())


# ── ObjC bridge ──────────────────────────────────────────

class _SettingsBridge(AppKit.NSObject):
    settings_ref = objc.ivar()

    def hotkeyClicked_(self, sender):
        s = self.settings_ref
        if s:
            s._begin_capture("hotkey")

    def cancelClicked_(self, sender):
        s = self.settings_ref
        if s:
            s._begin_capture("cancel")

    def saveClicked_(self, sender):
        s = self.settings_ref
        if s:
            s._on_save()

    def permissionsClicked_(self, sender):
        s = self.settings_ref
        if s:
            s._open_permissions_settings()

    def updatesClicked_(self, sender):
        s = self.settings_ref
        if s:
            s._check_for_updates()

    def windowShouldClose_(self, sender):
        s = self.settings_ref
        if s:
            return s._confirm_close()
        return True

    def windowWillClose_(self, notification):
        s = self.settings_ref
        if s:
            s._window = None

    def gladiaClicked_(self, sender):
        s = self.settings_ref
        if s:
            s._gladia_radio.setState_(1)
            s._cohere_radio.setState_(0)
            s._on_provider_changed("gladia")

    def cohereClicked_(self, sender):
        s = self.settings_ref
        if s:
            s._gladia_radio.setState_(0)
            s._cohere_radio.setState_(1)
            s._on_provider_changed("cohere")

    def gladiaLinkClicked_(self, sender):
        subprocess.Popen(["open", "https://app.gladia.io/account/api-keys"])

    def cohereLinkClicked_(self, sender):
        subprocess.Popen(["open", "https://dashboard.cohere.com/api-keys"])

    def gladiaEyeClicked_(self, sender):
        s = self.settings_ref
        if s:
            s._toggle_key_visibility("gladia")

    def cohereEyeClicked_(self, sender):
        s = self.settings_ref
        if s:
            s._toggle_key_visibility("cohere")

    def gladiaCopyClicked_(self, sender):
        s = self.settings_ref
        if s:
            s._copy_key("gladia")

    def cohereCopyClicked_(self, sender):
        s = self.settings_ref
        if s:
            s._copy_key("cohere")

    def updateUsageDisplay_(self, sender):
        s = self.settings_ref
        if s:
            s._update_usage_display()


# ── SettingsWindow ────────────────────────────────────────

class SettingsWindow:
    def __init__(self, app_ref):
        self._app = app_ref
        self._window = None
        self._views = []
        self._listening_for = None
        self._key_monitor = None
        self._mouse_monitor = None
        # Hotkey combo state
        self._hotkey_keycode = None
        self._hotkey_mod_flags = 0
        self._hotkey_display = None
        # Cancel combo state
        self._cancel_keycode = None
        self._cancel_mod_flags = 0
        self._cancel_display = None
        # Initial state for change detection
        self._initial_hotkey_keycode = None
        self._initial_hotkey_mod_flags = 0
        self._initial_cancel_keycode = None
        self._initial_cancel_mod_flags = 0
        self._initial_language = None
        self._initial_hold_to_record = True
        self._initial_api_key = None
        self._initial_cohere_api_key = None
        self._initial_provider = None
        self._initial_launch_at_startup = None
        self._initial_show_menu_bar_icon = True
        # Key visibility state
        self._gladia_key_hidden = False
        self._cohere_key_hidden = False
        self._gladia_key_value = ""
        self._cohere_key_value = ""
        # Capture state
        self._capture_held_mods = {}
        self._capture_snapshot = {}
        self._capture_trigger = None
        self._capture_trigger_display = None
        # Usage data (set by background thread)
        self._gladia_usage_data = None
        self._cohere_usage_data = None
        # Persistent ObjC refs (survive across show/close cycles)
        self._persistent_refs = []
        # Bridge
        self._bridge = _SettingsBridge.alloc().init()
        self._bridge.settings_ref = self
        self._persistent_refs.append(self._bridge)

    def _activate_app_for_window(self):
        try:
            current_app = AppKit.NSRunningApplication.currentApplication()
            if current_app is not None:
                options = 0
                options |= getattr(AppKit, "NSApplicationActivateIgnoringOtherApps", 1)
                options |= getattr(AppKit, "NSApplicationActivateAllWindows", 1 << 1)
                current_app.activateWithOptions_(options)
        except Exception as e:
            log.debug(f"SettingsWindow currentApplication activate FAILED: {e}")
        try:
            AppKit.NSApp.activateIgnoringOtherApps_(True)
        except Exception as e:
            log.debug(f"SettingsWindow NSApp activate FAILED: {e}")

    def _frontmost_screen_frame(self):
        screen = None
        try:
            screen = NSScreen.mainScreen()
        except Exception as e:
            log.debug(f"SettingsWindow mainScreen lookup FAILED: {e}")
        if screen is None:
            try:
                screens = NSScreen.screens()
                if screens:
                    screen = screens[0]
            except Exception as e:
                log.debug(f"SettingsWindow screens lookup FAILED: {e}")
        if screen is not None:
            try:
                return screen.visibleFrame()
            except Exception:
                return screen.frame()
        return None

    def _bring_window_to_front(self):
        if self._window is None:
            return
        self._activate_app_for_window()
        try:
            self._window.setLevel_(AppKit.NSFloatingWindowLevel)
        except Exception:
            pass
        try:
            self._window.orderFrontRegardless()
            self._window.makeKeyAndOrderFront_(None)
        except Exception as e:
            log.debug(f"SettingsWindow bring-to-front FAILED: {e}")

    # ── Card builder ──────────────────────────────────────

    def _make_engine_card(self, parent_view, card_y, card_w, name, logo_filename,
                          is_selected, api_key, radio_action, link_action,
                          eye_action, keep_fn):
        """Create an engine card. Returns (radio, api_field, usage_lbl, progress_iv, eye_btn)."""
        card = NSBox.alloc().initWithFrame_(((PAD, card_y), (card_w, CARD_H)))
        card.setBoxType_(4)  # NSBoxCustom
        card.setTitlePosition_(0)  # NSNoTitle
        card.setContentViewMargins_((0, 0))
        card.setBorderWidth_(1)
        card.setBorderColor_(NSColor.separatorColor())
        card.setFillColor_(NSColor.controlBackgroundColor())
        card.setCornerRadius_(12)

        cv = card.contentView()
        y_top = CARD_H - CARD_PAD - 28  # top row baseline
        y_bot = CARD_PAD  # bottom row baseline

        # Logo (22×22) + name — same y, same height for perfect alignment
        logo_path = resource_path(logo_filename)
        item_y = y_top + 3
        item_h = 22
        if os.path.exists(logo_path):
            logo_img = NSImage.alloc().initWithContentsOfFile_(logo_path)
            logo_iv = NSImageView.alloc().initWithFrame_(
                ((CARD_PAD, item_y), (item_h, item_h)))
            logo_iv.setImage_(logo_img)
            logo_iv.setImageScaling_(3)  # ProportionallyUpOrDown
            logo_iv.setWantsLayer_(True)
            logo_iv.layer().setCornerRadius_(5)
            logo_iv.layer().setMasksToBounds_(True)
            cv.addSubview_(keep_fn(logo_iv))

        # Provider name — exactly same y and height as logo
        name_lbl = NSTextField.alloc().initWithFrame_(
            ((CARD_PAD + item_h + 6, item_y - 2), (100, item_h)))
        name_lbl.setStringValue_(name)
        name_lbl.setBezeled_(False)
        name_lbl.setDrawsBackground_(False)
        name_lbl.setEditable_(False)
        name_lbl.setSelectable_(False)
        name_lbl.setFont_(NSFont.systemFontOfSize_weight_(14, 0.4))
        cv.addSubview_(keep_fn(name_lbl))

        # Circular progress ring (50×50, centered vertically)
        ring_size = 62
        ring_x = 133
        ring_y = (CARD_H - ring_size) // 2
        ring_img = _make_progress_ring(ring_size, 0, NSColor.systemGrayColor())
        progress_iv = NSImageView.alloc().initWithFrame_(
            ((ring_x, ring_y), (ring_size, ring_size)))
        progress_iv.setImage_(ring_img)
        cv.addSubview_(keep_fn(progress_iv))

        # API key field + eye toggle (no copy button)
        api_x = 220
        eye_x = card_w - CARD_PAD - 28
        api_w = eye_x - 8 - api_x

        api_field = NSTextField.alloc().initWithFrame_(
            ((api_x, y_top), (api_w, 28)))
        # Start hidden: show dots, not editable
        if api_key:
            api_field.setStringValue_(
                "\u2022" * min(max(len(api_key), 8), 32))
            api_field.setEditable_(False)
        else:
            api_field.setStringValue_("")
            api_field.setEditable_(True)
        api_field.setFont_(NSFont.monospacedSystemFontOfSize_weight_(11, 0.0))
        api_field.setPlaceholderString_(f"{name} API key")
        cv.addSubview_(keep_fn(api_field))

        # Eye toggle button (starts as eye.slash since key is hidden)
        eye_btn = NSButton.alloc().initWithFrame_(((eye_x, y_top), (28, 28)))
        eye_btn.setBezelStyle_(NSBezelStyleRounded)
        eye_btn.setTitle_("")
        ei = _sf_image("eye.slash", 12)
        if ei:
            eye_btn.setImage_(ei)
            eye_btn.setImagePosition_(1)  # NSImageOnly
        else:
            eye_btn.setTitle_("👁")
        eye_btn.setTarget_(self._bridge)
        eye_btn.setAction_(eye_action)
        cv.addSubview_(keep_fn(eye_btn))

        # Radio button (bottom-left)
        radio = NSButton.alloc().initWithFrame_(
            ((CARD_PAD + 2, y_bot), (130, 24)))
        radio.setButtonType_(4)  # NSRadioButton
        radio.setTitle_(f"Use {name}")
        radio.setFont_(NSFont.systemFontOfSize_(13))
        radio.setState_(1 if is_selected else 0)
        radio.setTarget_(self._bridge)
        radio.setAction_(radio_action)
        cv.addSubview_(keep_fn(radio))

        # Usage label (bottom-center)
        usage_lbl = NSTextField.alloc().initWithFrame_(
            ((api_x, y_bot + 2), (220, 18)))
        usage_lbl.setStringValue_("Loading...")
        usage_lbl.setBezeled_(False)
        usage_lbl.setDrawsBackground_(False)
        usage_lbl.setEditable_(False)
        usage_lbl.setSelectable_(False)
        usage_lbl.setFont_(NSFont.systemFontOfSize_(11))
        usage_lbl.setTextColor_(NSColor.secondaryLabelColor())
        cv.addSubview_(keep_fn(usage_lbl))

        # "Get API Key ↗" link (bottom-right)
        link_w = 110
        link_btn = NSButton.alloc().initWithFrame_(
            ((card_w - CARD_PAD - link_w, y_bot), (link_w, 22)))
        link_btn.setBordered_(False)
        ts = NSMutableAttributedString.alloc().initWithString_("Get API Key \u2197")
        rng = (0, ts.length())
        ts.addAttribute_value_range_(
            NSForegroundColorAttributeName, NSColor.systemBlueColor(), rng)
        ts.addAttribute_value_range_(
            NSFontAttributeName, NSFont.systemFontOfSize_(11), rng)
        link_btn.setAttributedTitle_(ts)
        link_btn.setTarget_(self._bridge)
        link_btn.setAction_(link_action)
        cv.addSubview_(keep_fn(link_btn))

        parent_view.addSubview_(keep_fn(card))
        return radio, api_field, usage_lbl, progress_iv, eye_btn

    # ── Show ──────────────────────────────────────────────

    def show(self):
        log.debug(
            "SettingsWindow.show start has_window=%s visible=%s"
            % (
                "yes" if self._window is not None else "no",
                "yes" if self._window is not None and self._window.isVisible() else "no",
            )
        )
        if self._window and self._window.isVisible():
            self._bring_window_to_front()
            log.debug("SettingsWindow.show reused existing visible window")
            return

        # Ensure Edit menu exists (enables Cmd+V/C/X in text fields)
        main_menu = AppKit.NSApp.mainMenu()
        if main_menu and not main_menu.itemWithTitle_("Edit"):
            edit_menu = NSMenu.alloc().initWithTitle_("Edit")
            edit_menu.addItemWithTitle_action_keyEquivalent_("Cut", "cut:", "x")
            edit_menu.addItemWithTitle_action_keyEquivalent_("Copy", "copy:", "c")
            edit_menu.addItemWithTitle_action_keyEquivalent_("Paste", "paste:", "v")
            edit_menu.addItemWithTitle_action_keyEquivalent_(
                "Select All", "selectAll:", "a")
            edit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Edit", None, "")
            edit_item.setSubmenu_(edit_menu)
            main_menu.addItem_(edit_item)
            self._persistent_refs.extend([edit_menu, edit_item])

        cfg = self._app.cfg

        hotkey_name = cfg.get("hotkey", "`")
        key_info = HOTKEY_MODIFIER_KEYS.get(hotkey_name)
        if key_info:
            self._hotkey_keycode = key_info[0]
        elif hotkey_name in HOTKEY_REGULAR_KEYS:
            self._hotkey_keycode = HOTKEY_REGULAR_KEYS[hotkey_name]
        elif hotkey_name.isdigit():
            self._hotkey_keycode = int(hotkey_name)
        else:
            self._hotkey_keycode = 42
        self._cancel_keycode = cfg.get("cancel_keycode", 53)
        self._hotkey_mod_flags = cfg.get("hotkey_mod_flags", 0)
        self._cancel_mod_flags = cfg.get("cancel_mod_flags", 0)
        self._hotkey_display = cfg.get("hotkey_display",
                                       keycode_to_name(self._hotkey_keycode))
        self._cancel_display = cfg.get("cancel_display",
                                       keycode_to_name(self._cancel_keycode))

        # Store initial state
        self._initial_hotkey_keycode = self._hotkey_keycode
        self._initial_hotkey_mod_flags = self._hotkey_mod_flags
        self._initial_cancel_keycode = self._cancel_keycode
        self._initial_cancel_mod_flags = self._cancel_mod_flags
        self._initial_language = cfg.get("language", "fr")
        self._initial_hold_to_record = cfg.get("hold_to_record", True)
        self._initial_api_key = cfg.get("api_key", "")
        self._initial_cohere_api_key = cfg.get("cohere_api_key", "")
        self._initial_provider = cfg.get("provider", "gladia")
        self._initial_launch_at_startup = cfg.get("launch_at_startup",
                                                   _is_launch_at_startup())
        self._initial_show_menu_bar_icon = cfg.get("show_menu_bar_icon", True)
        self._initial_auto_update = cfg.get("auto_update", False)

        # Key visibility — start hidden
        self._gladia_key_value = self._initial_api_key
        self._cohere_key_value = self._initial_cohere_api_key
        self._gladia_key_hidden = True
        self._cohere_key_hidden = True
        self._gladia_usage_data = None
        self._cohere_usage_data = None

        # ── Create window ──
        self._activate_app_for_window()
        screen_frame = self._frontmost_screen_frame()
        if screen_frame is not None:
            x = screen_frame.origin.x + (screen_frame.size.width - WIN_W) / 2
            y = screen_frame.origin.y + (screen_frame.size.height - WIN_H) / 2
        else:
            x = 180
            y = 180
            log.debug("SettingsWindow.show using fallback origin because no screen was available")

        self._window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((x, y), (WIN_W, WIN_H)),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered, False,
        )
        self._window.setTitle_("FreeWhisper Settings")
        self._window.setLevel_(3)
        self._window.setReleasedWhenClosed_(False)
        self._window.setDelegate_(self._bridge)
        try:
            behavior = self._window.collectionBehavior()
        except Exception:
            behavior = 0
        try:
            behavior |= getattr(AppKit, "NSWindowCollectionBehaviorMoveToActiveSpace", 0)
            behavior |= getattr(AppKit, "NSWindowCollectionBehaviorFullScreenAuxiliary", 0)
            self._window.setCollectionBehavior_(behavior)
        except Exception as e:
            log.debug(f"SettingsWindow collectionBehavior FAILED: {e}")

        # Force dark appearance
        try:
            dark_name = getattr(AppKit, 'NSAppearanceNameDarkAqua',
                                "NSAppearanceNameDarkAqua")
            dark = AppKit.NSAppearance.appearanceNamed_(dark_name)
            if dark:
                self._window.setAppearance_(dark)
        except Exception:
            pass

        # Frosted glass / acrylic content (keep standard titlebar for proper corners)
        view = AppKit.NSVisualEffectView.alloc().initWithFrame_(
            ((0, 0), (WIN_W, WIN_H)))
        view.setBlendingMode_(1)   # NSVisualEffectBlendingModeBehindWindow
        view.setMaterial_(11)      # NSVisualEffectMaterialHUDWindow
        view.setState_(1)          # NSVisualEffectStateActive
        self._views.clear()

        def keep(w):
            self._views.append(w)
            return w

        _top = WIN_H - PAD
        field_x = LABEL_W + PAD * 2

        # ════════════════ GENERAL PREFERENCES ════════════════
        _top -= 4
        view.addSubview_(keep(_make_section_header("General Preferences", _top - 20)))
        _top -= 30

        # Record Hotkey
        y = _top - ROW_H
        view.addSubview_(keep(_make_label("Record Hotkey", y)))
        self._hotkey_btn = NSButton.alloc().initWithFrame_(
            ((field_x, y), (FIELD_W, ROW_H)))
        self._hotkey_btn.setTitle_(self._hotkey_display)
        self._hotkey_btn.setBezelStyle_(NSBezelStyleRounded)
        self._hotkey_btn.setFont_(NSFont.systemFontOfSize_(13))
        kb = _sf_image("keyboard", 13)
        if kb:
            self._hotkey_btn.setImage_(kb)
            self._hotkey_btn.setImagePosition_(3)  # NSImageRight
        self._hotkey_btn.setTarget_(self._bridge)
        self._hotkey_btn.setAction_(b"hotkeyClicked:")
        view.addSubview_(keep(self._hotkey_btn))
        _top = y - 8

        # Recording Mode
        y = _top - ROW_H
        view.addSubview_(keep(_make_label("Recording Mode", y)))
        self._record_mode_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            ((field_x, y), (FIELD_W, ROW_H)), False)
        self._record_mode_popup.addItemsWithTitles_([
            "Hold hotkey to record",
            "Press once to start, press again to stop",
        ])
        self._record_mode_popup.setFont_(NSFont.systemFontOfSize_(13))
        self._record_mode_popup.selectItemAtIndex_(
            0 if self._initial_hold_to_record else 1
        )
        view.addSubview_(keep(self._record_mode_popup))
        _top = y - 8

        # Cancel Key
        y = _top - ROW_H
        view.addSubview_(keep(_make_label("Cancel Key", y)))
        self._cancel_btn = NSButton.alloc().initWithFrame_(
            ((field_x, y), (FIELD_W, ROW_H)))
        self._cancel_btn.setTitle_(self._cancel_display)
        self._cancel_btn.setBezelStyle_(NSBezelStyleRounded)
        self._cancel_btn.setFont_(NSFont.systemFontOfSize_(13))
        if kb:
            self._cancel_btn.setImage_(kb)
            self._cancel_btn.setImagePosition_(3)
        self._cancel_btn.setTarget_(self._bridge)
        self._cancel_btn.setAction_(b"cancelClicked:")
        view.addSubview_(keep(self._cancel_btn))
        _top = y - 8

        # Transcription Language
        y = _top - ROW_H
        view.addSubview_(keep(_make_label("Transcription Language", y)))
        lang_names = [f"{FLAGS.get(code, '')} {name} ({code})"
                      for code, name in LANGUAGES]
        lang_codes = [code for code, _ in LANGUAGES]
        self._lang_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            ((field_x, y), (FIELD_W, ROW_H)), False)
        self._lang_popup.addItemsWithTitles_(lang_names)
        self._lang_popup.setFont_(NSFont.systemFontOfSize_(13))
        current_lang = cfg.get("language", "fr")
        if current_lang in lang_codes:
            self._lang_popup.selectItemAtIndex_(lang_codes.index(current_lang))
        self._lang_codes = lang_codes
        view.addSubview_(keep(self._lang_popup))
        _top = y - 8

        # Input Device (microphone)
        y = _top - ROW_H
        view.addSubview_(keep(_make_label("Microphone", y)))
        self._mic_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            ((field_x, y), (FIELD_W, ROW_H)), False)
        self._mic_popup.setFont_(NSFont.systemFontOfSize_(13))
        try:
            import sounddevice as sd
            self._mic_devices = []
            self._mic_popup.addItemWithTitle_("System Default")
            self._mic_devices.append(None)
            for i, d in enumerate(sd.query_devices()):
                if d['max_input_channels'] > 0:
                    self._mic_popup.addItemWithTitle_(d['name'])
                    self._mic_devices.append(i)
            current_dev = cfg.get("input_device")
            if current_dev is not None:
                current_dev = int(current_dev)
                if current_dev in self._mic_devices:
                    self._mic_popup.selectItemAtIndex_(
                        self._mic_devices.index(current_dev))
        except Exception:
            self._mic_devices = [None]
            self._mic_popup.addItemWithTitle_("System Default")
        view.addSubview_(keep(self._mic_popup))
        _top = y - 8

        # Launch at startup (toggle aligned with input fields above)
        y = _top - ROW_H
        view.addSubview_(keep(_make_label("Launch at startup", y)))
        if _NSSwitch is not None:
            self._startup_switch = _NSSwitch.alloc().initWithFrame_(
                ((field_x + 7, y + 4), (38, 22)))
        else:
            self._startup_switch = NSButton.alloc().initWithFrame_(
                ((field_x, y), (50, ROW_H)))
            self._startup_switch.setButtonType_(3)
            self._startup_switch.setTitle_("")
        self._startup_switch.setState_(
            1 if self._initial_launch_at_startup else 0)
        view.addSubview_(keep(self._startup_switch))

        self._permissions_btn = NSButton.alloc().initWithFrame_(
            ((field_x + 70, y), (190, ROW_H)))
        self._permissions_btn.setTitle_("Open Permissions")
        self._permissions_btn.setBezelStyle_(NSBezelStyleRounded)
        self._permissions_btn.setFont_(NSFont.systemFontOfSize_(13))
        self._permissions_btn.setTarget_(self._bridge)
        self._permissions_btn.setAction_(b"permissionsClicked:")
        view.addSubview_(keep(self._permissions_btn))
        _top = y - 8

        # Menu bar icon visibility
        y = _top - ROW_H
        view.addSubview_(keep(_make_label("Show menu bar icon", y)))
        if _NSSwitch is not None:
            self._menu_bar_icon_switch = _NSSwitch.alloc().initWithFrame_(
                ((field_x + 7, y + 4), (38, 22)))
        else:
            self._menu_bar_icon_switch = NSButton.alloc().initWithFrame_(
                ((field_x, y), (50, ROW_H)))
            self._menu_bar_icon_switch.setButtonType_(3)
            self._menu_bar_icon_switch.setTitle_("")
        self._menu_bar_icon_switch.setState_(
            1 if self._initial_show_menu_bar_icon else 0)
        view.addSubview_(keep(self._menu_bar_icon_switch))
        _top = y - 8

        # Automatic updates toggle
        y = _top - ROW_H
        view.addSubview_(keep(_make_label("Automatic updates", y)))
        if _NSSwitch is not None:
            self._auto_update_switch = _NSSwitch.alloc().initWithFrame_(
                ((field_x + 7, y + 4), (38, 22)))
        else:
            self._auto_update_switch = NSButton.alloc().initWithFrame_(
                ((field_x, y), (50, ROW_H)))
            self._auto_update_switch.setButtonType_(3)
            self._auto_update_switch.setTitle_("")
        self._auto_update_switch.setState_(
            1 if self._initial_auto_update else 0)
        view.addSubview_(keep(self._auto_update_switch))
        _top = y - 20

        # ════════════════ EXTERNAL TRANSCRIPTION ENGINES ═════
        _top -= 4
        view.addSubview_(keep(
            _make_section_header("Transcription Models", _top - 20)))
        _top -= 32

        card_w = WIN_W - 2 * PAD
        provider = cfg.get("provider", "gladia")

        # Gladia card
        gladia_y = _top - CARD_H
        (self._gladia_radio, self._gladia_api_field, self._gladia_usage_lbl,
         self._gladia_progress_iv, self._gladia_eye_btn
         ) = self._make_engine_card(
            view, gladia_y, card_w, "Gladia", "gladia_logo.png",
            provider == "gladia", cfg.get("api_key", ""),
            b"gladiaClicked:", b"gladiaLinkClicked:",
            b"gladiaEyeClicked:", keep)
        _top = gladia_y - 12

        # Cohere card
        cohere_y = _top - CARD_H
        (self._cohere_radio, self._cohere_api_field, self._cohere_usage_lbl,
         self._cohere_progress_iv, self._cohere_eye_btn
         ) = self._make_engine_card(
            view, cohere_y, card_w, "Cohere", "cohere_logo.png",
            provider == "cohere", cfg.get("cohere_api_key", ""),
            b"cohereClicked:", b"cohereLinkClicked:",
            b"cohereEyeClicked:", keep)
        _top = cohere_y - 20

        # ════════════════ SAVE BUTTON ════════════════════════
        btn_w, btn_h = 160, 36
        btn_gap = 12
        buttons_y = _top - btn_h
        total_w = btn_w * 2 + btn_gap
        buttons_x = (WIN_W - total_w) / 2

        update_btn = NSButton.alloc().initWithFrame_(
            ((buttons_x, buttons_y), (btn_w, btn_h)))
        update_btn.setTitle_("Check for Updates")
        update_btn.setBezelStyle_(NSBezelStyleRounded)
        update_btn.setFont_(NSFont.systemFontOfSize_(13))
        update_btn.setTarget_(self._bridge)
        update_btn.setAction_(b"updatesClicked:")
        view.addSubview_(keep(update_btn))

        save_btn = NSButton.alloc().initWithFrame_(
            ((buttons_x + btn_w + btn_gap, buttons_y), (btn_w, btn_h)))
        save_btn.setTitle_("Save & Restart")
        save_btn.setBezelStyle_(NSBezelStyleRounded)
        save_btn.setFont_(NSFont.systemFontOfSize_(13))
        save_btn.setKeyEquivalent_("\r")  # default button → blue
        save_btn.setTarget_(self._bridge)
        save_btn.setAction_(b"saveClicked:")
        view.addSubview_(keep(save_btn))

        self._window.setContentView_(view)
        self._bring_window_to_front()
        try:
            self._window.center()
        except Exception:
            pass
        self._bring_window_to_front()
        log.debug("SettingsWindow.show created and presented window")

        # Disable auto-detect if Cohere is selected
        self._on_provider_changed(provider)

        # Fetch usage in background
        threading.Thread(target=self._fetch_usage, daemon=True).start()

    # ── Provider switch (auto-detect handling) ─────────────

    def _open_permissions_settings(self):
        try:
            subprocess.Popen(["open", ACCESSIBILITY_SETTINGS_URL])
        except Exception:
            pass

    def _open_latest_release(self):
        try:
            open_latest_release_page()
        except Exception:
            log.exception("Failed to open latest GitHub release page")

    def _check_for_updates(self):
        from PyObjCTools import AppHelper

        def _do_check():
            try:
                result = check_for_update()
            except Exception:
                log.exception("Update check failed")
                AppHelper.callAfter(
                    lambda: self._show_update_alert(
                        "Update Check Failed",
                        "Could not reach GitHub. Please check your internet connection.",
                    )
                )
                return

            if result is None:
                AppHelper.callAfter(
                    lambda: self._show_update_alert(
                        "You're Up to Date",
                        "FreeWhisper is already the latest version.",
                    )
                )
            else:
                latest_version, dmg_url = result
                AppHelper.callAfter(
                    lambda v=latest_version, u=dmg_url: self._offer_update(v, u)
                )

        threading.Thread(target=_do_check, daemon=True).start()

    def _show_update_alert(self, title: str, message: str):
        alert = AppKit.NSAlert.alloc().init()
        icon = _app_icon()
        if icon:
            alert.setIcon_(icon)
        alert.setMessageText_(title)
        alert.setInformativeText_(message)
        alert.addButtonWithTitle_("OK")
        alert.runModal()

    def _offer_update(self, version: str, dmg_url: str):
        alert = AppKit.NSAlert.alloc().init()
        icon = _app_icon()
        if icon:
            alert.setIcon_(icon)
        alert.setMessageText_(f"Update Available — v{version}")
        alert.setInformativeText_(
            f"A new version of FreeWhisper ({version}) is available.\n\n"
            "Would you like to download and install it now? "
            "The app will restart automatically."
        )
        alert.addButtonWithTitle_("Update Now")
        alert.addButtonWithTitle_("Later")
        if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
            return

        log.debug("User accepted update to %s", version)

        def _do_update():
            try:
                download_and_apply_update(dmg_url)
            except Exception:
                log.exception("Auto-update failed")
                AppHelper.callAfter(
                    lambda: self._show_update_alert(
                        "Update Failed",
                        "The update could not be installed. "
                        "You can download it manually from GitHub.",
                    )
                )

        threading.Thread(target=_do_update, daemon=True).start()

    def _on_provider_changed(self, provider):
        auto_idx = self._lang_codes.index("auto") if "auto" in self._lang_codes else -1
        if auto_idx < 0:
            return
        self._lang_popup.setAutoenablesItems_(False)
        auto_item = self._lang_popup.itemAtIndex_(auto_idx)
        if provider == "cohere":
            # Disable auto-detect for Cohere
            auto_item.setEnabled_(False)
            if self._lang_popup.indexOfSelectedItem() == auto_idx:
                self._lang_popup.selectItemAtIndex_(0)  # Français
        else:
            auto_item.setEnabled_(True)

    # ── Key visibility & copy ─────────────────────────────

    def _toggle_key_visibility(self, provider):
        if provider == "gladia":
            field, btn = self._gladia_api_field, self._gladia_eye_btn
            if self._gladia_key_hidden:
                field.setStringValue_(self._gladia_key_value)
                field.setEditable_(True)
                self._gladia_key_hidden = False
                icon_name = "eye"
            else:
                self._gladia_key_value = str(field.stringValue())
                field.setStringValue_(
                    "\u2022" * min(max(len(self._gladia_key_value), 8), 32))
                field.setEditable_(False)
                self._gladia_key_hidden = True
                icon_name = "eye.slash"
        else:
            field, btn = self._cohere_api_field, self._cohere_eye_btn
            if self._cohere_key_hidden:
                field.setStringValue_(self._cohere_key_value)
                field.setEditable_(True)
                self._cohere_key_hidden = False
                icon_name = "eye"
            else:
                self._cohere_key_value = str(field.stringValue())
                field.setStringValue_(
                    "\u2022" * min(max(len(self._cohere_key_value), 8), 32))
                field.setEditable_(False)
                self._cohere_key_hidden = True
                icon_name = "eye.slash"
        ei = _sf_image(icon_name, 12)
        if ei and btn:
            btn.setImage_(ei)

    def _copy_key(self, provider):
        key = self._get_api_key(provider)
        if key:
            subprocess.run(["pbcopy"], input=key.encode("utf-8"))

    def _get_api_key(self, provider):
        if provider == "gladia":
            if self._gladia_key_hidden:
                return self._gladia_key_value
            return str(self._gladia_api_field.stringValue())
        else:
            if self._cohere_key_hidden:
                return self._cohere_key_value
            return str(self._cohere_api_field.stringValue())

    # ── Usage display ─────────────────────────────────────

    def _update_usage_display(self):
        if not self._window:
            return

        teal = NSColor.colorWithRed_green_blue_alpha_(0.05, 0.80, 0.73, 1.0)

        # Cohere
        count = self._cohere_usage_data
        if count is not None and self._cohere_usage_lbl:
            self._cohere_usage_lbl.setStringValue_(f"Usage: {count} / 1,000 requests")
            pct = min(count / 1000.0, 1.0)
            ring = _make_progress_ring(62, pct, teal)
            if self._cohere_progress_iv:
                self._cohere_progress_iv.setImage_(ring)

        # Gladia
        hours = self._gladia_usage_data
        if hours is not None and self._gladia_usage_lbl:
            self._gladia_usage_lbl.setStringValue_(f"Usage: {hours:.1f} / 10.0h")
            pct = min(hours / 10.0, 1.0)
            ring = _make_progress_ring(62, pct, teal)
            if self._gladia_progress_iv:
                self._gladia_progress_iv.setImage_(ring)

    def _fetch_usage(self):
        try:
            # Cohere — local counter
            self._cohere_usage_data = get_cohere_usage_count()

            # Gladia — aggregate billing_time from API
            api_key = self._app.cfg.get("api_key", "")
            if api_key:
                import requests as req
                from datetime import datetime
                first = datetime.now().strftime("%Y-%m-01")
                total_s = 0.0
                for ep in ("pre-recorded", "live"):
                    offset = 0
                    while True:
                        r = req.get(
                            f"https://api.gladia.io/v2/{ep}",
                            headers={"x-gladia-key": api_key},
                            params={"after_date": first, "limit": 100,
                                    "offset": offset},
                            timeout=10,
                        )
                        if not r.ok:
                            break
                        data = r.json()
                        items = (data if isinstance(data, list)
                                 else data.get("items", []))
                        if not items:
                            break
                        for item in items:
                            res = item.get("result") or {}
                            total_s += (res.get("metadata") or {}).get(
                                "billing_time", 0)
                        if isinstance(data, dict) and data.get("next"):
                            offset += 100
                        else:
                            break
                self._gladia_usage_data = total_s / 3600

            # Update UI on main thread
            if self._window and self._bridge:
                self._bridge.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "updateUsageDisplay:", None, False)
        except Exception:
            pass

    # ── Key combo capture ─────────────────────────────────

    def _begin_capture(self, which):
        self._stop_capture()
        self._listening_for = which
        btn = self._hotkey_btn if which == "hotkey" else self._cancel_btn
        btn.setTitle_("Press keys...")

        self._capture_held_mods = {}
        self._capture_snapshot = {}
        self._capture_trigger = None
        self._capture_trigger_display = None

        mask = AppKit.NSEventMaskKeyDown | AppKit.NSEventMaskFlagsChanged
        self._key_monitor = (
            AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                mask, self._on_key_event))
        self._mouse_monitor = (
            AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                AppKit.NSEventMaskLeftMouseDown, self._on_mouse_click))

    def _stop_capture(self):
        was = self._listening_for
        if self._key_monitor:
            AppKit.NSEvent.removeMonitor_(self._key_monitor)
            self._key_monitor = None
        if self._mouse_monitor:
            AppKit.NSEvent.removeMonitor_(self._mouse_monitor)
            self._mouse_monitor = None
        self._listening_for = None
        if was == "hotkey" and hasattr(self, '_hotkey_btn'):
            self._hotkey_btn.setTitle_(
                self._hotkey_display or keycode_to_name(self._hotkey_keycode))
        elif was == "cancel" and hasattr(self, '_cancel_btn'):
            self._cancel_btn.setTitle_(
                self._cancel_display or keycode_to_name(self._cancel_keycode))

    def _on_key_event(self, event):
        if not self._listening_for:
            return event

        keycode = event.keyCode()

        if event.type() == AppKit.NSEventTypeFlagsChanged:
            flag = MODIFIER_FLAG_MAP.get(keycode)
            if not flag:
                return event
            if event.modifierFlags() & flag:
                total = len(self._capture_held_mods) + (
                    1 if self._capture_trigger else 0)
                if total < 5:
                    self._capture_held_mods[keycode] = flag
                    self._capture_snapshot = self._capture_held_mods.copy()
                    self._update_capture_display()
            else:
                self._capture_held_mods.pop(keycode, None)
                if not self._capture_held_mods and self._capture_snapshot:
                    self._finalize_capture()
            return event

        total = len(self._capture_held_mods) + 1
        if total > 5:
            return None
        self._capture_trigger = keycode
        self._capture_trigger_display = (
            _char_from_event(event) or keycode_to_name(keycode))
        self._capture_snapshot = self._capture_held_mods.copy()
        self._finalize_capture()
        return None

    def _build_combo_display(self, mod_keycodes, trigger_display=None):
        ordered = sorted(mod_keycodes.keys(),
                         key=lambda kc: MODIFIER_ORDER.get(kc, 99))
        parts = [keycode_to_name(kc) for kc in ordered]
        if trigger_display:
            parts.append(trigger_display)
        return " + ".join(parts) if parts else ""

    def _update_capture_display(self):
        display = self._build_combo_display(self._capture_held_mods)
        btn = (self._hotkey_btn if self._listening_for == "hotkey"
               else self._cancel_btn)
        btn.setTitle_((display + " + ...") if display else "Press keys...")

    def _finalize_capture(self):
        mods = self._capture_snapshot
        trigger_kc = self._capture_trigger
        trigger_disp = self._capture_trigger_display

        if trigger_kc is not None:
            mod_flags = 0
            for f in mods.values():
                mod_flags |= f
            display = self._build_combo_display(mods, trigger_disp)
        elif mods:
            ordered = sorted(mods.keys(),
                             key=lambda kc: MODIFIER_ORDER.get(kc, 99))
            trigger_kc = ordered[-1]
            mod_flags = 0
            for kc in ordered[:-1]:
                mod_flags |= mods[kc]
            display = self._build_combo_display(mods)
        else:
            return

        which = self._listening_for
        if which == "hotkey":
            self._hotkey_keycode = trigger_kc
            self._hotkey_mod_flags = mod_flags
            self._hotkey_display = display
        elif which == "cancel":
            self._cancel_keycode = trigger_kc
            self._cancel_mod_flags = mod_flags
            self._cancel_display = display

        btn = (self._hotkey_btn if which == "hotkey" else self._cancel_btn)
        btn.setTitle_(display)
        self._stop_capture()

    def _on_mouse_click(self, event):
        if self._listening_for:
            self._stop_capture()
        return event

    # ── Close / save ──────────────────────────────────────

    def _has_changes(self):
        if self._hotkey_keycode != self._initial_hotkey_keycode:
            return True
        if self._hotkey_mod_flags != self._initial_hotkey_mod_flags:
            return True
        if self._cancel_keycode != self._initial_cancel_keycode:
            return True
        if self._cancel_mod_flags != self._initial_cancel_mod_flags:
            return True
        lang_idx = self._lang_popup.indexOfSelectedItem()
        if self._lang_codes[lang_idx] != self._initial_language:
            return True
        hold_to_record = self._record_mode_popup.indexOfSelectedItem() == 0
        if hold_to_record != self._initial_hold_to_record:
            return True
        if self._get_api_key("gladia") != self._initial_api_key:
            return True
        if self._get_api_key("cohere") != self._initial_cohere_api_key:
            return True
        provider = "cohere" if self._cohere_radio.state() == 1 else "gladia"
        if provider != self._initial_provider:
            return True
        if (self._startup_switch.state() == 1) != self._initial_launch_at_startup:
            return True
        if ((self._menu_bar_icon_switch.state() == 1)
                != self._initial_show_menu_bar_icon):
            return True
        if (self._auto_update_switch.state() == 1) != self._initial_auto_update:
            return True
        return False

    def _confirm_close(self):
        self._stop_capture()
        if not self._has_changes():
            return True
        alert = AppKit.NSAlert.alloc().init()
        icon = _app_icon()
        if icon:
            alert.setIcon_(icon)
        alert.setMessageText_("Unsaved changes")
        alert.setInformativeText_(
            "You have unsaved changes. Do you want to discard them?")
        alert.addButtonWithTitle_("Discard")
        alert.addButtonWithTitle_("Cancel")
        return alert.runModal() == AppKit.NSAlertFirstButtonReturn

    def _on_save(self):
        self._stop_capture()

        lang_idx = self._lang_popup.indexOfSelectedItem()
        language = self._lang_codes[lang_idx]
        hold_to_record = self._record_mode_popup.indexOfSelectedItem() == 0
        gladia_key = self._get_api_key("gladia")
        cohere_key = self._get_api_key("cohere")
        provider = "cohere" if self._cohere_radio.state() == 1 else "gladia"
        launch_at_startup = self._startup_switch.state() == 1
        show_menu_bar_icon = self._menu_bar_icon_switch.state() == 1

        hotkey_kc = self._hotkey_keycode
        hotkey_name = None
        for name, (kc, _) in HOTKEY_MODIFIER_KEYS.items():
            if kc == hotkey_kc:
                hotkey_name = name
                break
        if not hotkey_name:
            for name, kc in HOTKEY_REGULAR_KEYS.items():
                if kc == hotkey_kc:
                    hotkey_name = name
                    break
        if not hotkey_name:
            hotkey_name = str(hotkey_kc)

        cfg = self._app.cfg.copy()
        cfg["hotkey"] = hotkey_name
        cfg["hotkey_mod_flags"] = self._hotkey_mod_flags
        cfg["hotkey_display"] = self._hotkey_display
        cfg["cancel_keycode"] = self._cancel_keycode
        cfg["cancel_mod_flags"] = self._cancel_mod_flags
        cfg["cancel_display"] = self._cancel_display
        cfg["language"] = language
        cfg["hold_to_record"] = hold_to_record
        cfg["api_key"] = gladia_key
        cfg["cohere_api_key"] = cohere_key
        cfg["provider"] = provider
        cfg["launch_at_startup"] = launch_at_startup
        cfg["show_menu_bar_icon"] = show_menu_bar_icon
        cfg["auto_update"] = self._auto_update_switch.state() == 1
        mic_idx = self._mic_popup.indexOfSelectedItem()
        cfg["input_device"] = self._mic_devices[mic_idx]

        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)

        _set_launch_at_startup(launch_at_startup)

        self._window.close()
        self._app._restart(None)
