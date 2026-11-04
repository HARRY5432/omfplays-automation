#!/usr/bin/env python3
"""
run_local_pipeline.py — Windows Task Scheduler entry point.

Runs: download → build shorts → git push → Telegram alert when raw is done.
Schedule via Task Scheduler: daily at 09:00, repeat every 30 min until 21:00.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

HERE = Path(__file__).resolve().parent
SHORTS_DIR = HERE / "shorts"
RAW_DIR = HERE / "raw"
STATE_DIR = HERE / "state"
UPLOADS_PATH = STATE_DIR / "uploads.json"
MANIFEST_PATH = SHORTS_DIR / "manifest.json"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{ts}] {msg}")


def run(cmd: list[str], cwd: Path = HERE) -> int:
    log(f"RUN: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(cwd))
    return result.returncode


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def raw_is_done() -> bool:
    """Return True if every raw video is consumed in manifest."""
    if not RAW_DIR.exists():
        return False
    raw_files = [f for f in RAW_DIR.iterdir() if f.suffix.lower() in {".mp4", ".mov", ".mkv", ".avi", ".mxf", ".ts", ".webm", ".m4v"}]
    if not raw_files:
        return False
    manifest = load_json(MANIFEST_PATH, [])
    consumed = {e.get("source") for e in manifest}
    remaining = [f.name for f in raw_files if f.name not in consumed]
    log(f"Raw files: {len(raw_files)}, Consumed: {len(consumed)}, Remaining: {len(remaining)}")
    return len(remaining) == 0


def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram not configured; skipping alert.")
        return False
    import urllib.request, urllib.error
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as e:
        log(f"Telegram send failed: {e}")
        return False


def git_push_changes() -> None:
    """Push shorts + state to GitHub so night workflow can upload."""
    log("Pushing changes to GitHub...")
    run(["git", "add", "shorts/", "state/uploads.json"])
    result = subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=str(HERE))
    if result.returncode == 0:
        log("No changes to push.")
        return
    run(["git", "commit", "-m", f"Local pipeline: new shorts {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"])
    run(["git", "push"])


def main() -> int:
    log("=== Local Pipeline Started ===")

    # 1. Download from channels
    rc = run([sys.executable, "download_channels.py"])
    if rc != 0:
        log("Download step failed or no new videos.")

    # 2. Build shorts
    rc = run([sys.executable, "build_shorts_omf.py"])
    if rc != 0:
        log("Build step failed.")
        return 1

    # 3. Check if raw is fully consumed
    if raw_is_done():
        log("✅ All raw videos processed.")
        shorts_count = len(list(SHORTS_DIR.glob("s*.mp4")))
        msg = (
            "✅ <b>Local Pipeline Complete</b>\n"
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M IST')}\n"
            f"🎬 Shorts built: <b>{shorts_count}</b>\n"
            f"📂 Raw clips: <b>All processed</b>\n"
            f"🌙 Night upload will handle publishing via GitHub Actions."
        )
        send_telegram(msg)
    else:
        log("Raw clips remaining; will continue on next run.")

    # 4. Push to GitHub for night upload
    git_push_changes()

    log("=== Local Pipeline Finished ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())