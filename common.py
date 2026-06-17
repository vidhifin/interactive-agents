"""
common.py — shared helpers for the Intrynsic engagement agent.

Holds: config/env loading, the persona, Gemini-powered drafting, the Reddit
(PRAW) client, email sending, logging, draft/state storage, and the day-gating
logic that decides which subreddits are active.

Nothing in here posts anything on its own — posting lives in poster.py.
"""

from __future__ import annotations

import os
import json
import time
import smtplib
import logging
import datetime as dt
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

from dotenv import load_dotenv

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
ENV_PATH = ROOT / ".env"
DRAFTS_PATH = ROOT / "drafts.json"
STATE_PATH = ROOT / "state.json"
WARMUP_STATE_PATH = ROOT / "warmup_state.json"
QUORA_STATE_PATH = ROOT / "quora_state.json"
TWITTER_STATE_PATH = ROOT / "twitter_state.json"
# X is bot-hostile: use a persistent REAL-Chrome profile (looks like a normal
# browser) instead of a saved storage_state, so logins aren't flagged.
TWITTER_PROFILE_DIR = ROOT / "twitter_profile"
FRONTPAGE_STATE_PATH = ROOT / "frontpage_state.json"
POST_STATUS_PATH = ROOT / "post_status.json"
LOGS_DIR = ROOT / "logs"
ERRORS_LOG = ROOT / "errors.log"

LOGS_DIR.mkdir(exist_ok=True)

# Load .env once, on import.
load_dotenv(ENV_PATH)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def get_logger(name: str = "intrynsic") -> logging.Logger:
    """A logger that writes INFO+ to the console and WARNING+ to errors.log."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    err_file = logging.FileHandler(ERRORS_LOG, encoding="utf-8")
    err_file.setLevel(logging.WARNING)
    err_file.setFormatter(fmt)
    logger.addHandler(err_file)

    return logger


log = get_logger()


def log_post(platform: str, url: str, text: str) -> None:
    """Append a posted item to logs/YYYY-MM-DD.log."""
    today = dt.date.today().isoformat()
    line = (
        f"{dt.datetime.now().isoformat(timespec='seconds')} | {platform.upper()} | "
        f"{url}\n--- draft ---\n{text}\n{'=' * 70}\n"
    )
    with open(LOGS_DIR / f"{today}.log", "a", encoding="utf-8") as f:
        f.write(line)


# --------------------------------------------------------------------------- #
# Config / env
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default) or default


def require_env(*keys: str) -> None:
    """Raise a clear error if any required credential is missing from .env."""
    missing = [k for k in keys if not env(k)]
    if missing:
        raise RuntimeError(
            "Missing required values in .env: "
            + ", ".join(missing)
            + ".  Copy .env.example to .env and fill them in."
        )


# --------------------------------------------------------------------------- #
# State (start date -> day gating)
# --------------------------------------------------------------------------- #
def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_state() -> dict:
    """Returns persistent state, creating start_date on first call."""
    state = _read_json(STATE_PATH, {})
    if "start_date" not in state:
        state["start_date"] = dt.date.today().isoformat()
        _write_json(STATE_PATH, state)
    return state


def days_since_start() -> int:
    start = dt.date.fromisoformat(get_state()["start_date"])
    return (dt.date.today() - start).days + 1  # day 1 on the first run


def active_subreddits(config: dict) -> list[str]:
    """
    Day 1-7: only the four no-karma-gate subreddits.
    Day 8+:  all subreddits.
    """
    if days_since_start() >= 8:
        return config["reddit"]["all_subreddits"]
    return config["reddit"]["no_karma_gate_subreddits"]


# --------------------------------------------------------------------------- #
# Drafts
# --------------------------------------------------------------------------- #
def load_drafts() -> list[dict]:
    return _read_json(DRAFTS_PATH, [])


def save_drafts(drafts: list[dict]) -> None:
    _write_json(DRAFTS_PATH, drafts)


# --------------------------------------------------------------------------- #
# Posted ledger — remembers what we've already answered, so the scout never
# re-queues it and the poster never tries to answer it twice.
# --------------------------------------------------------------------------- #
POSTED_PATH = ROOT / "posted.json"


def load_posted() -> set:
    return set(_read_json(POSTED_PATH, []))


def add_posted(identifier: str) -> None:
    data = _read_json(POSTED_PATH, [])
    if identifier not in data:
        data.append(identifier)
        _write_json(POSTED_PATH, data)


def mark_draft_posted(draft_id: str, url: str = "") -> None:
    """Flag a draft as posted (and remember where) so the dashboard can link to it."""
    drafts = load_drafts()
    for d in drafts:
        if d["id"] == draft_id:
            d["posted"] = True
            if url:
                d["posted_url"] = url
    save_drafts(drafts)


# --------------------------------------------------------------------------- #
# Reddit (PRAW — official OAuth API)
# --------------------------------------------------------------------------- #
def get_reddit():
    """Authenticated PRAW client for the single engagement account."""
    import praw

    require_env(
        "REDDIT_USERNAME",
        "REDDIT_PASSWORD",
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
    )
    config = load_config()
    user_agent = f"{config['reddit']['user_agent']} (by u/{env('REDDIT_USERNAME')})"
    return praw.Reddit(
        client_id=env("REDDIT_CLIENT_ID"),
        client_secret=env("REDDIT_CLIENT_SECRET"),
        username=env("REDDIT_USERNAME"),
        password=env("REDDIT_PASSWORD"),
        user_agent=user_agent,
    )


def reddit_karma(reddit) -> int:
    me = reddit.user.me()
    return int(getattr(me, "comment_karma", 0)) + int(getattr(me, "link_karma", 0))


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
def send_email(subject: str, body: str, html: bool = False) -> None:
    require_env("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")
    sender = env("GMAIL_ADDRESS")
    recipient = env("DIGEST_RECIPIENT") or sender

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(body, "html" if html else "plain", "utf-8"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(sender, env("GMAIL_APP_PASSWORD"))
        server.sendmail(sender, [recipient], msg.as_string())
    log.info("Email sent: %s", subject)


# --------------------------------------------------------------------------- #
# Persona + Claude drafting
# --------------------------------------------------------------------------- #
PERSONA = """\
You write as a knowledgeable but approachable Indian retail investor in your \
early-30s (about 33).

