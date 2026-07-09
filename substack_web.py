"""
substack_web.py — engage on Substack (Notes) via the saved browser session:
post a Note, reply to Notes, like + restack Notes, and follow publications.

Substack's Notes feed is an inline feed (no reliable per-note URLs from the list),
so this does discovery + action in one session, like the X like/reply and
front.page flows. Notes are deduped by a hash of the note text so nothing repeats.

Selectors are mapped heuristically from the logged-in site and are deliberately
forgiving (aria-label + text + JS fallbacks), because Substack tweaks its markup:
  - composer:  "Write a note" / first contenteditable on /notes -> type -> "Post"
  - like:      a note's button whose aria-label/text contains "Like"
  - restack:   a note's button whose aria-label/text contains "Restack"
  - reply:     a note's button whose aria-label/text contains "Reply"/"Comment"
  - follow:    a publication/profile page's "Subscribe" or "Follow" button

Like X/Twitter, Substack is bot-hostile (its login CAPTCHA won't even render in
Playwright's bundled Chromium), so this uses a PERSISTENT REAL-Chrome profile
(substack_profile/). Run `python substack_login.py` once, log in by hand (email
code / Google / CAPTCHA all work in real Chrome), and the session sticks.
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


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _h(text: str) -> str:
    return hashlib.md5((text or "").strip().lower()[:200].encode()).hexdigest()[:12]


def _click(el) -> None:
    try:
        el.click(timeout=6000)
    except Exception:  # noqa: BLE001
        try:
            el.evaluate("e => e.click()")
        except Exception:  # noqa: BLE001
            pass


def substack_context(p, headless: bool):
    """A persistent, low-detection browser context for Substack.

    Uses a real-Chrome profile (channel="chrome") with the automation flags off so
    Substack's login CAPTCHA renders and the session persists across runs. Falls
    back to bundled Chromium only if real Chrome isn't installed. Returns a single
    persistent context (no separate browser object — close the context to quit).
    """
    args = ["--disable-blink-features=AutomationControlled"]
    ignore = ["--enable-automation"]
    common.SUBSTACK_PROFILE_DIR.mkdir(exist_ok=True)
    udir = str(common.SUBSTACK_PROFILE_DIR)
    try:
        return p.chromium.launch_persistent_context(
            udir, channel="chrome", headless=headless, args=args, ignore_default_args=ignore
        )
    except Exception as e:  # noqa: BLE001 - Chrome not installed / not found
        log.warning("Real Chrome unavailable (%s) — falling back to bundled Chromium.", e)
        return p.chromium.launch_persistent_context(
            udir, headless=headless, args=args, ignore_default_args=ignore
        )


def _session_ready() -> bool:
    """True if a persistent Substack profile exists (i.e. we've logged in once)."""
    d = common.SUBSTACK_PROFILE_DIR
    return d.exists() and any(d.iterdir())


def _is_logged_out(page) -> bool:
    """True if the page is showing a sign-in wall rather than the feed."""
    try:
        return bool(page.evaluate(
            "() => { const t=(document.body.innerText||'');"
            " const hasSignInBtn = [...document.querySelectorAll('a,button')]"
            ".some(e => /^(sign in|log in)$/i.test((e.innerText||'').trim()));"
            " const hasComposer = !!document.querySelector('[contenteditable=true]')"
            "   || /write a note/i.test(t);"
            " return hasSignInBtn && !hasComposer; }"
        ))
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# Programmatic sign-in (email + password). Best-effort; magic-link accounts
# should use the manual `substack_login.py` pause instead.
# --------------------------------------------------------------------------- #
def programmatic_login(page, email: str, password: str) -> bool:
    """Try to sign into Substack with email + password. Returns True on apparent
    success. Safe to call even if it can't complete (manual login can follow)."""
    try:
        page.goto("https://substack.com/sign-in", wait_until="domcontentloaded",
                  timeout=40000)
        time.sleep(3)
        # 1) email
        email_box = page.query_selector('input[type="email"]') or \
            page.query_selector('input[name="email"]')
        if email_box:
            _click(email_box)
            email_box.fill(email)
            time.sleep(1)
        # 2) reveal the password form (Substack defaults to magic-link)
        page.evaluate(
            "() => { const el=[...document.querySelectorAll('a,button,span')]"
            ".find(e=>/sign in with password|use password|log in with password/i"
            ".test((e.innerText||'').trim())); if (el) el.click(); }"
        )
        time.sleep(2)
        pw_box = page.query_selector('input[type="password"]')
        if not pw_box or not password:
            log.info("Substack: password form not available (likely magic-link).")
            return False
        _click(pw_box)
        pw_box.fill(password)
        time.sleep(1)
        page.evaluate(
            "() => { const b=[...document.querySelectorAll('button')]"
            ".find(x=>/^(continue|sign in|log in)$/i.test((x.innerText||'').trim())"
            " && !x.disabled); if (b) b.click(); }"
        )
        time.sleep(6)
        return not _is_logged_out(page)
    except Exception as e:  # noqa: BLE001
        log.warning("Substack programmatic login failed: %s", e)
        return False


# --------------------------------------------------------------------------- #
# Note discovery / actions
# --------------------------------------------------------------------------- #
def _note_text_of(btn) -> str:
    """Walk up from an action button to the note container and grab its text."""
    try:
        return btn.evaluate(
            "b => { let e=b; for (let i=0;i<10;i++){ if(e.parentElement){ e=e.parentElement;"
            " const t=(e.innerText||'').trim(); if (t.length>60) return t; } } "
            " return (e.innerText||'').trim(); }"
        )
    except Exception:  # noqa: BLE001
        return ""


def _action_buttons(page, *needles: str):
    """All visible buttons whose aria-label or text contains any of the needles."""
    out = []
    for b in page.query_selector_all("button, a[role=button]"):
        try:
            if not b.is_visible():
                continue
            label = ((b.get_attribute("aria-label") or "") + " " +
                     (b.inner_text() or "")).lower()
        except Exception:  # noqa: BLE001
            continue
        if any(n in label for n in needles):
            out.append(b)
    return out


def _open_composer(page, notes_url: str):
    """Open the Note composer and return its editable element.

    The trigger is a button labelled "New post" (its visible text is the
    placeholder "What's on your mind?"). We must click that exact button — a
    loose innerText search for the placeholder matches nested nav containers
    too, and clicking one of those navigates away from the feed. The editor
    that opens is a focused tiptap/ProseMirror contenteditable div.
    """
    page.goto(notes_url, wait_until="domcontentloaded", timeout=40000)
    time.sleep(5)
    trigger = page.query_selector('button[aria-label="New post"]')
    if not trigger:
        # Fallback: a button whose OWN text is the placeholder (not a container).
        trigger = page.evaluate_handle(
            "() => [...document.querySelectorAll('button')]"
            ".find(b => /^what.?s on your mind\\??$/i.test((b.innerText||'').trim()))"
        ).as_element()
    if trigger:
        _click(trigger)
        time.sleep(3)
    # Prefer the just-focused composer; fall back to any visible editable.
    for sel in (".tiptap.ProseMirror.ProseMirror-focused",
                ".tiptap.ProseMirror[contenteditable='true']"):
        ed = page.query_selector(sel)
        try:
            if ed and ed.is_visible():
                return ed
        except Exception:  # noqa: BLE001
            pass
    for ed in page.query_selector_all('[contenteditable="true"], textarea'):
        try:
            if ed.is_visible():
                return ed
        except Exception:  # noqa: BLE001
            continue
    return None


def post_note(page, text: str, notes_url: str) -> None:
    """Compose and publish a single Note."""
    ed = _open_composer(page, notes_url)
    if not ed:
        raise RuntimeError("Substack: note composer not found")
    _click(ed)
    time.sleep(1)
    try:
        page.keyboard.type(text, delay=8)
    except Exception:  # noqa: BLE001
        # contenteditable fallback
        ed.evaluate("(el, val) => { el.focus();"
                    " document.execCommand('insertText', false, val); }", text)
    time.sleep(1)
    # The Post button stays disabled until the editor registers the text, so
    # poll for a few seconds rather than checking once.
    ok = False
    for _ in range(10):
        ok = page.evaluate(
            "() => { const b=[...document.querySelectorAll('button')]"
            ".find(x=>/^(post|publish|send)$/i.test((x.innerText||'').trim()) && !x.disabled);"
            " if (b) { b.click(); return true; } return false; }"
        )
        if ok:
            break
        time.sleep(1)
    if not ok:
        raise RuntimeError("Substack: Post button stayed disabled/not found")
    time.sleep(4)


_CHROME = ("Write a note", "Subscribe", "Share", "Restack", "comments")


def _save_card(action: str, title: str, text: str, url: str) -> None:
    """Persist a Substack action as a 'posted' card so the dashboard shows it."""
    try:
        drafts = common.load_drafts()
        cid = "substack_" + hashlib.md5((action + title).encode()).hexdigest()[:12]
        if any(d.get("id") == cid for d in drafts):
            return
        ctx = {"comment": "reply to a note", "like": "liked a note",
               "restack": "restacked a note", "note": "original note",
               "follow": "followed a publication"}.get(action, action)
        drafts.append({
            "id": cid, "platform": "substack", "action": action, "topic": "Substack",
            "title": title[:120], "url": url, "context": ctx, "opp_score": 8,
            "mentions_intrynsic": "intrynsic" in (text or "").lower(),
            "draft": text or title, "posted": True, "posted_url": url,
            "posted_at": common.now_str(), "approved": True,
        })
        common.save_drafts(drafts)
    except Exception as e:  # noqa: BLE001
        log.warning("Substack card save failed: %s", e)


# --------------------------------------------------------------------------- #
# Follow relevant publications (capped)
# --------------------------------------------------------------------------- #
def follow_publications(config: dict) -> list[tuple[str, str]]:
    """Visit configured publications and follow/subscribe (free) the ones not yet
    followed. Returns [(name, url)] of those newly followed this run."""
    from playwright.sync_api import sync_playwright

    sb = config.get("substack", {})
    n = sb.get("follow_per_run", 0)
    pubs = sb.get("publications", [])
    if not n or not pubs or not _session_ready():
        return []
    posted = common.load_posted()
    headless = sb.get("headless", False)
    followed: list[tuple[str, str]] = []

    with sync_playwright() as p:
        context = substack_context(p, headless)
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(30000)
        try:
            count = 0
            for url in pubs:
                if count >= n:
                    break
                key = "subpub:" + _h(url)
                if key in posted:
                    continue
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=40000)
                    time.sleep(5)
                    clicked = page.evaluate(
                        "() => { const el=[...document.querySelectorAll('button,a')]"
                        ".find(e=>/^(subscribe|follow|subscribe for free)$/i"
                        ".test((e.innerText||'').trim())); if (el){ el.click(); return true; }"
                        " return false; }"
                    )
                    common.add_posted(key)
                    if clicked:
                        # A free-subscribe modal may appear; try to confirm it.
                        time.sleep(3)
                        page.evaluate(
                            "() => { const el=[...document.querySelectorAll('button')]"
                            ".find(e=>/^(subscribe|continue|confirm|none for now|"
                            "subscribe for free)$/i.test((e.innerText||'').trim()));"
                            " if (el) el.click(); }"
                        )
                        followed.append((url, url))
                        log.info("Substack: followed %s", url)
                        count += 1
                        time.sleep(random.uniform(4, 9))
                except Exception as e:  # noqa: BLE001
                    log.warning("Substack follow failed (%s): %s", url, e)
        finally:
            context.close()
    log.info("Substack: followed %d new publication(s)", len(followed))
    return followed


