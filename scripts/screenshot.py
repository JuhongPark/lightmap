import argparse
import os
import subprocess
import sys

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
DOCS_DIR = os.path.join(os.path.dirname(__file__), "..", "docs")
HTML_PATH = os.path.join(DOCS_DIR, "prototype.html")
SCREENSHOT_DIR = os.path.join(DOCS_DIR, "screenshots")

CHROME = "google-chrome"


def run_prototype(args):
    cmd = [sys.executable, "prototype.py"]
    if args.night:
        cmd.append("--night")
    elif args.time:
        cmd.extend(["--time", args.time])
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=SRC_DIR, capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr)
        sys.exit(1)


def take_screenshot(name, width=1280, height=900):
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
    size_kb = os.path.getsize(out_path) / 1024
    print(f"Screenshot saved: {out_path} ({size_kb:.0f} KB)")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="Generate prototype and screenshot")
    parser.add_argument("--night", action="store_true", help="Night mode")
    parser.add_argument("--time", type=str, help="Custom time (YYYY-MM-DD HH:MM)")
    parser.add_argument("--both", action="store_true", help="Generate both day and night")
    parser.add_argument("--name", type=str, help="Custom screenshot filename (without .png)")
    args = parser.parse_args()

    if args.both:
        run_prototype(argparse.Namespace(night=False, time=None))
        take_screenshot("day")
        run_prototype(argparse.Namespace(night=True, time=None))
        take_screenshot("night")
    else:
        run_prototype(args)
        mode = "night" if args.night else "day"
        name = args.name if args.name else mode
        take_screenshot(name)


if __name__ == "__main__":
    main()
