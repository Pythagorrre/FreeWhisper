#!/usr/bin/env python3
"""Build a polished drag-and-drop DMG installer for FreeWhisper."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from build_standalone_app import APP_NAME, BUNDLE_PATH, build_bundle


ROOT = Path(__file__).resolve().parent
DMG_PATH = ROOT / f"{APP_NAME}.dmg"
VOLUME_NAME = APP_NAME
BACKGROUND_DIR_NAME = ".background"
BACKGROUND_FILE_NAME = "background.png"
WINDOW_BOUNDS = (120, 120, 1120, 700)
WINDOW_WIDTH = WINDOW_BOUNDS[2] - WINDOW_BOUNDS[0]
WINDOW_HEIGHT = WINDOW_BOUNDS[3] - WINDOW_BOUNDS[1]
APP_ICON_POSITION = (230, 280)
APPLICATIONS_ICON_POSITION = (760, 280)
ICON_SIZE = 160
RETINA_SCALE = 2
CUSTOM_BACKGROUND_SOURCE = ROOT / "docs" / "assets" / "dmg-background-clean.png"
BUNDLE_CANDIDATES = (
    BUNDLE_PATH,
    Path("/Applications") / f"{APP_NAME}.app",
    Path.home() / "Applications" / f"{APP_NAME}.app",
    Path.home() / "Desktop" / f"{APP_NAME}.app",
)


def run(cmd: list[str], capture_output: bool = False) -> str:
    completed = subprocess.run(
        cmd,
        check=True,
        capture_output=capture_output,
        text=True,
    )
    return completed.stdout if capture_output else ""


def ensure_app_bundle() -> Path:
    for candidate in BUNDLE_CANDIDATES:
        if candidate.exists():
            return candidate

    build_bundle()
    return BUNDLE_PATH


def default_background_image() -> Image.Image:
    width, height = WINDOW_WIDTH * RETINA_SCALE, WINDOW_HEIGHT * RETINA_SCALE
    image = Image.new("RGBA", (width, height), "#f4f7fb")
    draw = ImageDraw.Draw(image)

    top_color = (244, 247, 251)
    bottom_color = (229, 238, 248)
    for y in range(height):
        mix = y / max(height - 1, 1)
        row = tuple(
            int((1 - mix) * top_color[i] + mix * bottom_color[i]) for i in range(3)
        )
        draw.line((0, y, width, y), fill=row)

    arrow_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    arrow_draw = ImageDraw.Draw(arrow_layer)
    arrow_draw.rounded_rectangle(
        (312, 228, 592, 328),
        radius=46,
        fill=(222, 228, 236, 255),
    )
    arrow_draw.polygon(
        ((540, 170), (750, 280), (540, 390)),
        fill=(222, 228, 236, 255),
    )
    arrow_layer = arrow_layer.filter(ImageFilter.GaussianBlur(0.4))
    image.alpha_composite(arrow_layer)

    cloud_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    cloud_draw = ImageDraw.Draw(cloud_layer)
    cloud_fill = (251, 252, 254, 255)
    for ellipse in (
        (-120, 860, 320, 1160),
        (150, 820, 640, 1180),
        (500, 840, 980, 1180),
        (820, 800, 1360, 1180),
        (1150, 840, 1680, 1200),
    ):
        cloud_draw.ellipse(ellipse, fill=cloud_fill)
    image.alpha_composite(cloud_layer)

    return image


def soften_live_drop_targets(image: Image.Image) -> Image.Image:
    scale_x = image.width / WINDOW_WIDTH
    scale_y = image.height / WINDOW_HEIGHT

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    def center_box(center: tuple[int, int], width: int, height: int) -> tuple[int, int, int, int]:
        cx = int(center[0] * scale_x)
        cy = int(center[1] * scale_y)
        half_w = int(width * scale_x / 2)
        half_h = int(height * scale_y / 2)
        return (cx - half_w, cy - half_h, cx + half_w, cy + half_h)

    # Remove the baked text from the artwork so Finder can render the live labels
    # cleanly, while keeping the arrow and decorative atmosphere.
    draw.rounded_rectangle(
        center_box((APP_ICON_POSITION[0], APP_ICON_POSITION[1] + 152), 320, 72),
        radius=int(26 * scale_x),
        fill=(248, 250, 253, 246),
    )
    draw.rounded_rectangle(
        center_box((APPLICATIONS_ICON_POSITION[0], APPLICATIONS_ICON_POSITION[1] + 152), 300, 72),
        radius=int(26 * scale_x),
        fill=(248, 250, 253, 246),
    )

    overlay = overlay.filter(ImageFilter.GaussianBlur(int(14 * scale_x)))
    image.alpha_composite(overlay)
    return image


def create_background_image(path: Path) -> None:
    if CUSTOM_BACKGROUND_SOURCE.exists():
        image = Image.open(CUSTOM_BACKGROUND_SOURCE).convert("RGBA")
        image = ImageOps.fit(
            image,
            (WINDOW_WIDTH * RETINA_SCALE, WINDOW_HEIGHT * RETINA_SCALE),
            method=Image.LANCZOS,
            centering=(0.5, 0.5),
        )
        image = soften_live_drop_targets(image)
    else:
        image = default_background_image()

    image.convert("RGB").save(path)


def stage_payload(staging_dir: Path, app_bundle: Path) -> None:
    app_dst = staging_dir / f"{APP_NAME}.app"
    run(["cp", "-R", str(app_bundle), str(app_dst)])
    (staging_dir / "Applications").symlink_to("/Applications")
    (staging_dir / ".hidden").write_text(".background\n.fseventsd\n.hidden\n")

    background_dir = staging_dir / BACKGROUND_DIR_NAME
    background_dir.mkdir()
    create_background_image(background_dir / BACKGROUND_FILE_NAME)


def hide_auxiliary_entries(mount_point: Path) -> None:
    hidden_names = (
        BACKGROUND_DIR_NAME,
        ".fseventsd",
        ".hidden",
        ".Trashes",
        ".VolumeIcon.icns",
    )
    for name in hidden_names:
        target = mount_point / name
        if target.exists():
            subprocess.run(["chflags", "hidden", str(target)], check=False)


def customize_finder_window(mount_point: Path) -> None:
    hide_auxiliary_entries(mount_point)

    applescript = f'''
tell application "Finder"
    tell disk "{VOLUME_NAME}"
        open
        delay 1
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set pathbar visible of container window to false
        set the bounds of container window to {{{WINDOW_BOUNDS[0]}, {WINDOW_BOUNDS[1]}, {WINDOW_BOUNDS[2]}, {WINDOW_BOUNDS[3]}}}
        set arrangement of icon view options of container window to not arranged
        set icon size of icon view options of container window to {ICON_SIZE}
        set background picture of icon view options of container window to file "{BACKGROUND_DIR_NAME}:{BACKGROUND_FILE_NAME}"
        set position of item "{APP_NAME}.app" to {{{APP_ICON_POSITION[0]}, {APP_ICON_POSITION[1]}}}
        set position of item "Applications" to {{{APPLICATIONS_ICON_POSITION[0]}, {APPLICATIONS_ICON_POSITION[1]}}}
        if exists item ".background" then set position of item ".background" to {{1400, 1200}}
        if exists item ".fseventsd" then set position of item ".fseventsd" to {{1550, 1200}}
        if exists item ".hidden" then set position of item ".hidden" to {{1700, 1200}}
        close
        open
        update without registering applications
        delay 2
    end tell
end tell
'''
    subprocess.run(["osascript", "-e", applescript], check=True)
    hide_auxiliary_entries(mount_point)


def build_dmg() -> Path:
    app_bundle = ensure_app_bundle()

    with tempfile.TemporaryDirectory(prefix="freewhisper-dmg-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        staging_dir = tmp_path / "payload"
        staging_dir.mkdir()
        stage_payload(staging_dir, app_bundle)

        rw_dmg = tmp_path / f"{APP_NAME}-rw.dmg"
        run(
            [
                "hdiutil",
                "create",
                "-volname",
                VOLUME_NAME,
                "-srcfolder",
                str(staging_dir),
                "-ov",
                "-format",
                "UDRW",
                "-fs",
                "HFS+",
                str(rw_dmg),
            ]
        )

        attach_output = run(
            [
                "hdiutil",
                "attach",
                str(rw_dmg),
                "-readwrite",
                "-noverify",
                "-noautoopen",
            ],
            capture_output=True,
        )

        mount_point = None
        for line in attach_output.splitlines():
            if "/Volumes/" in line:
                mount_point = Path(line.split("\t")[-1])
        if mount_point is None:
            raise RuntimeError("Could not determine mounted DMG path.")

        customize_finder_window(mount_point)
        run(["hdiutil", "detach", str(mount_point)])

        if DMG_PATH.exists():
            DMG_PATH.unlink()

        run(
            [
                "hdiutil",
                "convert",
                str(rw_dmg),
                "-ov",
                "-format",
                "UDZO",
                "-imagekey",
                "zlib-level=9",
                "-o",
                str(DMG_PATH),
            ]
        )

    return DMG_PATH


def main() -> None:
    dmg_path = build_dmg()
    print(f"Built DMG at {dmg_path}")


if __name__ == "__main__":
    main()
