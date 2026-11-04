#!/usr/bin/env python3
"""
cleanup_shorts.py — Delete shorts that have been successfully uploaded.

Run after upload to free disk space. Keeps only unposted shorts.
"""
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHORTS_DIR = HERE / "shorts"
STATE_DIR = HERE / "state"
UPLOADS_PATH = STATE_DIR / "uploads.json"

def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default

def main():
    uploads = load_json(UPLOADS_PATH, {})
    
    # Find all shorts that are marked as posted
    posted_shorts = set()
    for key, val in uploads.items():
        if val.get("status") == "posted":
            short_name = val.get("short")
            if short_name:
                posted_shorts.add(short_name)
    
    if not posted_shorts:
        print("No posted shorts to clean up")
        return 0
    
    # Delete posted shorts from disk
    deleted = 0
    freed_bytes = 0
    for short_name in posted_shorts:
        for ext in (".mp4", ".jpg", ".txt"):
            f = SHORTS_DIR / f"{short_name}{ext}"
            if f.exists():
                size = f.stat().st_size
                f.unlink()
                freed_bytes += size
                deleted += 1
                print(f"  🗑️ Deleted {f.name} ({size/1024/1024:.1f} MB)")
    
    print(f"\n✅ Cleanup complete: {deleted} files deleted, {freed_bytes/1024/1024:.1f} MB freed")
    return 0

if __name__ == "__main__":
    exit(main())