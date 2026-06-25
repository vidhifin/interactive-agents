"""
notifications.py — read each platform's notifications and email a daily digest of
ONLY the interactions OTHER people had with you (replies, comments, upvotes/likes,
mentions, follows, shares) — each with a link to the post/comment/profile.

It deliberately SKIPS the noisy feed-style notifications ("X posted ...",
"new post in <space/club>", "X answered the question ...") — anything that isn't
someone interacting with YOUR content or YOU.

Nothing here posts or changes anything — it only reads notifications and sends one
email to DIGEST_RECIPIENT. Already-seen interactions are remembered in the ledger
(prefix "notifseen:") so each interaction is emailed once, not re-sent every day.

Usage:
    python notifications.py        # scrape all 3 platforms + email the digest
"""

from __future__ import annotations

import re
import time
import html
import hashlib
import datetime as dt

import common
from common import log

# --------------------------------------------------------------------------- #
# What counts as "someone interacted with ME" vs. a generic feed notification.
# Include only rows that name an interaction verb AND are about you/your content.
# --------------------------------------------------------------------------- #
INTERACTION_RE = re.compile(
    r"(replied to|comment(?:ed)? on|left a comment|upvoted|liked|reacted to|"
    r"mention(?:ed)? you|tagged you|started following you|followed you|"
    r"shared your|reshared|reposted|quoted your|answered your|"
    r"requested your answer|asked you to answer|thanked you|voted on your)",
    re.I,
)
ABOUT_ME_RE = re.compile(r"\b(you|your|you're|yours)\b", re.I)
# Hard excludes — pure feed/"someone posted" noise, even if it mentions you.
EXCLUDE_RE = re.compile(
    r"(\bposted\b|new post in|added a post|published a|answered the question|"
    r"wrote an answer|suggested for you|people you may know|might (?:like|know)|"
    r"trending|recommended|view \d+ more)",
    re.I,
)


def _classify(text: str) -> str:
    """A short human label for the kind of interaction."""
    t = text.lower()
    if "repl" in t:
        return "reply"
    if "comment" in t:
        return "comment"
    if "mention" in t or "tagged" in t:
        return "mention"
    if "follow" in t:
        return "follow"
    if "upvot" in t or "liked" in t or "react" in t or "voted" in t:
        return "upvote/like"
    if "shared" in t or "reshared" in t or "repost" in t or "quoted" in t:
        return "share"
    if "answer" in t:
        return "answer request"
    if "thank" in t:
        return "thanks"
    return "interaction"


def _is_interaction(text: str) -> bool:
    if not text or len(text) < 6:
        return False
    if EXCLUDE_RE.search(text):
        return False
    return bool(INTERACTION_RE.search(text) and ABOUT_ME_RE.search(text))


def _seen_key(platform: str, text: str) -> str:
    return "notifseen:" + platform + ":" + hashlib.md5(text.strip().lower().encode()).hexdigest()[:12]


def _dedupe_new(platform: str, rows: list[dict]) -> list[dict]:
    """Drop rows already emailed on a previous run; remember the new ones."""
    posted = common.load_posted()
    out, seen_now = [], set()
    for r in rows:
        text = " ".join((r.get("text") or "").split())
        if not _is_interaction(text):
            continue
        key = _seen_key(platform, text)
        if key in posted or key in seen_now:
            continue
        seen_now.add(key)
        out.append({"type": _classify(text), "text": text[:240],
                    "url": r.get("url") or "", "key": key})
    return out


