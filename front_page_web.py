"""
front_page_web.py — engage on front.page (Indian stock community) via the saved
browser session: create a post, comment on posts, and upvote.

front.page is an inline feed (no per-post URLs we can rely on), so this does
discovery + action in one session, like the X like/reply flows. Posts/comments
are deduped by a hash of the post text so nothing repeats.

Selectors (mapped from the live logged-in site):
  - composer:  first <textarea> on home -> click -> editable "Share your insights..."
               -> type -> button "Submit"
  - comment:   a post's button labelled "Comment" -> "Add a comment..." textarea
               -> type -> "Submit"
  - upvote:    a post's button labelled "Vote"
"""

from __future__ import annotations

import time
import random
import hashlib

import common
from common import log

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _h(text: str) -> str:
    return hashlib.md5((text or "").strip().lower()[:200].encode()).hexdigest()[:12]


def _click(el) -> None:
    try:
        el.click(timeout=6000)
    except Exception:  # noqa: BLE001
        el.evaluate("e => e.click()")


def _buttons_labelled(page, label: str):
    return [b for b in page.query_selector_all("button")
            if (b.inner_text() or "").strip() == label]


def _post_text_of(btn) -> str:
    """Walk up from an action button to the post container and grab its text."""
    try:
        return btn.evaluate(
            "b => { let e=b; for (let i=0;i<8;i++){ if(e.parentElement){ e=e.parentElement;"
            " const t=(e.innerText||'').trim(); if (t.length>80) return t; } } return (e.innerText||'').trim(); }"
        )
    except Exception:  # noqa: BLE001
        return ""


def _comment_box(page):
    for ta in page.query_selector_all("textarea"):
        ph = (ta.get_attribute("placeholder") or "").lower()
        if "comment" in ph and ta.is_visible():
            return ta
    vis = [t for t in page.query_selector_all("textarea") if t.is_visible()]
    return vis[-1] if vis else None


def _find_submit(page):
    for sel in ['button:has-text("Submit")', 'button:has-text("Reply")']:
        el = page.query_selector(sel)
        if el and el.is_visible() and el.is_enabled():
            return el
    return None


def _comment_submit(box):
    """The comment submit is a button labelled 'Comment'/'Post' INSIDE the composer
    container (near the textarea), not the far-away comment-count toggle."""
    try:
        h = box.evaluate_handle(
            "el => { let c=el; for (let i=0;i<4;i++){ if(c.parentElement) c=c.parentElement; }"
            " const bs=[...c.querySelectorAll('button')];"
            " return bs.find(b => /^(comment|post|submit|reply|send)$/i.test((b.innerText||'').trim())) || null; }"
        )
        return h.as_element()
    except Exception:  # noqa: BLE001
        return None


_CHROME = ("No Comments Yet", "Sort by", "Discover more", "Add a comment")


def _create_post(page, text: str) -> None:
    ta = page.query_selector("textarea")
    if not ta:
        raise RuntimeError("front.page: no composer textarea found")
    _click(ta)
    time.sleep(2)
    # Clicking the composer opens a club-picker popper; choose "Post to Profile"
    # as the destination so the editor becomes usable and Submit can enable.
    ptp = page.query_selector('button:has-text("Post to Profile")')
    if ptp:
        _click(ptp)
        time.sleep(1)
    # Focus the editable composer and type.
    box = None
    for t in page.query_selector_all("textarea"):
        if "insight" in (t.get_attribute("placeholder") or "").lower() and t.is_visible():
            box = t
            break
    box = box or page.query_selector("textarea")
    _click(box)
    page.keyboard.type(text, delay=5)
    time.sleep(1)
    btn = page.query_selector('button:has-text("Submit")')
    if not btn or btn.evaluate("b => b.disabled"):
        raise RuntimeError("front.page: Submit stayed disabled (composer/club-picker)")
    _click(btn)
    time.sleep(4)


