"""
twitter_login.py — one-time manual X/Twitter login into a persistent REAL-Chrome
profile (twitter_profile/). Looks like a normal browser, so it's far less likely
to trip X's "logins limited" bot-defense than Playwright's bundled Chromium.

Usage:
    python twitter_login.py

Tip: if X still says logins are limited, wait ~24h (it's a temporary cap) and try
again — this stealthier browser should not re-trigger it.
"""

from playwright.sync_api import sync_playwright

import common
import twitter_web

with sync_playwright() as p:
    context = twitter_web.twitter_context(p, headless=False)
    page = context.pages[0] if context.pages else context.new_page()
    page.goto("https://x.com/login")
    print("\nA real Chrome window has opened.")
    print("Log into X/Twitter fully (handle any verification), wait until you see")
    print("your home timeline, then come back here.")
    input("\nPress Enter once you are logged in... ")
    context.close()
    print(f"Session saved in profile: {common.TWITTER_PROFILE_DIR}")
