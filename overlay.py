"""Floating waveform overlay using WKWebView for UI."""

import math
import numpy as np
import os
from collections import deque

import objc
import AppKit
import WebKit

from app_paths import resource_path
from AppKit import (
    NSWindow, NSColor, NSScreen,
    NSWindowStyleMaskBorderless, NSBackingStoreBuffered,
    NSFloatingWindowLevel, NSVisualEffectView,
    NSVisualEffectMaterialDark, NSVisualEffectBlendingModeBehindWindow, NSVisualEffectStateActive
)
from Foundation import NSURL, NSURLRequest, NSObject
from PyObjCTools import AppHelper


# The HTML UI size roughly maps to a wide pill
EXPANDED_W = 386
EXPANDED_H = 110


class PyScriptHandler(NSObject):
    """Bridge to receive JS messages and navigation events."""
    def userContentController_didReceiveScriptMessage_(self, controller, message):
        body = str(message.body())
        if hasattr(self, 'overlay_ref'):
            if body == "cancel" and self.overlay_ref._cancel_cb:
                self.overlay_ref._cancel_cb()
            elif body == "stop" and self.overlay_ref._stop_cb:
                self.overlay_ref._stop_cb()

    def webView_didFinishNavigation_(self, webView, navigation):
        if hasattr(self, 'overlay_ref'):
            self.overlay_ref._on_page_loaded()


class OverlayWindow:
    """Manages the floating waveform overlay using WebKit frontend."""

    def __init__(self):
        self._window = None
        self._webview = None
        self._visible = False
        self._cancel_cb = None
        self._stop_cb = None
        
        self._handler = None
        self._cancel_label = "esc"
        self._page_loaded = False
        self._pending_js = None

        # Update Audio Speed to 80 chunks/sec to flawlessly feed the 60FPS conveyor belt
        self.SUBS_PER_CHUNK = 8
        # Adaptive gain for waveform visualization
        self._peak_rms = 0.01  # running peak, starts low

    def set_callbacks(self, cancel_cb=None, stop_cb=None):
        self._cancel_cb = cancel_cb
        self._stop_cb = stop_cb

    def set_cancel_label(self, label):
        self._cancel_label = str(label)

    def set_state(self, state_name):
        AppHelper.callAfter(self._set_state_main_thread, state_name)
        
    def _set_state_main_thread(self, state_name):
        if not getattr(self, '_visible', False) or self._webview is None:
            return
        js_code = f"if(window.setUIState) window.setUIState('{state_name}');"
        self._webview.evaluateJavaScript_completionHandler_(js_code, None)

    def _ensure_window(self):
        if self._window is not None:
            return

        w, h = EXPANDED_W, EXPANDED_H
        screen = NSScreen.mainScreen().frame()
        # Center horizontally, slightly above bottom
        x = (screen.size.width - w) / 2
        y = screen.size.height * 0.22

        self._window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((x, y), (w, h)),
            NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered,
            False,
        )
        self._window.setLevel_(NSFloatingWindowLevel)
        self._window.setOpaque_(False)
        self._window.setBackgroundColor_(NSColor.clearColor())
        self._window.setHasShadow_(False) # the HTML already draws its own drop shadow
        self._window.setMovableByWindowBackground_(True)
        self._window.setIgnoresMouseEvents_(False)

        # 1. Native frosted glass background for macOS UI
        visual_effect = NSVisualEffectView.alloc().initWithFrame_(((0, 0), (w, h)))
        visual_effect.setMaterial_(NSVisualEffectMaterialDark)
        visual_effect.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        visual_effect.setState_(NSVisualEffectStateActive)
        
        # Match tailwind CSS rounded
        visual_effect.setWantsLayer_(True)
        visual_effect.layer().setCornerRadius_(28)
        visual_effect.layer().setMasksToBounds_(True)

        # Config WKWebView
        conf = WebKit.WKWebViewConfiguration.alloc().init()
        conf.preferences().setValue_forKey_(True, "developerExtrasEnabled")

        # JS Bridge mapping
        self._handler = PyScriptHandler.alloc().init()
        self._handler.overlay_ref = self
        conf.userContentController().addScriptMessageHandler_name_(self._handler, "pyBridge")

        self._webview = WebKit.WKWebView.alloc().initWithFrame_configuration_(((0, 0), (w, h)), conf)
        self._webview.setValue_forKey_(False, "drawsBackground")
        self._webview.setNavigationDelegate_(self._handler)

        html_path = resource_path("ui.html")
        url = NSURL.fileURLWithPath_(html_path)
        # Force reload — bypass WKWebView disk cache for local HTML
        req = NSURLRequest.requestWithURL_cachePolicy_timeoutInterval_(url, 1, 30)
        
        self._webview.loadRequest_(req)
        
        self._window.setContentView_(visual_effect)
        visual_effect.addSubview_(self._webview)

    def show(self):
        self._peak_rms = 0.01  # reset adaptive gain for new recording
        AppHelper.callAfter(self._show_main_thread)
        
    def _build_show_js(self):
        safe = self._cancel_label.replace("'", "\\'")
        return (
            "if(window.setUIState) window.setUIState('recording');"
            "if(window.resetAnimation) window.resetAnimation();"
            f"if(window.setCancelLabel) window.setCancelLabel('{safe}');"
        )

    def _on_page_loaded(self):
        self._page_loaded = True
        if self._pending_js and self._webview:
            self._webview.evaluateJavaScript_completionHandler_(self._pending_js, None)
            self._pending_js = None

    def _show_main_thread(self):
        self._ensure_window()
        js = self._build_show_js()
        if self._page_loaded and self._webview:
            self._webview.evaluateJavaScript_completionHandler_(js, None)
        else:
            self._pending_js = js
        self._window.orderFront_(None)
        self._visible = True

    def hide(self):
        AppHelper.callAfter(self._hide_main_thread)
        
    def _hide_main_thread(self):
        self._visible = False
        if self._window:
            self._window.orderOut_(None)

    def push_audio(self, raw_bytes):
        """Feed raw int16 audio data to JS visualizer."""
        if not getattr(self, '_visible', False) or self._webview is None:
            return
            
        # Heavy computing kept on background thread
        samples = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        chunk_size = max(1, len(samples) // self.SUBS_PER_CHUNK)
        
        levels = []
        for i in range(self.SUBS_PER_CHUNK):
            seg = samples[i * chunk_size:(i + 1) * chunk_size]
            if len(seg) > 0:
                rms = math.sqrt(np.mean(seg ** 2))
                # Adaptive gain: track peak and normalize to it
                if rms > self._peak_rms:
                    self._peak_rms = rms
                else:
                    self._peak_rms *= 0.998  # slow decay
                gain = 0.7 / max(self._peak_rms, 0.001)
                levels.append(min(rms * gain, 1.0))
                
        # Dispatch UI update securely to main loop
        if levels:
            js_code = f"if(window.receiveAudioLevelArray) window.receiveAudioLevelArray({levels});"
            AppHelper.callAfter(self._webview.evaluateJavaScript_completionHandler_, js_code, None)