# --------------------------------------------------------------------------- #
# Quora
# --------------------------------------------------------------------------- #
def quora_notifications(config: dict) -> list[dict]:
    if not common.QUORA_STATE_PATH.exists():
        log.warning("Quora notifications: no session — run `python quora_login.py`.")
        return []
    import poster  # reuse the logged-in Quora browser helpers

    pw, browser, context, page = poster._quora_browser(config)
    rows: list[dict] = []
    try:
        poster._quora_login(page, context)
        page.goto("https://www.quora.com/notifications",
                  wait_until="domcontentloaded", timeout=30000)
        time.sleep(5)
        for _ in range(4):
            page.mouse.wheel(0, 2200)
            time.sleep(1.2)
        rows = page.evaluate(
            "() => { const out=[], seen=new Set();"
            " for (const a of document.querySelectorAll('a[href]')) {"
            "  const t=(a.innerText||'').trim();"
            "  if(!t || t.length<8 || t.length>300 || seen.has(t)) continue;"
            "  let h=a.getAttribute('href')||'';"
            "  if(h && !h.startsWith('http')) h='https://www.quora.com'+h;"
            "  seen.add(t); out.push({text:t, url:h}); }"
            " return out; }"
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Quora notifications scrape failed: %s", e)
    finally:
        context.close()
        browser.close()
        pw.stop()
    return _dedupe_new("quora", rows)


# --------------------------------------------------------------------------- #
# X / Twitter
# --------------------------------------------------------------------------- #
def twitter_notifications(config: dict) -> list[dict]:
    from playwright.sync_api import sync_playwright
    import twitter_web

    headless = config.get("twitter", {}).get("headless", False)
    rows: list[dict] = []
    with sync_playwright() as p:
        ctx = twitter_web.twitter_context(p, headless)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(30000)
            page.goto("https://x.com/notifications",
                      wait_until="domcontentloaded", timeout=30000)
            time.sleep(5)
            if twitter_web._logged_out(page):
                raise RuntimeError("Not logged into X. Run `python twitter_login.py`.")
            for _ in range(4):
                page.mouse.wheel(0, 2200)
                time.sleep(1.2)
            # Each notification is a cellInnerDiv. Likes/follows/reposts are plain
            # cells; replies/mentions wrap an <article>. Grab text + best link.
            rows = page.evaluate(
                "() => { const out=[], seen=new Set();"
                " for (const c of document.querySelectorAll('div[data-testid=\"cellInnerDiv\"]')) {"
                "  const t=(c.innerText||'').trim();"
                "  if(!t || t.length<6 || seen.has(t)) continue;"
                "  let h='';"
                "  const s=c.querySelector('a[href*=\"/status/\"]');"
                "  if(s){ h=s.getAttribute('href'); }"
                "  else { const pr=c.querySelector('a[href^=\"/\"]'); if(pr) h=pr.getAttribute('href'); }"
                "  if(h && !h.startsWith('http')) h='https://x.com'+h.split('?')[0];"
                "  seen.add(t); out.push({text:t, url:h}); }"
                " return out; }"
            )
        except Exception as e:  # noqa: BLE001
            log.warning("X notifications scrape failed: %s", e)
        finally:
            ctx.close()
    return _dedupe_new("twitter", rows)


# --------------------------------------------------------------------------- #
# front.page
# --------------------------------------------------------------------------- #
def frontpage_notifications(config: dict) -> list[dict]:
    if not common.FRONTPAGE_STATE_PATH.exists():
        log.warning("front.page notifications: no session — run `python front_page_login.py`.")
        return []
    from playwright.sync_api import sync_playwright
    import front_page_web

    fp = config.get("front_page", {})
    base = fp.get("base_url", "https://front.page").rstrip("/")
    rows: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=fp.get("headless", False))
        context = browser.new_context(
            user_agent=front_page_web.BROWSER_UA,
            storage_state=str(common.FRONTPAGE_STATE_PATH))
        page = context.new_page()
        page.set_default_timeout(30000)
        try:
            # Try a dedicated notifications/activity route; fall back to the bell.
            opened = False
            for path in ("/notifications", "/activity"):
                try:
                    page.goto(base + path, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(4)
                    if "notif" in page.url.lower() or "activ" in page.url.lower():
                        opened = True
                        break
                except Exception:  # noqa: BLE001
                    continue
            if not opened:
                page.goto(base + "/", wait_until="domcontentloaded", timeout=40000)
                time.sleep(5)
                page.evaluate(
                    "() => { const el=[...document.querySelectorAll('a,button,[role=button]')]"
                    ".find(e=>/notification|activity|alerts/i.test("
                    "(e.getAttribute('aria-label')||'')+' '+(e.innerText||'')));"
                    " if(el) el.click(); }"
                )
                time.sleep(4)
            for _ in range(4):
                page.mouse.wheel(0, 2200)
                time.sleep(1.2)
            rows = page.evaluate(
                "() => { const out=[], seen=new Set();"
                " for (const el of document.querySelectorAll('a[href], li, div')) {"
                "  const t=(el.innerText||'').trim();"
                "  if(!t || t.length<8 || t.length>300 || seen.has(t)) continue;"
                "  let h=''; const a=el.closest('a')||el.querySelector('a[href]');"
                "  if(a){ h=a.getAttribute('href')||''; }"
                "  if(h && !h.startsWith('http')) h='" + base + "'+h;"
                "  seen.add(t); out.push({text:t, url:h}); }"
                " return out; }"
            )
        except Exception as e:  # noqa: BLE001
            log.warning("front.page notifications scrape failed: %s", e)
        finally:
            context.close()
            browser.close()
    return _dedupe_new("frontpage", rows)


# --------------------------------------------------------------------------- #
# Digest email
# --------------------------------------------------------------------------- #
def _digest_email(by_platform: dict[str, list[dict]], config: dict) -> int:
    date_str = dt.date.today().strftime("%d %b %Y")
    total = sum(len(v) for v in by_platform.values())
    labels = {"quora": "Quora", "twitter": "X / Twitter", "frontpage": "front.page"}

    parts = [f"<h2>🔔 New interactions with you — {date_str}</h2>",
             f"<p>{total} new interaction(s) across your platforms.</p>"]
    for plat in ("quora", "twitter", "frontpage"):
        items = by_platform.get(plat, [])
        parts.append(f"<h3>{labels[plat]} — {len(items)}</h3>")
        if not items:
            parts.append("<p style='color:#888'>Nothing new.</p>")
            continue
        parts.append("<ul>")
        for it in items:
            text = html.escape(it["text"])
            if it["url"]:
                link = f' &nbsp;<a href="{html.escape(it["url"])}">→ open</a>'
            else:
                link = ""
            parts.append(f"<li><b>[{it['type']}]</b> {text}{link}</li>")
        parts.append("</ul>")
    body = "<body style='font-family:sans-serif;line-height:1.5'>" + "".join(parts) + "</body>"

    subject = f"🔔 Intrynsic — {total} new interaction(s) — {date_str}"
    try:
        common.send_email(subject, body, html=True)
        log.info("Notification digest sent (%d interactions).", total)
    except Exception as e:  # noqa: BLE001
        log.warning("Notification digest email failed: %s", e)
    return total


def run(config: dict | None = None) -> dict[str, list[dict]]:
    """Scrape all 3 platforms, email the digest, and mark interactions as seen."""
    config = config or common.load_config()
    by_platform: dict[str, list[dict]] = {}
    for name, fn in (("quora", quora_notifications),
                     ("twitter", twitter_notifications),
                     ("frontpage", frontpage_notifications)):
        try:
            by_platform[name] = fn(config)
        except Exception as e:  # noqa: BLE001
            log.warning("%s notifications failed: %s", name, e)
            by_platform[name] = []
        log.info("%s: %d new interactions", name, len(by_platform[name]))

    _digest_email(by_platform, config)
    # Only remember as "seen" AFTER the email is sent, so a send failure re-tries.
    for items in by_platform.values():
        for it in items:
            common.add_posted(it["key"])
    return by_platform


def main() -> None:
    log.info("==== Notification digest %s ====", dt.date.today().isoformat())
    run()


if __name__ == "__main__":
    main()
