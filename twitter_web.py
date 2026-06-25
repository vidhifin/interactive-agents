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


def join_communities(config: dict) -> list[tuple[str, str]]:
    """Discover finance X Communities and auto-join the good ones (capped, ledger-tracked).
    Returns [(name, url)] of communities joined this run."""
    from playwright.sync_api import sync_playwright
    from urllib.parse import quote

    tw = config.get("twitter", {})
    n = tw.get("join_per_run", 0)
    if not n:
        return []
    keywords = tw.get("community_keywords") or ["Indian stock market", "stock market investing"]
    filters = [f.lower() for f in (tw.get("community_name_filters")
               or ["stock", "market", "invest", "finance", "nifty", "trading", "equity"])]
    headless = tw.get("headless", False)
    posted = common.load_posted()
    random.shuffle(keywords)
    joined: list[tuple[str, str]] = []

    with sync_playwright() as p:
        context = twitter_context(p, headless)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(30000)
            page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
            time.sleep(4)
            if _logged_out(page):
                raise RuntimeError("Not logged into X. Run `python twitter_login.py` once.")

            # Discover candidate communities via search, filtered to finance names.
            cands = {}
            for kw in keywords:
                if len(cands) >= n * 4:
                    break
                try:
                    page.goto(f"https://x.com/search?q={quote(kw)}&src=typed_query",
                              wait_until="domcontentloaded", timeout=30000)
                    time.sleep(5)
                    page.mouse.wheel(0, 800)
                    time.sleep(2)
                    found = page.evaluate(
                        "() => { const out=[], seen=new Set();"
                        " for (const a of document.querySelectorAll('a[href*=\"/i/communities/\"]')) {"
                        "  const h=a.getAttribute('href'); const t=(a.innerText||'').trim();"
                        "  if(!h || !/\\/i\\/communities\\/\\d+$/.test(h) || seen.has(h)) continue;"
                        "  seen.add(h); out.push({h:h, t:t}); }"
                        " return out; }"
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("X community search failed '%s': %s", kw, e)
                    continue
                for f in found:
                    cid = f["h"].rsplit("/", 1)[-1]
                    name = (f["t"] or "").strip()
                    if not name or not any(w in name.lower() for w in filters):
                        continue
                    if ("xcommunity:" + cid) in posted:
                        continue
                    url = f["h"] if f["h"].startswith("http") else "https://x.com" + f["h"]
                    cands.setdefault(cid, (url, name))

            for cid, (url, name) in list(cands.items())[:n]:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(5)
                    btn = None
                    for b in page.query_selector_all("button"):
                        if (b.inner_text() or "").strip() in ("Join", "Request to join"):
                            btn = b
                            break
                    if not btn:
                        common.add_posted("xcommunity:" + cid)  # already a member / not joinable
                        continue
                    _click(btn)
                    time.sleep(3)
                    common.add_posted("xcommunity:" + cid)
                    common.note_recent("x_community", url, keep=50)
                    joined.append((name, url))
                    log.info("Joined X community: %s", name)
                    time.sleep(random.uniform(5, 12))
                except Exception as e:  # noqa: BLE001
                    log.warning("X community join failed (%s): %s", name, e)
        finally:
            context.close()

    log.info("X: joined %d communities", len(joined))
    return joined


def _community_list(config: dict) -> list[str]:
    tw = config.get("twitter", {})
    urls = list(tw.get("communities") or []) + common.recent("x_community", 50)
    return list(dict.fromkeys(u for u in urls if u))


def engage_communities(config: dict) -> list[tuple[str, str, str]]:
    """Reply + like (+ optional original post) WITHIN joined X communities.
    Ledger-deduped. Returns (action, snippet, url) tuples."""
    from playwright.sync_api import sync_playwright

    tw = config.get("twitter", {})
    communities = _community_list(config)
    n_targets = tw.get("community_targets_per_run", 6)
    n_like = tw.get("likes_per_community", 0)
    n_reply = tw.get("replies_per_community", 0)
    n_post = tw.get("community_posts_per_run", 0)
    if not communities or not (n_like or n_reply or n_post):
        return []
    headless = tw.get("headless", False)
    posted = common.load_posted()
    local: set = set()
    done: list[tuple[str, str, str]] = []
    random.shuffle(communities)
    communities = communities[:n_targets]

    with sync_playwright() as p:
        context = twitter_context(p, headless)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(30000)
            page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
            time.sleep(4)
            if _logged_out(page):
                raise RuntimeError("Not logged into X. Run `python twitter_login.py` once.")

            # ---- ORIGINAL POST into a community ----
            for _ in range(n_post):
                comm = random.choice(communities)
                try:
                    page.goto(comm, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(5)
                    cbtn = page.query_selector('a[href$="/compose/post"]')
                    if cbtn:
                        _click(cbtn)
                        time.sleep(3)
                    box = page.wait_for_selector('[data-testid="tweetTextarea_0"]', timeout=10000, state="visible")
                    text = common.draft_tweet("the markets", False, config)
                    _click(box)
                    page.keyboard.type(text, delay=2)
                    common.note_recent("tweet", text)
                    time.sleep(1)
                    btn = None
                    for sel in ['[data-testid="tweetButton"]', '[data-testid="tweetButtonInline"]']:
                        el = page.query_selector(sel)
                        if el and el.is_enabled():
                            btn = el
                            break
                    if btn:
                        _click(btn)
                        time.sleep(5)
                        done.append(("xcomm_post", text[:60], comm))
                        log.info("Posted into X community: %s", comm)
                except Exception as e:  # noqa: BLE001
                    log.warning("X community post failed: %s", e)

            # ---- per-community LIKES + REPLIES (visit each community once) ----
            for comm in communities:
                try:
                    page.goto(comm, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(5)
                    page.mouse.wheel(0, 1500)
                    time.sleep(2)
                except Exception:  # noqa: BLE001
                    continue

                # likes in THIS community
                liked = 0
                for art in page.query_selector_all('article[data-testid="tweet"]'):
                    if liked >= n_like:
                        break
                    link = art.query_selector('a[href*="/status/"]')
                    href = link.get_attribute("href") if link else None
                    if not href:
                        continue
                    url = "https://x.com" + href.split("?")[0]
                    key = "xclike:" + url
                    if key in posted or key in local:
                        continue
                    lb = art.query_selector('button[data-testid="like"]')
                    if not lb:
                        continue
                    te = art.query_selector('[data-testid="tweetText"]')
                    txt = " ".join(((te.inner_text() if te else "") or "").split())
                    try:
                        _click(lb)
                    except Exception:  # noqa: BLE001
                        continue
                    local.add(key)
                    common.add_posted(key)
                    done.append(("xcomm_like", txt[:50], url))
                    liked += 1
                    time.sleep(random.uniform(3, 7))

                # gather reply targets in THIS community
                targets = []
                for art in page.query_selector_all('article[data-testid="tweet"]'):
                    if len(targets) >= n_reply * 3:
                        break
                    link = art.query_selector('a[href*="/status/"]')
                    href = link.get_attribute("href") if link else None
                    if not href:
                        continue
                    url = "https://x.com" + href.split("?")[0]
                    if ("xcreply:" + url) in posted:
                        continue
                    te = art.query_selector('[data-testid="tweetText"]')
                    text = " ".join(((te.inner_text() if te else "") or "").split())
                    if len(text) < 5:
                        continue
                    targets.append((url, text))

                # replies in THIS community
                replied = 0
                for url, text in targets:
                    if replied >= n_reply:
                        break
                    try:
                        reply = common.draft_tweet_reply(text, False, config)
                        page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        time.sleep(4)
                        box = None
                        for sel in ['[data-testid="tweetTextarea_0"]', 'div[role="textbox"]']:
                            try:
                                box = page.wait_for_selector(sel, timeout=10000, state="visible")
                            except Exception:  # noqa: BLE001
                                box = None
                            if box:
                                break
                        if not box:
                            raise RuntimeError("reply box not found")
                        _click(box)
                        page.keyboard.type(reply, delay=2)
                        time.sleep(1)
                        btn = None
                        for sel in ['[data-testid="tweetButtonInline"]', '[data-testid="tweetButton"]']:
                            el = page.query_selector(sel)
                            if el and el.is_enabled():
                                btn = el
                                break
                        if not btn:
                            raise RuntimeError("reply button not found")
                        _click(btn)
                        time.sleep(5)
                        common.add_posted("xcreply:" + url)
                        common.log_post("twitter-community-reply", url, reply)
                        done.append(("xcomm_reply", text[:50], url))
                        replied += 1
                    except Exception as e:  # noqa: BLE001
                        log.warning("X community reply failed (%s): %s", url, e)
        finally:
            context.close()

    log.info("X communities: %d in-community actions", len(done))
    return done


def reply_to_mentions(config: dict) -> list[tuple[str, str]]:
    """Follow up on people who replied to / mentioned us. One reply per thread,
    capped via the ledger ('xfollowup:<url>') so we never loop forever.
    Returns [(snippet, url)]."""
    from playwright.sync_api import sync_playwright

    tw = config.get("twitter", {})
    n = tw.get("followups_per_run", 0)
    if not n:
        return []
    headless = tw.get("headless", False)
    posted = common.load_posted()
    done: list[tuple[str, str]] = []

    with sync_playwright() as p:
        context = twitter_context(p, headless)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(30000)
            page.goto("https://x.com/notifications/mentions",
                      wait_until="domcontentloaded", timeout=30000)
            time.sleep(5)
            if _logged_out(page):
                raise RuntimeError("Not logged into X. Run `python twitter_login.py` once.")
            page.mouse.wheel(0, 1500)
            time.sleep(2)

            # Collect (url, text) of recent mentions we haven't followed up on.
            pairs = []
            seen = set()
            for art in page.query_selector_all('article[data-testid="tweet"]'):
                if len(pairs) >= n:
                    break
                link = art.query_selector('a[href*="/status/"]')
                href = link.get_attribute("href") if link else None
                if not href:
                    continue
                url = "https://x.com" + href.split("?")[0]
                if url in seen or ("xfollowup:" + url) in posted:
                    continue
                seen.add(url)
                te = art.query_selector('[data-testid="tweetText"]')
                text = (te.inner_text() if te else "").strip()
                if len(text) < 5:
                    continue
                pairs.append((url, " ".join(text.split())))

            for url, text in pairs:
                try:
                    reply = common.draft_tweet_reply(text, False, config)
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(4)
                    box = None
                    for sel in ['[data-testid="tweetTextarea_0"]', 'div[role="textbox"]']:
                        try:
                            box = page.wait_for_selector(sel, timeout=10000, state="visible")
                        except Exception:  # noqa: BLE001
                            box = None
                        if box:
                            break
                    if not box:
                        raise RuntimeError("reply box not found")
                    _click(box)
                    page.keyboard.type(reply, delay=2)
                    time.sleep(1)
                    btn = None
                    for sel in ['[data-testid="tweetButtonInline"]', '[data-testid="tweetButton"]']:
                        el = page.query_selector(sel)
                        if el and el.is_enabled():
                            btn = el
                            break
                    if not btn:
                        raise RuntimeError("reply button not found")
                    _click(btn)
                    time.sleep(5)
                    common.add_posted("xfollowup:" + url)
                    common.log_post("twitter-followup", url, reply)
                    done.append((text[:60], url))
                except Exception as e:  # noqa: BLE001
                    log.warning("X follow-up failed (%s): %s", url, e)
        finally:
            context.close()

    log.info("X: %d follow-up replies", len(done))
    return done


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
