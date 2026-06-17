"""
scout.py — the daily morning scout.

Reddit:
  - fetch top posts from the last 24h across the active subreddits
  - filter: score > 10, comments > 5
  - also scan top-level comments for unanswered questions
  - score each opportunity 1-10 (keywords + question-type + engagement)
  - take the top 8, draft a 100-200 word reply in the persona voice
  - mention Intrynsic ONLY on tool/platform-recommendation posts

Quora (RSS was discontinued by Quora, so we scrape the topic pages with a
logged-in browser session instead):
  - open each topic's "unanswered" page in Playwright
  - collect question titles + links (anything ending in "?")
  - prioritize them, take the top 5, draft a 200-400 word answer

Then it saves everything to drafts.json and emails the digest.

Usage:
    python scout.py
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
# Scoring helpers
# --------------------------------------------------------------------------- #
def _text_of(title: str, body: str) -> str:
    return f"{title}\n{body}".lower()


def _is_tool_recommendation(text: str, config: dict) -> bool:
    kws = config["reddit"]["tool_recommendation_keywords"]
    return any(kw in text for kw in kws)


def _looks_like_question(text: str, config: dict) -> bool:
    sigs = config["reddit"]["question_signals"]
    return any(sig in text for sig in sigs)


def score_reddit(title: str, body: str, score: int, num_comments: int, config: dict) -> tuple[int, str]:
    """
    Return (1-10 score, one-line reason). Weighting:
      - keyword relevance
      - question-type (higher priority)
      - engagement (score + comments)
    """
    text = _text_of(title, body)
    reasons = []
    points = 0.0

    matched = [kw for kw in config["reddit"]["priority_keywords"] if kw.lower() in text]
    if matched:
        points += min(len(matched), 4) * 1.2
        reasons.append(f"keywords: {', '.join(matched[:3])}")

    if _is_tool_recommendation(text, config):
        points += 3.0
        reasons.append("asking for a tool/platform")

    if _looks_like_question(text, config):
        points += 2.0
        reasons.append("question-type post")

    # Engagement, gently scaled.
    points += min(score / 50.0, 2.0)
    points += min(num_comments / 30.0, 1.5)
    reasons.append(f"{score} upvotes / {num_comments} comments")

    final = max(1, min(10, round(points)))
    return final, "; ".join(reasons)


# --------------------------------------------------------------------------- #
# Reddit scout
# --------------------------------------------------------------------------- #
def scout_reddit(config: dict) -> list[dict]:
    reddit = common.get_reddit()
    subs = common.active_subreddits(config)
    posted = common.load_posted()
    log.info("Reddit scout across: %s", ", ".join(subs))

    min_score = config["reddit"]["min_score"]
    min_comments = config["reddit"]["min_comments"]
    cutoff = time.time() - 24 * 3600
    candidates: list[dict] = []

    for sub in subs:
        try:
            for post in reddit.subreddit(sub).top(time_filter="day", limit=40):
                if post.stickied:
                    continue
                if post.id in posted:  # already replied to this one
                    continue
                if post.created_utc < cutoff:
                    continue
                if post.score <= min_score or post.num_comments <= min_comments:
                    continue

                body = post.selftext or ""
                sc, reason = score_reddit(post.title, body, post.score, post.num_comments, config)
                candidates.append(
                    {
                        "kind": "post",
                        "platform": "reddit",
                        "subreddit": sub,
                        "title": post.title,
                        "selftext": body,
                        "url": f"https://www.reddit.com{post.permalink}",
                        "target_id": post.id,
                        "score_num": post.score,
                        "comments_num": post.num_comments,
                        "opp_score": sc,
                        "context": reason,
                    }
                )

                # Also scan top-level comments for unanswered questions.
                _scan_comments_for_questions(post, sub, config, candidates)

        except Exception as e:  # noqa: BLE001
            log.warning("Reddit scout failed in r/%s: %s", sub, e)

    # Dedup by target id, keep highest score, take top N.
    best: dict[str, dict] = {}
    for c in candidates:
        key = c["target_id"]
        if key not in best or c["opp_score"] > best[key]["opp_score"]:
            best[key] = c
    ranked = sorted(best.values(), key=lambda c: c["opp_score"], reverse=True)
    top = ranked[: config["reddit"]["max_opportunities"]]

    log.info("Reddit: %d candidates -> %d opportunities", len(best), len(top))

    drafts = []
    for opp in top:
        text = _text_of(opp["title"], opp.get("selftext", ""))
        mention = _is_tool_recommendation(text, config)
        try:
            draft = common.draft_reddit_reply(opp, mention_intrynsic=mention, config=config)
        except Exception as e:  # noqa: BLE001
            log.warning("Drafting failed for %s: %s", opp["url"], e)
            continue
        drafts.append(
            {
                "id": f"reddit_{opp['target_id']}",
                "platform": "reddit",
                "subreddit": opp["subreddit"],
                "title": opp["title"],
                "url": opp["url"],
                "target_id": opp["target_id"],
                "kind": opp["kind"],
                "context": opp["context"],
                "opp_score": opp["opp_score"],
                "mentions_intrynsic": mention,
                "draft": draft,
                "approved": False,
            }
        )
    return drafts


def _scan_comments_for_questions(post, sub, config, candidates) -> None:
    """Look at top-level comments; surface unanswered questions as opportunities."""
    try:
        post.comments.replace_more(limit=0)
        for c in post.comments[:25]:
            text = (c.body or "").lower()
            if "?" not in text or len(text) < 25:
                continue
            # "unanswered" heuristic: no replies
            if len(c.replies) > 0:
                continue
            sc, reason = score_reddit(c.body, "", c.score, 0, config)
            # comment-level opportunities are slightly down-weighted
            sc = max(1, sc - 1)
            candidates.append(
                {
                    "kind": "comment",
                    "platform": "reddit",
                    "subreddit": sub,
                    "title": f"[unanswered comment] {c.body[:120]}",
                    "selftext": c.body,
                    "url": f"https://www.reddit.com{c.permalink}",
                    "target_id": c.id,
                    "score_num": c.score,
                    "comments_num": 0,
                    "opp_score": sc,
                    "context": f"unanswered question in comments; {reason}",
                }
            )
    except Exception as e:  # noqa: BLE001
        log.warning("Comment scan failed on %s: %s", getattr(post, "id", "?"), e)


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
                try:
                    txt = common.draft_quora_answer(opp, mention_intrynsic=True, config=config)
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
                mention = (made == 0)  # "rarely" -> only the first comment may mention Intrynsic
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


# --------------------------------------------------------------------------- #
# Email digest
# --------------------------------------------------------------------------- #
def send_digest(reddit_drafts, quora_drafts, config) -> None:
    date_str = dt.date.today().strftime("%d %b %Y")
    subject = config["email"]["digest_subject_template"].format(
        date=date_str, n_reddit=len(reddit_drafts), n_quora=len(quora_drafts)
    )
    dash = config["dashboard"]["url"]
    body = f"""\
