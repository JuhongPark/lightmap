import argparse
import os
import subprocess
import sys

from PIL import Image

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
DOCS_DIR = os.path.join(os.path.dirname(__file__), "..", "docs")
HTML_PATH = os.path.join(DOCS_DIR, "prototype.html")
SCREENSHOT_DIR = os.path.join(DOCS_DIR, "screenshots")

CHROME = "google-chrome"


def run_prototype(night=False, time=None, scale=1):
    cmd = [sys.executable, "prototype.py", "--scale", str(scale)]
    if night:
        cmd.append("--night")
    elif time:
        cmd.extend(["--time", time])
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=SRC_DIR, capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr)
        sys.exit(1)


# Capture at 1280x807 because folium adds an 87px white bar at the bottom.
# _crop_whitespace removes it, giving a final 1280x720 (16:9) image.
def take_screenshot(name, width=1280, height=807):
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    out_path = os.path.join(SCREENSHOT_DIR, f"{name}.png")
    cmd = [
        CHROME,
        "--headless",
        "--disable-gpu",
        f"--screenshot={out_path}",
        f"--window-size={width},{height}",
        "--virtual-time-budget=5000",
        HTML_PATH,
    ]
    subprocess.run(cmd, capture_output=True)
    _crop_whitespace(out_path)
    size_kb = os.path.getsize(out_path) / 1024
    print(f"Screenshot saved: {out_path} ({size_kb:.0f} KB)")
    return out_path


def _crop_whitespace(path):
    img = Image.open(path)
    w, h = img.size
    crop_y = h
    for y in range(h - 1, 0, -1):
        pixels = [img.getpixel((x, y))[:3] for x in range(0, w, 100)]
        if not all(r > 240 and g > 240 and b > 240 for r, g, b in pixels):
            crop_y = y + 1
            break
    if crop_y < h:
        img.crop((0, 0, w, crop_y)).save(path)
        print(f"  Cropped {h - crop_y}px white bar from bottom")


def main():
    parser = argparse.ArgumentParser(description="Generate prototype and screenshot")
    parser.add_argument("--night", action="store_true", help="Night mode")
    parser.add_argument("--time", type=str, help="Custom time (YYYY-MM-DD HH:MM)")
    parser.add_argument("--both", action="store_true", help="Generate both day and night")
    parser.add_argument("--scale", type=int, default=1, help="Percent of data (1, 10, 50, 100)")
    parser.add_argument("--name", type=str, help="Custom screenshot filename (without .png)")
    args = parser.parse_args()

    suffix = "_1each" if args.scale == 0 else f"_{args.scale}pct"

    if args.both:
        run_prototype(night=False, scale=args.scale)
        take_screenshot(f"day{suffix}")
        run_prototype(night=True, scale=args.scale)
        take_screenshot(f"night{suffix}")
    else:
        run_prototype(night=args.night, time=args.time, scale=args.scale)
        mode = "night" if args.night else "day"
        name = args.name if args.name else f"{mode}{suffix}"
        take_screenshot(name)


if __name__ == "__main__":
    main()
