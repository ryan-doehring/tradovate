# tradovate-bot

Automated sync between a Google Sheet trade plan and Tradovate bracket orders (entry + take profit + stop loss), with USD high-impact news protection and end-of-day position flattening.

Runs on **GitHub Actions** (free tier). Defaults to **`dry_run`** mode — logs planned actions without placing, cancelling, or closing anything.

## What it does

| Job | Schedule | Behavior |
|-----|----------|----------|
| **trade-sync** | 4× daily (weekdays) | Sheet is source of truth → place/cancel bracket orders |
| **news-guard** | Every 5 min (session) | Flatten 5 min before USD red news; re-place 30 min after |
| **eod-flat** | 1× daily | Close **positions only** before session close (leave working orders) |

## Quick start (local)

```powershell
cd c:\Users\ryand\Repos\tradovate
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env
# Edit .env, then:
tradovate-bot sync --dry-run
pytest
```

## Setup

**Your checklist for secrets, API keys, and go-live steps:** see [SETUP.md](SETUP.md).

## Google Sheet format (`VP 2026` tab)

Active rows have an empty **Outcome** cell. Any Outcome (`Win`, `Loss`, `Miss`) deactivates the row.

| Column | Field |
|--------|-------|
| A | Date Added |
| B | Ticker (e.g. `MES`, `MNQ`) — traded as listed |
| D | Short/Long |
| E | # Contracts (defaults to **1** if empty) |
| G | Entry |
| I | Target (take profit) |
| J | SL (stop loss) |

Orders match on **product root + quantity + entry price**. Contract month is auto-resolved to the active front month via Tradovate (`contract/suggest`, skipping maturities within 14 days of expiry).

## Safety

- `TRADING_MODE=dry_run` — read + plan only (default in Actions until you change it)
- `TRADING_MODE=demo` — writes to demo Tradovate account
- `TRADING_MODE=live` — real trading (use GitHub Environment approval on `live`)

Every run uploads a JSON artifact with planned and executed actions.

## Branch note

Scheduled workflows run on the **default branch** (`main`). Develop on `initial`, merge to `main` when ready.

## Project layout

```
src/tradovate_bot/     Python package
.github/workflows/     GitHub Actions
tests/                 Unit tests
SETUP.md               Secrets & go-live checklist
```
