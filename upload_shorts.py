#!/usr/bin/env python3
"""upload_shorts.py — post omfplays shorts to Instagram.

Source: shorts/s001.mp4, s002.mp4... (from build_shorts_omf.py)
Caption: from matching .txt file (optional)
Cover: from matching .jpg file (optional)
Schedule: configurable slots in slots.json
Progress: state/uploads.json — never double-posts.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from instagrapi import Client
    from instagrapi.exceptions import ChallengeRequired, BadPassword, ClientThrottledError
except ImportError:
    print("ERROR: instagrapi not installed. Run: pip install instagrapi")
    sys.exit(1)

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

_DEFAULT_SLOTS = [
    ("06:07", "Morning peak"),
    ("10:30", "Mid-morning"),
    ("14:00", "Afternoon"),
    ("18:30", "Evening peak"),
    ("21:00", "Night"),
]

BETWEEN_POSTS_SECS = 600


def _load_slots():
    if SLOTS_PATH.exists():
        try:
            data = json.loads(SLOTS_PATH.read_text(encoding="utf-8"))
            rows = [(s["time"], s.get("label", "")) for s in data.get("slots", [])]
            if rows:
                return rows
        except Exception:
            pass
    return _DEFAULT_SLOTS


SLOTS = _load_slots()


def log(msg: str, level: str = "info", date_str: str = "") -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now(timezone.utc).isoformat(),
           "level": level, "event": msg}
    with open(LOGS_DIR / f"upload_{date_str or datetime.now(IST).strftime('%Y-%m-%d')}.log",
               "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"  [{level.upper():5}] {msg}")


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_auth() -> tuple[str, str] | tuple[None, None]:
    import os
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
        print("ERROR: no publisher credentials. Set IG_PUBLISHER_USER +")
        print("IG_PUBLISHER_PASSWORD env vars, or publisher_auth.json.")
        sys.exit(1)

    cl = Client()

    def verified(client) -> bool:
        expected = (expected_user or user).strip().lower()
        if expected and client.username and client.username.lower() != expected:
            print(f"ERROR: logged in as @{client.username}, expected @{user}.")
            return False
        return True

    if PUBLISHER_COOKIES.exists():
        try:
            raw = json.loads(PUBLISHER_COOKIES.read_text(encoding="utf-8"))
            items = raw if isinstance(raw, list) else raw.get("cookies", [])
            sid = next((c.get("value") for c in items
                        if isinstance(c, dict) and c.get("name") == "sessionid"), None)
            if sid:
                cl.login_by_sessionid(sid)
                if not verified(cl):
                    sys.exit(4)
                cl.dump_settings(PUBLISHER_SESSION)
                return cl
        except ClientThrottledError as e:
            print("  ERROR: Instagram rate-limiting logins (429). Wait 15-60 min.")
            sys.exit(3)
        except Exception:
            pass

    if PUBLISHER_SESSION.exists():
        try:
            cl.load_settings(PUBLISHER_SESSION)
            cl.login(user, pw)
            if not verified(cl):
                sys.exit(4)
            cl.dump_settings(PUBLISHER_SESSION)
            return cl
        except ClientThrottledError as e:
            print("  ERROR: Instagram rate-limiting logins (429). Wait 15-60 min.")
            sys.exit(3)
        except Exception:
            pass

    try:
        cl.login(user, pw)
    except BadPassword:
        print("ERROR: Instagram rejected the password. Update publisher_auth.json.")
        sys.exit(1)
    except ChallengeRequired:
        print("Instagram wants a verification code (new device).")
        print("Check email/SMS, then re-run with:  --code 123456")
        PUBLISHER_SESSION.unlink(missing_ok=True)
        if not challenge_code:
            print("(no --code given, stopping.)")
            sys.exit(2)
        try:
            cl.challenge_resolve(challenge_code)
        except Exception as e:
            print(f"ERROR: challenge failed ({type(e).__name__}: {e})")
            sys.exit(1)
    except ClientThrottledError as e:
        print("ERROR: Instagram rate-limiting logins (429). Wait 15-60 min.")
        sys.exit(3)
    except Exception as e:
        print(f"ERROR: login failed ({type(e).__name__}: {e})")
        sys.exit(1)

    if not verified(cl):
        sys.exit(4)

    cl.dump_settings(PUBLISHER_SESSION)
    return cl


def collect_shorts() -> list[dict]:
    """Return list of {video, cover, caption, short_name} sorted by short number."""
    items = []
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
    ap.add_argument("--trial", action="store_true",
                    help="post as TRIAL reel (followers don't see it)")
    ap.add_argument("--now", action="store_true",
                    help="ignore clock: post all unposted immediately")
    ap.add_argument("--slot", type=int, default=None, help="only this slot index")
    ap.add_argument("--code", default=None, help="challenge verification code")
    ap.add_argument("--publisher-user", default=None,
                    help="expected publisher username for login guard")
    args = ap.parse_args()

    now = datetime.now(IST)
    date_str = now.strftime("%Y-%m-%d")
    items = collect_shorts()
    if not items:
        print(f"No shorts found in {SHORTS_DIR}. Run build_shorts_omf.py first.")
        return 1

    uploads: dict = load_json(UPLOADS_PATH, {})
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

    print(f"Date: {date_str} | Shorts: {len(items)} | Now: {now.strftime('%H:%M IST')}")
    for i, hm, label, it, st in plan:
        what = it["short"] if it else "-"
        print(f"  slot {i} [{hm} IST] {label:20} {what:10} {st}")

    if args.dry_run:
        print("\n(dry run — nothing posted)")
        return 0

    due = [(i, it) for i, _, _, it, st in plan if st == "DUE"]
    if not due:
        print("Nothing due right now.")
        return 0
    if args.trial and len(due) > 1:
        due = due[:1]
        print("(trial mode: posting 1 reel only)")

    cl = login(args.code, args.publisher_user)
    me = cl.username if hasattr(cl, "username") else "?"
    print(f"Logged in as @{me}")

    posted_count = 0
    for n, (i, it) in enumerate(due):
        if n > 0:
            print(f"  ...waiting {BETWEEN_POSTS_SECS // 60} min before next post...")
            time.sleep(BETWEEN_POSTS_SECS)

        caption = ""
        if it["caption_file"]:
            caption = Path(it["caption_file"]).read_text(
                encoding="utf-8", errors="replace").strip()[:2200]

        print(f"\nPosting slot {i}: {it['short']} (trial={args.trial}) ...")
        try:
            media = cl.clip_upload(
                it["video"], caption,
                thumbnail=it["cover"] if it["cover"] else None,
                trial=args.trial,
            )
        except Exception as e:
            log(f"FAILED slot {i} {it['short']}: {type(e).__name__}: {e}",
                level="error", date_str=date_str)
            print(f"  FAILED: {type(e).__name__}: {e}")
            print("  Stopping (progress kept in state/uploads.json).")
            return 1

        key = f"{date_str}#{i}"
        uploads[key] = {"status": "posted", "slot": SLOTS[i][0],
                        "short": it["short"],
                        "media_pk": str(getattr(media, "pk", "?")),
                        "media_code": getattr(media, "code", None),
                        "trial": args.trial,
                        "posted_at": datetime.now(timezone.utc).isoformat()}
        save_json(UPLOADS_PATH, uploads)
        posted_count += 1
        log(f"posted slot {i} {it['short']} pk={uploads[key]['media_pk']} trial={args.trial}", date_str=date_str)
        print(f"  POSTED media_pk={uploads[key]['media_pk']}")

    posted_total = sum(1 for k, v in uploads.items()
                       if k.startswith(date_str + "#") and v.get("status") == "posted")
    print(f"\nDone. Posted this run: {posted_count}, Total today: {posted_total}/{len(items)}")
    print(f"Ledger: {UPLOADS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())