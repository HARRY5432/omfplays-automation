#!/usr/bin/env python3
"""
cleanup_shorts.py — Safely delete shorts that have been successfully uploaded.

Only removes shorts marked as 'posted' in uploads.json.
Never deletes pending shorts.
"""
import json
import os
import sys
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


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def main() -> int:
    uploads = load_json(UPLOADS_PATH, {})
    posted_shorts = {
        v.get("short") for v in uploads.values()
        if v.get("status") == "posted" and v.get("short")
    }

    if not posted_shorts:
        print("No posted shorts to clean up.", flush=True)
        return 0

    deleted = 0
    freed_bytes = 0
    for short_name in sorted(posted_shorts):
        for ext in (".mp4", ".jpg", ".txt", ".json"):
            f = SHORTS_DIR / f"{short_name}{ext}"
            if f.exists():
                try:
                    size = f.stat().st_size
                    f.unlink()
                    freed_bytes += size
                    deleted += 1
                    print(f"  🗑️  {f.name} ({size / 1024 / 1024:.1f} MB)", flush=True)
                except Exception as e:
                    print(f"  ⚠️  Could not delete {f.name}: {e}", flush=True)

    print(f"\n✅ Cleanup: {deleted} files deleted, {freed_bytes / 1024 / 1024:.1f} MB freed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())