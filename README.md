# robinhood-monitor

Daily portfolio digest for a Robinhood account. Pulls live positions, enriches them with technical indicators (RSI, moving averages, volume), scans a self-updating screener watchlist for momentum opportunities, pulls in market news from Sherwood (Robinhood's media outlet) and Yahoo Finance, generates a Claude AI analysis, and emails a formatted HTML digest.

The screener watchlist is maintained automatically — Claude evaluates momentum signals and recent news each run and incrementally adds or removes tickers from `tickers.json`. Your Robinhood watchlists are also checked; tickers you've added there are prioritised as candidates.

## Requirements

- Python 3.11+
- A Robinhood account
- An Anthropic API key
- A Gmail account with an [App Password](https://myaccount.google.com/apppasswords) enabled

## Setup

### 1. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env`:

```
ROBINHOOD_USERNAME=your_robinhood_email@example.com
ROBINHOOD_PASSWORD=your_robinhood_password
ANTHROPIC_API_KEY=sk-ant-...
GMAIL_ADDRESS=you@example.com
GMAIL_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
```

**Gmail App Password:** Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords), create an app password for "Mail", and paste the 16-character code into `GMAIL_APP_PASSWORD`.

### 3. First run — device approval

Robinhood requires device approval on first login. Run the script interactively:

```bash
.venv/bin/python portfolio_monitor.py
```

You will be prompted for an SMS/authenticator code or to approve the device in the Robinhood app. Once approved, the session token is cached in `.robin_token`. Subsequent runs authenticate silently.

**Do not commit `.robin_token`** — it is already in `.gitignore`.

## Running the script

```bash
.venv/bin/python portfolio_monitor.py
```

The script logs to both stdout and `monitor.log`. A formatted HTML digest email is sent to `GMAIL_ADDRESS` on success. If a critical stage fails, a short error email is sent instead. Non-critical failures (news fetch, watchlist sync, ticker recommendations) are logged and skipped without aborting the run.

Expected runtime: 60–90 seconds (network-bound).

## Scheduling with cron (weekdays 6am)

```bash
crontab -e
```

Add (adjust paths):

```cron
0 6 * * 1-5 cd /Users/chris/robinhood-monitor && /Users/chris/robinhood-monitor/.venv/bin/python portfolio_monitor.py >> monitor.log 2>&1
```

- `0 6 * * 1-5` — 6:00 AM Monday–Friday
- The `cd` is required — `python-dotenv` loads `.env` from the working directory

## Screener watchlist

The screener list lives in `tickers.json` as a plain sorted JSON array. It is read at startup and rewritten at the end of each run by Claude Haiku based on momentum signals, news, and your Robinhood watchlists.

**To make manual changes:** edit `tickers.json` directly. Add or remove ticker strings freely.

**Constraints enforced automatically:**
- Minimum 25 tickers, maximum 40
- No more than 3 adds or 3 removes per run
- Portfolio holdings are never added

## Robinhood watchlist integration

The script reads your Robinhood watchlists each run and scores any tickers not already in the screener. Those showing momentum signals (score ≥ 15/100) are surfaced to Claude as preferred add candidates, prioritised by list:

1. Your lists (`Gaming`, `Tech`, `My First List`) — highest priority
2. Robinhood-provided lists (`Cannabis`, `Software`) — added only if signals are strong

To change which lists are treated as user-priority vs. Robinhood-provided, edit `USER_WATCHLISTS` and `ROBINHOOD_WATCHLISTS` near the top of `portfolio_monitor.py`.

## Email digest sections

1. Current Positions — shares, price, avg cost, return, equity
2. Technical Indicators — RSI, MA50, MA200, daily change, volume ratio (colour-coded)
3. Top Momentum Movers — top screener candidates not in portfolio
4. Watchlist Updates — tickers added/removed from `tickers.json` with reasoning and linked source articles
5. Claude Analysis — AI portfolio recommendations (buy/sell/hold)
6. Market News (Sherwood) — linked headlines from Robinhood's media outlet
7. Ticker News — linked Yahoo Finance headlines per held position
8. Abbreviations glossary

## File reference

| File | Purpose |
|------|---------|
| `portfolio_monitor.py` | Main script — all logic |
| `tickers.json` | Screener watchlist — edit manually or let Claude maintain it |
| `news.json` | Latest run's fetched news with URLs (overwritten each run, gitignored) |
| `requirements.txt` | Python dependencies |
| `.env` | Credentials (never commit) |
| `.env.example` | Credentials template (safe to commit) |
| `.robin_token` | Cached Robinhood session (never commit) |
| `monitor.log` | Run log (appended each run, gitignored) |
| `.gitignore` | Excludes secrets and ephemeral files |
