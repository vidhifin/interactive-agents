"""
quora_login.py — one-time manual Quora login (seeds quora_state.json).

Quora hides its question feed (and the answer editor) behind login, and may show
a CAPTCHA on automated logins — so log in by hand once. This opens a real
browser window; log in (solve any CAPTCHA), then press Enter and the session
cookies are saved for the scout (scraping) and poster (answering) to reuse.

Usage:
    python quora_login.py
"""

from playwright.sync_api import sync_playwright

import common

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://www.quora.com/")
    print("\nA browser window has opened.")
    print("Log into Quora fully (solve any CAPTCHA), wait until you see your")
    print("logged-in Quora home feed, then come back here.")
    input("\nPress Enter once you are logged in... ")
    context.storage_state(path=str(common.QUORA_STATE_PATH))
    print(f"Saved session to {common.QUORA_STATE_PATH}")
    browser.close()
