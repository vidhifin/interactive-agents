# Intrynsic — Community Engagement Agent

A fully-automatic agent that engages in Indian investing communities on **Quora**,
**X/Twitter**, **front.page**, and **Substack** — drafting authentic, helpful
content in a consistent persona and posting it daily. It mentions
[Intrynsic](https://intrynsic.ai) only where someone is genuinely asking about a
tool/screener/platform — never as spam.

> Runs automatically at **10:00 IST** every day (`daily.py`). A local dashboard
> (`poster.py`) stays on so you can see everything that was drafted and posted,
> with view links. One summary email is sent after each run.

---

## What's in the box

| File | What it does |
|------|--------------|
| `daily.py` | The 10:00 auto-run — scouts, posts, engages in communities, emails one summary |
| `scout.py` | Generates the day's Quora + X drafts (answers, comments, a question, tweets, replies) |
| `poster.py` | Flask dashboard on `localhost:5050` + Quora posting + Quora-Space engagement |
| `twitter_web.py` | X/Twitter automation: tweet, reply, like, join + engage Communities, follow-ups |
| `front_page_web.py` | front.page automation: post, comment, upvote, join Clubs |
| `substack_web.py` | Substack automation: post Notes, reply, like + restack, follow publications |
| `dashboard.html` | Quora / Twitter / front.page / Substack dashboard UI (posted cards show date + view link) |
| `common.py` | Shared helpers: config/env, persona, LLM drafting (Groq/Gemini), email, logging, ledger |
| `*_login.py` | One-time manual logins that save each platform's browser session |
| `config.json` | All knobs: platforms, keywords, per-community quotas, timings |
| `.env.example` | Template for credentials (copy to `.env`) |
| `logs/`, `errors.log` | Per-day posted items + warnings/errors |

---

## How the persona sounds

Every draft is written as an *ordinary Indian retail investor in their early-30s* —
humble, casual, sometimes unsure, speaking softly from personal experience. Never
an expert/guru, never strong opinions, never "the best", and never citing current
market specifics (no live prices or index levels, which go stale). Intrynsic is
mentioned **only** on genuine tool/screener/platform questions, always with the
`https://intrynsic.ai/` link.

---

## One-time setup

### 1. Install dependencies
```powershell
cd C:\Users\vidhi\Desktop\intrynsic-agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

### 2. Get a Groq API key (drafts the content — free, high daily limit)
<https://console.groq.com/keys> → create a key. (Optional fallback: a Gemini key
from <https://aistudio.google.com/apikey>, but its free tier is only ~20/day.)

### 3. Get a Gmail App Password (for the summary email)
Enable 2-Step Verification, then create an app password at
<https://myaccount.google.com/apppasswords> (16 characters).

### 4. Fill in credentials
```powershell
copy .env.example .env
notepad .env
```
Add: Quora email/password, your X username, Gmail address + app password, and the
Groq API key. **Keep `.env` local — never commit it.**

### 5. Log into each platform once (saves a browser session)
```powershell
python quora_login.py        # saves quora_state.json
python twitter_login.py      # logs into a persistent real-Chrome profile
python front_page_login.py   # saves frontpage_state.json
python substack_login.py     # tries auto sign-in, then saves substack_state.json
```
Each opens a browser; log in fully (solve any CAPTCHA), then return and press Enter.
For Substack, add `SUBSTACK_EMAIL` + `SUBSTACK_PASSWORD` to `.env` first — the login
script attempts an automatic sign-in, but if your account uses a magic link or
Google, just finish logging in by hand in the open window before pressing Enter.

---

## Running it

- **Automatic:** the `Intrynsic Daily` scheduled task runs `daily.py` at 10:00.
  The `Intrynsic Dashboard` startup shortcut keeps `poster.py` running so the
  dashboard at <http://localhost:5050> is always available.
- **Manual run now:**
  ```powershell
  .\.venv\Scripts\python.exe daily.py     # do the full daily run immediately
  .\.venv\Scripts\python.exe poster.py    # just the dashboard
  ```

Each daily run, per the current `config.json`, engages within up to **6
communities per platform** (2 upvotes + 1 comment in each), posts originals,
auto-joins relevant new communities, and does follow-up replies — then sends one
summary email with links.

---

## Files written at runtime
- `drafts.json` — the day's drafts (the dashboard shows posted state + view links).
- `posted.json` — the ledger of everything done, so nothing repeats.
- `recent.json` — recent content memory, used to avoid repetitive wording.
- `quora_state.json`, `frontpage_state.json`, `twitter_profile/` — saved sessions.
- `post_status.json` — live posting status the dashboard polls.
- `logs/YYYY-MM-DD.log`, `errors.log` — posted items + warnings/errors.

---

## Please use this responsibly
These platforms have rules about automation and self-promotion. Keep the volumes
conservative (the per-community quotas in `config.json` are deliberately modest),
add genuine value first, and respect each platform's Terms of Service. Aggressive
automation gets accounts banned and doesn't help Intrynsic.
