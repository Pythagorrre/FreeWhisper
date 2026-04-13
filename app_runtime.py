"""Helpers for launching FreeWhisper through its macOS app bundle."""

from __future__ import annotations

import logging
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile

from app_paths import APP_SOURCE_DIR, BUNDLE_ID, BUNDLE_PATH

log = logging.getLogger("freewhisper")

CONFIG_DIR = APP_SOURCE_DIR
FREEWHISPER_BUNDLE_ID = BUNDLE_ID
LATEST_RELEASE_URL = "https://github.com/Pythagorrre/FreeWhisper/releases/latest"
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


GITHUB_API_LATEST = "https://api.github.com/repos/Pythagorrre/FreeWhisper/releases/latest"

# Fallback version when not running from an app bundle (dev mode).
_FALLBACK_VERSION = "0.0.0"


def get_current_version() -> str:
    """Return the running app version from Info.plist, or a fallback."""
    if BUNDLE_PATH:
        info_plist = os.path.join(BUNDLE_PATH, "Contents", "Info.plist")
        if os.path.exists(info_plist):
            try:
                with open(info_plist, "rb") as f:
                    info = plistlib.load(f)
                v = info.get("CFBundleShortVersionString")
                if v:
                    return str(v)
            except Exception:
                pass
    return _FALLBACK_VERSION


def _version_tuple(v: str) -> tuple[int, ...]:
    """Parse '1.2.3' into (1, 2, 3) for comparison."""
    parts: list[int] = []
    for part in v.strip().lstrip("v").split("."):
        try:
            parts.append(int(part))
        except ValueError:
            break
    return tuple(parts)


def check_for_update() -> tuple[str, str] | None:
    """Check GitHub for a newer release.

    Returns ``(latest_version, dmg_download_url)`` when an update is
    available, or *None* if already up-to-date (or on error).
    """
    import requests  # imported lazily to keep startup fast

    current = get_current_version()
    log.debug("Update check: current version %s", current)
    try:
        resp = requests.get(
            GITHUB_API_LATEST,
            headers={"Accept": "application/vnd.github+json"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        log.exception("Update check: failed to query GitHub API")
        raise

    latest_tag: str = data.get("tag_name", "")
    if not latest_tag:
        raise RuntimeError("GitHub release has no tag_name")

    if _version_tuple(latest_tag) <= _version_tuple(current):
        return None  # already up-to-date

    # Find the .dmg asset
    dmg_url: str | None = None
    for asset in data.get("assets", []):
        name: str = asset.get("name", "")
        if name.lower().endswith(".dmg"):
            dmg_url = asset.get("browser_download_url")
            break

    if not dmg_url:
        raise RuntimeError(f"Release {latest_tag} has no .dmg asset")

    log.debug("Update available: %s -> %s  (%s)", current, latest_tag, dmg_url)
    return latest_tag, dmg_url


def download_and_apply_update(dmg_url: str, relaunch: bool = True) -> None:
    """Download the DMG, replace the running app bundle, and relaunch.

    Must be called from a background thread — this function blocks while
    downloading.  The actual relaunch (if *relaunch* is True) quits the
    current process via ``rumps.quit_application()``.
    """
    import requests
    import shlex

    bundle = canonical_app_bundle_path()
    if not bundle:
        raise RuntimeError("Cannot determine app bundle path")

    tmp_dir = tempfile.mkdtemp(prefix="freewhisper_update_")
    dmg_path = os.path.join(tmp_dir, "FreeWhisper.dmg")

    try:
        # 1. Download DMG
        log.debug("Downloading update from %s", dmg_url)
        with requests.get(dmg_url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(dmg_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    f.write(chunk)

        # 2. Mount DMG
        log.debug("Mounting DMG")
        mount_res = subprocess.run(
            ["hdiutil", "attach", dmg_path, "-nobrowse", "-readonly",
             "-mountrandom", tmp_dir],
            capture_output=True, text=True, check=True,
        )
        # Parse mount point from hdiutil output (last column of last line)
        mount_point: str | None = None
        for line in mount_res.stdout.strip().splitlines():
            cols = line.split("\t")
            if len(cols) >= 3:
                mount_point = cols[-1].strip()
        if not mount_point or not os.path.isdir(mount_point):
            raise RuntimeError(f"Failed to parse mount point: {mount_res.stdout}")

        # 3. Find the .app inside the mounted volume
        new_app: str | None = None
        for entry in os.listdir(mount_point):
            if entry.endswith(".app"):
                new_app = os.path.join(mount_point, entry)
                break
        if not new_app:
            raise RuntimeError("No .app found in mounted DMG")

        # 4. Stage the new bundle next to the old one
        dest_parent = os.path.dirname(bundle)
        staged = os.path.join(dest_parent, ".FreeWhisper_update.app")
        if os.path.exists(staged):
            shutil.rmtree(staged)
        log.debug("Copying new bundle to %s", staged)
        subprocess.run(
            ["ditto", new_app, staged],
            check=True, capture_output=True,
        )

        # 5. Unmount (best effort)
        subprocess.run(
            ["hdiutil", "detach", mount_point, "-quiet"],
            check=False, capture_output=True,
        )

    except Exception:
        # Clean up temp dir on error
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    shutil.rmtree(tmp_dir, ignore_errors=True)

    if not relaunch:
        return

    # 6. Launch a helper script that replaces the bundle and relaunches.
    #    We do this from a detached shell so the current process can quit.
    launch_args = launch_program_arguments(force_new_instance=True)
    open_cmd = " ".join(shlex.quote(a) for a in launch_args)

    script = (
        f'sleep 2 && '
        f'rm -rf {shlex.quote(bundle)} && '
        f'mv {shlex.quote(staged)} {shlex.quote(bundle)} && '
        f'exec {open_cmd}'
    )
    log.debug("Launching updater script: %s", script)
    subprocess.Popen(
        ["bash", "-lc", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    # Quit the running app so the bundle can be replaced
    import rumps
    rumps.quit_application()


def open_latest_release_page() -> None:
    subprocess.Popen(["open", LATEST_RELEASE_URL])
