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


def _save_card(action: str, title: str, text: str, url: str) -> None:
    """Persist a front.page action as a 'posted' card so the dashboard shows it."""
    try:
        drafts = common.load_drafts()
        cid = "frontpage_" + hashlib.md5((action + title).encode()).hexdigest()[:12]
        if any(d.get("id") == cid for d in drafts):
            return
        ctx = {"comment": "comment on a post", "upvote": "upvoted a post",
               "post": "original post"}.get(action, action)
        drafts.append({
            "id": cid, "platform": "frontpage", "action": action, "topic": "front.page",
            "title": title[:120], "url": url, "context": ctx, "opp_score": 8,
            "mentions_intrynsic": "intrynsic" in (text or "").lower(),
            "draft": text or title, "posted": True, "posted_url": url,
            "posted_at": common.now_str(), "approved": True,
        })
        common.save_drafts(drafts)
    except Exception as e:  # noqa: BLE001
        log.warning("front.page card save failed: %s", e)


def _create_post(page, text: str, destination: str = "Post to Profile") -> None:
    ta = page.query_selector("textarea")
    if not ta:
        raise RuntimeError("front.page: no composer textarea found")
    _click(ta)
    time.sleep(2)
    # The destination picker is a Radix combobox of <div role="option">. Select the
    # requested destination (a club name, or "Post to Profile"); fall back to profile.
    chose = page.evaluate(
        "(dest) => { const opts=[...document.querySelectorAll('[role=option]')];"
        " let o=opts.find(x=>(x.innerText||'').trim().startsWith(dest));"
        " if(!o) o=opts.find(x=>(x.innerText||'').trim().startsWith('Post to Profile'));"
        " if (o) { o.click(); return (o.innerText||'').trim().slice(0,30); } return false; }",
        destination,
    )
    if not chose:
        raise RuntimeError("front.page: destination option not found")
    log.info("front.page: posting to '%s'", chose.split(chr(10))[0])
    time.sleep(2)
    box = None
    for t in page.query_selector_all("textarea"):
        if t.is_visible() and t.is_editable():
            box = t
            break
    box = box or ta
    # Set the value the React-compatible way (native setter + input event), so it
    # registers even though the picker keeps stealing focus from keyboard typing.
    box.evaluate(
        "(el, val) => { const d = Object.getOwnPropertyDescriptor("
        "window.HTMLTextAreaElement.prototype, 'value'); d.set.call(el, val);"
        " el.dispatchEvent(new Event('input', {bubbles: true}));"
        " el.dispatchEvent(new Event('change', {bubbles: true})); }",
        text,
    )
    time.sleep(1)
    ok = page.evaluate(
        "() => { const b=[...document.querySelectorAll('button')]"
        ".find(x=>/^Submit$/.test((x.innerText||'').trim()) && !x.disabled);"
        " if (b) { b.click(); return true; } return false; }"
    )
    if not ok:
        raise RuntimeError("front.page: Submit stayed disabled (composer/picker)")
    time.sleep(4)


