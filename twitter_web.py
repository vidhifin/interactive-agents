"""
twitter_web.py — post original tweets to X/Twitter via a logged-in REAL Chrome.

X aggressively detects automation, so we:
  - use your installed Chrome (channel="chrome"), not Playwright's bundled Chromium
  - turn off the automation flags (navigator.webdriver / "controlled by automation")
  - use a PERSISTENT profile (twitter_profile/) so you log in once and it sticks

Seed the session once with `python twitter_login.py`.
"""

from __future__ import annotations

import time
import random
from urllib.parse import quote

import common
from common import log

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _profile_url() -> str:
    user = common.env("TWITTER_USERNAME")
    return f"https://x.com/{user}" if user else "https://x.com/home"


def twitter_context(p, headless: bool):
    """A persistent, low-detection browser context for X.

    Prefers real Chrome (channel="chrome"); falls back to bundled Chromium.
    """
    args = ["--disable-blink-features=AutomationControlled"]
    ignore = ["--enable-automation"]
    common.TWITTER_PROFILE_DIR.mkdir(exist_ok=True)
    udir = str(common.TWITTER_PROFILE_DIR)
    try:
        return p.chromium.launch_persistent_context(
            udir, channel="chrome", headless=headless, args=args, ignore_default_args=ignore
        )
    except Exception as e:  # noqa: BLE001 - Chrome not installed / not found
        log.warning("Real Chrome unavailable (%s) — falling back to bundled Chromium.", e)
        return p.chromium.launch_persistent_context(
            udir, headless=headless, args=args, ignore_default_args=ignore
        )


def _logged_out(page) -> bool:
    return ("/login" in page.url or "/i/flow/login" in page.url
            or page.query_selector('input[name="text"]') is not None)


def post_tweet(text: str, config: dict) -> str:
    """Compose and post one tweet. Returns the profile URL."""
    from playwright.sync_api import sync_playwright

    headless = config.get("twitter", {}).get("headless", False)
    with sync_playwright() as p:
        context = twitter_context(p, headless)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(30000)
            log.info("Twitter: opening home")
            page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
            time.sleep(5)

            if _logged_out(page):
                raise RuntimeError(
                    "Not logged into X. Run `python twitter_login.py` once to log in."
                )

            box = None
            for sel in ['[data-testid="tweetTextarea_0"]', 'div[role="textbox"]']:
                try:
                    box = page.wait_for_selector(sel, timeout=10000, state="visible")
                except Exception:  # noqa: BLE001
                    box = None
                if box:
                    break
            if not box:
                raise RuntimeError("Could not find the tweet compose box")
            box.click()
            page.keyboard.type(text, delay=2)
            time.sleep(1)

            btn = None
            for sel in ['[data-testid="tweetButtonInline"]', '[data-testid="tweetButton"]']:
                el = page.query_selector(sel)
                if el and el.is_enabled():
                    btn = el
                    break
            if not btn:
                raise RuntimeError("Could not find the Post button (or it was disabled)")
            btn.click()
            time.sleep(5)
            return _profile_url()
        finally:
            context.close()


def _click(el) -> None:
    try:
        el.click(timeout=6000)
    except Exception:  # noqa: BLE001
        el.evaluate("e => e.click()")


def search_tweets(config: dict, limit: int) -> list[dict]:
    """Search X across your topics and return up to `limit` tweets [{url, text}]."""
    from playwright.sync_api import sync_playwright

    tw = config.get("twitter", {})
    keywords = list(tw.get("topics") or [])
    random.shuffle(keywords)
    headless = tw.get("headless", False)
    found: list[dict] = []
    seen = set()
    with sync_playwright() as p:
        context = twitter_context(p, headless)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(30000)
            page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
            time.sleep(4)
            if _logged_out(page):
                raise RuntimeError("Not logged into X. Run `python twitter_login.py` once.")
            for kw in keywords:
                if len(found) >= limit:
                    break
                try:
                    page.goto(f"https://x.com/search?q={quote(kw)}&f=live",
                              wait_until="domcontentloaded", timeout=30000)
                    time.sleep(4)
                    page.mouse.wheel(0, 1500)
                    time.sleep(2)
                    arts = page.query_selector_all('article[data-testid="tweet"]')
                except Exception as e:  # noqa: BLE001
                    log.warning("X search failed for '%s': %s", kw, e)
                    continue
                for art in arts:
                    if len(found) >= limit:
                        break
                    link = art.query_selector('a[href*="/status/"]')
                    href = link.get_attribute("href") if link else None
                    if not href:
                        continue
                    url = "https://x.com" + href.split("?")[0]
                    if url in seen:
                        continue
                    txtel = art.query_selector('[data-testid="tweetText"]')
                    text = (txtel.inner_text() if txtel else (art.inner_text() or "")).strip()
                    if len(text) < 15:
                        continue
                    seen.add(url)
                    found.append({"url": url, "text": " ".join(text.split())})
        finally:
            context.close()
    return found


