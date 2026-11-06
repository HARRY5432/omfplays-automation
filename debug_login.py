#!/usr/bin/env python3
"""debug_login.py — prove Instagram login works without publishing anything.

Reuses upload_shorts.login() so the workflow tests the exact code path that
publishes; the previous version had its own copy that called
cl.login("user", "pass") with literal placeholders.
"""
from __future__ import annotations

import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main() -> int:
    try:
        from upload_shorts import login
    except ImportError as e:
        print(f"instagrapi/upload_shorts unavailable: {e}")
        return 1

    try:
        client = login()
    except SystemExit as e:
        print(f"login failed (exit {e.code})")
        return int(e.code or 1)
    except Exception as e:
        print(f"login failed: {type(e).__name__}: {e}")
        return 1

    print(f"login ok: @{getattr(client, 'username', '?')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
