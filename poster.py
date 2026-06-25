"""
poster.py — Flask server (localhost:5050) + the auto-poster.

Serves the approval dashboard, exposes a small JSON API, and posts approved
drafts to Quora (Playwright) and X/Twitter. Posting runs in a background
thread and is staggered with randomized delays; the dashboard polls /api/status
to show a green tick / red cross per draft.

Usage:
    python poster.py
Then open http://localhost:5050
"""

from __future__ import annotations

import time
import random
import threading
import datetime as dt

from flask import Flask, jsonify, request, send_from_directory

import common
from common import log

app = Flask(__name__, static_folder=None)

# A real browser UA so Quora doesn't serve the stripped "headless" page (which
# hides the Answer button and editor).
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# --------------------------------------------------------------------------- #
# Posting status (shared with the dashboard via /api/status)
# --------------------------------------------------------------------------- #
_status_lock = threading.Lock()
_post_thread: threading.Thread | None = None


def _load_status() -> dict:
    return common._read_json(common.POST_STATUS_PATH, {"running": False, "results": {}})


def _save_status(status: dict) -> None:
    # Windows can throw a transient PermissionError if the dashboard is reading
    # the file at the same moment — never let that kill the posting thread.
    with _status_lock:
        for _ in range(5):
            try:
                common._write_json(common.POST_STATUS_PATH, status)
                return
            except OSError:
                time.sleep(0.2)


def _set_result(state: dict, draft_id: str, **fields) -> None:
    state["results"].setdefault(draft_id, {})
    state["results"][draft_id].update(fields)
    _save_status(state)


# --------------------------------------------------------------------------- #
# Quora posting (Playwright)
# --------------------------------------------------------------------------- #
def _quora_login(page, context) -> None:
    """Log into Quora and persist cookies, if not already logged in."""
    page.goto("https://www.quora.com/", wait_until="domcontentloaded")
    time.sleep(3)
    # If a login form is present, fill it.
    if page.query_selector('input[name="email"]'):
        log.info("Logging into Quora...")
        page.fill('input[name="email"]', common.env("QUORA_EMAIL"))
        page.fill('input[name="password"]', common.env("QUORA_PASSWORD"))
        # The login button text/markup varies; try a few options.
        for sel in [
            'div.q-click-wrapper:has-text("Login")',
            'button:has-text("Login")',
            'input[type="submit"]',
        ]:
            btn = page.query_selector(sel)
            if btn:
                btn.click()
                break
        time.sleep(6)
        context.storage_state(path=str(common.QUORA_STATE_PATH))
        log.info("Quora session saved.")


def _robust_click(target) -> None:
    """Click that tolerates Quora's sticky header intercepting pointer events.

    Falls back to a JS .click(), which ignores the overlay entirely.
    """
    try:
        target.click(timeout=8000)
    except Exception:  # noqa: BLE001
        target.evaluate("el => el.click()")