Voice rules (follow strictly):
- Frame everything from your life-stage: you started investing ~10 years ago, \
early in your career, and have lived through real market events — the 2020 COVID \
crash and recovery, the 2018 NBFC/IL&FS scare, demonetization. Let that show in \
your references and perspective (a working professional balancing a job, a salary, \
and a growing portfolio). Do NOT state your age outright or force it — let it come \
through naturally.
- Speak from personal experience, not theory. Use "I" — what worked for you, \
what confused you, what you tried.
- Explain complex ideas (DCF, ratios, filings analysis, valuation) in simple, \
plain language. Never condescending.
- Occasionally ground things in Indian market context — Nifty, Sensex, specific \
NSE/BSE names, BSE/NSE filings, annual reports, AGMs, screener.in habits, etc.
- Come across as a fellow investor sharing what's worked, never a salesperson \
or a brand account.

Hard bans:
- Never open with "Great question!", "Great post!", "Thanks for sharing", or any \
similar filler.
- No marketing language, no hype, no buzzwords, no exclamation-heavy tone.
- No emoji unless it would be genuinely natural and rare.
- Don't sound like an ad or a press release. Write like a real Reddit/Quora comment.

About the product (mention ONLY when explicitly told to in the task):
- Intrynsic is an AI-powered Indian stock analytics platform — a \
Bloomberg-terminal alternative for retail investors covering NSE/BSE. It is in \
early access and free right now.
- ALWAYS include the full link https://intrynsic.ai/ whenever you mention \
Intrynsic — exactly once, right where you name it. Never mention it without the link.
- Frame it naturally as one of the tools you personally use, tied to the specific \
thing the poster is asking about. One mention, woven in, never the focus of the \
reply. Example of the right touch: "I've also been using Intrynsic \
(https://intrynsic.ai/) lately which pulls all of this into one place — it's in \
early access and free right now."
- If the task says NOT to mention it, do not mention it or the link at all. Just \
be genuinely helpful.
"""


def _gemini_client():
    from google import genai

    require_env("GEMINI_API_KEY")
    return genai.Client(api_key=env("GEMINI_API_KEY"))


_LAST_LLM_TS = 0.0


def _throttle(min_interval: float) -> None:
    """Keep at least `min_interval` seconds between Gemini calls.

    The free tier allows only ~5 requests/minute, so we pace calls to stay under
    that instead of hammering and hitting 429s.
    """
    global _LAST_LLM_TS
    wait = min_interval - (time.monotonic() - _LAST_LLM_TS)
    if wait > 0:
        time.sleep(wait)
    _LAST_LLM_TS = time.monotonic()


def _gemini_complete(system: str, user: str, llm: dict) -> str:
    from google.genai import types

    client = _gemini_client()
    resp = client.models.generate_content(
        model=llm["model"],
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=llm.get("max_tokens", 1500),
            temperature=llm.get("temperature", 0.8),
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    return (resp.text or "").strip()


def _groq_complete(system: str, user: str, llm: dict) -> str:
    """Groq (OpenAI-compatible) chat completion — free, high daily limit."""
    import requests

    require_env("GROQ_API_KEY")
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {env('GROQ_API_KEY')}",
                 "Content-Type": "application/json"},
        json={
            "model": llm["model"],
            "max_tokens": llm.get("max_tokens", 1500),
            "temperature": llm.get("temperature", 0.8),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Groq {r.status_code}: {r.text[:200]}")
    return (r.json()["choices"][0]["message"]["content"] or "").strip()


def _complete(user_prompt: str, config: dict) -> str:
    """Draft text in the persona voice via the configured provider (gemini|groq).

    Retries on transient errors (rate limit / overload / timeout) with backoff.
    """
    llm = config["llm"]
    provider = llm.get("provider", "gemini")
    _throttle(llm.get("min_interval_sec", 2 if provider == "groq" else 13))

    last_err = None
    for attempt in range(4):
        try:
            if provider == "groq":
                return _groq_complete(PERSONA, user_prompt, llm)
            return _gemini_complete(PERSONA, user_prompt, llm)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            transient = any(
                s in msg
                for s in ("503", "502", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED",
                          "overloaded", "high demand", "rate", "timeout", "Timeout")
            )
            if not transient or attempt == 3:
                raise
            last_err = e
            wait = 3 * (attempt + 1)
            log.warning("%s transient error (%s) — retrying in %ds", provider, msg[:60], wait)
            time.sleep(wait)
    raise RuntimeError(f"{provider} failed after retries: {last_err}")


def draft_reddit_reply(opportunity: dict, mention_intrynsic: bool, config: dict) -> str:
    """Draft a 100-200 word Reddit reply in the persona voice."""
    lo, hi = config["reddit"]["reply_word_range"]
    mention_line = (
        "This post IS asking for a tool/platform recommendation, so naturally "
        "mention Intrynsic ONCE, tied to what they're asking for. Frame it as a "
        "tool you personally use, in early access and free. Keep it understated."
        if mention_intrynsic
        else "This post is NOT asking for a tool recommendation. Do NOT mention "
        "Intrynsic or the URL at all — just be genuinely helpful."
    )
    body = opportunity.get("selftext") or "(no body text — respond to the title)"
    prompt = f"""\
