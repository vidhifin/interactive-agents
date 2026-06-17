# Intrynsic — Reddit + Quora Community Engagement Agent

A human-in-the-loop assistant that finds genuine conversations in Indian
investing communities, **drafts** authentic, helpful replies in a consistent
persona, and posts only what **you approve** from a local dashboard. It mentions
[Intrynsic](https://intrynsic.ai) only where a poster is actually asking for a
tool — never as spam.

> **Nothing posts automatically.** The scout drafts; you review and approve in
> the dashboard; the poster only sends approved drafts. You are always the
> editor-in-chief.

---

## What's in the box

| File | What it does |
|------|--------------|
| `warmup.py` | 7-day Reddit karma warmup (comments, upvotes, natural replies) |
| `scout.py` | Morning scout — finds opportunities, drafts replies/answers, emails a digest |
| `poster.py` | Flask server on `localhost:5050` + the staggered auto-poster |
| `dashboard.html` | 2-tab (Reddit / Quora) approval UI |
| `common.py` | Shared helpers: config, persona, Gemini drafting, Reddit (PRAW), email, logging |
| `quora_login.py` | One-time manual Quora login that saves the browser session |
| `config.json` | Subreddits, Quora topics, keywords, timings, thresholds |
| `.env.example` | Template for all credentials (copy to `.env`) |
| `logs/` | One `YYYY-MM-DD.log` per day of posted items |
| `errors.log` | Warnings + errors across all modules |

---

## How the persona sounds

Every draft is written as a *knowledgeable but approachable Indian retail
investor* speaking from personal experience — plain language, Indian market
context (Nifty/Sensex/NSE/BSE), never condescending, never a brand voice, never
opening with "Great question!". Intrynsic is mentioned **only** on
tool/platform-recommendation posts, framed naturally as a tool you personally
use ("...it's in early access and free right now").

---

## One-time setup

### 0. Prerequisites
- Python 3.10+ installed and on your PATH.

### 1. Install dependencies
```powershell
cd C:\Users\vidhi\Desktop\intrynsic-agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```
The last line downloads the browser Playwright uses to post on Quora. (Reddit
uses the official API, not a browser.)

### 2. Create a Reddit "script" app (official OAuth API)
Reddit's **website** is blocked on your work network and on datacenter/VPN IPs,
but Reddit's **official API** (`oauth.reddit.com`) is reachable through your VPN —
so this build uses the API via PRAW. You need a one-time app:

1. On **home wifi or a personal phone hotspot** (a normal residential IP — the
   work network and the datacenter VPN both block this page), open
   <https://www.reddit.com/prefs/apps>.
2. **create another app…** → type **script**.
3. Name `intrynsic-agent`, redirect URI `http://localhost:8080`.
4. Copy the **client_id** (short string under "personal use script") and the
   **client_secret** into `.env`.

These are static strings — once saved, PRAW authenticates from this machine
through the VPN. **Keep the VPN connected** whenever the agent runs (the website
is blocked without it, and so is the API on the raw work network).

### 3. Get a Gmail App Password (for digest/confirmation emails)
1. Enable 2-Step Verification on the Gmail account.
2. Go to <https://myaccount.google.com/apppasswords>.
3. Create an app password (name it "intrynsic-agent"). Copy the 16-char code.

### 4. Get a Google Gemini API key (drafts the replies — free)
1. <https://aistudio.google.com/apikey> → **Create API key** (free tier).

### 5. Fill in credentials
```powershell
copy .env.example .env
notepad .env
```
Paste in every value: Reddit username/password, Reddit client_id/secret, Quora
email/password, Gmail address + app password, and your Gemini API key.
**Keep `.env` local — never commit it or paste it anywhere.**

### 6. (Optional) tune `config.json`
- `scout_time` — when you'll schedule the morning scout (default `07:00`, IST).
- Subreddits, Quora topics, keyword lists, posting delays, warmup settings.

---

## First run (do these in order)

> **Keep your VPN connected** for anything that touches Reddit. The Reddit API
> works through the VPN; the raw work network blocks it.

### A. Quora login (REQUIRED — saves a session)
Quora hides its question feed and the answer editor behind login, so the scout
can't find questions and the poster can't answer until you've logged in once:
```powershell
python quora_login.py
```
A browser opens; log in fully (solve any CAPTCHA), then press Enter — the session
is saved to `quora_state.json` and reused by both the scout (scraping topics) and
the poster (answering). Without this, Quora returns 0 questions.

### B. Run a scout
```powershell
python scout.py
```
This drafts up to 8 Reddit + 5 Quora replies into `drafts.json` and emails you a
digest.

### C. Open the dashboard
```powershell
python poster.py
```
Then open <http://localhost:5050>. Review each card, edit the text inline, tick
**Approve**, and click **🚀 POST EVERYTHING APPROVED** (or per-tab). Ticks turn
green as each post lands; you get a confirmation email when the run finishes.

### D. (Days 1–7) run the warmup in the background
```powershell
python warmup.py
```
Run it once per day for the first week. It stops automatically after 7 days or
once the account hits 50 karma.

---

## Timeline (handled automatically)

- **Day 1:** Quora is active, plus the 4 low-barrier subreddits
  (`IndiaInvestments`, `IndianStockMarket`, `DalalStreetTalks`,
  `personalfinanceindia`). The scout uses these automatically.
- **Days 1–7:** run `warmup.py` daily in the background.
- **Day 8+:** the scout automatically expands to **all 10 subreddits**.

The "what day is it" logic is based on `state.json` (created on your first
scout/warmup run). No manual switch needed.

---

## Scheduling the morning scout

### Windows Task Scheduler
1. Open **Task Scheduler** → **Create Basic Task**.
2. Trigger: **Daily** at your `scout_time` (e.g. 07:00).
3. Action: **Start a program**
   - Program/script: `C:\Users\vidhi\Desktop\intrynsic-agent\.venv\Scripts\python.exe`
   - Arguments: `scout.py`
   - Start in: `C:\Users\vidhi\Desktop\intrynsic-agent`
4. Add a second daily task the same way for `warmup.py` (for the first week).

Leave `poster.py` running (or start it when you want to review). The digest email
links you straight to the dashboard.

---

## How drafts are scored & when Intrynsic gets mentioned

- **Reddit:** posts from the last 24h with score > 10 and comments > 5 are
  scored 1–10 on keyword relevance, whether they're a question, and engagement.
  Top-level comments that look like unanswered questions are also surfaced. Top 8
  are drafted. Intrynsic is mentioned **only** when the post matches
  tool/platform-recommendation keywords (`which tool`, `platform recommendation`,
  `Bloomberg alternative`, `stock analytics`, …).
- **Quora:** new questions are pulled per topic via RSS, unanswered/low-answer
  ones are prioritized, top 5 are drafted (direct answer → explanation → optional
  natural mention).

Every draft is tagged in `drafts.json` by platform, post/question id,
subreddit/topic, opportunity score, and whether it mentions Intrynsic.

---

## Files written at runtime
- `drafts.json` — current batch of drafts (edited live by the dashboard).
- `state.json` — start date (drives the Day 1 → Day 8 subreddit gating).
- `warmup_state.json` — warmup progress and daily history.
- `quora_state.json` — saved Quora login cookies.
- `post_status.json` — live posting status the dashboard polls.
- `logs/YYYY-MM-DD.log` — every posted item (timestamp, platform, URL, text).
- `errors.log` — warnings and errors.

---

## Troubleshooting
- **`Missing required values in .env`** — you skipped a field; open `.env`.
- **Reddit calls fail / "blocked by network security"** — your VPN dropped, or
  it's exiting through an IP Reddit's website blocks. Reconnect the VPN (the API
  needs it on this network). The API (`oauth.reddit.com`) works through the VPN
  even though the website doesn't.
- **Reddit `401` / `invalid_grant`** — wrong `client_id`/`client_secret` or
  username/password in `.env`. Re-create the script app (from a residential
  connection) and re-copy the values.
- **Reddit `RATELIMIT`** — the poster waits 60s and retries automatically; new
  accounts are rate-limited harder, which is exactly why the warmup exists. Posts
  are staggered 5–15 min apart.
- **Quora post fails / can't find Answer button** — Quora changes its HTML
  often. Do the manual headful login (step A), and if needed adjust the
  selectors in `poster.py` (`_post_quora`). Errors are logged to `errors.log`.
- **No Quora drafts** — Quora sometimes throttles RSS; rerun the scout later.
- **Emails not arriving** — confirm you used a Gmail **App Password**, not your
  normal password, and that 2-Step Verification is on.

---

## Please use this responsibly
These platforms have rules about automation and self-promotion. This tool is
built to keep a human in the loop and to add genuine value first — keep it that
way. Read and respect each platform's Terms of Service and self-promotion norms,
disclose your affiliation where a community expects it, and don't let approval
become a rubber stamp. Authentic participation is the whole point; spam will get
the account banned and won't help Intrynsic.
