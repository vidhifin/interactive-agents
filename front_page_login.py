"""
front_page_login.py — one-time manual login to front.page (the Indian stock
community). Opens a browser; log in however front.page wants (email / phone OTP /
Google), wait until you're on your logged-in feed, then press Enter. The session
(cookies + localStorage) is saved to frontpage_state.json for the agent to reuse.

Usage:
    python front_page_login.py
"""

from playwright.sync_api import sync_playwright

import common

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(user_agent=BROWSER_UA)
    page = context.new_page()
    page.goto("https://front.page/")
    print("\nA browser window has opened.")
    print("Log into front.page fully (email / phone OTP / Google — whatever it")
    print("uses), wait until you can see your logged-in feed, then come back here.")
    input("\nPress Enter once you are logged in... ")
    context.storage_state(path=str(common.FRONTPAGE_STATE_PATH))
    print(f"Saved session to {common.FRONTPAGE_STATE_PATH}")
    browser.close()
