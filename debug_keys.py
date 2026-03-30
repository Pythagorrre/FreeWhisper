#!/usr/bin/env python3
"""Diagnostic: test all key detection methods on this Mac."""

import time
import threading
import Quartz

print("=== FreeWhisper Key Diagnostic ===")
print("Press modifier keys (Option, Cmd, Shift, Ctrl, Fn).")
print("Press Ctrl+C to quit.\n")

# ── Method 1: Poll CGEventSourceKeyState ────────────────
KEYS_TO_TEST = {
    58: "alt_l", 61: "alt_r",
    55: "cmd_l", 54: "cmd_r",
    56: "shift_l", 60: "shift_r",
    59: "ctrl_l", 62: "ctrl_r",
    63: "fn",
}

def poll_method():
    states = {k: False for k in KEYS_TO_TEST}
    while True:
        for keycode, name in KEYS_TO_TEST.items():
            pressed = Quartz.CGEventSourceKeyState(
                Quartz.kCGEventSourceStateCombinedSessionState, keycode
            )
            if pressed and not states[keycode]:
                print(f"  [POLL]  {name} (keycode={keycode}) -> PRESSED")
            elif not pressed and states[keycode]:
                print(f"  [POLL]  {name} (keycode={keycode}) -> RELEASED")
            states[keycode] = pressed
        time.sleep(0.05)

# ── Method 2: CGEvent tap (flagsChanged) ────────────────
def tap_method():
    def callback(proxy, event_type, event, refcon):
        if event_type == Quartz.kCGEventFlagsChanged:
            keycode = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
            flags = Quartz.CGEventGetFlags(event)
            name = KEYS_TO_TEST.get(keycode, f"unknown({keycode})")
            print(f"  [TAP]   keycode={keycode} ({name})  flags=0x{flags:08x}")
        return event

    mask = Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap,
        Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionListenOnly,
        mask,
        callback,
        None,
    )
    if tap is None:
        print("  [TAP]   FAILED — Cannot create event tap (need Accessibility permission)")
        return

    print("  [TAP]   Event tap created OK")
    source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
    loop = Quartz.CFRunLoopGetCurrent()
    Quartz.CFRunLoopAddSource(loop, source, Quartz.kCFRunLoopCommonModes)
    Quartz.CGEventTapEnable(tap, True)
    Quartz.CFRunLoopRun()

# ── Method 3: NSEvent global monitor ────────────────────
def nsevent_method():
    try:
        from AppKit import NSEvent, NSApplication
        NSApplication.sharedApplication()

        def handler(event):
            keycode = event.keyCode()
            flags = event.modifierFlags()
            name = KEYS_TO_TEST.get(keycode, f"unknown({keycode})")
            print(f"  [NSEVT] keycode={keycode} ({name})  flags=0x{flags:08x}")

        NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            1 << 12,  # NSEventTypeFlagsChanged
            handler,
        )
        print("  [NSEVT] Global monitor registered OK")
    except Exception as e:
        print(f"  [NSEVT] FAILED — {e}")


print("Starting 3 detection methods in parallel...\n")

# Start poll in background
threading.Thread(target=poll_method, daemon=True).start()

# Start CGEvent tap in background
threading.Thread(target=tap_method, daemon=True).start()

# NSEvent on main thread (needs run loop)
nsevent_method()

# Keep main thread alive with a simple run loop
try:
    from AppKit import NSRunLoop, NSDate
    while True:
        NSRunLoop.currentRunLoop().runUntilDate_(
            NSDate.dateWithTimeIntervalSinceNow_(0.2)
        )
except KeyboardInterrupt:
    print("\nDone.")
