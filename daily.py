"""
daily.py — the 10am fully-automatic run (Quora + X/Twitter + front.page).

Each run:
  1. Scouts Quora and drafts new answers + comments + a question (Gemini)
  2. Posts ALL of them automatically — no approval step
  3. Sends ONE summary email with links to everything that was posted

Reuses the posting flows in poster.py, so it behaves exactly like the dashboard's
"Post All Approved" — staggered, ledger-tracked (won't repeat), drafts marked
posted (so the always-on dashboard shows them with view links).

Usage:
    python daily.py          # normal automatic run
"""

from __future__ import annotations

import time
import random
import datetime as dt

import common
import scout
import poster
import twitter_web
import front_page_web
import substack_web
import notifications
from common import log


def _post_one(draft: dict, config: dict) -> tuple[str, str]:
    """Post a single draft. Returns (url, ledger_key)."""
    if draft.get("platform") == "twitter":
        if draft.get("action") == "reply":
            url = twitter_web.reply_to_tweet(draft["url"], draft["draft"], config)
            return url, "reply:" + draft["url"]
        url = twitter_web.post_tweet(draft["draft"], config)
        return url, "tweet:" + draft["id"]
    action = draft.get("action", "answer")
    if action == "comment":
        url = poster._post_quora_comment(draft, config)
        return url, "comment:" + draft["url"].replace("/unanswered/", "/")
    if action == "question":
        url = poster._post_quora_question(draft, config)
        return url, "question:" + draft["id"]
    url = poster._post_quora(draft, config)
    return url, url


def _summary_email(done: list[tuple[str, str, str]], config: dict) -> None:
    """done = list of (action, title, url). Sends ONE email with all the links."""
    date_str = dt.date.today().strftime("%d %b %Y")
    buckets = {}
    for action, title, url in done:
        buckets.setdefault(action, []).append((title, url))

    subject = f"✅ Intrynsic — {len(done)} actions today — {date_str}"
    lines = [f"Done automatically — {date_str}", ""]
    for label, key in (("ANSWERS", "answer"), ("COMMENTS", "comment"),
                       ("QUESTIONS", "question"), ("QUORA FOLLOW-UPS", "qfollowup"),
                       ("QUORA SPACES FOLLOWED", "qspace_join"),
                       ("QUORA SPACE COMMENTS", "qs_comment"),
                       ("QUORA SPACE UPVOTES", "qs_upvote"),
                       ("TWEETS", "tweet"),
                       ("FRONT.PAGE CLUBS JOINED", "fpclub_join"),
                       ("REPLIES ON X", "reply"), ("X FOLLOW-UPS", "xfollowup"),
                       ("X COMMUNITIES JOINED", "xjoin"),
                       ("X COMMUNITY POSTS", "xcomm_post"),
                       ("X COMMUNITY REPLIES", "xcomm_reply"),
                       ("X COMMUNITY LIKES", "xcomm_like"),
                       ("LIKED ON X", "like"),
                       ("FRONT.PAGE POSTS", "fp_post"),
                       ("FRONT.PAGE COMMENTS", "fp_comment"),
                       ("UPVOTED ON FRONT.PAGE", "fp_upvote"),
                       ("SUBSTACK PUBLICATIONS FOLLOWED", "sb_follow"),
                       ("SUBSTACK NOTES", "sb_note"),
                       ("SUBSTACK REPLIES", "sb_comment"),
                       ("RESTACKED ON SUBSTACK", "sb_restack"),
                       ("LIKED ON SUBSTACK", "sb_like")):
        items = buckets.get(key, [])
        lines.append(f"{label}: {len(items)}")
        for title, url in items:
            lines.append(f"  → {title[:70]}")
            lines.append(f"     {url}")
        lines.append("")
    lines.append(f"Total: {len(done)} actions posted today.")

    try:
        common.send_email(subject, "\n".join(lines))
        log.info("Summary email sent (%d actions).", len(done))
    except Exception as e:  # noqa: BLE001
        log.warning("Summary email failed: %s", e)