def frontpage_join_clubs(config: dict) -> list[tuple[str, str]]:
    """Auto-join (follow) the configured finance clubs that aren't joined yet.
    Returns [(name, url)] of clubs newly joined this run."""
    from playwright.sync_api import sync_playwright

    fp = config.get("front_page", {})
    n = fp.get("join_clubs_per_run", 0)
    clubs = fp.get("clubs", [])
    if not n or not clubs or not common.FRONTPAGE_STATE_PATH.exists():
        return []
    base = fp.get("base_url", "https://front.page").rstrip("/")
    posted = common.load_posted()
    joined: list[tuple[str, str]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=fp.get("headless", False))
        context = browser.new_context(
            user_agent=BROWSER_UA, storage_state=str(common.FRONTPAGE_STATE_PATH))
        page = context.new_page()
        page.set_default_timeout(30000)
        try:
            page.goto(base + "/", wait_until="domcontentloaded", timeout=40000)
            time.sleep(6)
            if page.query_selector('button:has-text("Login")') is not None:
                raise RuntimeError("Not logged into front.page.")
            hrefs = page.evaluate(
                "(names) => { const out={};"
                " for (const a of document.querySelectorAll('a[href]')) {"
                "  const t=(a.innerText||'').trim();"
                "  for (const nm of names){ if (t===nm || t.startsWith(nm)) out[nm]=a.getAttribute('href'); } }"
                " return out; }",
                clubs,
            )
            count = 0
            for name in clubs:
                if count >= n:
                    break
                if ("fpclub:" + name) in posted:
                    continue
                href = hrefs.get(name)
                if not href:
                    common.add_posted("fpclub:" + name)
                    continue
                url = href if href.startswith("http") else base + href
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(5)
                    clicked = page.evaluate(
                        "() => { const el=[...document.querySelectorAll('button,a,span,div')]"
                        ".find(e=>/^(Join|Follow)$/.test((e.innerText||'').trim()));"
                        " if (el) { el.click(); return true; } return false; }"
                    )
                    common.add_posted("fpclub:" + name)
                    common.note_recent("frontpage_club", name, keep=50)
                    if clicked:
                        joined.append((name, url))
                        log.info("Joined front.page club: %s", name)
                        count += 1
                        time.sleep(random.uniform(4, 9))
                except Exception as e:  # noqa: BLE001
                    log.warning("front.page club join failed (%s): %s", name, e)
        finally:
            context.close()
            browser.close()
    log.info("front.page: joined %d new clubs", len(joined))
    return joined


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

            # Engage WITHIN communities: visit up to N clubs; per-club quota of
            # upvotes + comments in EACH (so engagement spreads across clubs).
            clubs = list(dict.fromkeys((fp.get("clubs") or []) + common.recent("frontpage_club", 50)))
            feeds = []
            if clubs:
                hrefs = page.evaluate(
                    "(names) => { const out=[];"
                    " for (const a of document.querySelectorAll('a[href]')) {"
                    "  const t=(a.innerText||'').trim();"
                    "  for (const n of names){ if (t===n || t.startsWith(n)) out.push(a.getAttribute('href')); } }"
                    " return [...new Set(out)]; }",
                    clubs,
                )
                feeds = [(h if h.startswith("http") else base + h) for h in hrefs]
            random.shuffle(feeds)
            if not feeds:
                feeds = [base + "/"]
            feeds = feeds[: fp.get("club_targets_per_run", 6)]

            up_per = fp.get("upvotes_per_club", 0)
            cm_per = fp.get("comments_per_club", 0)
            for feed in feeds:
                try:
                    page.goto(feed, wait_until="domcontentloaded", timeout=40000)
                    time.sleep(4)
                    page.mouse.wheel(0, 1200)
                    time.sleep(2)
                except Exception as e:  # noqa: BLE001
                    log.warning("front.page club open failed (%s): %s", feed, e)
                    continue

                # upvotes in THIS club
                u = 0
                for vb in _buttons_labelled(page, "Vote"):
                    if u >= up_per:
                        break
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
                    done.append(("fp_upvote", txt[:60], feed))
                    _save_card("upvote", txt, "", feed)
                    u += 1
                    time.sleep(random.uniform(2, 5))

                # comments in THIS club
                c = 0
                attempts = 0
                while c < cm_per and attempts < cm_per * 6:
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
                            local.add(key)
                            acted = True
                            break
                        local.add(key)
                        common.add_posted(key)
                        common.log_post("frontpage-comment", feed, draft)
                        done.append(("fp_comment", txt[:60], feed))
                        _save_card("comment", txt, draft, feed)
                        c += 1
                        acted = True
                        time.sleep(random.uniform(3, 7))
                        break
                    if not acted:
                        break

            # 3) CREATE POST (last). Reload home first so the composer is the
            # first textarea again (commenting/upvoting mutates the page).
            clubs = list(dict.fromkeys((fp.get("clubs") or []) + common.recent("frontpage_club", 50)))
            for _ in range(fp.get("posts_per_run", 0)):
                try:
                    page.goto(base + "/", wait_until="domcontentloaded", timeout=40000)
                    time.sleep(5)
                    # Rotate destination: a joined club, or your profile.
                    dest = random.choice(["Post to Profile"] + clubs) if clubs else "Post to Profile"
                    club = None if dest == "Post to Profile" else dest
                    text = common.draft_frontpage_post(config, club=club)
                    _create_post(page, text, destination=dest)
                    common.log_post("frontpage", base + "/", text)
                    common.note_recent("frontpage_post", text)
                    done.append(("fp_post", f"[{dest}] {text[:60]}", base + "/"))
                    _save_card("post", text, text, base + "/")
                    log.info("front.page: posted to '%s'", dest)
                except Exception as e:  # noqa: BLE001
                    log.warning("front.page post failed (club-picker/composer): %s", e)
        finally:
            context.close()
            browser.close()

    log.info("front.page: %d actions done", len(done))
    return done
