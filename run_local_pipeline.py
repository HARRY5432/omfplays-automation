#!/usr/bin/env python3
"""
run_local_pipeline.py — Windows Task Scheduler entry point.

Autonomous daily pipeline:
  1. Check raw status — alert if empty/low BEFORE downloading
  2. Download only if raw is below threshold
  3. Build shorts (capped at daily_shorts = 5 per run)
  4. Git push → GitHub Actions handles night upload
  5. Telegram alerts at every milestone
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
RAW_DIR = HERE / "raw"
SHORTS_DIR = HERE / "shorts"
STATE_DIR = HERE / "state"
UPLOADS_PATH = STATE_DIR / "uploads.json"
MANIFEST_PATH = SHORTS_DIR / "manifest.json"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CONFIG_PATH = HERE / "config.json"


def load_config():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


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


def raw_status() -> dict:
    """Return raw folder stats: total, consumed, remaining."""
    cfg = load_config()
    skip = set(cfg.get("skip_files", []))
    video_exts = {".mp4", ".mov", ".mkv", ".avi", ".mxf", ".ts", ".webm", ".m4v"}
    if not RAW_DIR.exists():
        return {"total": 0, "consumed": 0, "remaining": 0, "files": []}
    raw_files = [f for f in RAW_DIR.iterdir() if f.suffix.lower() in video_exts and f.name not in skip]
    manifest = load_json(MANIFEST_PATH, [])
    consumed = {e.get("source") for e in manifest}
    remaining = [f for f in raw_files if f.name not in consumed]
    return {
        "total": len(raw_files),
        "consumed": len(raw_files) - len(remaining),
        "remaining": len(remaining),
        "files": [f.name for f in remaining],
    }


def git_push_changes() -> None:
    log("Pushing changes to GitHub...")
    run(["git", "add", "shorts/", "state/uploads.json"])
    result = subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=str(HERE))
    if result.returncode == 0:
        log("No changes to push.")
        return
    run(["git", "commit", "-m", f"Local pipeline: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"])
    run(["git", "push"])


def main() -> int:
    log("=== Local Pipeline Started ===")
    cfg = load_config()
    daily_shorts = int(cfg.get("daily_shorts", 5))

    # ── 1. CHECK RAW STATUS FIRST ──
    st = raw_status()
    log(f"Raw: {st['consumed']} consumed / {st['remaining']} remaining / {st['total']} total")

    # Alert if raw is critically low (< 2 remaining) or empty
    if st["remaining"] == 0:
        msg = (
            "⚠️ <b>RAW EMPTY</b>\n"
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M IST')}\n"
            f"📂 No raw clips left to process.\n"
            f"⬇️ Will download new videos now."
        )
        send_telegram(msg)
    elif st["remaining"] <= 2:
        msg = (
            "⚠️ <b>RAW LOW</b>\n"
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M IST')}\n"
            f"📥 Only {st['remaining']} raw clip(s) left.\n"
            f"⬇️ Downloading more now."
        )
        send_telegram(msg)

    # ── 2. DOWNLOAD ONLY IF NEEDED ──
    if st["remaining"] < 2:
        log("Raw is low/empty → downloading new videos...")
        rc = run([sys.executable, "download_channels.py"])
        if rc != 0:
            log("Download step failed.")
        else:
            log("Download complete.")
            # Re-check after download
            st = raw_status()
            log(f"After download: {st['remaining']} remaining")

    # ── 3. BUILD SHORTS (capped at daily_shorts = 5) ──
    rc = run([sys.executable, "build_shorts_omf.py"])
    if rc != 0:
        log("Build step failed.")
        return 1

    # ── 4. RAW DONE ALERT ──
    st = raw_status()
    if st["remaining"] == 0:
        shorts_count = len(list(SHORTS_DIR.glob("s*.mp4")))
        msg = (
            "✅ <b>Raw Fully Processed</b>\n"
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M IST')}\n"
            f"🎬 Shorts built today: <b>{shorts_count}</b> (limit {daily_shorts}/day)\n"
            f"📂 Raw clips: <b>All consumed</b>\n"
            f"🌙 Night upload will publish via GitHub Actions."
        )
        send_telegram(msg)
    else:
        log(f"Raw clips remaining: {st['remaining']} — will continue next run.")

    # ── 5. PUSH TO GITHUB ──
    git_push_changes()

    log("=== Local Pipeline Finished ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())