Write a Reddit reply ({lo}-{hi} words) to the post below, in r/{opportunity['subreddit']}.

TITLE: {opportunity['title']}
BODY: {body[:1500]}

{mention_line}

Be specific and actually useful — give concrete steps, numbers, or where to look,
not vague encouragement. Output ONLY the reply text, nothing else."""
    return _complete(prompt, config)


def draft_quora_answer(opportunity: dict, mention_intrynsic: bool, config: dict) -> str:
    """Draft a 200-400 word Quora answer: direct answer -> explanation -> optional mention."""
    lo, hi = config["quora"]["answer_word_range"]
    mention_line = (
        "If — and only if — it fits naturally, you may mention Intrynsic ONCE near "
        "the end as a tool you use, in early access and free. If it doesn't fit, skip it."
        if mention_intrynsic
        else "Do NOT mention Intrynsic or the URL — just answer well."
    )
    prompt = f"""\
Write a Quora answer ({lo}-{hi} words) to this question in the topic "{opportunity['topic']}".

QUESTION: {opportunity['title']}

Structure: direct answer first, then a clear explanation with concrete Indian-market
detail, then {mention_line}

Output ONLY the answer text, nothing else."""
    return _complete(prompt, config)


def draft_quora_question(topic: str, config: dict) -> str:
    """Generate ONE genuine question to ask on Quora (no promotion)."""
    prompt = f"""\
Write ONE genuine question an Indian retail investor would naturally ask on Quora
in the "{topic}" topic. Make it specific and real — something you'd actually wonder
about (Indian markets, Nifty/Sensex, a sector, NSE/BSE filings, ratios, valuation).
One sentence, ends with a question mark, no preamble or quotes. Do NOT mention Intrynsic.

