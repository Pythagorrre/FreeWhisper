"""Shared runtime paths for repo and standalone app-bundle layouts."""

from __future__ import annotations

import os
import shutil


APP_NAME = "FreeWhisper"
BUNDLE_ID = "com.freewhisper.app"

APP_SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))


def _bundle_contents_dir_from_source(source_dir: str) -> str | None:
    resources_dir = os.path.dirname(source_dir)
    if os.path.basename(resources_dir) != "Resources":
        return None

    contents_dir = os.path.dirname(resources_dir)
    if os.path.basename(contents_dir) != "Contents":
        return None

    return contents_dir


CONTENTS_DIR = _bundle_contents_dir_from_source(APP_SOURCE_DIR)
BUNDLE_PATH = os.path.dirname(CONTENTS_DIR) if CONTENTS_DIR else None

USER_SUPPORT_DIR = os.path.expanduser(f"~/Library/Application Support/{APP_NAME}")
USER_LOG_DIR = os.path.expanduser(f"~/Library/Logs/{APP_NAME}")


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def resource_path(*parts: str) -> str:
    return os.path.join(APP_SOURCE_DIR, *parts)


def user_support_path(*parts: str) -> str:
    path = os.path.join(USER_SUPPORT_DIR, *parts)
    ensure_dir(os.path.dirname(path) if parts else path)
    return path


def user_log_path(*parts: str) -> str:
    path = os.path.join(USER_LOG_DIR, *parts)
    ensure_dir(os.path.dirname(path) if parts else path)
    return path


def _legacy_data_candidates(filename: str) -> list[str]:
    candidates: list[str] = []

    local_path = resource_path(filename)
    candidates.append(local_path)

    if BUNDLE_PATH:
        sibling_path = os.path.join(os.path.dirname(BUNDLE_PATH), filename)
        candidates.append(sibling_path)

    unique: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        norm = os.path.abspath(path)
        if norm in seen:
            continue
        seen.add(norm)
        unique.append(path)
    return unique


def ensure_user_data_file(filename: str) -> str:
    destination = user_support_path(filename)
    if os.path.exists(destination):
        return destination

    for candidate in _legacy_data_candidates(filename):
        if not os.path.exists(candidate):
            continue
        if os.path.abspath(candidate) == os.path.abspath(destination):
            continue
        shutil.copy2(candidate, destination)
        return destination

    return destination