def _post_quora(draft: dict, config: dict) -> str:
    """Navigate to the question, open the answer editor, paste, submit. Returns URL."""
    from playwright.sync_api import sync_playwright

    common.require_env("QUORA_EMAIL", "QUORA_PASSWORD")
    # Headed mode (quora_headless=false) often clears Cloudflare Turnstile when
    # headless does not. A window will appear while answering.
    headless = config.get("posting", {}).get("quora_headless", True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        kwargs = {"user_agent": BROWSER_UA}
        if common.QUORA_STATE_PATH.exists():
            kwargs["storage_state"] = str(common.QUORA_STATE_PATH)
        context = browser.new_context(**kwargs)
        page = context.new_page()
        page.set_default_timeout(30000)  # never hang forever on a missing element

        try:
            _quora_login(page, context)

            qurl = draft["url"].replace("/unanswered/", "/")
            log.info("Quora: opening question page")
            page.goto(qurl, wait_until="domcontentloaded", timeout=30000)
            time.sleep(4)

            # Click an "Answer" affordance. Quora sometimes serves a partial page
            # (with a "Try again" button) on a cold load, so reload and retry.
            answer_selectors = [
                'button:has-text("Answer")',
                'div.q-click-wrapper:has-text("Answer")',
                'div[role="button"]:has-text("Answer")',
            ]
            clicked = False
            for attempt in range(3):
                page.mouse.wheel(0, 600)  # nudge lazy rendering
                time.sleep(1)
                for sel in answer_selectors:
                    try:
                        el = page.wait_for_selector(sel, timeout=7000, state="visible")
                    except Exception:  # noqa: BLE001
                        el = None
                    if el:
                        _robust_click(el)
                        clicked = True
                        break
                if clicked:
                    break
                log.info("Quora: Answer button not found, reloading (attempt %d/3)", attempt + 1)
                page.reload(wait_until="domcontentloaded", timeout=30000)
                time.sleep(5)
            if not clicked:
                raise RuntimeError("Could not find an 'Answer' button on the page")
            time.sleep(3)

            # Type into the rich-text editor.
            editor = None
            for sel in [
                'div.doc[contenteditable="true"]',
                'div[contenteditable="true"]',
                'div.q-text[contenteditable="true"]',
            ]:
                editor = page.query_selector(sel)
                if editor:
                    break
            if not editor:
                raise RuntimeError("Could not find the answer editor")
            try:
                editor.click(timeout=8000)
            except Exception:  # noqa: BLE001
                editor.evaluate("el => el.focus()")
            page.keyboard.type(draft["draft"], delay=2)
            time.sleep(2)

            # Submit ("Post" / "Submit").
            submitted = False
            for sel in [
                'div.q-click-wrapper:has-text("Post")',
                'button:has-text("Post")',
                'div[role="button"]:has-text("Submit")',
                'button:has-text("Submit")',
            ]:
                el = page.query_selector(sel)
                if el:
                    _robust_click(el)
                    submitted = True
                    break
            if not submitted:
                raise RuntimeError("Could not find the Post/Submit button")
            time.sleep(5)

            # Refresh cookies for next time.
            context.storage_state(path=str(common.QUORA_STATE_PATH))
            return qurl
        finally:
            context.close()
            browser.close()


def _quora_browser(config):
    """Open a logged-in Quora browser context. Returns (pw, browser, context, page)."""
    from playwright.sync_api import sync_playwright

    common.require_env("QUORA_EMAIL", "QUORA_PASSWORD")
    headless = config.get("posting", {}).get("quora_headless", True)
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=headless)
    kwargs = {"user_agent": BROWSER_UA}
    if common.QUORA_STATE_PATH.exists():
        kwargs["storage_state"] = str(common.QUORA_STATE_PATH)
    context = browser.new_context(**kwargs)
    page = context.new_page()
    page.set_default_timeout(30000)
    return pw, browser, context, page


def _post_quora_comment(draft: dict, config: dict) -> str:
    """Comment on the top answer of a question. Returns the question URL."""
    qurl = draft["url"].replace("/unanswered/", "/")
    pw, browser, context, page = _quora_browser(config)
    try:
        _quora_login(page, context)
        log.info("Quora: opening question to comment")
        page.goto(qurl, wait_until="domcontentloaded", timeout=30000)
        time.sleep(4)
        page.mouse.wheel(0, 1000)
        time.sleep(2)

        # Open the comment composer on the top answer. Quora's control is a
        # <button aria-label="comment"> / "N comments" (no visible text).
        btn = None
        for sel in ['button[aria-label*="comment" i]',
                    'button:has-text("Comment")',
                    'div[role="button"]:has-text("Comment")']:
            try:
                btn = page.wait_for_selector(sel, timeout=8000, state="visible")
            except Exception:  # noqa: BLE001
                btn = None
            if btn:
                break
        if not btn:
            raise RuntimeError("Could not find a 'Comment' button on the top answer")
        _robust_click(btn)
        time.sleep(3)

        editor = None
        for sel in ['div[contenteditable="true"]', 'div.doc[contenteditable="true"]']:
            try:
                editor = page.wait_for_selector(sel, timeout=8000, state="visible")
            except Exception:  # noqa: BLE001
                editor = None
            if editor:
                break
        if not editor:
            raise RuntimeError("Could not find the comment editor")
        try:
            editor.click(timeout=8000)
        except Exception:  # noqa: BLE001
            editor.evaluate("el => el.focus()")
        page.keyboard.type(draft["draft"], delay=2)
        time.sleep(2)

        submitted = False
        for sel in ['button:has-text("Post")', 'button:has-text("Add Comment")',
                    'div[role="button"]:has-text("Post")',
                    'button[aria-label*="post" i]', 'button[aria-label*="add comment" i]']:
            el = page.query_selector(sel)
            if el:
                _robust_click(el)
                submitted = True
                break
        if not submitted:
            raise RuntimeError("Could not find the comment submit button")
        time.sleep(4)
        context.storage_state(path=str(common.QUORA_STATE_PATH))
        return qurl
    finally:
        context.close()
        browser.close()
        pw.stop()


