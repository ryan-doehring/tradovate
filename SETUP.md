# Setup Checklist (Your Tasks)

Complete these steps before enabling live trading. The bot defaults to **`TRADING_MODE=dry_run`**, which logs intended actions without placing, cancelling, or closing anything.

---

## 1. GitHub Environments

You already created **`demo`** and **`live`**. Add the secrets below to each environment.

| Secret | `demo` | `live` | Notes |
|--------|--------|--------|-------|
| `TRADOVATE_USERNAME` | Demo username | Live username | Tradovate login |
| `TRADOVATE_PASSWORD` | Demo password | Live password | Or API-dedicated password |
| `TRADOVATE_APP_ID` | Demo app ID | Live app ID | From API key creation |
| `TRADOVATE_CID` | Demo cid (integer) | Live cid | Shown when key is created |
| `TRADOVATE_SEC` | Demo secret | Live secret | Shown once at key creation |
| `TRADOVATE_API_URL` | `https://demo.tradovateapi.com/v1` | `https://live.tradovateapi.com/v1` | Base API URL |
| `TRADING_MODE` | `dry_run` → then `demo` | `dry_run` → then `live` | Start with `dry_run` always |

**Recommended protection on `live`:** Settings → Environments → `live` → Required reviewers (you).

---

## 2. Repository Secrets (shared)

Settings → Secrets and variables → Actions → **Repository secrets**:

| Secret | Value |
|--------|-------|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full JSON key file from Google Cloud (see step 3) |
| `GOOGLE_SHEET_ID` | `<sheet-id>` — the long id in your spreadsheet URL (`docs.google.com/spreadsheets/d/<sheet-id>/edit`). Keep it in repo secrets, not in the repo. |
| `GOOGLE_SHEET_TAB` | `VP 2026` |

Optional repository **variables** (non-secret defaults):

| Variable | Default | Purpose |
|----------|---------|---------|
| `DEFAULT_CONTRACTS` | `1` | Used when `# Contracts` cell is empty |
| `NEWS_BUFFER_MINUTES` | `5` | Flatten before high-impact USD news |
| `NEWS_REOPEN_MINUTES` | `30` | Re-place orders after news passes |
| `EOD_MINUTES_BEFORE_CLOSE` | `5` | Flatten positions before session close |

---

## 3. Google Sheets API

1. Open [Google Cloud Console](https://console.cloud.google.com/).
2. Create a project (e.g. `tradovate-bot`).
3. **APIs & Services → Library** → enable **Google Sheets API**.
4. **APIs & Services → Credentials → Create Credentials → Service account**.
5. Create a JSON key and download it.
6. Open your spreadsheet and **Share** with the service account email (`…@….iam.gserviceaccount.com`) as **Viewer**.
7. Paste the entire JSON into GitHub secret `GOOGLE_SERVICE_ACCOUNT_JSON`.

---

## 4. Tradovate API Keys

1. Log in to [Tradovate](https://trader.tradovate.com/).
2. **Application Settings → API Access → Generate API Key**.
3. Create separate keys for **demo/sim** and **live** if you have both.
4. Save: username, password, `appId`, `cid`, `sec` immediately (`sec` is shown once).

Docs: [Tradovate API](https://api.tradovate.com/) · [Auth overview](https://partner.tradovate.com/overview/quick-setup/auth-overview)

---

## 5. Local development

```powershell
cd c:\Users\ryand\Repos\tradovate
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
py -3 -m pip install -e ".[dev]"
copy .env.example .env
# Edit .env with your credentials (never commit .env)
```

Run locally (dry run):

```powershell
tradovate-bot sync --dry-run
tradovate-bot news-guard --dry-run
tradovate-bot eod-flat --dry-run
```

Run tests:

```powershell
pytest
```

---

## 6. Go-live sequence

| Phase | `TRADING_MODE` | Environment | What to verify |
|-------|----------------|-------------|----------------|
| 1 | `dry_run` | `demo` | Actions artifacts show correct planned bracket orders |
| 2 | `demo` | `demo` | Demo account receives OSO brackets matching sheet |
| 3 | `dry_run` | `live` | Same artifacts against live reads (no writes) |
| 4 | `live` | `live` | Real trading with approval gate on `live` environment |

Use **Actions → Run workflow** (`workflow_dispatch`) before relying on schedules.

---

## 7. Merge to `main`

Scheduled workflows only run on the **default branch**. When satisfied with testing on `initial`, merge to `main`.

---

## 8. Forex Factory / news data

There is **no official free Forex Factory API**. This project uses a lightweight scraper of the public calendar page (USD + high impact only). If parsing breaks, workflows fail loudly and upload debug artifacts.

Alternatives if scraping becomes unreliable:

- [forexfactory](https://pypi.org/project/forexfactory/) PyPI package (unofficial, free)
- [Apify Forex Factory actor](https://apify.com/scrapemint/forexfactory-economic-calendar) (paid)

---

## 9. Holiday / early-close data

Uses the free **[pandas_market_calendars](https://github.com/rsheftel/pandas_market_calendars)** library with the **`CME_Equity`** calendar (covers ES/MES/NQ/MNQ equity index futures).

- Full holidays → skip sync/EOD actions
- Early-close days → EOD flatten uses the early `market_close` time minus buffer

**Note:** Holiday close times vary (e.g. 1:00 PM CT vs 12:00 PM CT). The calendar provides the official close time per date; we do not assume a single holiday close time.

## 9b. Front-month / most-active contract

Sheet tickers like `MES` are resolved to a specific maturity (e.g. `MESU6`) using Tradovate:

1. `GET /contract/suggest?t=MES` (ordered best-first)
2. Skip contracts with fewer than **14 days** until expiry (avoids low-liquidity near roll)
3. Fall back to `POST /contract/rollcontract`

CME’s old public “contracts by volume” HTTP APIs are no longer available. Tradovate’s suggest ordering plus the 14-day roll buffer is the free, reliable approach for micros.
---

## 10. Workflow schedule summary

| Workflow | Frequency | Purpose |
|----------|-----------|---------|
| `trade-sync` | 4× daily (CT) | Reconcile sheet → Tradovate bracket orders |
| `news-guard` | Every 5 min, US session | Blackout flatten + 30 min re-place |
| `eod-flat` | 1× daily before close | Close **positions only** (leave working orders) |

Estimated Actions usage: ~700 min/month on a private repo (within the 2,000 min free tier).
