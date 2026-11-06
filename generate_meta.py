#!/usr/bin/env python3
"""generate_meta.py — OpenRouter captions ("Day XX of Modern Warfare").

Reliability notes
-----------------
* Idempotent: an existing non-empty .txt sidecar is left alone (the old code
  looked for a "TITLE:" marker it never wrote, so it called the API again on
  every single run).
* The "Day XX" counter is persistent (state/meta_counter.json) and committed,
  so numbering continues across runs and across both nodes instead of
  restarting at 1 every day.
* Missing key / API hiccup falls back to a usable caption and still consumes a
  day number, so the series never repeats a number.

Usage:
  python generate_meta.py [--force] [--limit N]
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

HERE = Path(__file__).resolve().parent
SHORTS_DIR = HERE / "shorts"
STATE_DIR = HERE / "state"
COUNTER_PATH = STATE_DIR / "meta_counter.json"

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/fusion")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def next_day() -> int:
    state = load_json(COUNTER_PATH, {})
    day = int(state.get("next_day", 1) or 1)
    return day, state


def commit_day(state: dict, day: int) -> None:
    state = dict(state or {})
    state["next_day"] = int(day) + 1
    atomic_write_json(COUNTER_PATH, state)


def build_prompt(short_name: str, day: int) -> str:
    return f"""
You are a premium short-form gaming content writer.

Task:
Write a YouTube Shorts TITLE and DESCRIPTION for a gaming clip.

Clip details:
- File: {short_name}
- Series position: Day {day} of Modern Warfare
- Theme: Modern Warfare / Tactical Gaming / Combat Highlights

Requirements:
- TITLE: must start with "Day {day} of Modern Warfare" followed by a punchy hook. Max 60 chars.
- DESCRIPTION: 2-4 sentences, hype-driven, gaming tone, hashtags at the end.
- Tone: intense, tactical, modern warfare, gaming community.

Output EXACTLY this JSON and nothing else:
{{
  "title": "Day {day} of Modern Warfare - <hook>",
  "description": "<2-4 sentences>\\n\\n#ModernWarfare #Gaming #Tactical #Shorts"
}}
"""


def call_openrouter(prompt: str) -> dict | None:
    if not OPENROUTER_API_KEY:
        print("  OPENROUTER_API_KEY not set — using fallback caption", flush=True)
        return None

    payload = json.dumps(
        {
            "model": OPENROUTER_MODEL,
            "messages": [
                {"role": "system", "content": "You are a premium gaming short-form content writer."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.8,
            "max_tokens": 300,
        }
    ).encode("utf-8")

    for attempt in (1, 2):
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            parsed = json.loads(content)
            if parsed.get("title") and parsed.get("description"):
                return parsed
            raise ValueError("response missing title/description")
        except Exception as e:
            print(f"  OpenRouter attempt {attempt} failed: {e}", flush=True)
            if attempt == 1:
                time.sleep(3)
    return None


def fallback_meta(day: int) -> dict:
    return {
        "title": f"Day {day} of Modern Warfare - Tactical Highlight",
        "description": (
            f"Day {day} of Modern Warfare brings you intense tactical combat highlights. "
            "Pure adrenaline, zero filler.\n\n"
            "#ModernWarfare #Gaming #Tactical #CombatHighlights #Shorts"
        ),
    }


def main() -> int:
    args = sys.argv[1:]
    force = "--force" in args
    limit = None
    if "--limit" in args:
        try:
            limit = max(1, int(args[args.index("--limit") + 1]))
        except Exception:
            print("ERROR: --limit requires a positive integer", flush=True)
            return 2

    if not SHORTS_DIR.exists():
        print("No shorts/ folder found.", flush=True)
        return 0

    shorts = sorted(SHORTS_DIR.glob("s*.mp4"))
    if not shorts:
        print("No shorts found to generate captions for.", flush=True)
        return 0

    created = skipped = 0
    for short_path in shorts:
        txt_path = short_path.with_suffix(".txt")
        if txt_path.exists() and not force:
            try:
                if txt_path.stat().st_size > 0:
                    skipped += 1
                    continue
            except OSError:
                pass

        day, state = next_day()
        result = call_openrouter(build_prompt(short_path.name, day)) or fallback_meta(day)

        title = str(result.get("title") or f"Day {day} of Modern Warfare").strip()
        description = str(result.get("description") or "").strip()
        content = f"{title}\n\n{description}".strip()

        txt_path.write_text(content + "\n", encoding="utf-8")
        commit_day(state, day)
        created += 1
        print(f"  {txt_path.name} -> Day {day}: {title[:48]}", flush=True)

        if limit is not None and created >= limit:
            break

    print(f"\nCaptions: {created} new, {skipped} already present.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
