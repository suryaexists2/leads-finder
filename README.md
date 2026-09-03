# Leads Finder — Business Lead Discovery Engine

Automated discovery and qualification of **local business prospects that have no website** — gather phone + email, verify what's reachable, and push ready-to-contact leads straight to your Telegram.

Runs two complementary pipelines:

- **Maps track** — Google Business/Maps no-website local businesses (India + global).
- **Hotfrog directory track** — continuous sweeper across `hotfrog.in` / `hotfrog.com` for local service businesses (plumbers, electricians, hotels, restaurants, salons, …) whose listing shows no website.

Everything is **free / zero-spend** by design. Email addresses come from free public-resource hunts (never invented), and every website verdict follows a strict "no website means no website — not dead website" policy.

---

## Features

- **Multi-source discovery** — Maps / Google Business, Hotfrog directories, Apollo, Chocodata, Apify, SERP intent, LinkedIn.
- **Strict website triage** — 3-state verdict per business: `REJECT` (has a website), `INCONCLUSIVE` (never saved, logged for manual check), `TARGET` (confirmed no website). A broken/WAF/403/Cloudflare page is treated as **has** a website, never as a prospect.
- **Free email waterfall** — MX-verified address discovery from public sources only. Honest zeros — never fabricates an email.
- **Telegram alerts** — formatted lead cards (business, area/city, phone, email, category, why-it's-a-prospect) pushed live; all-time dedup so the same business is never re-notified.
- **Dedup everywhere** — CSV-level (name/profile-url/phone) + notification-level (phone+name) so leads stay unique end-to-end.
- **Zero spend** — no paid API calls in the discovery/verification path; local directories + Google Maps public page checks.
- **Continuous runner** — `hotfrog_runner.py` sweeps hundreds of city × category combinations in its own window, forever.
- **Budget + throttle guards** — send caps, generation caps, and pacing so bulk runs never blow quotas.

---

## Pipeline

```
Directory / SERP / Maps search
        │
        ▼
Raw listings (name, phone, address, slug)
        │
        ▼  qualify()          → phone + address present
detail page check  detail_keep()  → reject if a website is declared
        │
        ▼  maps_verify()       → 3-state verdict (REJECT / INCONCLUSIVE / TARGET)
Strict targets (confirmed no website)
        │   volume mode: keep every listing that lists no website (user's definition)
        ▼
Email hunt  resolve_email()    → free, MX-verified, never invented
        │
        ▼
storage.add_leads()            → leads.csv / leads_no_email.csv (deduped)
        │
        ▼
Notify  →  Telegram lead cards (deduped)
```

---

## Quick start

```powershell
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure secrets in .env (see .env.example pattern)
cp .env.example .env
# fill in TELEGRAM_BOT_TOKEN, TELEGRAM_USER_ID, API keys, etc.

# 3. Run one Hotfrog batch
python hotfrog_leads.py run <state> <category> <city> --volume

# 4. Run the continuous sweeper (from its own cmd window)
python hotfrog_runner.py
```

### Hotfrog batch CLI

```powershell
python hotfrog_leads.py run <state> <category> <city> [--volume] [--city-any] [--in]
#   --volume     maximum-flow mode (phone-only qualify, drop Maps identity guard)
#   --city-any   ignore city filter
#   --in         use india hotfrog.in (default: hotfrog.com US)
```

---

## Configuration (`.env`)

| Key | Purpose |
|-----|---------|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token for lead cards |
| `TELEGRAM_USER_ID` | Your Telegram chat id |
| `APOLLO_API_KEY` | Apollo.io (people search / email) |
| `CHOCODATA_API_KEY` | Chocodata LinkedIn API |
| `APIFY_API_TOKEN` | Apify actor token |
| `PROSPEO_API_KEY` | Prospeo email lookup |
| `GEMINI_API_KEY` | Gemini for cold-email drafts |
| `SMTP_*` | Outbound email identity |

> All keys are read from `.env` only — **never hardcode secrets in source**. `.env`, cookies, databases, and logs are excluded via `.gitignore`.

---

## Repository layout

```
linkedin-leads/
├── hotfrog_leads.py       # Hotfrog directory module + CLI (US & India)
├── hotfrog_runner.py      # Continuous Hotfrog sweeper (own cmd window)
├── maps_public.py         # Google Maps place lookup + website verdict
├── email_waterfall.py     # Free, MX-verified email resolution
├── storage.py             # CSV persistence + all-time dedup
├── telegram_bot.py        # Telegram lead-card notifier
├── engine.py              # Orchestration / notify pipeline
├── config.py              # .env-backed config, no hardcoded secrets
├── qualification.py       # Lead intent/quality scoring
├── scheduler_worker.py    # 30-min scheduled cycle worker
├── scheduler_watchdog.py  # Restarts the worker if it dies
├── templates/             # Simple web UI (Flask)
└── data/                  # Git-ignored: leads.csv, notified.csv, caches
```

---

## Safety & integrity guarantees

- **No website → really no website.** A 403/WAF/Cloudflare/broken page means the business *has* a website and is never treated as a prospect.
- **Emails are never invented.** Every address comes from a public, MX-verified source, or is honestly reported as not found.
- **No secrets in the repo.** `.env`, cookies, databases, and logs are ignored; API keys are read from the environment only.
- **No duplicates.** Both the saved-lead store and the Telegram notifier dedupe by business identity.

---

## License

Private / internal project. Not licensed for redistribution.