# --------------------------------------------------------------------------- #
# Main engagement: like + restack + reply on Notes, then post a Note.
# --------------------------------------------------------------------------- #
def run_engagement(config: dict) -> list[tuple[str, str, str]]:
    """Like, restack, reply on Notes and post original Note(s).
    Returns [(action, title, url)]."""
    sb = config.get("substack", {})
    if not sb.get("enabled", True):
        return []
    if not _session_ready():
        log.warning("Substack: no session — run `python substack_login.py` first.")
        return []

    from playwright.sync_api import sync_playwright

    notes_url = sb.get("notes_url", "https://substack.com/notes")
    headless = sb.get("headless", False)
    posted = common.load_posted()
    local: set = set()
    done: list[tuple[str, str, str]] = []

    like_n = sb.get("likes_per_run", 0)
    restack_n = sb.get("restacks_per_run", 0)
    comment_n = sb.get("comments_per_run", 0)

    with sync_playwright() as p:
        context = substack_context(p, headless)
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(30000)
        try:
            page.goto(notes_url, wait_until="domcontentloaded", timeout=40000)
            time.sleep(6)
            if _is_logged_out(page):
                raise RuntimeError("Not logged into Substack. Run `python substack_login.py`.")

            # Scroll to load a pool of notes.
            for _ in range(4):
                page.mouse.wheel(0, 1600)
                time.sleep(1.5)

            # 1) LIKES
            liked = 0
            for lb in _action_buttons(page, "like"):
                if liked >= like_n:
                    break
                txt = _note_text_of(lb)
                key = "sblike:" + _h(txt)
                if not txt or key in posted or key in local:
                    continue
                _click(lb)
                local.add(key)
                common.add_posted(key)
                done.append(("sb_like", txt[:60], notes_url))
                _save_card("like", txt, "", notes_url)
                liked += 1
                time.sleep(random.uniform(2, 5))

            # 2) RESTACKS
            restacked = 0
            for rb in _action_buttons(page, "restack"):
                if restacked >= restack_n:
                    break
                txt = _note_text_of(rb)
                key = "sbrestack:" + _h(txt)
                if not txt or key in posted or key in local:
                    continue
                _click(rb)
                time.sleep(2)
                # A popover may ask to confirm "Restack" (vs "Restack with comment").
                page.evaluate(
                    "() => { const el=[...document.querySelectorAll('button,div[role=menuitem]')]"
                    ".find(e=>/^restack$/i.test((e.innerText||'').trim())); if (el) el.click(); }"
                )
                local.add(key)
                common.add_posted(key)
                done.append(("sb_restack", txt[:60], notes_url))
                _save_card("restack", txt, "", notes_url)
                restacked += 1
                time.sleep(random.uniform(2, 5))

            # 3) REPLIES (comments on notes)
            commented = 0
            attempts = 0
            reply_buttons = _action_buttons(page, "reply", "comment")
            for cb in reply_buttons:
                if commented >= comment_n or attempts >= comment_n * 6:
                    break
                attempts += 1
                txt = _note_text_of(cb)
                key = "sbcomment:" + _h(txt)
                if not txt or any(m in txt for m in _CHROME) or key in posted or key in local:
                    continue
                try:
                    draft = common.draft_substack_comment(txt, config)
                    _click(cb)
                    time.sleep(3)
                    box = None
                    for ed in page.query_selector_all('[contenteditable="true"], textarea'):
                        if ed.is_visible():
                            box = ed
                    if not box:
                        raise RuntimeError("reply box not found")
                    _click(box)
                    page.keyboard.type(draft, delay=6)
                    time.sleep(1)
                    sent = page.evaluate(
                        "() => { const b=[...document.querySelectorAll('button')]"
                        ".find(x=>/^(reply|post|send|comment)$/i.test((x.innerText||'').trim())"
                        " && !x.disabled); if (b){ b.click(); return true; } return false; }"
                    )
                    if not sent:
                        raise RuntimeError("reply submit not found")
                    time.sleep(3)
                except Exception as e:  # noqa: BLE001
                    log.warning("Substack reply failed: %s", e)
                    local.add(key)
                    continue
                local.add(key)
                common.add_posted(key)
                common.log_post("substack-comment", notes_url, draft)
                done.append(("sb_comment", txt[:60], notes_url))
                _save_card("comment", txt, draft, notes_url)
                commented += 1
                time.sleep(random.uniform(3, 7))

            # 4) POST original Note(s) last (composing reloads/mutates the feed).
            for _ in range(sb.get("notes_per_run", 0)):
                try:
                    text = common.draft_substack_note(config)
                    post_note(page, text, notes_url)
                    common.log_post("substack", notes_url, text)
                    common.note_recent("substack_note", text)
                    done.append(("sb_note", text[:60], notes_url))
                    _save_card("note", text, text, notes_url)
                    log.info("Substack: posted a note")
                except Exception as e:  # noqa: BLE001
                    log.warning("Substack note post failed: %s", e)
        finally:
            context.close()

    log.info("Substack: %d actions done", len(done))
    return done
