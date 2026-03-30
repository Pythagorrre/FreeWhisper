"""Helpers for launching FreeWhisper through its macOS app bundle."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys

from app_paths import APP_SOURCE_DIR, BUNDLE_ID, BUNDLE_PATH

CONFIG_DIR = APP_SOURCE_DIR
FREEWHISPER_BUNDLE_ID = BUNDLE_ID
FREEWHISPER_BUNDLE_CANDIDATES = tuple(
    path
    for path in (
        BUNDLE_PATH,
        os.path.join(CONFIG_DIR, "FreeWhisper.app"),
        os.path.expanduser("~/Desktop/FreeWhisper.app"),
    )
    if path
)


def _bundle_identifier(bundle_path: str) -> str | None:
    info_plist = os.path.join(bundle_path, "Contents", "Info.plist")
    if not os.path.exists(info_plist):
        return None
    try:
        with open(info_plist, "rb") as f:
            info = plistlib.load(f)
        bundle_id = info.get("CFBundleIdentifier")
        return str(bundle_id) if bundle_id else None
    except Exception:
        return None


def _bundle_path_from_command(command: str) -> str | None:
    if not command:
        return None
    marker = ".app/Contents/MacOS/"
    idx = command.find(marker)
    if idx == -1:
        return None
    bundle_path = command[: idx + 4]
    if not os.path.exists(bundle_path):
        return None
    if _bundle_identifier(bundle_path) != FREEWHISPER_BUNDLE_ID:
        return None
    return bundle_path


def running_app_bundle_path() -> str | None:
    """Return the parent FreeWhisper app bundle when launched from one."""
    try:
        res = subprocess.run(
            ["ps", "-o", "command=", "-p", str(os.getppid())],
            capture_output=True,
            text=True,
            check=False,
        )
        return _bundle_path_from_command((res.stdout or "").strip())
    except Exception:
        return None


def canonical_app_bundle_path() -> str | None:
    running_bundle = running_app_bundle_path()
    if running_bundle:
        return running_bundle

    for path in FREEWHISPER_BUNDLE_CANDIDATES:
        if os.path.exists(path) and _bundle_identifier(path) == FREEWHISPER_BUNDLE_ID:
            return path
    return None


def launch_program_arguments(force_new_instance: bool = False) -> list[str]:
    bundle_path = canonical_app_bundle_path()
    if bundle_path:
        args = ["/usr/bin/open"]
        if force_new_instance:
            args.append("-n")
        args.append(bundle_path)
        return args

    return [sys.executable, os.path.join(CONFIG_DIR, "free_whisper.py")]
