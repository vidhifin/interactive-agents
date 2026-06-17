"""
warmup.py — 7-day Reddit karma warmup (official API / PRAW).

Run once per day (e.g. via Task Scheduler / cron) for the first week after the
account is created. Each run:
  - waits a randomized delay so it doesn't fire at the same minute daily
  - posts one simple comment in r/FreeKarma4U and r/karma
  - upvotes 3-5 posts across the assigned (no-karma-gate) subreddits
  - leaves 1-2 short, natural comments on popular posts in those subreddits
  - logs the karma count

It stops automatically after 7 days OR once the account reaches 50 karma.

Usage:
    python warmup.py            # normal daily run (with random start delay)
    python warmup.py --now      # skip the random delay (for testing)
"""

from __future__ import annotations

import sys
import time
import random
import datetime as dt

import common
from common import log

KARMA_COMMENTS = [
    "Glad to be here, hoping to learn a lot from this community.",
    "New here — this looks like a genuinely helpful sub.",
    "Happy to be part of this, thanks for having me.",
    "Just getting started, appreciate the welcome.",
    "Good to find a community like this. Looking forward to it.",
]


def _load_warmup_state() -> dict:
    state = common._read_json(common.WARMUP_STATE_PATH, {})
    if "start_date" not in state:
        state["start_date"] = dt.date.today().isoformat()
        state["done"] = False
        state["history"] = []
        common._write_json(common.WARMUP_STATE_PATH, state)
    return state


def _save_warmup_state(state: dict) -> None:
    common._write_json(common.WARMUP_STATE_PATH, state)


def _day_number(state: dict) -> int:
    start = dt.date.fromisoformat(state["start_date"])
    return (dt.date.today() - start).days + 1


def _already_ran_today(state: dict) -> bool:
    today = dt.date.today().isoformat()
    return any(h["date"] == today for h in state.get("history", []))


def _comment_in_karma_subs(reddit, config) -> int:
    """Post one simple comment in each karma subreddit. Returns count posted."""
    posted = 0
    for sub in config["warmup"]["karma_subreddits"]:
        try:
            submission = None
            for s in reddit.subreddit(sub).hot(limit=15):
                if not s.stickied:
                    submission = s
                    break
            if submission is None:
                continue
            submission.reply(random.choice(KARMA_COMMENTS))
            posted += 1
            log.info("Warmup comment in r/%s on: %s", sub, submission.title[:60])
            time.sleep(random.uniform(20, 60))
        except Exception as e:  # noqa: BLE001 - keep warmup resilient
            log.warning("Warmup karma comment failed in r/%s: %s", sub, e)
    return posted


def _upvote_across_subs(reddit, config) -> int:
    """Upvote 3-5 posts spread across the active (no-karma-gate) subreddits."""
    lo, hi = config["warmup"]["upvotes_per_day"]
    target = random.randint(lo, hi)
    subs = config["reddit"]["no_karma_gate_subreddits"]
    upvoted = 0
    attempts = 0
    while upvoted < target and attempts < target * 4:
        attempts += 1
        sub = random.choice(subs)
        try:
            posts = [s for s in reddit.subreddit(sub).hot(limit=20) if not s.stickied]
            if not posts:
                continue
            post = random.choice(posts)
            post.upvote()
            upvoted += 1
            log.info("Upvoted in r/%s: %s", sub, post.title[:60])
            time.sleep(random.uniform(8, 25))
        except Exception as e:  # noqa: BLE001
            log.warning("Warmup upvote failed in r/%s: %s", sub, e)
    return upvoted


def _natural_comments(reddit, config) -> int:
    """Leave 1-2 short, natural comments on popular posts in finance subs."""
    lo, hi = config["warmup"]["warmup_comments_per_day"]
    target = random.randint(lo, hi)
    subs = config["reddit"]["no_karma_gate_subreddits"]
    posted = 0
    attempts = 0
    while posted < target and attempts < target * 5:
        attempts += 1
        sub = random.choice(subs)
        try:
            posts = [
                s
                for s in reddit.subreddit(sub).hot(limit=15)
                if not s.stickied and s.num_comments >= 3
            ]
            if not posts:
                continue
            post = random.choice(posts)
            opp = {"subreddit": sub, "title": post.title, "selftext": (post.selftext or "")[:1500]}
            # Short, helpful, no promotion during warmup.
            reply = common.draft_reddit_reply(opp, mention_intrynsic=False, config=config)
            reply = " ".join(reply.split()[:80])  # keep warmup comments short
            post.reply(reply)
            posted += 1
            log.info("Warmup natural comment in r/%s: %s", sub, post.title[:60])
            time.sleep(random.uniform(40, 90))
        except Exception as e:  # noqa: BLE001
            log.warning("Warmup natural comment failed in r/%s: %s", sub, e)
    return posted


def main() -> None:
    config = common.load_config()
    state = _load_warmup_state()

    if state.get("done"):
        log.info("Warmup already complete. Nothing to do.")
        return

    day = _day_number(state)
    if day > config["warmup"]["duration_days"]:
        state["done"] = True
        _save_warmup_state(state)
        log.info("Warmup finished (reached %d days).", config["warmup"]["duration_days"])
        return

    if _already_ran_today(state):
        log.info("Warmup already ran today. Skipping.")
        return

    # Randomized start delay (so the daily run isn't at a fixed minute).
    if "--now" not in sys.argv:
        lo, hi = config["warmup"]["random_start_delay_min"]
        delay_min = random.uniform(lo, hi)
        log.info("Warmup day %d — sleeping %.1f min before starting.", day, delay_min)
        time.sleep(delay_min * 60)

    reddit = common.get_reddit()

    start_karma = common.reddit_karma(reddit)
    if start_karma >= config["warmup"]["karma_target"]:
        state["done"] = True
        _save_warmup_state(state)
        log.info("Karma target (%d) already reached. Warmup complete.", start_karma)
        return

    log.info("=== Warmup day %d/%d (karma: %d) ===",
             day, config["warmup"]["duration_days"], start_karma)

    karma_comments = _comment_in_karma_subs(reddit, config)
    upvotes = _upvote_across_subs(reddit, config)
    natural = _natural_comments(reddit, config)

    end_karma = common.reddit_karma(reddit)
    state.setdefault("history", []).append(
        {
            "date": dt.date.today().isoformat(),
            "day": day,
            "start_karma": start_karma,
            "end_karma": end_karma,
            "karma_sub_comments": karma_comments,
            "upvotes": upvotes,
            "natural_comments": natural,
        }
    )

    if end_karma >= config["warmup"]["karma_target"] or day >= config["warmup"]["duration_days"]:
        state["done"] = True
        log.info("Warmup complete (karma %d, day %d).", end_karma, day)

    _save_warmup_state(state)
    log.info("Warmup day %d done. Karma now %d.", day, end_karma)


if __name__ == "__main__":
    main()