Open your approval dashboard -> {dash}

REDDIT: {len(reddit_drafts)} drafts
QUORA: {len(quora_drafts)} drafts

Approve, edit, and post in under 5 minutes.
"""
    try:
        common.send_email(subject, body)
    except Exception as e:  # noqa: BLE001
        log.warning("Digest email failed: %s", e)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    config = common.load_config()
    common.get_state()  # ensure start_date exists for day-gating

    log.info("==== Scout run %s (day %d) ====", dt.date.today().isoformat(),
             common.days_since_start())

    # Each platform is isolated: if Reddit creds are missing or Quora is down,
    # the other still runs and you still get a digest.
    try:
        reddit_drafts = scout_reddit(config)
    except Exception as e:  # noqa: BLE001
        log.warning("Reddit scout skipped: %s", e)
        reddit_drafts = []

    try:
        quora_drafts = scout_quora(config)
    except Exception as e:  # noqa: BLE001
        log.warning("Quora scout skipped: %s", e)
        quora_drafts = []

    all_drafts = reddit_drafts + quora_drafts
    common.save_drafts(all_drafts)
    log.info("Saved %d drafts to drafts.json", len(all_drafts))

    send_digest(reddit_drafts, quora_drafts, config)
    log.info("Scout complete. Open %s to review.", config["dashboard"]["url"])


if __name__ == "__main__":
    main()
