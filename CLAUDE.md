# CLAUDE.md — robinhood-monitor

> **At the start of every session, read `session_notes.md`** for current project state, recent changes, and known issues. Update it at the end of the session before closing.

Daily Robinhood portfolio digest: pulls live positions, enriches with technical indicators (RSI, MAs, volume), scans a self-updating watchlist for momentum opportunities, fetches market news from Sherwood and Yahoo Finance, generates a Claude AI analysis, and emails a formatted HTML digest.

## Project Structure

| File | Purpose |
|------|---------|
| `portfolio_monitor.py` | Single-file main script — all logic lives here |
| `tickers.json` | Screener watchlist — read at startup, rewritten by Claude each run |
| `news.json` | Latest run's fetched news with URLs (overwritten each run, gitignored) |
| `last_analysis.json` | Full daily technical snapshot + analysis. Lives in `~/Dropbox/robinhood-monitor/` (gitignored — repo is public and this carries real dollar figures), synced across machines via Dropbox instead |
| `protected_commitments.json` | Outstanding protected-symbol reinvestment commitments. Also Dropbox-synced, same reasoning as above |
| `ethical_exclusions.json` | Ethically-screened symbols and screening cache (committed — auto-pushed by the script; syncs across machines via git, since it carries no dollar figures) |
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
| `CLAUDE_MODEL` | `claude-sonnet-5` | Model for portfolio analysis (thinking explicitly disabled — see below) |
| `CLAUDE_MAX_TOKENS` | `4000` | Max response length for analysis |
| `RSI_PERIOD` | `14` | RSI calculation window |
| `MOMENTUM_TOP_N` | `10` | Max momentum candidates returned |
| `USER_WATCHLISTS` | `{"My First List", "Gaming", "Tech"}` | Your watchlists (highest priority) |
| `ROBINHOOD_WATCHLISTS` | `{"Cannabis", "Software"}` | Robinhood-provided watchlists (lower priority) |
| `PROTECTED_SYMBOLS` | `{"COST"}` | Core long-term holdings — trims are heavily constrained, see below |
| `PROTECTED_TRIM_MAX_PCT` | `0.10` | Max fraction of a protected symbol's own equity trimmable per action |
| `SMALL_POSITION_THRESHOLD` | `10` | Equity ($) at/under which a position is a stale-cleanup candidate |
| `ETHICAL_SCREEN_CRITERIA` | (see below) | Exclusion rubric applied by the ethical screen — not a user-curated symbol list |

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

## Protected Symbols & Small Position Cleanup

**Protected symbols** (`PROTECTED_SYMBOLS`, e.g. Costco) are core long-term holdings that shouldn't get trimmed just for being a consistent winner. They aren't off-limits, but every trim is capped at `PROTECTED_TRIM_MAX_PCT` (10%) of *that symbol's own equity* — far stricter than the normal ~50%-of-position trim rule — and must come with a stated reinvestment condition (buy back at/below the sale price, or a named dip/support level).

That reinvestment condition is **mechanically enforced across runs, not just requested in the prompt**. When the daily analysis *recommends* a protected-symbol trim, a small follow-up Claude call extracts the trim amount and reinvestment price into `protected_commitments.json` — but only as a **pending** commitment, since this script never places trades itself (trading is manual/human-in-the-loop, see the MCP workflow below). A recommendation is not a trade. Every subsequent run:
- Checks each pending commitment against real order history (`get_recent_orders`) for a matching sell — only then is it promoted to **confirmed** and actually enforced. If no matching trade shows up within `PROTECTED_COMMITMENT_PENDING_DAYS` (7 days), the pending commitment is dropped as never executed.
- Resolves each *confirmed* commitment against real order history — a qualifying buy-back, or the position being fully exited, clears it. Nothing the model says clears or confirms a commitment; only real trade data does.
- Injects any confirmed, still-outstanding commitment into the prompt as `=== OUTSTANDING REINVESTMENT COMMITMENTS ===`, and the `PROTECTED SYMBOL REINVESTMENT` hard constraint forbids recommending another partial trim of that symbol until it's gone (a full exit for a specifically broken thesis is the only override). Pending (unconfirmed) commitments don't block anything yet.
- `protected_commitments.json` lives in `~/Dropbox/robinhood-monitor/` (via `resolve_sync_paths()`, called at the top of `main()`), not the repo, so the state stays in sync across the two machines this script runs from without putting real dollar figures in the (public) git history.

This is the second place in the codebase (after `compute_trim_warnings`/TRIM COUNT WARNINGS) where Python-tracked state — not model self-restraint — blocks what the analysis is allowed to recommend.

**Small position cleanup**: any position at/under `SMALL_POSITION_THRESHOLD` ($10) is tagged `[SMALL POSITION]` in the prompt. The `SMALL POSITION CLEANUP` rule defaults to recommending a full exit when it's stale (no momentum signal, no supportive news, not bought in the last 30 days) — and, unlike normal positions, this may happen even at a loss, since it's cleanup rather than funding a new buy.

## Ethical Investment Screen

The system prompt frames the analyst as pursuing "ethically responsible" investing, but that phrase alone has no teeth — it's judged fresh each run with no persisted criteria or memory. The screen fixes that with the same **Python-tracked state, not model self-restraint** pattern used for protected-symbol commitments and trim warnings:

