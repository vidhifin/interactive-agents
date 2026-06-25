"""
scout.py — generates the day's Quora + X/Twitter drafts.

Quora (RSS was discontinued by Quora, so we scrape with a logged-in browser):
  - search each keyword for matching questions
  - draft a 200-400 word answer per question, plus a few comments and a question
  - mention Intrynsic ONLY on tool/platform-recommendation questions

X/Twitter:
  - draft original tweets, and search X for tweets to reply to

The drafts are consumed by daily.py (the 10am auto-run), which posts them.
"""

from __future__ import annotations

import time
import hashlib
import datetime as dt

import common
from common import log

# A realistic browser UA so Quora is less likely to wall us as a bot.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# --------------------------------------------------------------------------- #
# Quora scout
# --------------------------------------------------------------------------- #
def _question_links_from_anchors(items: list[dict]) -> list[dict]:
    """Filter raw <a> anchors down to Quora question links (title ends with '?')."""
    skip = ("/topic/", "/profile/", "/q/", "/answer", "/search", "/about",
            "/contact", "/sitemap", "/careers", "/login", "/signup")
    out: list[dict] = []
    seen = set()
    for it in items:
        t, h = it["text"], it["href"]
        if not t.endswith("?") or len(t) < 15:
            continue
        if not h.startswith("https://www.quora.com/"):
            continue
        if any(x in h for x in skip):
            continue
        h = h.split("?")[0].replace("/unanswered/", "/")
        if h in seen:
            continue
        seen.add(h)
        out.append({"title": t, "url": h})
    return out


def _scrape_search_questions(page, keyword: str) -> list[dict]:
    """Scrape question results from Quora search for a keyword (diverse, reliable)."""
    from urllib.parse import quote

    url = f"https://www.quora.com/search?q={quote(keyword)}&type=question"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(4)
        for _ in range(5):  # lazy-loaded results; scroll to pull more
            page.mouse.wheel(0, 4000)
            time.sleep(1.5)
        items = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(a => ({href: a.href, text: (a.innerText || '').trim()}))",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Quora search failed for '%s': %s", keyword, e)
        return []
    return _question_links_from_anchors(items)


