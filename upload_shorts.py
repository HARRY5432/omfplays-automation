#!/usr/bin/env python3
"""
upload_shorts.py — Robust Instagram uploader for GitHub Actions night run.

Features:
  • Cookie + session + password login fallbacks
  • Atomic uploads.json writes
  • Idempotent — never double-posts
  • Retry on transient upload failure
  • Telegram alerts on success/failure
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from instagrapi import Client
    from instagrapi.exceptions import ChallengeRequired, BadPassword, ClientThrottledError
except ImportError:
    print("Installing instagrapi...")
    subprocess = __import__("subprocess")
    subprocess.run([sys.executable, "-m", "pip", "install", "instagrapi", "--break-system-packages"], check=True)
    from instagrapi import Client
    from instagrapi.exceptions import ChallengeRequired, BadPassword, ClientThrottledError

IST = ZoneInfo("Asia/Kolkata")
HERE = Path(__file__).resolve().parent
STATE_DIR = HERE / "state"
UPLOADS_PATH = STATE_DIR / "uploads.json"
LOGS_DIR = HERE / "logs"
PUBLISHER_AUTH = HERE / "publisher_auth.json"
PUBLISHER_SESSION = HERE / "publisher_session.json"
PUBLISHER_COOKIES = HERE / "publisher_cookies.json"
SLOTS_PATH = HERE / "slots.json"
SHORTS_DIR = HERE / "shorts"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

DEFAULT_SLOTS = [
    ("19:30", "US East Lunch + EU Afternoon"),
    ("22:30", "US East Prime Evening + EU Night"),
    ("01:00", "US East Late Night + AU Morning"),
    ("05:00", "US East Early Morning + Asia Start"),
    ("08:30", "US East Morning + India Night"),
]
BETWEEN_POSTS_SECS = 600
MAX_UPLOAD_RETRIES = 2


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


def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    import urllib.request
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps(
        {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
         "disable_web_page_preview": True}
    ).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"Telegram send failed: {e}", flush=True)
        return False


def load_slots() -> list[tuple[str, str]]:
    if SLOTS_PATH.exists():
        try:
            data = json.loads(SLOTS_PATH.read_text(encoding="utf-8"))
            rows = [(s["time"], s.get("label", "")) for s in data.get("slots", [])]
            if rows:
                return rows
        except Exception:
            pass
    return DEFAULT_SLOTS


SLOTS = load_slots()


def load_auth() -> tuple[str | None, str | None]:
    user = os.environ.get("IG_PUBLISHER_USER")
    pw = os.environ.get("IG_PUBLISHER_PASSWORD")
    if user and pw:
        return user, pw
    if PUBLISHER_AUTH.exists():
        try:
            d = json.loads(PUBLISHER_AUTH.read_text(encoding="utf-8"))
            if d.get("username") and d.get("password"):
                return d["username"], d["password"]
        except Exception:
            pass
    return None, None


def login(challenge_code: str | None = None, expected_user: str | None = None):
    user, pw = load_auth()
    if not user or not pw:
        print("ERROR: no publisher credentials.", flush=True)
        sys.exit(1)

    cl = Client()

    def verified(client) -> bool:
        expected = (expected_user or user).strip().lower()
        if expected and client.username and client.username.lower() != expected:
            print(f"ERROR: logged in as @{client.username}, expected @{user}.", flush=True)
            return False
        return True

    # 1. Cookie login
    if PUBLISHER_COOKIES.exists():
        try:
            raw = json.loads(PUBLISHER_COOKIES.read_text(encoding="utf-8"))
            items = raw if isinstance(raw, list) else raw.get("cookies", [])
            sid = next((c.get("value") for c in items
                        if isinstance(c, dict) and c.get("name") == "sessionid"), None)
            if sid:
                cl.login_by_sessionid(sid)
                if verified(cl):
                    cl.dump_settings(PUBLISHER_SESSION)
                    return cl
        except ClientThrottledError:
            print("ERROR: Instagram rate-limiting logins (429).", flush=True)
            sys.exit(3)
        except Exception:
            pass

    # 2. Saved session
    if PUBLISHER_SESSION.exists():
        try:
            cl.load_settings(PUBLISHER_SESSION)
            cl.login(user, pw)
            if verified(cl):
                cl.dump_settings(PUBLISHER_SESSION)
                return cl
        except ClientThrottledError:
            print("ERROR: Instagram rate-limiting logins (429).", flush=True)
            sys.exit(3)
        except Exception:
            pass

    # 3. Password login
    try:
        cl.login(user, pw)
    except BadPassword:
        print("ERROR: Instagram rejected the password.", flush=True)
        sys.exit(1)
    except ChallengeRequired:
        print("Instagram wants a verification code (new device).", flush=True)
        print("Re-run with: --code 123456", flush=True)
        PUBLISHER_SESSION.unlink(missing_ok=True)
        if not challenge_code:
            sys.exit(2)
        try:
            cl.challenge_resolve(challenge_code)
        except Exception as e:
            print(f"ERROR: challenge failed ({e})", flush=True)
            sys.exit(1)
    except ClientThrottledError:
        print("ERROR: Instagram rate-limiting logins (429).", flush=True)
        sys.exit(3)
    except Exception as e:
        print(f"ERROR: login failed ({e})", flush=True)
        sys.exit(1)

    if not verified(cl):
        sys.exit(4)

    cl.dump_settings(PUBLISHER_SESSION)
    return cl


def collect_shorts() -> list[dict]:
    items = []
    if not SHORTS_DIR.exists():
        return items
    for v in sorted(SHORTS_DIR.glob("s*.mp4")):
        stem = v.stem
        cover = v.with_suffix(".jpg")
        caption = v.with_suffix(".txt")
        items.append({
            "short": stem,
            "video": str(v),
            "cover": str(cover) if cover.exists() else None,
            "caption_file": str(caption) if caption.exists() else None,
        })
    return items


def slot_due(slot_hm: str, now: datetime) -> bool:
    hh, mm = map(int, slot_hm.split(":"))
    slot = datetime(now.year, now.month, now.day, hh, mm, tzinfo=IST)
    return now >= slot


def main() -> int:
    ap = argparse.ArgumentParser(description="Post omfplays shorts to Instagram.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--trial", action="store_true")
    ap.add_argument("--now", action="store_true")
    ap.add_argument("--slot", type=int, default=None)
    ap.add_argument("--code", default=None)
    ap.add_argument("--publisher-user", default=None)
    args = ap.parse_args()

    now = datetime.now(IST)
    date_str = now.strftime("%Y-%m-%d")
    items = collect_shorts()
    if not items:
        print(f"No shorts found in {SHORTS_DIR}.", flush=True)
        return 0

    uploads = load_json(UPLOADS_PATH, {})
    plan = []
    for i, (hm, label) in enumerate(SLOTS):
        if args.slot is not None and i != args.slot:
            continue
        if i >= len(items):
            plan.append((i, hm, label, None, "no-item"))
            continue
        key = f"{date_str}#{i}"
        rec = uploads.get(key)
        if rec and rec.get("status") == "posted":
            plan.append((i, hm, label, items[i], "already-posted"))
        elif not args.now and not slot_due(hm, now):
            plan.append((i, hm, label, items[i], "waiting"))
        else:
            plan.append((i, hm, label, items[i], "DUE"))

    print(f"Date: {date_str} | Shorts: {len(items)} | Now: {now.strftime('%H:%M IST')}", flush=True)
    for i, hm, label, it, st in plan:
        what = it["short"] if it else "-"
        print(f"  slot {i} [{hm} IST] {label:20} {what:10} {st}", flush=True)

    if args.dry_run:
        print("(dry run — nothing posted)", flush=True)
        return 0

    due = [(i, it) for i, _, _, it, st in plan if st == "DUE"]
    if not due:
        print("Nothing due right now.", flush=True)
        return 0
    if args.trial and len(due) > 1:
        due = due[:1]
        print("(trial mode: posting 1 reel only)", flush=True)

    cl = login(args.code, args.publisher_user)
    me = cl.username if hasattr(cl, "username") else "?"
    print(f"Logged in as @{me}", flush=True)

    posted_count = 0
    for n, (i, it) in enumerate(due):
        if n > 0:
            print(f"Waiting {BETWEEN_POSTS_SECS // 60} min before next post...", flush=True)
            time.sleep(BETWEEN_POSTS_SECS)

        caption = ""
        if it["caption_file"]:
            try:
                caption = Path(it["caption_file"]).read_text(encoding="utf-8", errors="replace").strip()[:2200]
            except Exception:
                pass

        print(f"Posting slot {i}: {it['short']} (trial={args.trial})...", flush=True)
        success = False
        for attempt in range(1, MAX_UPLOAD_RETRIES + 1):
            try:
                media = cl.clip_upload(
                    it["video"], caption,
                    thumbnail=it["cover"] if it["cover"] else None,
                    trial=args.trial,
                )
                success = True
                break
            except Exception as e:
                print(f"  Upload attempt {attempt} failed: {type(e).__name__}: {e}", flush=True)
                if attempt < MAX_UPLOAD_RETRIES:
                    time.sleep(RETRY_DELAY_SEC if 'RETRY_DELAY_SEC' in dir() else 10)

        if not success:
            send_telegram(
                f"🚨 <b>Upload Failed</b>\n"
                f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M IST')}\n"
                f"Slot {i}: {it['short']}\n"
                f"Check logs."
            )
            print("Stopping. Progress kept in state/uploads.json.", flush=True)
            return 1

        key = f"{date_str}#{i}"
        uploads[key] = {
            "status": "posted", "slot": SLOTS[i][0],
            "short": it["short"],
            "media_pk": str(getattr(media, "pk", "?")),
            "media_code": getattr(media, "code", None),
            "trial": args.trial,
            "posted_at": datetime.now(timezone.utc).isoformat(),
        }
        atomic_write_json(UPLOADS_PATH, uploads)
        posted_count += 1
        print(f"  POSTED media_pk={uploads[key]['media_pk']}", flush=True)

    send_telegram(
        f"✅ <b>Night Upload Complete</b>\n"
        f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M IST')}\n"
        f"📤 Posted: {posted_count} reel(s)"
    )

    posted_total = sum(1 for k, v in uploads.items()
                       if k.startswith(date_str + "#") and v.get("status") == "posted")
    print(f"\nDone. Posted this run: {posted_count}, Total today: {posted_total}/{len(items)}", flush=True)
    print(f"Ledger: {UPLOADS_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())