def reply_to_tweet(tweet_url: str, text: str, config: dict) -> str:
    """Reply to a specific tweet. Returns the tweet URL."""
    from playwright.sync_api import sync_playwright

    headless = config.get("twitter", {}).get("headless", False)
    with sync_playwright() as p:
        context = twitter_context(p, headless)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(30000)
            log.info("Twitter: opening tweet to reply")
            page.goto(tweet_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(5)
            if _logged_out(page):
                raise RuntimeError("Not logged into X. Run `python twitter_login.py` once.")

            box = None
            for sel in ['[data-testid="tweetTextarea_0"]', 'div[role="textbox"]']:
                try:
                    box = page.wait_for_selector(sel, timeout=10000, state="visible")
                except Exception:  # noqa: BLE001
                    box = None
                if box:
                    break
            if not box:
                raise RuntimeError("Could not find the reply box")
            _click(box)
            page.keyboard.type(text, delay=2)
            time.sleep(1)

            btn = None
            for sel in ['[data-testid="tweetButtonInline"]', '[data-testid="tweetButton"]']:
                el = page.query_selector(sel)
                if el and el.is_enabled():
                    btn = el
                    break
            if not btn:
                raise RuntimeError("Could not find the Reply button (or it was disabled)")
            _click(btn)
            time.sleep(5)
            return tweet_url
        finally:
            context.close()


def like_tweets(config: dict) -> list[tuple[str, str]]:
    """Search X for tweets on your topics and like a few. Returns [(snippet, url)]."""
    from playwright.sync_api import sync_playwright

    tw = config.get("twitter", {})
    keywords = list(tw.get("topics") or [])
    random.shuffle(keywords)
    target = tw.get("likes_per_run", 3)
    headless = tw.get("headless", False)
    posted = common.load_posted()
    liked: list[tuple[str, str]] = []

    with sync_playwright() as p:
        context = twitter_context(p, headless)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(30000)
            page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
            time.sleep(4)
            if _logged_out(page):
                raise RuntimeError("Not logged into X. Run `python twitter_login.py` once.")

            for kw in keywords:
                if len(liked) >= target:
                    break
                try:
                    page.goto(f"https://x.com/search?q={quote(kw)}&f=live",
                              wait_until="domcontentloaded", timeout=30000)
                    time.sleep(4)
                    page.mouse.wheel(0, 1500)
                    time.sleep(2)
                    articles = page.query_selector_all('article[data-testid="tweet"]')
                except Exception as e:  # noqa: BLE001
                    log.warning("X search failed for '%s': %s", kw, e)
                    continue

                for art in articles:
                    if len(liked) >= target:
                        break
                    link = art.query_selector('a[href*="/status/"]')
                    href = link.get_attribute("href") if link else None
                    if not href:
                        continue
                    turl = "https://x.com" + href.split("?")[0]
                    if ("like:" + turl) in posted:
                        continue
                    like_btn = art.query_selector('button[data-testid="like"]')
                    if not like_btn:  # already liked (shows "unlike") or no button
                        continue
                    try:
                        _click(like_btn)
                    except Exception as e:  # noqa: BLE001
                        log.warning("Like failed for %s: %s", turl, e)
                        continue
                    common.add_posted("like:" + turl)
                    snippet = " ".join((art.inner_text() or "").split())[:80]
                    liked.append((snippet, turl))
                    common.log_post("twitter-like", turl, snippet)
                    log.info("Liked: %s", turl)
                    time.sleep(random.uniform(5, 15))  # space them out
        finally:
            context.close()

    log.info("X: liked %d tweet(s)", len(liked))
    return liked