def _grab_top_answer(page, url: str) -> str:
    """Best-effort snippet of the top answer on a question page (context for a comment)."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(4)
        page.mouse.wheel(0, 1200)
        time.sleep(2)
        texts = page.eval_on_selector_all(
            "div.q-text",
            "els => els.map(e => (e.innerText || '').trim()).filter(t => t.length > 120)",
        )
        return texts[0] if texts else ""
    except Exception as e:  # noqa: BLE001
        log.warning("Quora: could not read top answer for %s: %s", url, e)
        return ""


def _quora_draft(action: str, topic: str, title: str, url: str, context: str, text: str) -> dict:
    base = url or topic
    qid = hashlib.md5(f"{action}|{base}|{title}".encode()).hexdigest()[:12]
    return {
        "id": f"quora_{action}_{qid}",
        "platform": "quora",
        "action": action,  # answer | comment | question
        "topic": topic.replace("-", " "),
        "title": title,
        "url": url,
        "context": context,
        "opp_score": 8,
        "mentions_intrynsic": "intrynsic" in text.lower(),
        "draft": text,
        "approved": False,
    }


def scout_quora(config: dict) -> list[dict]:
    from playwright.sync_api import sync_playwright

    keywords = config["quora"].get("search_keywords") or config["quora"].get("topics", [])
    posted = common.load_posted()
    drafts: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        kwargs = {"user_agent": BROWSER_UA}
        if common.QUORA_STATE_PATH.exists():
            kwargs["storage_state"] = str(common.QUORA_STATE_PATH)
        context = browser.new_context(**kwargs)
        page = context.new_page()
        page.set_default_timeout(30000)
        try:
            # Build a diverse pool of questions via keyword search.
            pool: list[tuple[str, dict]] = []   # (keyword, question)
            seen = set()
            for kw in keywords:
                qs = _scrape_search_questions(page, kw)
                log.info("Quora search '%s': %d questions", kw, len(qs))
                for q in qs:
                    if q["url"] in seen:
                        continue
                    seen.add(q["url"])
                    pool.append((kw, q))

            # ---- ANSWERS: questions we haven't answered yet ----
            used = set()
            answer_pool = [(kw, q) for kw, q in pool if q["url"] not in posted]
            for kw, q in answer_pool[: config["quora"]["max_opportunities"]]:
                used.add(q["url"])
                opp = {"topic": kw, "title": q["title"]}
                mention = common.is_tool_query(q["title"], config)
                try:
                    txt = common.draft_quora_answer(opp, mention_intrynsic=mention, config=config)
                except Exception as e:  # noqa: BLE001
                    log.warning("Quora answer drafting failed for %s: %s", q["url"], e)
                    continue
                drafts.append(_quora_draft("answer", kw, q["title"], q["url"],
                                           f"search: {kw}", txt))

            # ---- COMMENTS: different questions that already have a top answer ----
            comment_pool = [(kw, q) for kw, q in pool
                            if q["url"] not in used and ("comment:" + q["url"]) not in posted]
            made = 0
            for kw, q in comment_pool:
                if made >= config["quora"]["max_comments"]:
                    break
                snippet = _grab_top_answer(page, q["url"])
                if not snippet:  # nothing to comment on yet -> skip
                    continue
                mention = common.is_tool_query(q["title"], config)  # only on tool-related posts
                try:
                    txt = common.draft_quora_comment(q["title"], snippet, mention, config)
                except Exception as e:  # noqa: BLE001
                    log.warning("Quora comment drafting failed for %s: %s", q["url"], e)
                    continue
                drafts.append(_quora_draft("comment", kw, q["title"], q["url"],
                                           "comment on the top answer", txt))
                made += 1

            # ---- QUESTIONS: generated (no scraping, no promotion) ----
            for i in range(config["quora"].get("max_questions", 0)):
                kw = keywords[i % len(keywords)] if keywords else "the Indian stock market"
                try:
                    qtext = common.draft_quora_question(kw, config)
                except Exception as e:  # noqa: BLE001
                    log.warning("Quora question generation failed: %s", e)
                    continue
                common.note_recent("quora_question", qtext)
                drafts.append(_quora_draft("question", kw, qtext, "", "new question to ask", qtext))
        finally:
            context.close()
            browser.close()

    n = {"answer": 0, "comment": 0, "question": 0}
    for d in drafts:
        n[d["action"]] += 1
    log.info("Quora: %d drafts (%d answers, %d comments, %d questions)",
             len(drafts), n["answer"], n["comment"], n["question"])
    return drafts


# --------------------------------------------------------------------------- #
# Twitter scout (generate original tweets — no scraping)
# --------------------------------------------------------------------------- #
def scout_twitter(config: dict) -> list[dict]:
    tw = config.get("twitter", {})
    if not tw.get("enabled", True):
        return []
    topics = tw.get("topics") or ["the Indian stock market"]
    n = tw.get("tweets_per_run", 1)
    today = dt.date.today().isoformat()
    drafts: list[dict] = []
    for i in range(n):
        topic = topics[i % len(topics)]
        mention = (i == 0 and tw.get("allow_intrynsic", False))
        try:
            text = common.draft_tweet(topic, mention, config)
        except Exception as e:  # noqa: BLE001
            log.warning("Tweet generation failed: %s", e)
            continue
        common.note_recent("tweet", text)
        tid = hashlib.md5(f"tweet|{today}|{i}|{topic}".encode()).hexdigest()[:12]
        drafts.append(
            {
                "id": f"twitter_{tid}",
                "platform": "twitter",
                "action": "tweet",
                "topic": topic,
                "title": text[:80],
                "url": "",
                "context": "original tweet",
                "opp_score": 8,
                "mentions_intrynsic": "intrynsic" in text.lower(),
                "draft": text,
                "approved": False,
            }
        )

    # ---- REPLIES: search X for tweets and draft genuine replies ----
    n_replies = tw.get("replies_per_run", 0)
    if n_replies:
        import twitter_web
        posted = common.load_posted()
        try:
            targets = twitter_web.search_tweets(config, n_replies * 4)
        except Exception as e:  # noqa: BLE001
            log.warning("X reply search failed: %s", e)
            targets = []
        made = 0
        for t in targets:
            if made >= n_replies:
                break
            if ("reply:" + t["url"]) in posted:
                continue
            try:
                rtext = common.draft_tweet_reply(t["text"], False, config)
            except Exception as e:  # noqa: BLE001
                log.warning("Reply drafting failed: %s", e)
                continue
            rid = hashlib.md5(("reply|" + t["url"]).encode()).hexdigest()[:12]
            drafts.append(
                {
                    "id": f"twitter_reply_{rid}",
                    "platform": "twitter",
                    "action": "reply",
                    "topic": "reply",
                    "title": t["text"][:80],
                    "url": t["url"],
                    "context": "reply to a tweet",
                    "opp_score": 8,
                    "mentions_intrynsic": "intrynsic" in rtext.lower(),
                    "draft": rtext,
                    "approved": False,
                }
            )
            made += 1

    n_tweet = sum(1 for d in drafts if d["action"] == "tweet")
    n_reply = sum(1 for d in drafts if d["action"] == "reply")
    log.info("Twitter: %d tweet(s), %d reply draft(s)", n_tweet, n_reply)
    return drafts
