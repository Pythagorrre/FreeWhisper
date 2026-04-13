#!/usr/bin/env python3
"""Build a self-contained FreeWhisper.app bundle."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
APP_NAME = "FreeWhisper"
APP_VERSION = "1.0.3"
BUNDLE_ID = "com.freewhisper.app"
BUNDLE_PATH = ROOT / f"{APP_NAME}.app"

PYTHON_VERSION = f"{sys.version_info.major}.{sys.version_info.minor}"
PYTHON_HOME_SRC = Path(sys.base_prefix)
SITE_PACKAGES_SRC = ROOT / ".venv" / "lib" / f"python{PYTHON_VERSION}" / "site-packages"

APP_FILES = (
    "app_paths.py",
    "app_runtime.py",
    "free_whisper.py",
    "overlay.py",
    "settings_window.py",
    "ui.html",
    "waveform_frontend.html",
    "iconTemplate.png",
    "gladia_logo.png",
    "cohere_logo.png",
)

MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
}


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def remove_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
        return
    path.unlink()


def write_info_plist(path: Path) -> None:
    info = {
        "CFBundleDisplayName": APP_NAME,
        "CFBundleExecutable": APP_NAME,
        "CFBundleIconFile": APP_NAME,
        "CFBundleIconName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleName": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": APP_VERSION,
        "LSUIElement": True,
        "NSAppleEventsUsageDescription": (
            "FreeWhisper needs to paste transcribed text into your active application."
        ),
        "NSMicrophoneUsageDescription": (
            "FreeWhisper needs microphone access for speech-to-text dictation."
        ),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        plistlib.dump(info, f)


def ensure_python_runtime() -> None:
    if not PYTHON_HOME_SRC.exists():
        raise FileNotFoundError(f"Python home not found: {PYTHON_HOME_SRC}")
    python_app = (
        PYTHON_HOME_SRC
        / "Resources"
        / "Python.app"
        / "Contents"
        / "MacOS"
        / "Python"
    )
    if not python_app.exists():
        raise FileNotFoundError(f"Framework Python.app not found: {python_app}")
    if not SITE_PACKAGES_SRC.exists():
        raise FileNotFoundError(f"Virtualenv site-packages not found: {SITE_PACKAGES_SRC}")


def macho_install_name(path: Path) -> str | None:
    proc = subprocess.run(
        ["otool", "-D", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None

    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    return lines[1]


def macho_dependencies(path: Path) -> list[str]:
    proc = subprocess.run(
        ["otool", "-L", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return []

    deps: list[str] = []
    for line in proc.stdout.splitlines()[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        deps.append(stripped.split(" (compatibility version", 1)[0])
    return deps


def is_macho_file(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    try:
        with path.open("rb") as f:
            return f.read(4) in MACHO_MAGICS
    except OSError:
        return False


def rewrite_macho_paths(bundle_path: Path) -> None:
    old_prefix = str(PYTHON_HOME_SRC)
    new_prefix = "@executable_path/../../../.."

    for path in bundle_path.rglob("*"):
        if not is_macho_file(path):
            continue

        args: list[str] = []
        install_name = macho_install_name(path)
        if install_name and install_name.startswith(old_prefix):
            args.extend(["-id", install_name.replace(old_prefix, new_prefix, 1)])

        for dep in macho_dependencies(path):
            if dep.startswith(old_prefix):
                args.extend(["-change", dep, dep.replace(old_prefix, new_prefix, 1)])

        if args:
            run(["install_name_tool", *args, str(path)])


def sign_runtime(bundle_path: Path) -> None:
    python_framework = bundle_path / "Contents" / "Frameworks" / "Python.framework"
    version_root = python_framework / "Versions" / PYTHON_VERSION
    python_app = version_root / "Resources" / "Python.app"
    nested_bundles = (
        version_root / "Frameworks" / "Tcl.framework",
        version_root / "Frameworks" / "Tk.framework",
        python_app,
    )
    nested_prefixes = tuple(str(path) + os.sep for path in nested_bundles)

    for path in bundle_path.rglob("*"):
        if is_macho_file(path):
            path_str = str(path)
            if any(path_str.startswith(prefix) for prefix in nested_prefixes):
                continue
            run(["codesign", "--force", "--sign", "-", str(path)])

    for nested_bundle in nested_bundles[:2]:
        run(["codesign", "--force", "--deep", "--sign", "-", str(nested_bundle)])
    run(["codesign", "--force", "--deep", "--sign", "-", str(python_app)])
    run(["codesign", "--force", "--deep", "--sign", "-", str(python_framework)])
    run(["codesign", "--force", "--deep", "--sign", "-", str(bundle_path)])


def prune_python_runtime(version_root: Path) -> None:
    stdlib_root = version_root / "lib" / f"python{PYTHON_VERSION}"
    removable_paths = (
        version_root / "share",
        version_root / "Resources" / "English.lproj",
        stdlib_root / "ensurepip",
        stdlib_root / "idlelib",
        stdlib_root / "test",
        stdlib_root / "turtledemo",
        stdlib_root / "venv",
    )

    for path in removable_paths:
        remove_path(path)

    for cache_dir in version_root.rglob("__pycache__"):
        remove_path(cache_dir)

    site_packages_root = stdlib_root / "site-packages"
    for pattern in ("pip", "pip-*.dist-info"):
        for path in site_packages_root.glob(pattern):
            remove_path(path)


def copy_python_runtime(bundle_path: Path) -> None:
    framework_root = bundle_path / "Contents" / "Frameworks" / "Python.framework"
    framework_version_dst = framework_root / "Versions" / PYTHON_VERSION
    framework_version_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PYTHON_HOME_SRC, framework_version_dst, symlinks=True)

    current_link = framework_root / "Versions" / "Current"
    if current_link.exists() or current_link.is_symlink():
        current_link.unlink()
    current_link.symlink_to(PYTHON_VERSION)

    for top_level_name in ("Python", "Headers", "Resources"):
        top_level_path = framework_root / top_level_name
        if top_level_path.exists() or top_level_path.is_symlink():
            top_level_path.unlink()
        top_level_path.symlink_to(Path("Versions") / "Current" / top_level_name)

    site_packages_dst = (
        framework_version_dst / "lib" / f"python{PYTHON_VERSION}" / "site-packages"
    )
    remove_path(site_packages_dst)
    shutil.copytree(SITE_PACKAGES_SRC, site_packages_dst, symlinks=True)
    prune_python_runtime(framework_version_dst)

    rewrite_macho_paths(bundle_path)


def copy_app_sources(bundle_path: Path) -> None:
    resources_app_dir = bundle_path / "Contents" / "Resources" / "app"
    resources_app_dir.mkdir(parents=True, exist_ok=True)
    for name in APP_FILES:
        copy_file(ROOT / name, resources_app_dir / name)


def existing_icon_path() -> Path:
    candidates = (
        BUNDLE_PATH / "Contents" / "Resources" / f"{APP_NAME}.icns",
        Path("/Applications") / f"{APP_NAME}.app" / "Contents" / "Resources" / f"{APP_NAME}.icns",
        Path.home() / "Desktop" / f"{APP_NAME}.app" / "Contents" / "Resources" / f"{APP_NAME}.icns",
    )
    for icon_path in candidates:
        if icon_path.exists():
            return icon_path
    raise FileNotFoundError(
        "Missing FreeWhisper.icns in the repo, /Applications, or Desktop bundle."
    )


def compile_launcher(bundle_path: Path) -> None:
    launcher_dst = bundle_path / "Contents" / "MacOS" / APP_NAME
    launcher_dst.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "clang",
            "-Os",
            "-arch",
            "arm64",
            "-arch",
            "x86_64",
            str(ROOT / "macos_launcher.c"),
            "-framework",
            "CoreFoundation",
            "-o",
            str(launcher_dst),
        ]
    )


def build_bundle() -> Path:
    ensure_python_runtime()

    with tempfile.TemporaryDirectory(prefix="freewhisper-build-") as tmp_dir:
        bundle_path = Path(tmp_dir) / f"{APP_NAME}.app"
        contents_dir = bundle_path / "Contents"
        resources_dir = contents_dir / "Resources"
        resources_dir.mkdir(parents=True, exist_ok=True)

        write_info_plist(contents_dir / "Info.plist")
        copy_file(existing_icon_path(), resources_dir / f"{APP_NAME}.icns")
        compile_launcher(bundle_path)
        copy_python_runtime(bundle_path)
        copy_app_sources(bundle_path)

        sign_runtime(bundle_path)

        if BUNDLE_PATH.exists():
            shutil.rmtree(BUNDLE_PATH)
        shutil.move(str(bundle_path), str(BUNDLE_PATH))

    return BUNDLE_PATH


def main() -> None:
    build_bundle()
    print(f"Built standalone bundle at {BUNDLE_PATH}")
    print(f"Embedded Python home: {PYTHON_HOME_SRC}")
    print(f"Embedded site-packages: {SITE_PACKAGES_SRC}")


if __name__ == "__main__":
    main()
