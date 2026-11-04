#!/usr/bin/env python3
"""
generate_meta.py — OpenRouter-powered title/description generator for shorts.

Reads shorts/s001.mp4, s002.mp4... and writes matching .txt sidecar files
with a "Day XX of Modern Warfare" style title + description.

Requires:
  OPENROUTER_API_KEY  (env var or .env)
  OPENROUTER_MODEL    (optional, defaults to openrouter/fusion)
"""
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

HERE = Path(__file__).resolve().parent
SHORTS_DIR = HERE / "shorts"

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/fusion")


def build_prompt(short_name: str, index: int, total: int) -> str:
    return f"""
You are a premium short-form gaming content writer.

Task:
Write a YouTube Shorts TITLE and DESCRIPTION for a gaming clip.

Clip details:
- File: {short_name}
- Sequence: Day {index} of {total}
- Theme: Modern Warfare / Tactical Gaming / Combat Highlights

Requirements:
- TITLE: Must start with "Day {index} of Modern Warfare" followed by a punchy hook. Max 60 chars.
- DESCRIPTION: 2-4 sentences, hype-driven, gaming tone. Include relevant hashtags at the end.
- Tone: Intense, tactical, modern warfare, gaming community.

Output EXACTLY this JSON format and nothing else:
{{
  "title": "Day {index} of Modern Warfare - <hook>",
  "description": "<2-4 sentences hype description>\n\n#ModernWarfare #Gaming #Tactical #Shorts"
}}
"""


def call_openrouter(prompt: str) -> dict | None:
    if not OPENROUTER_API_KEY:
        print("  ⚠️  OPENROUTER_API_KEY not set. Using fallback meta.", flush=True)
        return None

    payload = json.dumps({
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": "You are a premium gaming short-form content writer."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.8,
        "max_tokens": 300,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(content)
    except Exception as e:
        print(f"  ⚠️  OpenRouter failed: {e}. Using fallback meta.", flush=True)
        return None


def fallback_meta(short_name: str, index: int, total: int) -> dict:
    return {
        "title": f"Day {index} of Modern Warfare - Tactical Highlight",
        "description": (
            f"Day {index} of Modern Warfare brings you intense tactical combat highlights. "
            f"Pure adrenaline, zero filler.\n\n"
            f"#ModernWarfare #Gaming #Tactical #CombatHighlights #Shorts"
        ),
    }


def generate_meta_for_short(short_path: Path, index: int, total: int) -> dict:
    txt_path = short_path.with_suffix(".txt")

    # Skip if .txt already exists (idempotent)
    if txt_path.exists():
        try:
            existing = txt_path.read_text(encoding="utf-8")
            if "TITLE:" in existing and "DESCRIPTION:" in existing:
                return {"skipped": True, "short": short_path.stem}
        except Exception:
            pass

    prompt = build_prompt(short_path.name, index, total)
    result = call_openrouter(prompt)

    if result is None:
        result = fallback_meta(short_path.name, index, total)

    title = result.get("title", f"Day {index} of Modern Warfare")
    description = result.get("description", f"Day {index} of Modern Warfare highlight.")

    # Write clean caption text for Instagram (upload_shorts.py sends whole file as caption)
    content = f"{title}\n\n{description}"

    txt_path.write_text(content, encoding="utf-8")
    print(f"  📝 {txt_path.name} -> {title[:50]}...", flush=True)

    return {"short": short_path.stem, "title": title, "description": description}


def main() -> int:
    if not SHORTS_DIR.exists():
        print("No shorts/ folder found.", flush=True)
        return 0

    shorts = sorted(SHORTS_DIR.glob("s*.mp4"))
    if not shorts:
        print("No shorts found to generate meta for.", flush=True)
        return 0

    total = len(shorts)
    print(f"Generating meta for {total} short(s)...", flush=True)

    results = []
    for idx, short_path in enumerate(shorts, 1):
        result = generate_meta_for_short(short_path, idx, total)
        results.append(result)

    saved = sum(1 for r in results if not r.get("skipped"))
    skipped = sum(1 for r in results if r.get("skipped"))
    print(f"\n✅ Meta generated: {saved} new, {skipped} already existed.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())