#!/usr/bin/env python3
"""
run_local_pipeline.py — omfplays startup processor.

Runs at Windows login. Shows a startup summary, then auto-completes today's
shorts processing if it isn't done yet:

  1. render startup summary (raw/library/slots state)
  2. build missing vertical shorts (build_shorts_omf.py, idempotent/resumable)
  3. generate OpenRouter captions for shorts missing .txt sidecars
  4. generate HD thumbnails for shorts missing .jpg
  5. push logs + state + shorts to GitHub
  6. write webapp/dashboard.html (for Cloudflare Pages)

Never auto-uploads to Instagram — that stays a separate manual step.
Safe to run twice; never crashes on a single step failure.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

NODE = os.getenv("PIPELINE_NODE", "local")

HERE = Path(__file__).resolve().parent
RAW_DIR = HERE / "raw"
SHORTS_DIR = HERE / "shorts"
STATE_DIR = HERE / "state"
LOGS_DIR = HERE / "logs"
LOCK_PATH = STATE_DIR / "pipeline.lock"
UPLOADS_PATH = STATE_DIR / "uploads.json"
CONFIG_PATH = HERE / "config.json"
PIPELINE_LOG = LOGS_DIR / "pipeline.log"
DASHBOARD_PATH = HERE / "webapp" / "dashboard.html"

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════════════════
# LOGGING
# ════════════════════════════════════════════════════════════════════════════════

def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    line = f"[{ts}] [{level}] {msg}"
    print(line, flush=True)
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(PIPELINE_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════════════════
# LOCK (with stale-lock recovery)
# ════════════════════════════════════════════════════════════════════════════════

def _pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class PipelineLock:
    """Exclusive lock that survives crashes and never needs manual deletion."""

    def __init__(self, path: Path, max_age_hours: float = 6.0):
        self.path = path
        self.max_age_hours = float(max_age_hours)
        self.fd = None

    def _holder(self) -> dict:
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
        except Exception:
            return {}
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
            return {"pid": int(data)}
        except Exception:
            try:
                return {"pid": int(raw.split()[0])}
            except Exception:
                return {}

    def _is_stale(self, holder: dict) -> str | None:
        pid = int(holder.get("pid") or 0)
        if pid <= 0:
            return "lock file has no usable pid"
        if not _pid_alive(pid):
            return f"pid {pid} is not running"
        try:
            age_h = (datetime.now().timestamp() - self.path.stat().st_mtime) / 3600
            if age_h > self.max_age_hours:
                return f"lock is {age_h:.1f}h old (limit {self.max_age_hours}h)"
        except Exception:
            pass
        return None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                payload = json.dumps(
                    {
                        "pid": os.getpid(),
                        "node": NODE,
                        "started_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    }
                )
                os.write(self.fd, payload.encode("utf-8"))
                log(f"Lock acquired: {self.path} (pid {os.getpid()})")
                return True
            except FileExistsError:
                holder = self._holder()
                reason = self._is_stale(holder)
                if reason is None:
                    log(
                        f"Another run holds the lock (pid {holder.get('pid')}, "
                        f"node {holder.get('node')}) — exiting cleanly.",
                        "WARN",
                    )
                    return False
                log(f"Removing stale lock: {reason}", "WARN")
                try:
                    self.path.unlink(missing_ok=True)
                except Exception as e:
                    log(f"Could not remove stale lock: {e}", "ERROR")
                    return False
        log("Could not acquire the pipeline lock.", "ERROR")
        return False

    def release(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = None
        try:
            if self.path.exists():
                holder = self._holder()
                if int(holder.get("pid") or 0) in (0, os.getpid()):
                    self.path.unlink(missing_ok=True)
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════════════════
# CONFIG + SUBPROCESS
# ════════════════════════════════════════════════════════════════════════════════

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"config.json not found at {CONFIG_PATH}")
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"config.json is invalid JSON: {e}")

    cfg.setdefault("daily_shorts", 5)
    cfg.setdefault("short_seconds", 40)
    cfg.setdefault("frame_width", 1080)
    cfg.setdefault("frame_height", 1920)
    cfg.setdefault("max_lock_age_hours", 6)
    cfg.setdefault("max_push_mb", 90)
    cfg.setdefault("step_retries", 2)
    cfg.setdefault("step_retry_delay_sec", 15)
    return cfg


def run_step(name: str, script: str, args: list[str] | None = None, retries: int = 2, delay: int = 15) -> int:
    """Run a sibling script with retries. Never raises."""
    env = os.environ.copy()
    env["PIPELINE_NODE"] = NODE
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [sys.executable, "-u", script] + (args or [])
    last = 1
    for attempt in range(1, retries + 1):
        log(f"RUN [{name}] attempt {attempt}/{retries}: {script} {' '.join(args or [])}")
        try:
            last = subprocess.run(cmd, cwd=str(HERE), env=env).returncode
        except Exception as e:
            log(f"[{name}] could not start: {e}", "ERROR")
            last = 1
        if last == 0:
            log(f"[{name}] ok")
            return 0
        log(f"[{name}] failed with exit {last}", "WARN")
        if attempt < retries:
            time.sleep(delay)
    return last


# ════════════════════════════════════════════════════════════════════════════════
# THUMBNAILS (HD quality, ffmpeg)
# ════════════════════════════════════════════════════════════════════════════════

def generate_thumbnails(limit: int | None = None) -> int:
    """Generate HD 1080x1920 thumbnails for shorts missing a .jpg sidecar."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        log("ffmpeg not found — skipping thumbnail generation", "WARN")
        return 0

    SHORTS_DIR.mkdir(parents=True, exist_ok=True)
    shorts = sorted(SHORTS_DIR.glob("s*.mp4"))
    if limit is not None:
        shorts = shorts[:limit]

    made = 0
    skipped = 0
    for v in shorts:
        thumb = v.with_suffix(".jpg")
        if thumb.exists() and thumb.stat().st_size > 0:
            skipped += 1
            continue
        cmd = [
            ffmpeg, "-y",
            "-i", str(v),
            "-ss", "00:00:02",
            "-vframes", "1",
            "-q:v", "2",
            "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
            str(thumb),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
            if r.returncode == 0 and thumb.exists() and thumb.stat().st_size > 0:
                made += 1
                log(f"thumbnail: {thumb.name}")
            else:
                log(f"thumbnail failed: {v.name}", "WARN")
        except Exception as e:
            log(f"thumbnail error {v.name}: {e}", "WARN")

    log(f"thumbnails: {made} new, {skipped} already present")
    return made


# ════════════════════════════════════════════════════════════════════════════════
# GIT (never blocks the pipeline)
# ════════════════════════════════════════════════════════════════════════════════

def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(HERE), capture_output=True, text=True, timeout=120
    )