- `ETHICAL_SCREEN_CRITERIA` (in `portfolio_monitor.py`) is a fixed rubric excluding: weapons/defense contractors, mass-surveillance/policing/ICE contractors, fossil fuel extraction, data center *builders/operators* (REITs, colocation, construction/cooling/power infrastructure — **not** chipmakers, cloud hyperscalers, or general hardware/software companies whose products merely run in data centers), private prisons, predatory lenders, and factory farming. Tobacco, gambling, cannabis, and other vice industries are explicitly **not** excluded.
- Nobody hand-curates a symbol list. Each run, `screen_ethical_exclusions()` sends any *new* symbol (screener tickers, watchlist candidates, portfolio holdings) not already in `ethical_exclusions.json`'s `screened` set to Claude Haiku for a verdict against the rubric, then persists the result — so a symbol is judged once, not re-litigated daily.
- Enforcement is mechanical: excluded symbols are stripped from `tickers.json`, from watchlist candidates, and — via `apply_ticker_changes(excluded=...)` — from the ticker-recommendation call's own `add` output, regardless of what that call proposes. An excluded symbol simply never reaches the main analysis prompt as a buy candidate.
- A currently-*held* position that gets flagged is **not** force-sold. It's tagged `[ETHICAL SCREEN: reason]` in the position line and surfaced in a dedicated `=== ETHICAL SCREEN — HELD POSITIONS FLAGGED ===` prompt section; the `ETHICAL INVESTMENT SCREEN` system-prompt rule asks the analysis to recommend a full exit and state the reason, subject to normal `RECENT POSITIONS` timing — but this one recommendation, unlike the trim/commitment rules, is not mechanically forced.
- `ethical_exclusions.json` is auto-committed and pushed by the script itself (`git_commit_ethical_exclusions()`, same pattern as `tickers.json`/`protected_commitments.json`) whenever it changes, so a symbol screened on one machine doesn't get re-screened (and potentially re-judged differently) on another.

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

## Robinhood MCP Integration

The official Robinhood Agentic Trading MCP is connected to this project:

```bash
# Already registered — do not re-add
claude mcp add robinhood-trading --transport http https://agent.robinhood.com/mcp/trading
```

**What it exposes:** accounts, positions, portfolio value, order history, equity quotes, watchlists (read/write), equity order placement, order simulation (`review_equity_order`).

**Two accounts are visible:**
- Main account (••••4532) — margin, `agentic_allowed=false` — read-only via MCP; trades not possible
- Agentic account (••••0906) — cash, `agentic_allowed=true` — trade execution enabled; currently unfunded

**Agentic trading is NOT Robinhood's AI.** It is your own AI (Claude) accessing a dedicated isolated account via official OAuth. This is the sanctioned replacement for `robin_stocks`, which reverse-engineers Robinhood's private API. The script still uses `robin_stocks` for the automated cron job; the MCP is used for interactive Claude Code sessions.

**Workflow for trade decisions:**
1. Script runs at 6am → email digest sent → `last_analysis.json` updated with full technical snapshot in `~/Dropbox/robinhood-monitor/` (see Security Notes), current regardless of which machine ran it
2. Open a Claude Code session here and read `last_analysis.json` (in `~/Dropbox/robinhood-monitor/` — Dropbox syncs it automatically, no git pull needed) — it now includes positions with RSI, MAs, volume ratios, and top momentum movers
3. Pull live quotes via `get_equity_quotes` MCP tool to check if the 6am thesis still holds
4. Discuss the recommendation before acting — the pre-trade conversation is the human-in-the-loop filter
5. If a trade is warranted: fund the agentic account manually in the Robinhood app, then use `review_equity_order` + `place_equity_order` MCP tools

**`last_analysis.json` schema** (as of 2026-06-09):
```json
{
  "date": "...", "tldr": "...", "analysis": "...",
  "portfolio": {
    "total_value": 0.0, "cash": 0.0,
    "positions": [{ "symbol": "...", "shares": 0, "avg_cost": 0, "current_price": 0,
                    "equity": 0, "total_return_pct": 0, "rsi": 0, "ma50": 0, "ma200": 0,
                    "price_vs_ma50_pct": 0, "price_vs_ma200_pct": 0,
                    "volume_ratio": 0, "pct_change_today": 0 }]
  },
  "momentum": [{ "symbol": "...", "score": 0, "rsi": 0, "ma50": 0, "ma200": 0,
                 "price_vs_ma50_pct": 0, "volume_ratio": 0, "pct_change_today": 0 }]
}
```

## Security Notes

- `.env`, `.robin_token`, `.venv/`, `monitor.log`, `news.json` are all gitignored — never commit them
- `ethical_exclusions.json` is intentionally *not* gitignored — it carries no dollar figures, and syncing it via git (auto-committed by the script) lets a symbol screened on one machine avoid re-screening on the other (see Ethical Investment Screen above)
- `last_analysis.json` and `protected_commitments.json` *are* gitignored — this repo is public, and both carry real dollar figures from the user's account. Since the script legitimately runs from two machines, they're instead synced via `~/Dropbox/robinhood-monitor/` (`resolve_sync_paths()`, called at the top of `main()`) rather than committed anywhere (see Robinhood MCP Integration and Protected Symbols sections above)
- Gmail requires an App Password (not the account password)
- `chmod 600 .env` recommended

## No Tests

No test suite. Validate changes by running the script directly and checking `monitor.log`.