def main() -> None:
    config = common.load_config()
    log.info("==== Daily auto-run %s ====", dt.date.today().isoformat())

    # 1) Scout Quora (this does NOT send the digest email — we send one email later).
    try:
        drafts = scout.scout_quora(config)
    except Exception as e:  # noqa: BLE001
        log.warning("Quora scout failed: %s", e)
        drafts = []
    try:
        drafts += scout.scout_twitter(config)
    except Exception as e:  # noqa: BLE001
        log.warning("Twitter scout failed: %s", e)
    common.save_drafts(drafts)
    log.info("Generated %d drafts — auto-posting...", len(drafts))

    # 2) Post everything, staggered, no approval.
    done: list[tuple[str, str, str]] = []
    for idx, draft in enumerate(drafts):
        action = draft.get("action", "answer")
        platform = draft.get("platform", "quora")
        try:
            url, key = _post_one(draft, config)
            common.log_post(platform, url, draft["draft"])
            common.add_posted(key)
            common.mark_draft_posted(draft["id"], url)
            done.append((action, draft["title"], url))
            log.info("Posted (%s/%s): %s", platform, action, url)
        except Exception as e:  # noqa: BLE001
            log.warning("Post failed (%s, %s): %s", action, draft["id"], e)

        # human-like gap between posts (skip after the last one)
        if idx < len(drafts) - 1:
            key = "twitter_delay_sec" if platform == "twitter" else "quora_delay_sec"
            lo, hi = config["posting"][key]
            time.sleep(random.uniform(lo, hi))

    # 2b) Like a few relevant tweets on X (conservative engagement).
    try:
        for snippet, url in twitter_web.like_tweets(config):
            done.append(("like", snippet, url))
    except Exception as e:  # noqa: BLE001
        log.warning("X likes failed: %s", e)

    # 2b1b) Auto-join relevant finance Communities on X (capped).
    try:
        for name, url in twitter_web.join_communities(config):
            done.append(("xjoin", name, url))
    except Exception as e:  # noqa: BLE001
        log.warning("X community join failed: %s", e)

    # 2b1c) Engage WITHIN joined X communities (reply + like + optional post).
    try:
        done.extend(twitter_web.engage_communities(config))  # (action, snippet, url)
    except Exception as e:  # noqa: BLE001
        log.warning("X community engagement failed: %s", e)

    # 2b2) Follow up on people who replied to / mentioned us on X (capped).
    try:
        for snippet, url in twitter_web.reply_to_mentions(config):
            done.append(("xfollowup", snippet, url))
    except Exception as e:  # noqa: BLE001
        log.warning("X follow-ups failed: %s", e)

    # 2b3) Quora: follow up on people who commented on our answers (capped).
    try:
        for snippet, url in poster.quora_followups(config):
            done.append(("qfollowup", snippet, url))
    except Exception as e:  # noqa: BLE001
        log.warning("Quora follow-ups failed: %s", e)

    # 2b3b) Quora: auto-follow relevant finance Spaces (capped).
    try:
        for name, url in poster.quora_join_spaces(config):
            done.append(("qspace_join", name, url))
    except Exception as e:  # noqa: BLE001
        log.warning("Quora space follow failed: %s", e)

    # 2b4) Quora Spaces: comment + upvote inside finance communities.
    try:
        done.extend(poster.quora_space_engagement(config))  # (action, snippet, url) tuples
    except Exception as e:  # noqa: BLE001
        log.warning("Quora space engagement failed: %s", e)

    # 2c0) front.page: auto-join relevant finance Clubs (capped).
    try:
        for name, url in front_page_web.frontpage_join_clubs(config):
            done.append(("fpclub_join", name, url))
    except Exception as e:  # noqa: BLE001
        log.warning("front.page club join failed: %s", e)

    # 2c) front.page: post + comment + upvote.
    try:
        done.extend(front_page_web.run_engagement(config))
    except Exception as e:  # noqa: BLE001
        log.warning("front.page engagement failed: %s", e)

    # 2d0) Substack: follow relevant publications (capped).
    try:
        for name, url in substack_web.follow_publications(config):
            done.append(("sb_follow", name, url))
    except Exception as e:  # noqa: BLE001
        log.warning("Substack follow failed: %s", e)

    # 2d) Substack: post a note + reply + like + restack.
    try:
        done.extend(substack_web.run_engagement(config))
    except Exception as e:  # noqa: BLE001
        log.warning("Substack engagement failed: %s", e)

    # 3) ONE summary email with all the links (what WE did today).
    _summary_email(done, config)
    log.info("Daily run complete: %d of %d posted.", len(done), len(drafts))

    # 4) Separate digest email: what OTHER people did to you today (replies,
    #    comments, upvotes/likes, mentions, follows) — each with a link.
    try:
        notifications.run(config)
    except Exception as e:  # noqa: BLE001
        log.warning("Notification digest failed: %s", e)


if __name__ == "__main__":
    main()