def run_engagement(config: dict) -> list[tuple[str, str, str]]:
    """Create a post, comment, and upvote on front.page. Returns [(action, title, url)]."""
    fp = config.get("front_page", {})
    if not fp.get("enabled", True):
        return []
    if not common.FRONTPAGE_STATE_PATH.exists():
        log.warning("front.page: no session — run `python front_page_login.py` first.")
        return []

    from playwright.sync_api import sync_playwright

    base = fp.get("base_url", "https://front.page").rstrip("/")
    headless = fp.get("headless", False)
    posted = common.load_posted()
    local: set = set()
    done: list[tuple[str, str, str]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=BROWSER_UA, storage_state=str(common.FRONTPAGE_STATE_PATH)
        )
        page = context.new_page()
        page.set_default_timeout(30000)
        try:
            page.goto(base + "/", wait_until="domcontentloaded", timeout=40000)
            time.sleep(6)
            if page.query_selector('button:has-text("Login")') is not None:
                raise RuntimeError("Not logged into front.page. Run `python front_page_login.py`.")

            # 1) UPVOTES (engagement first; posting is the fragile bit, done last)
            target_up = fp.get("upvotes_per_run", 0)
            attempts = 0
            up = 0
            while up < target_up and attempts < target_up * 6:
                attempts += 1
                acted = False
                for vb in _buttons_labelled(page, "Vote"):
                    txt = _post_text_of(vb)
                    key = "fpv:" + _h(txt)
                    if not txt or key in posted or key in local:
                        continue
                    try:
                        _click(vb)
                    except Exception as e:  # noqa: BLE001
                        log.warning("front.page upvote failed: %s", e)
                        continue
                    local.add(key)
                    common.add_posted(key)
                    done.append(("fp_upvote", txt[:60], base + "/"))
                    up += 1
                    acted = True
                    time.sleep(random.uniform(3, 8))
                    break
                if not acted:
                    break

            # 3) COMMENTS
            target_c = fp.get("comments_per_run", 0)
            attempts = 0
            made = 0
            while made < target_c and attempts < target_c * 6:
                attempts += 1
                acted = False
                for cb in _buttons_labelled(page, "Comment"):
                    txt = _post_text_of(cb)
                    key = "fpc:" + _h(txt)
                    if not txt or any(m in txt for m in _CHROME) or key in posted or key in local:
                        continue
                    try:
                        draft = common.draft_frontpage_comment(txt, config)
                        _click(cb)
                        time.sleep(3)
                        box = _comment_box(page)
                        if not box:
                            raise RuntimeError("comment box not found")
                        _click(box)
                        page.keyboard.type(draft, delay=4)
                        time.sleep(1)
                        sb = _comment_submit(box) or _find_submit(page)
                        if not sb:
                            raise RuntimeError("comment submit not found")
                        _click(sb)
                        time.sleep(3)
                    except Exception as e:  # noqa: BLE001
                        log.warning("front.page comment failed: %s", e)
                        local.add(key)  # don't retry this same post this run
                        acted = True
                        break
                    local.add(key)
                    common.add_posted(key)
                    common.log_post("frontpage-comment", base + "/", draft)
                    done.append(("fp_comment", txt[:60], base + "/"))
                    made += 1
                    acted = True
                    time.sleep(random.uniform(4, 10))
                    break
                if not acted:
                    break

            # 3) CREATE POST (last — fragile: front.page opens a club-picker popper)
            for _ in range(fp.get("posts_per_run", 0)):
                try:
                    text = common.draft_frontpage_post(config)
                    _create_post(page, text)
                    common.log_post("frontpage", base + "/", text)
                    done.append(("fp_post", text[:70], base + "/"))
                    log.info("front.page: posted an update")
                except Exception as e:  # noqa: BLE001
                    log.warning("front.page post failed (club-picker/composer): %s", e)
        finally:
            context.close()
            browser.close()

    log.info("front.page: %d actions done", len(done))
    return done
