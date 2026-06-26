"""
substack_login.py — one-time Substack login into a persistent REAL-Chrome profile
(substack_profile/). Looks like a normal browser, so the login CAPTCHA actually
renders and you're far less likely to trip Substack's "logins limited" defense
than with Playwright's bundled Chromium.

Usage:
    python substack_login.py

Log in by hand in the window that opens (email code / Google / CAPTCHA all work),
wait until you can see your Notes feed, then come back here and press Enter. The
profile persists, so the agent reuses this session on every run — you should not
need to log in again unless the session expires.

Tip: if Substack says logins are limited, wait ~24h (it's a temporary cap) and try
once more — do NOT keep retrying, as each attempt can restart the timer.
"""

from playwright.sync_api import sync_playwright

import common
import substack_web

with sync_playwright() as p:
    context = substack_web.substack_context(p, headless=False)
    page = context.pages[0] if context.pages else context.new_page()
    page.goto("https://substack.com/sign-in")
    print("\nA real Chrome window has opened.")
    print("Log into Substack fully (email code / Google / solve any CAPTCHA),")
    print("wait until you can see your Notes feed, then come back here.")
    input("\nPress Enter once you are logged in... ")
    context.close()
    print(f"Session saved in profile: {common.SUBSTACK_PROFILE_DIR}")
