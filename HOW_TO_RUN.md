# How to run the Intrynsic agents & dashboard

A quick, copy-paste guide. All commands assume you're in the project folder:

```powershell
cd C:\Users\vidhi\Desktop\intrynsic-agent
```

The examples below call the venv's Python directly (`.\.venv\Scripts\python.exe`)
so you don't have to activate the environment first. If you'd rather activate it
once, run `.\.venv\Scripts\Activate.ps1` and then just use `python ...`.

---

## 0. First-time setup (do this once)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

Then create your `.env` from the template and fill in real values:

```powershell
copy .env.example .env
notepad .env
```

You need: Quora email/password, X username, Substack email/password, Gmail address
+ Gmail **App Password**, and a Groq (and/or Gemini) API key.
**Never commit `.env` or any file with real credentials to git.**

---

## 1. Log into each platform once (saves a browser session)

Each script opens a browser — log in fully (solve any CAPTCHA), then come back to
the terminal and press **Enter**. Sessions are saved so the agents stay logged in.

```powershell
.\.venv\Scripts\python.exe quora_login.py        # saves quora_state.json
.\.venv\Scripts\python.exe twitter_login.py      # saves the twitter_profile/ session
.\.venv\Scripts\python.exe front_page_login.py   # saves frontpage_state.json
.\.venv\Scripts\python.exe substack_login.py     # saves substack_state.json
```

You only repeat one of these if that platform logs you out.

---

## 2. Start the dashboard

The dashboard is a small Flask server. Start it and leave it running:

```powershell
.\.venv\Scripts\python.exe poster.py
```

Then open **<http://localhost:5050>** in your browser.

- It shows every draft, its posted/approved state, and a "view" link for each
  posted item across Quora / X / front.page / Substack.
- Host/port come from `config.json` → `"dashboard"` (currently `127.0.0.1:5050`).
- Leave this window open — the page polls it live while posting.

> If port 5050 is busy, change `"port"` under `"dashboard"` in `config.json`.

---

## 3. Run the agents

### Full daily run (everything, all platforms)
Scouts topics, drafts content, posts it, engages in communities, and emails one
summary. This is exactly what the scheduled 10:00 IST run does:

```powershell
.\.venv\Scripts\python.exe daily.py
```

### Just generate drafts (no posting)
Populates `drafts.json` so you can review/approve in the dashboard before posting:

```powershell
.\.venv\Scripts\python.exe scout.py
```

With the dashboard open, review the drafts, tick the ones you approve, and use
**Post All Approved** — or just run `daily.py` to do the whole thing unattended.

---

## Automatic scheduling (optional)

- **`Intrynsic Daily`** scheduled task runs `daily.py` at 10:00 every day.
- **`Intrynsic Dashboard`** startup shortcut keeps `poster.py` running so
  <http://localhost:5050> is always available.

To run these on demand, just use the manual commands above.

---

## Where things get written
- `drafts.json` — the day's drafts (shown in the dashboard).
- `posted.json` — ledger of everything done, so nothing repeats.
- `post_status.json` — live posting status the dashboard polls.
- `logs/YYYY-MM-DD.log`, `errors.log` — posted items + warnings/errors.

---

## Quick troubleshooting
- **"Post button stayed disabled" / editor not found** — the platform's UI likely
  changed or the session expired; re-run that platform's `*_login.py`.
- **Nothing posts / login loops** — delete that platform's saved state
  (`quora_state.json`, `substack_state.json`, etc.) and log in again.
- **No summary email** — check `GMAIL_APP_PASSWORD` in `.env` (must be a 16-char
  Gmail App Password, not your normal password).
