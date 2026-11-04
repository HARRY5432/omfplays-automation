#!/usr/bin/env python3
"""
debug_login.py — Debug login test for GitHub Actions workflow.
Tests cookie and session login without posting anything.
"""
import json
import os
import sys

try:
    from instagrapi import Client
except ImportError:
    print("❌ instagrapi not installed")
    sys.exit(1)

cl = Client()
ok = False

# Try cookie login
if os.path.exists("publisher_cookies.json"):
    try:
        raw = json.loads(open("publisher_cookies.json", "r", encoding="utf-8").read())
        items = raw if isinstance(raw, list) else raw.get("cookies", [])
        sid = next((c.get("value") for c in items if isinstance(c, dict) and c.get("name") == "sessionid"), None)
        if sid:
            try:
                cl.login_by_sessionid(sid)
                print(f"✅ Cookie login OK: @{cl.username}")
                ok = True
            except Exception as e:
                print(f"❌ Cookie login failed: {e}")
        else:
            print("⚠️ No sessionid found in cookies")
    except Exception as e:
        print(f"❌ Could not read cookies file: {e}")
else:
    print("⚠️ publisher_cookies.json not found")

# Try session login
if os.path.exists("publisher_session.json"):
    try:
        cl.load_settings("publisher_session.json")
        cl.login("user", "pass")
        print(f"✅ Session login OK: @{cl.username}")
        ok = True
    except Exception as e:
        print(f"⚠️ Session login failed: {e}")
else:
    print("⚠️ publisher_session.json not found")

if not ok:
    print("❌ No login method succeeded")
    sys.exit(1)
else:
    print("✅ Login test complete")
    sys.exit(0)