def git_push_changes(cfg: dict) -> tuple[bool, list[str]]:
    """Push to GitHub. Returns (success, list_of_pushed_short_stems)."""
    if not (HERE / ".git").exists():
        log("Not a git repository — skipping push.", "WARN")
        return False, []

    # Snapshot shorts before add so we know exactly what was pushed.
    shorts_before_push = sorted({p.stem for p in SHORTS_DIR.glob("s*.mp4")})
    log(f"Snapshot: {len(shorts_before_push)} short(s) on disk before push.")

    log("Pushing shorts + captions + thumbnails + logs + state to GitHub...")
    r = _git("pull", "--rebase", "--autostash")
    if r.returncode != 0:
        _git("rebase", "--abort")
        log(f"git pull --rebase failed: {(r.stderr or r.stdout).strip()[:300]}", "WARN")

    add = _git("add", "-A", "--", "shorts", "state", "webapp")
    if add.returncode != 0:
        log(f"git add failed (shorts/state/webapp): {(add.stderr or add.stdout).strip()[:300]}", "WARN")
        return False, []

    # Force-add the pipeline log (logs/ is gitignored but we want it on GitHub).
    log_add = _git("add", "-f", "logs/pipeline.log")
    if log_add.returncode != 0:
        log(f"git add -f logs/pipeline.log failed: {(log_add.stderr or log_add.stdout).strip()[:300]}", "WARN")

    max_mb = float(cfg.get("max_push_mb", 90))
    staged = [p for p in _git("diff", "--cached", "--name-only").stdout.splitlines() if p.strip()]
    oversized = []
    for name in staged:
        path = HERE / name
        try:
            if path.is_file() and path.stat().st_size > max_mb * 1024 * 1024:
                oversized.append(name)
        except OSError:
            pass
    if oversized:
        _git("restore", "--staged", "--", *oversized)
        log(
            f"Left {len(oversized)} file(s) over {max_mb:.0f} MB out of the commit: "
            f"{', '.join(oversized[:3])}",
            "WARN",
        )

    if _git("diff", "--staged", "--quiet").returncode == 0:
        log("No changes to push.")
        return True, []

    msg = f"chore({NODE}): pipeline {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    commit = _git("commit", "-m", msg)
    if commit.returncode != 0:
        log(f"git commit failed: {(commit.stderr or commit.stdout).strip()[:300]}", "WARN")
        return False, []

    for attempt in (1, 2):
        push = _git("push")
        if push.returncode == 0:
            log(f"Pushed {len(shorts_before_push)} short(s) + sidecars to GitHub.")
            for stem in shorts_before_push:
                log(f"  pushed: {stem}.mp4 + .txt + .jpg")
            return True, shorts_before_push
        log(f"git push failed (attempt {attempt}): {(push.stderr or push.stdout).strip()[:200]}", "WARN")
        _git("pull", "--rebase", "--autostash")

    log("git push still failing — changes stay local, next run retries.", "WARN")
    return False, []


