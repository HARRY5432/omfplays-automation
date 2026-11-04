#!/usr/bin/env python3
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"


def _yt_dlp_cmd():
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    return [sys.executable, "-m", "yt_dlp"]


def load_config():
    with open(CONFIG, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def cleanup_partials(raw_dir):
    for f in raw_dir.glob("*.part"):
        f.unlink(missing_ok=True)
    for f in raw_dir.glob("*.ytdl"):
        f.unlink(missing_ok=True)


def download_one_video(channel_url, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = _yt_dlp_cmd() + [
        "--playlist-items", "1",
        "-f", "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", str(out_dir / "%(title).80s_%(id)s.%(ext)s"),
        "--no-playlist",
        "--no-overwrites",
        "--ignore-errors",
        "--socket-timeout", "30",
        "--retries", "3",
        "--extractor-retries", "3",
        "--js-runtimes", "node",
        "--remote-components", "ejs:github",
    ]
    cookies = HERE / "cookies.txt"
    if cookies.exists():
        cmd += ["--cookies", str(cookies)]
    cmd.append(channel_url)
    print(f"Downloading 1 video from {channel_url}")
    print(f"  -> {out_dir}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"WARNING: yt-dlp exited with code {result.returncode}")
    else:
        print(f"  Done!")


def main():
    cfg = load_config()
    raw_dir = HERE / cfg["raw_dir"]
    raw_dir.mkdir(exist_ok=True)

    cleanup_partials(raw_dir)

    channels = cfg.get("channels", [])
    if not channels:
        print("No channels configured in config.json")
        sys.exit(1)

    existing = list(raw_dir.glob("*.mp4"))
    if existing:
        print(f"Already have {len(existing)} video(s) in raw/. Skipping download.")
        return

    download_one_video(channels[0], raw_dir)

    downloaded = list(raw_dir.glob("*.mp4"))
    print(f"\nTotal raw files: {len(downloaded)}")


if __name__ == "__main__":
    main()
