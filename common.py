"""
common.py — shared helpers for the Intrynsic engagement agent.

Holds: config/env loading, the persona, LLM-powered drafting (Groq/Gemini),
email sending, logging, and draft/ledger storage.

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
QUORA_STATE_PATH = ROOT / "quora_state.json"
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


def is_tool_query(text: str, config: dict) -> bool:
    """True if the text is asking about tools/platforms/screeners — the only place
    Intrynsic may be mentioned. Drives mention_intrynsic automatically (no human)."""
    t = (text or "").lower()
    return any(k in t for k in config.get("tool_keywords", []))


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


def now_str() -> str:
    """Human-readable local timestamp for 'posted at' display/logging."""
    return dt.datetime.now().strftime("%d %b %Y, %H:%M")


# --------------------------------------------------------------------------- #
# Recent-content memory — so generated posts don't repeat topic/opening/stocks.
# --------------------------------------------------------------------------- #
RECENT_PATH = ROOT / "recent.json"


def note_recent(kind: str, text: str, keep: int = 12) -> None:
    data = _read_json(RECENT_PATH, {})
    lst = data.get(kind, [])
    lst.append(text)
    data[kind] = lst[-keep:]
    _write_json(RECENT_PATH, data)


def recent(kind: str, n: int = 6) -> list:
    return _read_json(RECENT_PATH, {}).get(kind, [])[-n:]


def mark_draft_posted(draft_id: str, url: str = "") -> None:
    """Flag a draft as posted (and remember where + when) for the dashboard."""
    drafts = load_drafts()
    for d in drafts:
        if d["id"] == draft_id:
            d["posted"] = True
            d["posted_at"] = now_str()
            if url:
                d["posted_url"] = url
    save_drafts(drafts)


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
def send_email(subject: str, body: str, html: bool = False) -> None:
    require_env("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")
    sender = env("GMAIL_ADDRESS")
    # DIGEST_RECIPIENT may be a comma-separated list of addresses.
    recipients = [a.strip() for a in (env("DIGEST_RECIPIENT") or sender).split(",") if a.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(body, "html" if html else "plain", "utf-8"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(sender, env("GMAIL_APP_PASSWORD"))
        server.sendmail(sender, recipients, msg.as_string())
    log.info("Email sent: %s", subject)


# --------------------------------------------------------------------------- #
# Persona + Claude drafting
# --------------------------------------------------------------------------- #
PERSONA = """\
You write as a regular Indian retail investor — an ordinary working person in your
early-30s who invests on the side. You are NOT an expert, analyst, advisor, or
market guru. You sound like a normal human typing in a chat: casual, a little
informal, sometimes unsure. Not polished, not authoritative, not a lecture.

How you talk:
- Plain, everyday language. Short, natural sentences. A bit conversational and
  imperfect, like a real person — not an essay.
- Share from your own experience, but SOFTLY and tentatively: "I've personally
  found...", "for me it kind of...", "not sure if this helps, but in my case...".
  Always frame it as just one person's limited experience, never advice for all.
- You may explain EVERGREEN facts and concepts (what a ratio means, how to read a
  filing, what a term means) plainly, as information — not as a verdict.

What you must NOT claim (you have NO live or recent data):
- NEVER state current or recent market specifics as fact — no current prices, no
  "Nifty/Sensex is at X", no "at a record/all-time high or low", no "the market
  has been up/down lately", no recent results, news, or events. You cannot know
  today's market; your knowledge of levels and news is out of date, so avoid it.