def cleanup_pushed_short(stems: list[str]) -> None:
    """Delete local .mp4/.txt/.jpg for shorts that were pushed to GitHub.
    Records each cleaned file in state/pushed.json so we always know what is
    on GitHub vs what is still on disk."""
    pushed_log_path = STATE_DIR / "pushed.json"
    try:
        pushed_log = json.loads(pushed_log_path.read_text(encoding="utf-8"))
    except Exception:
        pushed_log = {}

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cleaned = []
    already_gone = []

    for stem in stems:
        pushed_log[stem] = {
            "pushed_at": now_iso,
            "cleaned_from_disk": True,
        }
        for ext in (".mp4", ".txt", ".jpg"):
            f = SHORTS_DIR / f"{stem}{ext}"
            if not f.exists():
                already_gone.append(f"{stem}{ext}")
                continue
            try:
                size = f.stat().st_size
                f.unlink()
                cleaned.append(f"{stem}{ext}")
                log(f"cleaned: {f.name} ({size / 1024 / 1024:.1f} MB)")
            except Exception as e:
                log(f"could not clean {f.name}: {e}", "WARN")

    pushed_log_path.parent.mkdir(parents=True, exist_ok=True)
    pushed_log_path.write_text(
        json.dumps(pushed_log, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    log(f"cleanup: {len(cleaned)} file(s) removed from disk after GitHub push.")
    if already_gone:
        log(f"cleanup: {len(already_gone)} file(s) already absent (skipped).")


# ═════════════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ════════════════════════════════════════════════════════════════════════════════

def write_dashboard() -> None:
    """Write a self-contained HTML dashboard to webapp/dashboard.html."""
    try:
        import html as _html
        import pipeline_status as ps

        snap = ps.snapshot()
        now = snap["now_ist"]
        rows = snap["slots_today"]

        lines = [
            "<!DOCTYPE html>",
            "<html lang='en'>",
            "<head>",
            "<meta charset='UTF-8'>",
            "<meta name='viewport' content='width=device-width, initial-scale=1.0'>",
            "<title>omfplays Dashboard</title>",
            "<style>",
            "body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0b0c10; color: #c5c6c7; padding: 20px; }",
            "h1 { color: #66fcf1; margin-bottom: 16px; }",
            ".card { background: #1f2833; border: 1px solid #45a29e; border-radius: 10px; padding: 16px; margin-bottom: 16px; }",
            ".card h2 { color: #66fcf1; font-size: 16px; margin-bottom: 8px; }",
            ".row { display: flex; gap: 16px; flex-wrap: wrap; }",
            ".stat { flex: 1; min-width: 120px; background: #0e1521; padding: 10px; border-radius: 6px; }",
            ".stat .val { font-size: 20px; font-weight: bold; color: #45a29e; }",
            ".stat .lbl { font-size: 12px; color: #7d8a99; }",
            "pre { background: #0e1521; padding: 12px; border-radius: 6px; overflow-x: auto; font-size: 13px; }",
            ".slot { margin: 6px 0; padding: 6px 10px; background: #0e1521; border-radius: 4px; }",
            ".slot.posted { border-left: 4px solid #45a29e; }",
            ".slot.due { border-left: 4px solid #f9c74f; }",
            ".slot.missed { border-left: 4px solid #ff6b6b; }",
            ".slot.scheduled { border-left: 4px solid #6c757d; }",
            ".updated { color: #7d8a99; font-size: 12px; margin-top: 16px; }",
            "</style>",
            "</head>",
            "<body>",
            "<h1>⚡ omfplays Dashboard</h1>",
            f"<p>Last updated: {now}</p>",
            "<div class='row'>",
            f"<div class='stat'><div class='val'>{snap['shorts']['posted_today']}</div><div class='lbl'>slots posted today</div></div>",
            f"<div class='stat'><div class='val'>{snap['shorts']['pending']}</div><div class='lbl'>shorts waiting</div></div>",
            f"<div class='stat'><div class='val'>{snap['manifest']['total']}</div><div class='lbl'>manifest total</div></div>",
            f"<div class='stat'><div class='val'>{snap['disk_free_gb']}</div><div class='lbl'>GB disk free</div></div>",
            "</div>",
            "<div class='card'>",
            "<h2>📅 Today's Slots</h2>",
        ]
        for row in rows:
            glyph = {"posted": "✅", "due": "⏳", "missed": "✖", "scheduled": "·"}.get(row["state"], "·")
            css = row["state"]
            detail = ""
            if row["state"] == "posted":
                detail = f"by {row.get('by', '?').upper()} · {row.get('short', '?')}"
            elif row["state"] == "due":
                detail = f"due now · owner {row['owner'].upper()}"
            elif row["state"] == "missed":
                detail = f"{row.get('overdue_min', 0)}m overdue · owner {row['owner'].upper()}"
            else:
                detail = f"owner {row['owner'].upper()}"
            lines.append(f"<div class='slot {css}'>{glyph} {row['time']} IST — {detail}</div>")

        lines += [
            "</div>",
            "<div class='card'>",
            "<h2>📜 Recent Logs</h2>",
            "<pre>",
        ]
        log_path = LOGS_DIR / "pipeline.log"
        if log_path.exists():
            log_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in log_lines[-50:]:
                lines.append(_html.escape(line))
        else:
            lines.append("No logs yet.")
        lines += [
            "</pre>",
            "</div>",
            "<div class='card'>",
            "<h2>📂 Raw Library</h2>",
            f"<p>Raw videos: {snap['raw']['total']} | Reels left: {snap['raw'].get('slices_left', 0)}</p>",
            "</div>",
            "<p class='updated'>Generated by omfplays local pipeline. Push this file to GitHub and Cloudflare Pages will redeploy.</p>",
            "</body>",
            "</html>",
        ]

        DASHBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
        DASHBOARD_PATH.write_text("\n".join(lines), encoding="utf-8")
        log(f"Dashboard written to {DASHBOARD_PATH}", "INFO")
    except Exception as e:
        log(f"Could not write dashboard: {e}", "WARN")


# ════════════════════════════════════════════════════════════════════════════════
# STARTUP SUMMARY
# ════════════════════════════════════════════════════════════════════════════════

def print_startup_summary(cfg: dict) -> None:
    """Print the startup summary block (no Telegram spam)."""
    import pipeline_status as ps

    snap = ps.snapshot()
    print("\n" + "═" * 58)
    print("  🟢 OMPLAYS STARTUP SUMMARY")
    print("═" * 58)
    print(f"  🕐  {snap['now_ist']}")
    print(f"  🖥  node : {snap['node'].upper()}")
    print(f"  📅  date : {snap['date']}")
    print()
    print("  📦 LIBRARY")
    print(f"     raw videos   : {snap['raw']['total']}")
    print(f"     reels left   : {snap['raw'].get('slices_left', 0)} × {snap['raw'].get('short_secs', 40)}s")
    print(f"     shorts waiting: {snap['shorts']['pending']}")
    print(f"     manifest total: {snap['manifest']['total']}")
    print()
    print("  📅 TODAY'S SLOTS")
    for row in snap["slots_today"]:
        glyph = {"posted": "✅", "due": "⏳", "missed": "✖", "scheduled": "·"}.get(row["state"], "·")
        print(f"     {glyph} {row['time']} IST  {row['state']}")
    print()
    print("  💾 SYSTEM")
    print(f"     disk free : {snap['disk_free_gb']} GB")
    if snap["attention"]:
        print("     ⚠ attention:")
        for item in snap["attention"][:5]:
            print(f"        - {item}")
    print("═" * 58 + "\n")


# ════════════════════════════════════════════════════════════════════════════════
# PIPELINE
# ════════════════════════════════════════════════════════════════════════════════

def run_pipeline(cfg: dict, dry_run: bool, no_push: bool) -> dict:
    import pipeline_status as ps

    steps: dict[str, str] = {}
    daily_shorts = int(cfg.get("daily_shorts", 5))
    started = datetime.now(ps.IST)
    any_work_done = False

    raw_before = ps.raw_counts()
    shorts_before = len(ps.pending_shorts())
    posted_before = len(ps.posted_on(ps.ist_now().strftime("%Y-%m-%d")))

    log("=== Startup Pipeline Started ===")
    log(
        f"Raw: {raw_before['total']} video(s) · {raw_before['slices_left']} reels left · "
        f"shorts waiting: {shorts_before}"
    )

    # Print startup summary to console (no Telegram spam) — ALWAYS shown on login
    print_startup_summary(cfg)

    # ── Step 1: build missing shorts ──
    if dry_run:
        steps["build"] = "skipped (dry run)"
    else:
        existing = len(list(SHORTS_DIR.glob("s*.mp4")))
        need = max(0, daily_shorts - existing)
        if need > 0:
            log(f"Building {need} missing short(s) for today...")
            before_build = len(list(SHORTS_DIR.glob("s*.mp4")))
            rc = run_step(
                "build",
                "build_shorts_omf.py",
                args=["--max-shorts", str(need)],
                retries=int(cfg.get("step_retries", 2)),
                delay=int(cfg.get("step_retry_delay_sec", 15)),
            )
            after_build = len(list(SHORTS_DIR.glob("s*.mp4")))
            steps["build"] = "ok" if rc == 0 else "failed"
            if rc == 0 and after_build > before_build:
                any_work_done = True
        else:
            log(f"Already {existing} shorts waiting (daily cap {daily_shorts}). Skipping build.")
            steps["build"] = "skipped (enough shorts)"

    # ── Step 2: generate OpenRouter captions ──
    if dry_run:
        steps["meta"] = "skipped (dry run)"
    else:
        shorts_on_disk = len(list(SHORTS_DIR.glob("s*.mp4")))
        if shorts_on_disk == 0:
            log("No shorts on disk — skipping OpenRouter captions.")
            steps["meta"] = "skipped (no shorts on disk)"
        else:
            log("Generating OpenRouter captions for shorts missing .txt...")
            rc = run_step(
                "meta",
                "generate_meta.py",
                retries=int(cfg.get("step_retries", 2)),
                delay=int(cfg.get("step_retry_delay_sec", 10)),
            )
            steps["meta"] = "ok" if rc == 0 else "failed"
            if rc == 0:
                any_work_done = True
            if rc != 0:
                log("Meta step failed — shorts will have fallback captions on next run.", "WARN")

    # ── Step 3: generate HD thumbnails ──
    if dry_run:
        steps["thumbnails"] = "skipped (dry run)"
    else:
        log("Generating HD thumbnails for shorts missing .jpg...")
        try:
            made = generate_thumbnails()
            steps["thumbnails"] = "ok"
            if made > 0:
                any_work_done = True
        except Exception as e:
            log(f"Thumbnail step crashed: {e}", "ERROR")
            steps["thumbnails"] = "failed"

    # ── Step 4: push to GitHub (logs + state + shorts + dashboard) ──
    pushed_stems: list[str] = []
    if dry_run or no_push:
        steps["push"] = "skipped"
    else:
        push_ok, pushed_stems = git_push_changes(cfg)
        steps["push"] = "ok" if push_ok else "failed"
        if push_ok and pushed_stems:
            any_work_done = True
            log("Step 4b: cleaning local copies after successful GitHub push...")
            cleanup_pushed_short(pushed_stems)
            steps["cleanup"] = "ok"
        elif push_ok and not pushed_stems:
            steps["cleanup"] = "skipped (nothing to clean)"
        else:
            steps["cleanup"] = "skipped (push failed)"

    # ── Step 5: write dashboard ──
    if not dry_run:
        write_dashboard()

    after = ps.raw_counts()
    waiting = len(ps.pending_shorts())
    stats = {
        "node": NODE.upper(),
        "started": started.strftime("%Y-%m-%d %H:%M IST"),
        "finished": datetime.now(ps.IST).strftime("%Y-%m-%d %H:%M IST"),
        "raw_before": raw_before["slices_left"],
        "raw_after": after["slices_left"],
        "waiting_shorts": waiting,
        "shorts_before": shorts_before,
        "steps": steps,
    }
    log("=== Startup Pipeline Finished ===")
    if not any_work_done:
        print("✅ Today's processing is complete. Nothing new to do.", flush=True)
    return stats


def render_summary(stats: dict) -> str:
    steps = stats["steps"]
    step_line = " · ".join(f"{k} {v}" for k, v in steps.items())
    return "\n".join(
        [
            "🏁 STARTUP RUN SUMMARY",
            "─" * 34,
            f"node     : {stats['node']}",
            f"started  : {stats['started']}",
            f"finished : {stats['finished']}",
            f"raw reels: {stats['raw_before']} → {stats['raw_after']}",
            f"shorts   : {stats['shorts_before']} → {stats['waiting_shorts']} waiting",
            f"steps    : {step_line}",
        ]
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="omfplays startup processor")
    ap.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    ap.add_argument("--no-push", action="store_true", help="skip git push")
    ap.add_argument("--ignore-lock", action="store_true", help="run despite an existing lock")
    args = ap.parse_args()

    try:
        cfg = load_config()
    except RuntimeError as e:
        log(f"FATAL: {e}", "ERROR")
        return 2

    if args.dry_run:
        import pipeline_status as ps
        print_startup_summary(cfg)
        return 0

    lock = PipelineLock(LOCK_PATH, max_age_hours=cfg.get("max_lock_age_hours", 6))
    if not args.ignore_lock:
        if not lock.acquire():
            return 0
    else:
        log("--ignore-lock: running without the lock", "WARN")

    result = "ok"
    rc = 0
    stats: dict = {}
    try:
        stats = run_pipeline(cfg, args.dry_run, args.no_push)
    except Exception as e:
        result = "failed"
        rc = 1
        log(f"FATAL: {e}\n{traceback.format_exc()}", "ERROR")
    finally:
        lock.release()
        if stats:
            print(render_summary(stats), flush=True)
            try:
                ps = __import__("pipeline_status")
                ps.record_node_run(
                    result,
                    note=f"shorts waiting: {stats['waiting_shorts']}",
                    built=max(0, stats["waiting_shorts"] - stats["shorts_before"]),
                )
            except Exception as e:
                log(f"Could not record node status: {e}", "WARN")

    return rc


if __name__ == "__main__":
    sys.exit(main())