def _post_quora_question(draft: dict, config: dict) -> str:
    """Ask a new question on Quora. Returns the Quora home URL (no direct link)."""
    pw, browser, context, page = _quora_browser(config)
    try:
        _quora_login(page, context)
        log.info("Quora: opening 'Add question'")
        page.goto("https://www.quora.com/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(4)

        btn = None
        for sel in ['button:has-text("Add question")', 'button:has-text("Add Question")',
                    'div[role="button"]:has-text("Add question")']:
            try:
                btn = page.wait_for_selector(sel, timeout=8000, state="visible")
            except Exception:  # noqa: BLE001
                btn = None
            if btn:
                break
        if not btn:
            raise RuntimeError("Could not find the 'Add question' button")
        _robust_click(btn)
        time.sleep(3)

        box = None
        for sel in ['textarea', 'div[contenteditable="true"]']:
            try:
                box = page.wait_for_selector(sel, timeout=8000, state="visible")
            except Exception:  # noqa: BLE001
                box = None
            if box:
                break
        if not box:
            raise RuntimeError("Could not find the question input box")
        try:
            box.click(timeout=8000)
        except Exception:  # noqa: BLE001
            box.evaluate("el => el.focus()")
        page.keyboard.type(draft["draft"], delay=2)
        time.sleep(2)

        submitted = False
        for sel in ['button:has-text("Add Question")', 'button:has-text("Add question")',
                    'div[role="button"]:has-text("Add Question")']:
            el = page.query_selector(sel)
            if el:
                _robust_click(el)
                submitted = True
                break
        if not submitted:
            raise RuntimeError("Could not find the 'Add Question' submit button")
        time.sleep(4)
        context.storage_state(path=str(common.QUORA_STATE_PATH))
        return "https://www.quora.com/"
    finally:
        context.close()
        browser.close()
        pw.stop()


def quora_followups(config: dict) -> list[tuple[str, str]]:
    """Reply once to people who COMMENTED on / replied to your Quora answers.
    Ignores upvote/follow notifications. Capped via the ledger so it never loops.
    Returns [(snippet, url)]."""
    import hashlib

    qcfg = config.get("quora", {})
    n = qcfg.get("followups_per_run", 0)
    if not n or not common.QUORA_STATE_PATH.exists():
        return []
    posted = common.load_posted()
    done: list[tuple[str, str]] = []
    pw, browser, context, page = _quora_browser(config)
    try:
        _quora_login(page, context)
        page.goto("https://www.quora.com/notifications", wait_until="domcontentloaded", timeout=30000)
        time.sleep(5)
        for _ in range(3):
            page.mouse.wheel(0, 2000)
            time.sleep(1.5)
        # Only comment/reply/mention notifications — never upvotes/follows.
        rows = page.evaluate(
            "() => { const out=[], seen=new Set();"
            " for (const e of document.querySelectorAll('div,span')) {"
            "  const t=(e.innerText||'').trim();"
            "  if(!t||t.length<15||t.length>160) continue;"
            "  if(!/commented on your|replied to your|replied to you|mentioned you/i.test(t)) continue;"
            "  if(/upvoted|started following|followed you/i.test(t)) continue;"
            "  if(seen.has(t)) continue; seen.add(t); out.push(t);"
            "  if(out.length>=15) break; } return out; }"
        )
        targets = []
        for t in rows:
            key = "qfollowup:" + hashlib.md5(t.encode()).hexdigest()[:12]
            if key not in posted:
                targets.append((t, key))
            if len(targets) >= n:
                break

        for text, key in targets:
            try:
                clicked = page.evaluate(
                    "(txt) => { const el=[...document.querySelectorAll('div,span')]"
                    ".find(e=>(e.innerText||'').trim().includes(txt));"
                    " if(!el) return false; (el.closest('a')||el).click(); return true; }",
                    text[:40],
                )
                if not clicked:
                    raise RuntimeError("notification row not clickable")
                time.sleep(5)
                draft = common.draft_quora_comment(text, "", False, config)
                box = None
                for ta in page.query_selector_all("textarea"):
                    if "comment" in (ta.get_attribute("placeholder") or "").lower() and ta.is_visible():
                        box = ta
                        break
                if not box:
                    raise RuntimeError("comment composer not found")
                _robust_click(box)
                page.keyboard.type(draft, delay=6)
                time.sleep(1)
                submitted = False
                for sel in ['button:has-text("Post")', 'button:has-text("Comment")',
                            'button:has-text("Add Comment")', 'button[aria-label*="post" i]']:
                    el = page.query_selector(sel)
                    if el and el.is_visible() and el.is_enabled():
                        _robust_click(el)
                        submitted = True
                        break
                if not submitted:
                    raise RuntimeError("comment submit not found")
                time.sleep(3)
                common.add_posted(key)
                common.log_post("quora-followup", page.url, draft)
                done.append((text[:60], page.url))
                page.goto("https://www.quora.com/notifications",
                          wait_until="domcontentloaded", timeout=30000)
                time.sleep(4)
            except Exception as e:  # noqa: BLE001
                log.warning("Quora follow-up failed: %s", e)
    finally:
        context.close()
        browser.close()
        pw.stop()

    log.info("Quora: %d follow-up replies", len(done))
    return done


def quora_join_spaces(config: dict) -> list[tuple[str, str]]:
    """Discover finance Quora Spaces and auto-follow the good ones (capped, tracked).
    Returns [(name, url)]."""
    qcfg = config.get("quora", {})
    n = qcfg.get("follow_spaces_per_run", 0)
    if not n or not common.QUORA_STATE_PATH.exists():
        return []
    filters = [w.lower() for w in (qcfg.get("space_keywords")
               or ["stock", "market", "invest", "finance", "trading", "equity"])]
    posted = common.load_posted()
    followed: list[tuple[str, str]] = []
    pw, browser, context, page = _quora_browser(config)
    try:
        _quora_login(page, context)
        page.goto("https://www.quora.com/spaces", wait_until="domcontentloaded", timeout=30000)
        time.sleep(6)
        page.mouse.wheel(0, 2000)
        time.sleep(2)
        cands = page.evaluate(
            "() => { const out=[], seen=new Set();"
            " for (const a of document.querySelectorAll('a[href]')) {"
            "  const h=a.getAttribute('href'); const t=(a.innerText||'').trim();"
            "  const m=h && h.match(/^https?:\\/\\/([a-z0-9-]+)\\.quora\\.com\\/?$/i);"
            "  if (m && t && t.length<55 && !seen.has(m[1]) && !['www','help','business'].includes(m[1])) {"
            "    seen.add(m[1]); out.push({sub:m[1], t:t}); } }"
            " return out; }"
        )
        picked = []
        for c in cands:
            if not any(w in c["t"].lower() for w in filters):
                continue
            sub = c["sub"] + ".quora.com"
            if ("qspace:" + sub) in posted:
                continue
            picked.append((c["t"], sub))
        for name, sub in picked[:n]:
            try:
                page.goto("https://" + sub + "/", wait_until="domcontentloaded", timeout=30000)
                time.sleep(5)
                btn = None
                for b in page.query_selector_all("button"):
                    if (b.inner_text() or "").strip() == "Follow":
                        btn = b
                        break
                common.add_posted("qspace:" + sub)
                common.note_recent("quora_space", sub, keep=50)
                if btn:
                    _robust_click(btn)
                    time.sleep(3)
                    followed.append((name, "https://" + sub + "/"))
                    log.info("Followed Quora space: %s", name)
                    time.sleep(random.uniform(4, 9))
            except Exception as e:  # noqa: BLE001
                log.warning("Quora space follow failed (%s): %s", name, e)
    finally:
        context.close()
        browser.close()
        pw.stop()
    log.info("Quora: followed %d new spaces", len(followed))
    return followed


def _q_post_text(btn) -> str:
    """Grab the actual post text near a feed button (skip the Upvote/Comment row)."""
    try:
        raw = btn.evaluate(
            "b => { let e=b; for (let i=0;i<10;i++){ if(e.parentElement) e=e.parentElement; } return e.innerText||''; }"
        )
    except Exception:  # noqa: BLE001
        return ""
    junk = {"upvote", "comment", "share", "follow", "downvote", "report", "more",
            "answer", "request", "related", "·"}
    good = []
    for line in (raw or "").split("\n"):
        l = line.strip()
        if len(l) <= 25 or l.lower() in junk:
            continue
        if l.replace(",", "").replace(".", "").replace("K", "").isdigit():
            continue
        good.append(l)
    return " ".join(good)[:200]


def quora_space_engagement(config: dict) -> list[tuple[str, str]]:
    """Comment + upvote on posts INSIDE relevant Quora Spaces (communities).
    Ledger-deduped. Returns [(snippet, url)]."""
    import hashlib

    qcfg = config.get("quora", {})
    spaces = list(dict.fromkeys((qcfg.get("spaces") or []) + common.recent("quora_space", 50)))
    n_targets = qcfg.get("space_targets_per_run", 6)
    u_per = qcfg.get("upvotes_per_space", 0)
    c_per = qcfg.get("comments_per_space", 0)
    if not spaces or not common.QUORA_STATE_PATH.exists() or (not u_per and not c_per):
        return []
    posted = common.load_posted()
    local: set = set()
    done: list[tuple[str, str]] = []
    order = list(spaces)
    random.shuffle(order)
    order = order[:n_targets]
    pw, browser, context, page = _quora_browser(config)
    try:
        _quora_login(page, context)

        # Visit up to N spaces; do per-space upvotes + comments in EACH.
        for sp in order:
            url = sp if sp.startswith("http") else "https://" + sp
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(5)
                page.mouse.wheel(0, 1500)
                time.sleep(2)
            except Exception as e:  # noqa: BLE001
                log.warning("Quora space open failed (%s): %s", sp, e)
                continue

            # ---- upvotes in THIS space ----
            up = 0
            for vb in page.query_selector_all("button[aria-label]"):
                if up >= u_per:
                    break
                al = (vb.get_attribute("aria-label") or "").lower()
                if not al.startswith("upvote") or al.startswith("upvoted"):
                    continue
                txt = _q_post_text(vb)
                key = "qsv:" + hashlib.md5(txt.encode()).hexdigest()[:12]
                if not txt or key in posted or key in local:
                    continue
                try:
                    _robust_click(vb)
                except Exception as e:  # noqa: BLE001
                    log.warning("Quora space upvote failed: %s", e)
                    continue
                local.add(key)
                common.add_posted(key)
                done.append(("qs_upvote", txt[:60], url))
                up += 1
                time.sleep(random.uniform(2, 5))

            # ---- comments in THIS space ----
            made = 0
            cbtns = [b for b in page.query_selector_all("button[aria-label]")
                     if "comment" in (b.get_attribute("aria-label") or "").lower()]
            for cb in cbtns:
                if made >= c_per:
                    break
                txt = _q_post_text(cb)
                key = "qsc:" + hashlib.md5(txt.encode()).hexdigest()[:12]
                if not txt or key in posted or key in local:
                    continue
                try:
                    draft = common.draft_quora_comment(txt, "", False, config)
                    _robust_click(cb)
                    time.sleep(3)
                    # Space comment composer is a contenteditable (not a textarea).
                    box = None
                    for ce in page.query_selector_all('[contenteditable="true"]'):
                        if ce.is_visible():
                            box = ce
                            break
                    if not box:
                        for ta in page.query_selector_all("textarea"):
                            if ta.is_visible():
                                box = ta
                                break
                    if not box:
                        raise RuntimeError("space comment box not found")
                    _robust_click(box)
                    page.keyboard.type(draft, delay=5)
                    time.sleep(1)
                    submitted = False
                    for sel in ['button:has-text("Post")', 'button:has-text("Comment")',
                                'button:has-text("Add Comment")', 'button[aria-label*="post" i]']:
                        el = page.query_selector(sel)
                        if el and el.is_visible() and el.is_enabled():
                            _robust_click(el)
                            submitted = True
                            break
                    if not submitted:
                        raise RuntimeError("space comment submit not found")
                    time.sleep(3)
                except Exception as e:  # noqa: BLE001
                    log.warning("Quora space comment failed: %s", e)
                    local.add(key)
                    continue
                local.add(key)
                common.add_posted(key)
                common.log_post("quora-space-comment", url, draft)
                done.append(("qs_comment", txt[:60], url))
                made += 1
                time.sleep(random.uniform(4, 10))
    finally:
        context.close()
        browser.close()
        pw.stop()

    log.info("Quora spaces: %d actions", len(done))
    return done


# --------------------------------------------------------------------------- #
# The posting job (background thread)
# --------------------------------------------------------------------------- #
def _posting_job(approved_ids: list[str]) -> None:
    config = common.load_config()
    status = {
        "running": True,
        "started_at": dt.datetime.now().isoformat(timespec="seconds"),
        "results": {i: {"status": "pending"} for i in approved_ids},
    }
    _save_status(status)

    quora_done: list[tuple[str, str]] = []
    twitter_done: list[tuple[str, str]] = []

    try:
        drafts = {d["id"]: d for d in common.load_drafts()}
        to_post = [drafts[i] for i in approved_ids if i in drafts]

        # Any approved id that's no longer in drafts.json (e.g. dashboard wasn't
        # reloaded after a new scout) -> mark failed instead of silently dropping.
        for i in approved_ids:
            if i not in drafts:
                _set_result(status, i, status="failed",
                            error="draft no longer in drafts.json — reload the dashboard")

        quora_drafts = [d for d in to_post if d["platform"] == "quora"]
        twitter_drafts = [d for d in to_post if d["platform"] == "twitter"]

        # --- Quora, staggered 3-5 min between posts ---
        for idx, draft in enumerate(quora_drafts):
            action = draft.get("action", "answer")
            _set_result(status, draft["id"], status="posting")
            log.info("Posting to Quora (%s): %s", action, draft["title"][:55])
            ok = False
            try:
                if action == "comment":
                    url = _post_quora_comment(draft, config)
                    ledger_key = "comment:" + draft["url"].replace("/unanswered/", "/")
                elif action == "question":
                    url = _post_quora_question(draft, config)
                    ledger_key = "question:" + draft["id"]
                else:
                    url = _post_quora(draft, config)
                    ledger_key = url
                _set_result(status, draft["id"], status="success", url=url)
                common.log_post("quora", url, draft["draft"])
                common.add_posted(ledger_key)
                common.mark_draft_posted(draft["id"], url)
                quora_done.append((draft["title"], url))
                log.info("Posted to Quora (%s): %s", action, url)
                ok = True
            except Exception as e:  # noqa: BLE001
                _set_result(status, draft["id"], status="failed", error=str(e))
                log.warning("Quora %s failed (%s): %s", action, draft["id"], e)

            # Only wait the human-like gap after a SUCCESSFUL post; don't make
            # failures cost an extra 2-3 min before trying the next one.
            if ok and idx < len(quora_drafts) - 1:
                lo, hi = config["posting"]["quora_delay_sec"]
                delay = random.uniform(lo, hi)
                log.info("Waiting %.0fs before next Quora post...", delay)
                time.sleep(delay)

        # --- Twitter (original tweets) ---
        import twitter_web
        for idx, draft in enumerate(twitter_drafts):
            _set_result(status, draft["id"], status="posting")
            log.info("Posting tweet: %s", draft["title"][:55])
            ok = False
            try:
                if draft.get("action") == "reply":
                    url = twitter_web.reply_to_tweet(draft["url"], draft["draft"], config)
                    key = "reply:" + draft["url"]
                else:
                    url = twitter_web.post_tweet(draft["draft"], config)
                    key = "tweet:" + draft["id"]
                _set_result(status, draft["id"], status="success", url=url)
                common.log_post("twitter", url, draft["draft"])
                common.add_posted(key)
                common.mark_draft_posted(draft["id"], url)
                twitter_done.append((draft["title"], url))
                log.info("Posted tweet/reply: %s", url)
                ok = True
            except Exception as e:  # noqa: BLE001
                _set_result(status, draft["id"], status="failed", error=str(e))
                log.warning("Tweet failed (%s): %s", draft["id"], e)

            if ok and idx < len(twitter_drafts) - 1:
                lo, hi = config["posting"]["twitter_delay_sec"]
                time.sleep(random.uniform(lo, hi))

    except Exception as e:  # noqa: BLE001 - never let the thread die silently
        log.exception("Posting job crashed: %s", e)
        for i, r in status["results"].items():
            if r.get("status") in ("pending", "posting"):
                r["status"] = "failed"
                r["error"] = f"job crashed: {e}"
    finally:
        # ALWAYS clear the running flag so the dashboard never gets stuck.
        status["running"] = False
        status["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")
        _save_status(status)
        try:
            _send_confirmation(quora_done, twitter_done, config)
        except Exception as e:  # noqa: BLE001
            log.warning("Confirmation email failed: %s", e)
        log.info("Posting job complete: %d quora, %d twitter.",
                 len(quora_done), len(twitter_done))


def _send_confirmation(quora_done, twitter_done, config) -> None:
    date_str = dt.date.today().strftime("%d %b %Y")
    subject = config["email"]["confirmation_subject_template"].format(date=date_str)
    lines = [f"✅ Posted today — {date_str}", ""]
    for label, items in (("QUORA", quora_done), ("TWITTER", twitter_done)):
        lines.append(f"{label}: {len(items)} posted")
        for _, url in items:
            lines.append(f"  → {url}")
        lines.append("")
    total = len(quora_done) + len(twitter_done)
    lines.append(f"📊 Total: {total} posts today")
    try:
        common.send_email(subject, "\n".join(lines))
    except Exception as e:  # noqa: BLE001
        log.warning("Confirmation email failed: %s", e)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/")
def index():
    return _no_cache(send_from_directory(str(common.ROOT), "dashboard.html"))


@app.route("/api/drafts")
def api_drafts():
    return _no_cache(jsonify(common.load_drafts()))


@app.route("/api/save", methods=["POST"])
def api_save():
    """Persist edited text + approved flags from the dashboard."""
    incoming = {d["id"]: d for d in request.json.get("drafts", [])}
    drafts = common.load_drafts()
    for d in drafts:
        if d["id"] in incoming:
            d["draft"] = incoming[d["id"]].get("draft", d["draft"])
            d["approved"] = bool(incoming[d["id"]].get("approved", d.get("approved")))
    common.save_drafts(drafts)
    return jsonify({"ok": True, "count": len(drafts)})


@app.route("/api/post", methods=["POST"])
def api_post():
    """Start posting. Body: {"ids": [...], "drafts": [...]} — saves then posts."""
    global _post_thread

    status = _load_status()
    if status.get("running"):
        return jsonify({"ok": False, "error": "A posting job is already running."}), 409

    # Save any edits first.
    payload = request.json or {}
    if payload.get("drafts"):
        incoming = {d["id"]: d for d in payload["drafts"]}
        drafts = common.load_drafts()
        for d in drafts:
            if d["id"] in incoming:
                d["draft"] = incoming[d["id"]].get("draft", d["draft"])
                d["approved"] = bool(incoming[d["id"]].get("approved", d.get("approved")))
        common.save_drafts(drafts)

    approved_ids = payload.get("ids") or [
        d["id"] for d in common.load_drafts() if d.get("approved")
    ]
    if not approved_ids:
        return jsonify({"ok": False, "error": "No approved drafts to post."}), 400

    _post_thread = threading.Thread(target=_posting_job, args=(approved_ids,), daemon=True)
    _post_thread.start()
    return jsonify({"ok": True, "count": len(approved_ids)})


@app.route("/api/status")
def api_status():
    return jsonify(_load_status())


def main() -> None:
    config = common.load_config()
    dash = config["dashboard"]
    log.info("Dashboard at http://localhost:%d", dash["port"])
    app.run(host=dash["host"], port=dash["port"], debug=False, threaded=True)


if __name__ == "__main__":
    main()