Output ONLY the question text."""
    return _complete(prompt, config).strip().strip('"')


def draft_quora_comment(question_title: str, answer_snippet: str,
                        mention_intrynsic: bool, config: dict) -> str:
    """Short, genuine comment replying to someone's answer."""
    lo, hi = config["quora"].get("comment_word_range", [20, 60])
    mention_line = (
        "You MAY add a brief, natural Intrynsic mention ONLY if it genuinely fits; "
        "otherwise skip it entirely."
        if mention_intrynsic
        else "Do NOT mention Intrynsic."
    )
    snippet = (answer_snippet or "")[:800] or "(answer text unavailable — engage with the question)"
    prompt = f"""\
Write a SHORT Quora comment ({lo}-{hi} words) replying to the answer below, as a
fellow Indian retail investor. Engage genuinely — agree and add a point, share a
quick personal experience, or ask a thoughtful follow-up. Conversational, not an
essay. No "Great answer!" filler. {mention_line}

QUESTION: {question_title}
THE ANSWER YOU ARE COMMENTING ON: {snippet}

Output ONLY the comment text."""
    return _complete(prompt, config)


def draft_frontpage_post(config: dict) -> str:
    """Original post for the front.page Indian stock community."""
    fp = config.get("front_page", {})
    mention_line = (
        "You MAY weave in a natural Intrynsic mention with the link https://intrynsic.ai/ "
        "only if it truly fits; otherwise skip it."
        if fp.get("allow_intrynsic", False)
        else "Do NOT mention Intrynsic."
    )
    prompt = f"""\
Write a SHORT original post for an Indian stock-market community (front.page), as
an early-30s retail investor. A genuine market view, observation, or tip — 2 to 4
sentences, specific (name a stock/sector/Nifty level/ratio), conversational. No
"Great...", no hype, at most 1-2 hashtags. {mention_line}

Output ONLY the post text."""
    return _complete(prompt, config).strip().strip('"')


def draft_frontpage_comment(post_text: str, config: dict) -> str:
    """Short, genuine comment on a front.page post."""
    fp = config.get("front_page", {})
    mention_line = (
        "You MAY add a natural Intrynsic mention with the link only if it truly fits; "
        "otherwise skip it."
        if fp.get("allow_intrynsic", False)
        else "Do NOT mention Intrynsic."
    )
    prompt = f"""\
Write a SHORT comment (1-3 sentences) replying to this post on an Indian
stock-market community, as a fellow early-30s retail investor. Engage genuinely —
agree and add a point, share a quick experience, or ask a thoughtful question.
Conversational, no "Great post" filler. {mention_line}

THE POST: {post_text[:600]}

Output ONLY the comment text."""
    return _complete(prompt, config).strip().strip('"')


def draft_tweet(topic: str, mention_intrynsic: bool, config: dict) -> str:
    """One original tweet in the persona voice (<= char limit)."""
    limit = config.get("twitter", {}).get("char_limit", 280)
    mention_line = (
        "You MAY weave in a natural Intrynsic mention ONLY if it genuinely fits; "
        "otherwise skip it."
        if mention_intrynsic
        else "Do NOT mention Intrynsic."
    )
    prompt = f"""\
Write ONE tweet (max {limit} characters) as an Indian retail investor — a genuine,
useful market insight, observation, or tip about {topic} (Nifty/Sensex/NSE/BSE,
fundamentals, valuation). Specific and conversational, not generic. No "Great
question", no hashtag spam (0-2 tasteful hashtags at most). {mention_line}

Output ONLY the tweet text."""
    t = _complete(prompt, config).strip().strip('"')
    if len(t) > limit:  # drop the partial last word instead of cutting mid-word
        t = t[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return t


def draft_tweet_reply(tweet_text: str, mention_intrynsic: bool, config: dict) -> str:
    """Short, genuine reply to someone else's tweet (<= char limit)."""
    limit = config.get("twitter", {}).get("char_limit", 280)
    mention_line = (
        "You MAY add a natural Intrynsic mention with the link only if it truly fits; "
        "otherwise skip it."
        if mention_intrynsic
        else "Do NOT mention Intrynsic."
    )
    prompt = f"""\
Write a SHORT reply (max {limit} characters) to the tweet below, as an Indian
retail investor in your early-30s. Engage genuinely — add a point, share a quick
personal experience, or ask a thoughtful question. Conversational, no "Great
tweet" filler, at most 1 hashtag. {mention_line}

THE TWEET YOU ARE REPLYING TO: {tweet_text[:500]}

Output ONLY the reply text."""
    t = _complete(prompt, config).strip().strip('"')
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return t
