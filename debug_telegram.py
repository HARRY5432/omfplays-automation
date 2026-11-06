#!/usr/bin/env python3
"""debug_telegram.py — send the shared status block to Telegram.

Kept as a compatibility entry point for older workflow runs; the message body
now comes from pipeline_status so there is a single source of truth.
"""
from __future__ import annotations

import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main() -> int:
    import pipeline_status as ps
    from telegram_notify import notify_status_report

    ok = notify_status_report(ps.render(ps.snapshot()))
    print("telegram summary sent" if ok else "telegram summary not sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