- If a number is truly needed, make it clearly hypothetical ("say a stock at a
  P/E of around 20...") — never a claim about the real, current market.

Strict rules:
- NEVER give strong opinions or tell anyone what to do. No "you should", no buy/
  sell calls, no confident predictions. Hedge everything.
- NEVER call any technique, strategy, tool, stock, or approach "the best" or "the
  right way" — for investing, trading, analysis, anything. Describe trade-offs and
  what you happen to use, making clear it's only your take.
- Do NOT sound mature, wise, or guru-like. No grand lessons, no "what 10 years
  taught me" wisdom. Stay humble and ordinary.
- Vary your wording and structure. Do NOT open the same way or follow the same
  template each time — your replies must not look mass-produced or repetitive.
- Never open with "Great question!"/filler. No hype, no buzzwords, no marketing
  tone. Rare or no emoji.
- Keep Indian-market context light and in passing (NSE/BSE names, screener.in,
  filings, AGMs) — never to show off, never with current price/level claims.

Engagement:
- If someone replies to you, you may respond once more, genuinely and briefly.
  Don't keep a back-and-forth going forever — after a reply or two, let it rest.

About Intrynsic — WHEN you may mention it (and only then):
- Only if THIS specific draft's instructions say you may. If they don't, never
  bring it up at all.
- Even when allowed, mention it only if the conversation is genuinely about tools,
  platforms, apps, websites, or screeners for analysing Indian stocks, doing
  fundamental analysis, reading annual reports & filings, or a Bloomberg-terminal
  style alternative. If a tool isn't relevant to what's being discussed, skip it.
- When you do: mention it ONLY as one of the tools you personally happen to use
  (it's in early access and free right now) — never as a recommendation, never
  "the best", never the focus of your reply.
- Always include the link https://intrynsic.ai/ exactly once, right next to it.
- At most once per reply. Never force it. If unsure, leave it out.
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
    """Generate ONE genuine question to ask on Quora — varied, avoids repeats."""
    recents = recent("quora_question", 8)
    avoid = ""
    if recents:
        avoid = ("Your recent questions — do NOT ask anything similar or worded the "
                 "same way:\n- " + "\n- ".join(r[:120] for r in recents) + "\n")
    prompt = f"""\
Write ONE genuine question an ordinary Indian retail investor would naturally ask
on Quora, loosely about {topic}. Something you'd actually wonder — real and a bit
specific. One sentence, ends with a question mark, no preamble or quotes. Vary your
phrasing — do NOT always start with "How" or "What is". No current market levels/
prices. Do NOT mention Intrynsic.

{avoid}Output ONLY the question text."""
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


_FP_ANGLES = [
    "ask the community ONE genuine question you're actually unsure about",
    "share a small, humble lesson or mistake from your own investing, briefly",
    "explain ONE simple concept in plain words, like to a beginner",
    "post a short open-ended thought you're mulling over, with no firm conclusion",
    "ask what others are currently reading, using, or watching",
    "raise a small doubt about a sector or a type of stock, kept general",
    "react to a common debate (SIP vs lumpsum, large- vs small-cap, active vs passive, "
    "growth vs value) WITHOUT declaring a winner",
    "share a tiny habit or routine that works for you, softly",
    "ask for book / resource / podcast recommendations",
    "admit something you find confusing and ask how others handle it",
]


def draft_frontpage_post(config: dict, club: str | None = None) -> str:
    """Original post for front.page — rotates an angle + avoids repeating recents.

    If `club` is given, the post is tailored to that community's focus.
    """
    import random as _r

    fp = config.get("front_page", {})
    mention_line = (
        "You MAY weave in a natural Intrynsic mention with the link https://intrynsic.ai/ "
        "only if it truly fits; otherwise skip it."
        if fp.get("allow_intrynsic", False)
        else "Do NOT mention Intrynsic."
    )
    angle = _r.choice(_FP_ANGLES)
    topics = fp.get("topics") or ["the markets generally"]
    topic = _r.choice(topics)
    club_line = (
        f"This post goes into the \"{club}\" club, so keep it genuinely relevant to "
        f"that community's focus.\n"
        if club else ""
    )
    recents = recent("frontpage_post", 6)
    avoid = ""
    if recents:
        avoid = ("Here are your RECENT posts — do NOT repeat their topic, the same "
                 "stocks, or the same opening line; sound clearly different:\n- "
                 + "\n- ".join(r[:130] for r in recents) + "\n")

    prompt = f"""\
Write ONE short post for an Indian stock-market community (front.page), as an
ordinary early-30s retail investor — like a real person typing off the cuff.

{club_line}This specific post: {angle}. Loosely around {topic}.

Make it feel human and varied:
- Do NOT open with "I've been tracking/watching..." or a recital of stock tickers.
- You usually do NOT need to name specific stocks — often better not to.
- Casual, a little informal. Length can be 1 to 3 sentences (vary it).
- No "best", no predictions, no current/real market prices or index levels.
{mention_line}

{avoid}Output ONLY the post text."""
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


_TWEET_ANGLES = [
    "ask a genuine question to other investors",
    "share a small personal habit or routine, softly",
    "a short relatable thought or doubt about investing, with no conclusion",
    "a light, mildly self-deprecating thought about being a retail investor",
    "react to a common debate (SIP vs lumpsum, growth vs value, etc.) without picking a winner",
    "a tiny lesson you're still figuring out",
    "ask what others are reading / using / following",
    "an everyday observation about investing life",
]


def draft_tweet(topic: str, mention_intrynsic: bool, config: dict) -> str:
    """One original tweet — rotates an angle + avoids repeating recent tweets."""
    import random as _r

    limit = config.get("twitter", {}).get("char_limit", 280)
    mention_line = (
        "You MAY weave in a natural Intrynsic mention with the link only if it genuinely "
        "fits; otherwise skip it."
        if mention_intrynsic
        else "Do NOT mention Intrynsic."
    )
    angle = _r.choice(_TWEET_ANGLES)
    recents = recent("tweet", 6)
    avoid = ""
    if recents:
        avoid = ("Your recent tweets — do NOT repeat their angle, wording, or topic:\n- "
                 + "\n- ".join(r[:120] for r in recents) + "\n")
    prompt = f"""\
Write ONE tweet (max {limit} characters) as an ordinary early-30s Indian retail
investor — like a real person, off the cuff.

This tweet: {angle}. Loosely around {topic}.
- Vary your opening; don't sound like your other tweets.
- You usually don't need to name specific stocks; NO current prices or index
  levels, no "best", no predictions.
- Casual and human. 0-2 tasteful hashtags at most.
{mention_line}

{avoid}Output ONLY the tweet text."""
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
