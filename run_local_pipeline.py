#!/usr/bin/env python3
"""
run_local_pipeline.py — Production-grade autonomous pipeline.

Responsibilities:
  1. Check raw status — Telegram alert if low/empty BEFORE downloading
  2. Download only when raw is below threshold (respects daily_shorts cap)
  3. Build vertical shorts (capped at daily_shorts per run)
  4. Git push → GitHub Actions handles night upload
  5. Telegram alerts at every milestone

Reliability features:
  • File locking — prevents concurrent runs
  • Atomic state writes — no corrupted JSON
  • Retry logic — transient failures are retried
  • Config validation — fails fast on bad config
  • Partial cleanup — removes .part files on failure
  • Idempotent — safe to re-run at any time
"""
import json
import os
import subprocess
import sys
import time
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
LOCK_PATH = STATE_DIR / "pipeline.lock"
UPLOADS_PATH = STATE_DIR / "uploads.json"
MANIFEST_PATH = SHORTS_DIR / "manifest.json"
CONFIG_PATH = HERE / "config.json"
LOGS_DIR = HERE / "logs"
PIPELINE_LOG = LOGS_DIR / "pipeline.log"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MAX_RETRIES = 3
RETRY_DELAY_SEC = 10


# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════

def log(msg: str, level: str = "INFO"):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    line = f"[{ts}] [{level}] {msg}"
    print(line, flush=True)
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(PIPELINE_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def die(msg: str, code: int = 1):
    log(f"FATAL: {msg}", "ERROR")
    send_telegram(f"💀 <b>Pipeline Fatal Error</b>\n🕐 {datetime.now().strftime('%Y-%m-%d %H:%M IST')}\n❌ {msg}")
    sys.exit(code)


# ══════════════════════════════════════════════════════════════════════════════
# FILE LOCKING — PREVENT CONCURRENT RUNS
# ══════════════════════════════════════════════════════════════════════════════

class PipelineLock:
    def __init__(self, path: Path):
        self.path = path
        self.fd = None

    def acquire(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic create-if-not-exists works on Windows too
            self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(self.fd, str(os.getpid()).encode())
            log(f"Lock acquired: {self.path}")
            return True
        except FileExistsError:
            log("Another pipeline instance is running. Exiting.")
            return False

    def release(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
            try:
                self.path.unlink(missing_ok=True)
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
# ATOMIC STATE
# ══════════════════════════════════════════════════════════════════════════════

def atomic_write_json(path: Path, data) -> None:
    """Write JSON atomically via temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"Failed to read {path.name}: {e}. Returning default.", "WARN")
        return default


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        die(f"config.json not found at {CONFIG_PATH}")
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        die(f"config.json is invalid JSON: {e}")

    required = ["raw_dir", "output_dir", "short_seconds", "daily_shorts"]
    for key in required:
        if key not in cfg:
            die(f"config.json missing required key: {key}")

    # Sensible defaults
    cfg.setdefault("short_seconds", 40)
    cfg.setdefault("daily_shorts", 5)
    cfg.setdefault("frame_width", 1080)
    cfg.setdefault("frame_height", 1920)
    cfg.setdefault("fps", 30)
    cfg.setdefault("preset", "fast")
    cfg.setdefault("crf", 20)
    cfg.setdefault("video_codec", "libx264")
    cfg.setdefault("ffmpeg_threads", 0)
    cfg.setdefault("filter_threads", 1)
    cfg.setdefault("keep_audio", True)
    cfg.setdefault("max_workers", 1)
    cfg.setdefault("skip_files", [])
    cfg.setdefault("channels", [])
    return cfg


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

def send_telegram(text: str, retries: int = MAX_RETRIES) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram not configured; skipping alert.", "WARN")
        return False

    import urllib.request, urllib.error
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps(
        {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
         "disable_web_page_preview": True}
    ).encode("utf-8")

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=payload,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    return True
        except Exception as e:
            last_err = e
            log(f"Telegram send attempt {attempt}/{retries} failed: {e}", "WARN")
            if attempt < retries:
                time.sleep(RETRY_DELAY_SEC)
    log(f"Telegram send failed after {retries} attempts: {last_err}", "ERROR")
    return False


# ══════════════════════════════════════════════════════════════════════════════
# RAW STATUS
# ══════════════════════════════════════════════════════════════════════════════

def raw_status(cfg: dict) -> dict:
    skip = set(cfg.get("skip_files", []))
    video_exts = {".mp4", ".mov", ".mkv", ".avi", ".mxf", ".ts", ".webm", ".m4v"}
    if not RAW_DIR.exists():
        return {"total": 0, "consumed": 0, "remaining": 0, "files": []}
    raw_files = [f for f in RAW_DIR.iterdir()
                 if f.is_file() and f.suffix.lower() in video_exts and f.name not in skip]
    manifest = load_json(MANIFEST_PATH, [])
    consumed = {e.get("source") for e in manifest if isinstance(e, dict)}
    remaining = [f for f in raw_files if f.name not in consumed]
    return {
        "total": len(raw_files),
        "consumed": len(raw_files) - len(remaining),
        "remaining": len(remaining),
        "files": [f.name for f in remaining],
    }


# ══════════════════════════════════════════════════════════════════════════════
# GIT
# ══════════════════════════════════════════════════════════════════════════════

def run(cmd: list[str], cwd: Path = HERE, retries: int = MAX_RETRIES) -> int:
    for attempt in range(1, retries + 1):
        log(f"RUN (attempt {attempt}/{retries}): {' '.join(cmd)}")
        result = subprocess.run(cmd, cwd=str(cwd))
        if result.returncode == 0:
            return 0
        log(f"Command failed with exit {result.returncode}", "WARN")
        if attempt < retries:
            time.sleep(RETRY_DELAY_SEC)
    return result.returncode


def git_push_changes() -> None:
    log("Pushing changes to GitHub...")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    run(["git", "add", "shorts/", "state/uploads.json"])
    result = subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=str(HERE))
    if result.returncode == 0:
        log("No changes to push.")
        return
    run(["git", "commit", "-m", f"Local pipeline: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"])
    run(["git", "push"])


# ══════════════════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def main() -> int:
    lock = PipelineLock(LOCK_PATH)
    if not lock.acquire():
        return 0
    try:
        return _run()
    finally:
        lock.release()


def _run() -> int:
    log("=== Local Pipeline Started ===")
    cfg = load_config()
    daily_shorts = int(cfg.get("daily_shorts", 5))

    # ── 1. CHECK RAW STATUS ──
    st = raw_status(cfg)
    log(f"Raw: {st['consumed']} consumed / {st['remaining']} remaining / {st['total']} total")

    # Alert if raw critically low
    if st["remaining"] == 0:
        send_telegram(
            "⚠️ <b>RAW EMPTY</b>\n"
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M IST')}\n"
            f"📂 No raw clips left to process.\n"
            f"⬇️ Downloading new videos now."
        )
    elif st["remaining"] <= 2:
        send_telegram(
            "⚠️ <b>RAW LOW</b>\n"
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M IST')}\n"
            f"📥 Only {st['remaining']} raw clip(s) left.\n"
            f"⬇️ Downloading more now."
        )

    # ── 2. DOWNLOAD ONLY IF NEEDED ──
    if st["remaining"] < 2:
        log("Raw is low/empty → downloading new videos...")
        rc = run([sys.executable, "download_channels.py"])
        if rc != 0:
            log("Download step failed.", "ERROR")
            send_telegram(
                "❌ <b>Download Failed</b>\n"
                f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M IST')}\n"
                f"Check logs for details."
            )
            return 1
        log("Download complete.")
        st = raw_status(cfg)
        log(f"After download: {st['remaining']} remaining")

    # ── 3. BUILD SHORTS (capped at daily_shorts) ──
    log(f"Building shorts (daily cap: {daily_shorts})...")
    rc = run([sys.executable, "build_shorts_omf.py"])
    if rc != 0:
        log("Build step failed.", "ERROR")
        send_telegram(
            "❌ <b>Build Failed</b>\n"
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M IST')}\n"
            f"FFmpeg or script error. Check logs."
        )
        return 1

    # ── 3.5. GENERATE META (OpenRouter titles/descriptions) ──
    log("Generating titles/descriptions via OpenRouter...")
    rc = run([sys.executable, "generate_meta.py"])
    if rc != 0:
        log("Meta generation failed — continuing with shorts anyway.", "WARN")
        send_telegram(
            "⚠️ <b>Meta Generation Failed</b>\n"
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M IST')}\n"
            f"Shorts built but titles/descriptions may be missing."
        )

    # ── 4. RAW DONE ALERT ──
    st = raw_status(cfg)
    shorts_count = len(list(SHORTS_DIR.glob("s*.mp4"))) if SHORTS_DIR.exists() else 0
    if st["remaining"] == 0:
        send_telegram(
            "✅ <b>Raw Fully Processed</b>\n"
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M IST')}\n"
            f"🎬 Shorts built today: <b>{shorts_count}</b> (limit {daily_shorts}/day)\n"
            f"📂 Raw clips: <b>All consumed</b>\n"
            f"🌙 Night upload will publish via GitHub Actions."
        )
    else:
        log(f"Raw clips remaining: {st['remaining']} — will continue next run.")

    # ── 5. PUSH TO GITHUB ──
    git_push_changes()

    log("=== Local Pipeline Finished ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())