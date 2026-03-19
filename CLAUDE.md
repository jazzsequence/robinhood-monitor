# CLAUDE.md — robinhood-monitor

Daily Robinhood portfolio digest: pulls live positions, enriches with technical indicators (RSI, MAs, volume), scans a self-updating watchlist for momentum opportunities, fetches market news from Sherwood and Yahoo Finance, generates a Claude AI analysis, and emails a formatted HTML digest.

## Project Structure

| File | Purpose |
|------|---------|
| `portfolio_monitor.py` | Single-file main script — all logic lives here |
| `tickers.json` | Screener watchlist — read at startup, rewritten by Claude each run |
| `news.json` | Latest run's fetched news with URLs (overwritten each run, gitignored) |
| `requirements.txt` | Python dependencies |
| `.env` | Credentials (never commit) |
| `.env.example` | Credentials template |
| `.robin_token` | Cached Robinhood session (never commit) |
| `monitor.log` | Appended each run |
| `.venv/` | Virtual environment (never commit) |

## Running

```bash
.venv/bin/python portfolio_monitor.py
```

Logs to stdout and `monitor.log`. Sends an HTML digest email on success, an error email on failure. Non-critical failures (news fetch, watchlist sync, ticker recommendations) are logged and skipped without aborting.

**First run:** Robinhood will prompt for MFA/device approval. Complete it interactively. The session is cached in `.robin_token` for subsequent silent runs.

## Environment Variables

```
ROBINHOOD_USERNAME=
ROBINHOOD_PASSWORD=
ANTHROPIC_API_KEY=
GMAIL_ADDRESS=
GMAIL_APP_PASSWORD=   # Gmail App Password, not account password
```

Copy `.env.example` to `.env` and fill in values. `python-dotenv` loads `.env` from the working directory — cron must `cd` into the project first or `.env` won't be found.

## Dependencies

```bash
.venv/bin/python -m pip install -r requirements.txt
```

Key packages: `robin_stocks`, `yfinance`, `anthropic`, `python-dotenv`, `pandas`, `numpy`, `requests`, `feedparser`.

Python 3.11+ required (uses `float | None` union type syntax).

## Key Constants (top of `portfolio_monitor.py`)

| Constant | Default | Purpose |
|----------|---------|---------|
| `TICKERS_FILE` | `tickers.json` | Path to screener watchlist |
| `NEWS_FILE` | `news.json` | Path to news cache |
| `MIN_SCREENER_TICKERS` | `25` | Minimum watchlist size |
| `MAX_SCREENER_TICKERS` | `40` | Hard cap on watchlist size |
| `WATCHLIST_MIN_SCORE` | `15` | Minimum momentum score to surface a Robinhood watchlist ticker |
| `CLAUDE_MODEL` | `claude-sonnet-4-20250514` | Model for portfolio analysis |
| `CLAUDE_MAX_TOKENS` | `1500` | Max response length for analysis |
| `RSI_PERIOD` | `14` | RSI calculation window |
| `MOMENTUM_TOP_N` | `10` | Max momentum candidates returned |
| `USER_WATCHLISTS` | `{"My First List", "Gaming", "Tech"}` | Your watchlists (highest priority) |
| `ROBINHOOD_WATCHLISTS` | `{"Cannabis", "Software"}` | Robinhood-provided watchlists (lower priority) |

## Script Flow

1. Load `.env` via `python-dotenv`
2. Load `tickers.json` into screener watchlist
3. Robinhood login (session-cached in `.robin_token`)
4. Fetch open positions + cash balance via `robin_stocks`
5. Fetch market data for portfolio symbols + screener tickers (yfinance, bulk)
6. Score Robinhood watchlist tickers not in screener/portfolio — surface interesting ones as preferred add candidates
7. Compute indicators per symbol: RSI, MA50, MA200, volume ratio, daily % change
8. Run momentum scan — score each screener ticker (0–100), return top N
9. Build summary dict
10. Fetch Sherwood news (RSS) + per-ticker Yahoo Finance news (parallel); save to `news.json`
11. Call Claude Haiku for ticker recommendations (JSON: adds + removes with reasons, informed by news + watchlist candidates) → rewrite `tickers.json`
12. Call Claude Sonnet for portfolio analysis
13. Format HTML + plain text digest (includes watchlist changes with linked articles, news sections)
14. Send via Gmail SMTP SSL (port 465)

## Momentum Scoring

Scores 0–100 across four signals:
- RSI in 55–75 zone: up to 40 pts (penalises overbought >75)
- Volume spike (ratio vs 30-day avg): up to 30 pts
- Today's % price move: up to 20 pts
- Price above MA50 but <20% extended: 10 pts

## Robinhood Watchlist Integration

Reads all Robinhood watchlists each run. Tickers not already in `tickers.json` or the portfolio are scored. Those scoring ≥ `WATCHLIST_MIN_SCORE` are passed to Claude as preferred add candidates, split by priority:
- **User lists** (`USER_WATCHLISTS`) — highest priority
- **Robinhood lists** (`ROBINHOOD_WATCHLISTS`) — added only if signals are strong

## Ticker Recommendation Logic

A second Claude call (Haiku model, 500 tokens) runs after the momentum scan and news fetch. It receives current watchlist, portfolio positions, momentum results, watchlist candidates, and news headlines. Returns `{"add": [...], "remove": [...]}` with reasons per ticker. The script enforces min/max bounds regardless of what Claude returns. If the call or JSON parse fails, `tickers.json` is left unchanged.

## Email Sections

1. Current Positions (table, colour-coded returns)
2. Technical Indicators (table, colour-coded RSI/MA/vol)
3. Top Momentum Movers (table)
4. Watchlist Updates (add/remove with reasons + linked source articles)
5. Claude Analysis (markdown-rendered)
6. Market News — Sherwood (linked headlines)
7. Ticker News — Yahoo Finance per holding (linked headlines)
8. Abbreviations glossary (footer)

## Cron Schedule (weekdays 6am)

```cron
0 6 * * 1-5 cd /path/to/robinhood-monitor && /path/to/.venv/bin/python portfolio_monitor.py >> monitor.log 2>&1
```

## Security Notes

- `.env`, `.robin_token`, `.venv/`, `monitor.log`, `news.json` are all gitignored — never commit them
- Gmail requires an App Password (not the account password)
- `chmod 600 .env` recommended

## No Tests

No test suite. Validate changes by running the script directly and checking `monitor.log`.
