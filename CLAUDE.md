# CLAUDE.md — robinhood-monitor

> **At the start of every session, read `session_notes.md`** for current project state, recent changes, and known issues. Update it at the end of the session before closing.

Daily Robinhood portfolio digest: pulls live positions, enriches with technical indicators (RSI, MAs, volume), scans a self-updating watchlist for momentum opportunities, fetches market news from the Robinhood/Sherwood newsletters (with wire feeds as fallback) plus per-ticker Yahoo Finance news, generates a Claude AI analysis, and emails a formatted HTML digest.

## Project Structure

| File | Purpose |
|------|---------|
| `portfolio_monitor.py` | Main script — orchestration, prompt, digest, state |
| `portfolio_risk.py` | Pure risk calculations (gain giveback, correlation clusters, benchmarks) + their text rendering. Split out because `portfolio_monitor.py` is already 3.6k lines; importable for tests |
| `newsletter_views.py` | Distills each Sherwood newsletter into verified analyst views, events and a dated catalyst calendar (one model call per issue), plus the prompt/digest renderers for them. Importable without the rest of `portfolio_monitor.py` |
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

## Auth Failures: Stale Token and Pickle Truncation

When the session expires, `reauth.py` copies the web app's token from the browser's
`localStorage` into `~/.tokens/robinhood.robin_token.pickle`. Two non-obvious failure modes:

- **Stale browser token.** If the robinhood.com tab sat idle, `web:auth_state` still holds an
  already-expired access token. Saving it gives a 401 `JWT verification failed` on every endpoint
  and the 6am run fails exactly as if reauth never happened (2026-10-09: token `exp` was 3 days old).
  `reauth.py` now decodes the JWT `exp` and refuses to save an expired token. Fix: hard-reload
  robinhood.com and click around so it refreshes, then rerun.
- **Pickle truncation.** `robin_stocks.login()` opens the pickle with `'wb'` *before* checking the
  login response, so a failed password login leaves a 0-byte file and destroys the session reauth
  just wrote. `robinhood_login()` snapshots the pickle before `r.login()` and restores it if it comes
  back empty (logged as `Fresh login left an empty session file`).

Diagnosing: `ls -la ~/.tokens/` (0 bytes = truncated), then decode the access token's `exp` claim.
Never print the token itself.

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
| `CLAUDE_MAX_TOKENS` | `5000` | Max response length for analysis (the TL;DR is written last, so truncation loses it) |
| `RSI_PERIOD` | `14` | RSI calculation window |
| `MOMENTUM_TOP_N` | `10` | Max momentum candidates returned |
| `USER_WATCHLISTS` | `{"My First List", "Gaming", "Tech"}` | Your watchlists (highest priority) |
| `ROBINHOOD_WATCHLISTS` | `{"Cannabis", "Software"}` | Robinhood-provided watchlists (lower priority) |
| `PROTECTED_SYMBOLS` | `{"COST"}` | Core long-term holdings — trims are heavily constrained, see below |
| `PROTECTED_COMMITMENT_MAX_DAYS` / `PROTECTED_COMMITMENT_RESTORE_FRACTION` | `14` / `0.6` | A confirmed reinvestment commitment expires this many days after the trim; buys after the trim adding back this share of the trimmed $ (at any price) also clear it |
| `NEWSLETTER_MAX_CHARS` | `12000` | Per-issue cap on newsletter text reaching the prompt |
| `CATALYST_TAG_DAYS` / `CATALYST_HORIZON_DAYS` | `4` / `10` | (`newsletter_views.py`) a held name reporting inside the first window gets a `[REPORTS EARNINGS …]` tag; catalysts past the second are ignored |
| `PROTECTED_TRIM_MAX_PCT` | `0.10` | Max fraction of a protected symbol's own equity trimmable per action |
| `SMALL_POSITION_THRESHOLD` | `10` | Equity ($) at/under which a position is a stale-cleanup candidate |
| `WATCH_ITEM_MAX_AGE_DAYS` | `5` | Days a "one key thing to watch" item is carried forward and re-reported |
| `WATCH_ITEM_MIN_RESOLVE_AGE_DAYS` | `3` | A RESOLVED verdict on a watch item younger than this is ignored (item stays open) |
| `WATCH_ITEM_MAX_OPEN` | `3` | Max watch items open at once |
| `WATCH_ITEM_MAX_SYMBOLS` | `8` | Max symbols tracked per watch item |
| `BREADTH_HISTORY_LEN` | `5` | Sessions of portfolio breadth kept for the continuity section |
| `GIVEBACK_MIN_PEAK_GAIN_PCT` / `GIVEBACK_ALERT_PCT` | `20` / `33` | (`portfolio_risk.py`) peak gain over cost needed to be protectable / share of it surrendered to alert |
| `CONCENTRATION_ALERT_PCT` | `25` | (`portfolio_risk.py`) cluster share of equity flagged CONCENTRATED |
| `CLUSTER_CORR_THRESHOLD` | `0.45` | (`portfolio_risk.py`) average-linkage cutoff for "moves together" |
| `ETHICAL_SCREEN_CRITERIA` | (see below) | Exclusion rubric applied by the ethical screen — not a user-curated symbol list |

## Script Flow

1. Load `.env` via `python-dotenv`
2. Load `tickers.json` into screener watchlist
3. Robinhood login (session-cached in `.robin_token`)
4. Fetch open positions + cash balance via `robin_stocks`, plus filled order history and **pending (unfilled) orders** — see Pending (Unfilled) Orders
5. Fetch market data for portfolio symbols + screener tickers (yfinance, bulk)
6. Score Robinhood watchlist tickers not in screener/portfolio — surface interesting ones as preferred add candidates
7. Compute indicators per symbol: RSI, MA50, MA200, volume ratio, daily % change
8. Run momentum scan — score each screener ticker (0–100), return top N
9. Build summary dict
10. Fetch market news (Sherwood newsletters via IMAP, else first fresh wire feed — see Market News Sources); when newsletters are present, distill them into verified analyst views / events / catalysts (`newsletter_views.extract_all`, one Sonnet call per issue, non-critical — see Newsletter Analyst Views & Catalysts); fetch per-ticker Yahoo Finance news (parallel); save to `news.json`
11. Call Claude Haiku for ticker recommendations (JSON: adds + removes with reasons, informed by news + watchlist candidates) → rewrite `tickers.json`
11b. Build the risk context (`portfolio_risk.build_risk_context`) from the price history `fetch_bulk_market_data` stashed in `PRICE_HISTORY` — non-critical, omitted on failure
12. Compute session breadth, roll the breadth history forward, and load any still-open watch items — see Session-Over-Session Continuity
13. Call Claude Sonnet for portfolio analysis
14. Parse the analysis's `SINCE LAST SESSION` verdicts (closing resolved/escalated watch items) and its new `ONE KEY THING TO WATCH` line (snapshotting baselines for next run)
15. Format HTML + plain text digest (includes watchlist changes with linked articles, news sections)
16. Send via Gmail SMTP SSL (port 465)

## Cash Balance

`get_cash()` sums the `portfolio_cash` field across `load_account_profile(dataType="results")`. Two earlier fixes (`load_portfolio_profile()['withdrawable_amount']`, then a single account's `load_account_profile()['cash']`) both read $0 — this account is on margin, and margin accounts hold settled cash against margin, so neither field reflects actual spendable balance. `portfolio_cash` was confirmed against a live account: it matched `load_portfolio_profile()`'s `equity - market_value` exactly, and matched the real balance shown in the Robinhood app.

## Pending (Unfilled) Orders

An order placed over a weekend or after hours sits in Robinhood in a `queued`/`confirmed`/
`unconfirmed`/`partially_filled` state until the next session opens. Such an order is invisible to
**both** account reads the script otherwise trusts: `get_open_stock_positions()` (no shares are held
yet) and `get_recent_orders()` (which keeps only `state == "filled"`). That blind spot produced a
real failure on 2026-08-17 — a weekend IONQ buy left the symbol looking unowned *and* left its
committed cash looking spendable, so the 6am Monday analysis recommended buying IONQ with money
already spent on IONQ.

`get_pending_orders()` closes it, via `r.get_all_open_stock_orders()`. Enforcement is mechanical,
the same pattern as trim warnings and protected commitments:

- **Pending buys count as owned.** `committed_symbols = portfolio_symbols | pending_buy_symbols`
  feeds the momentum scan, the screener data fetch, and the watchlist-candidate exclude set, so the
  symbol can never surface as a candidate. `build_prompt` filters held/pending symbols out of the
  momentum section a second time at render, since that section's header is the one place the prompt
  asserts a symbol is unowned.
- **Pending sells are not funding sources.** The position is tagged NOT FUNDING-ELIGIBLE and dropped
  from ELIGIBLE FUNDING SOURCES / PROTECTED SYMBOLS, so proceeds already in flight can't be
  double-counted.
- **Committed cash is not spendable.** `uncommitted_cash = cash - committed_cash` (summed notional of
  open buys). Robinhood does not reliably deduct a queued order from `portfolio_cash`, so this is
  netted in Python. The prompt's `Available Cash:` line, both digests, and `last_analysis.json` all
  lead with the deployable figure, showing the gross total only as a parenthetical.
- The `PENDING ORDERS` hard constraint in `CLAUDE_SYSTEM_PROMPT` states that absence from CURRENT
  POSITIONS and RECENT TRANSACTIONS is *not* evidence a symbol is unowned.

`_order_notional()` reads the dollar value across Robinhood's several order shapes: nested
`dollar_based_amount`/`total_notional`/`executed_notional` blocks (dollar-based fractional orders
often carry no `quantity` until they fill), falling back to `quantity × price`.

## TL;DR Ordering

The digest's TL;DR is generated **after** the analysis blocks, not before, and the parse in
`get_claude_analysis()` splits on the **last** `\n---\n` (`rsplit`) rather than the first.

The `TL;DR — WRITTEN LAST, ON PURPOSE` rule in `CLAUDE_SYSTEM_PROMPT` asks it to do **two** jobs,
in order:

1. **Read the broad market mood** — risk-on/risk-off, which sectors are leading or breaking down,
   how broad the move is, what the news flow is fixated on, and whether it looks like a one-day
   wobble or something sustained. Drawn from the market news plus the *breadth* of moves across the
   whole position list, not from any single ticker.
2. **Connect that mood to the decisions actually recommended** — the mood is the "why now" behind
   the advice, stated explicitly. A sector-wide selloff with intact theses is why almost everything
   is a hold; one real catalyst breaking is why the single buy goes where it goes.

The ordering is what makes job 2 possible, and it is not an implementation detail. The analysis call
runs with `thinking={"type": "disabled"}`, so whatever the model emits first *is* its first reasoning
about the portfolio. The original prompt said "First, write 2-3 sentences framed as a tl;dr…" and
framed it as "not a trade recommendation, but the broader context" — so it was written before any
trim/buy/hold decision existed *and* was pushed away from the advice, and it routinely narrated a
market theme that contradicted the recommendations printed beneath it in the same email. Generating
it last means the model reads the mood and then explains its own committed decisions through it.

The prompt states plainly that a TL;DR telling a different story than the advice beneath it is a
failure rather than a difference in altitude, and that if the market mood genuinely argues against
the recommendations then the *recommendations* are what need fixing.

The TL;DR rule also carries a continuity clause — read the breadth series and the prior TL;DR,
say which session of a condition this is rather than re-narrating it as new, and never hint at a
shift without either acting on it or naming the checkable trigger (which then belongs in the watch
item). See Session-Over-Session Continuity.

`rsplit` also makes the parse strictly more robust than the old `split`: a stray `---` inside the
analysis can no longer steal the boundary and swallow the recommendations. A trailing separator
after the TL;DR is stripped before splitting, and a response with no separator at all still falls
back to empty TL;DR + full text as analysis.

## Session-Over-Session Continuity (breadth + watch items)

The digest used to have no memory of its own narrative. Several consecutive runs opened with a
near-identical TL;DR about the same chip-sector pullback, each written as if that morning had
discovered it, and each closing with a "watch whether this is a one-day wobble or something
broader" line that the next run never followed up on. Both halves are structurally unfixable by
prompt wording alone: with `thinking={"type": "disabled"}` the model sees one day's snapshot plus
the prior run's prose, and has no measured record of what the thing it flagged actually did.

So this is the third place in the codebase using the **Python-tracked state, not model
self-restraint** pattern (after `compute_trim_warnings` and protected-symbol commitments). Both
pieces of state ride on `last_analysis.json` rather than a new file — it is already dual-written
with a local mirror, already gitignored (real dollar figures, public repo), and already loaded
before the prompt is built.

### Market breadth

`compute_breadth()` reduces the position list to advancers/decliners, median move, and how many
are above their MA50. `build_breadth_history()` appends it to the prior run's series (replacing a
same-date entry, so a same-day rerun doesn't stack a duplicate) and keeps the last
`BREADTH_HISTORY_LEN` sessions; `describe_breadth_streak()` names the current run of down- or
up-breadth sessions. The whole series is injected as `=== MARKET BREADTH ===`, and the
`SESSION-OVER-SESSION CONTINUITY` hard constraint tells the analysis to characterize today's mood
*relative to* those rows and the prior TL;DR — naming which session of a condition this is rather
than re-narrating a multi-day pullback as this morning's news, and stating plainly when the
recommendations are unchanged plus the condition that would change them.

### Watch items

`ONE KEY THING TO WATCH TODAY:` is now a tracked commitment rather than a closing flourish:

- `extract_watch_item()` parses the line out of the analysis (pure string work, no extra API
  call), matches the symbols it names against the run's known universe, and **snapshots each
  one's current price/RSI/vs-MA50/volume** as a baseline.
- `carry_forward_watch_items()` reloads still-open items — younger than
  `WATCH_ITEM_MAX_AGE_DAYS`, not flagged today (a same-day rerun has nothing to report yet),
  capped at `WATCH_ITEM_MAX_OPEN`.
- `watch_item_deltas()` computes then→now for each tracked symbol, and those figures go into the
  prompt as `=== OPEN WATCH ITEMS ===`. The `WATCH ITEM FOLLOW-THROUGH` hard constraint requires
  the analysis to open with a `SINCE LAST SESSION` block, one line per open item, citing the
  supplied numbers and ending in exactly one of `RESOLVED` / `STILL OPEN` / `ESCALATED` —
  where ESCALATED requires a matching action line in TRIMS/EXITS or BUYS, and "still open" for
  several sessions while the numbers trend consistently one way is explicitly called a failure.
- `apply_watch_verdicts()` closes items marked RESOLVED or ESCALATED so an answered question stops
  being re-asked. This is the one place a verdict is taken from the model's prose rather than from
  real data — unlike a protected-symbol commitment, nothing here moves money, and a tracker
  silting up with zombie lines would be its own kind of daily repetition. It is deliberately
  conservative: an item closes only when a line naming one of *its own* symbols carries the
  verdict word, and anything unmatched stays open.
  RESOLVED is **age-gated** (`WATCH_ITEM_MIN_RESOLVE_AGE_DAYS`): on 2026-10-02 a multi-session MU
  cushion-compression watch was closed as "resolved bullish" the morning after it was flagged, off one
  up day, and the idle cash was then deployed into that same bounce. Younger items now stay open
  regardless of the verdict; ESCALATED is not gated (it already requires a real action line).
- `merge_watch_item()` folds today's item into the open list. A restatement (any symbol overlap)
  updates the text in place but **keeps the original `flagged_date` and baseline** — that is what
  makes a multi-session trend measurable instead of resetting to zero every morning. The prompt
  tells the model this, and tells it to restate deliberately rather than invent a new item when
  the old one still matters.
- The `ONE KEY THING TO WATCH TODAY` instruction now requires the line to be *trackable*: at least
  one specific symbol plus a condition a later run can check against price/RSI/MA data. "Watch
  whether the pullback continues" is invalid; "watch whether MU holds its MA50 near $X" is.

The digest renders both as a **Trend Tracker** section (breadth series + each open item with a
then→now table) directly under the TL;DR, so the follow-through is visible even if the analysis
prose is terse about it. It shows the items that were open *at the start of the run* — what the
analysis was asked to report on; verdict closures take effect on the next run.

## Analysis Persistence (Dropbox is not trusted alone)

`save_analysis()` writes **two** copies and `load_last_analysis()` reads whichever is intact and
newest:

| Constant | Location | Purpose |
|----------|----------|---------|
| `ANALYSIS_FILE` | repointed to `~/Dropbox/robinhood-monitor/` by `resolve_sync_paths()` | cross-machine sync |
| `ANALYSIS_MIRROR_FILE` | repo-relative `last_analysis.json`, **never** repointed | survives Dropbox eating the synced copy |

Both are gitignored under the same filename, so neither reaches the (public) repo.

**Why the mirror exists — a confirmed, still-live Dropbox fault.** On 2026-08-19 the Dropbox copy
read back as **0 bytes** despite the run logging `Analysis saved to …`, which broke the MCP
workflow that reads this file. Reproduced directly: a 514-byte write with `flush()` + `os.fsync()`
verified non-empty immediately after close, then measured 0 bytes 1.5 seconds later. A probe file
under a *new* name in the same directory survived intact for minutes, so it is not selective sync
or dehydration — Dropbox is actively zeroing these two specific paths, which have an existing
(evidently corrupt) sync record. `protected_commitments.json` is in the same state.

This is a Dropbox-side problem, not a code bug. The mirror means the script is correct regardless,
but **cross-machine sync for these two files is dead until the Dropbox state is repaired** (delete
them via the Dropbox web UI and let them be recreated, or roll back via version history). Until
then each machine effectively uses its own local mirror.

`save_analysis()` now also `fsync`s each write, verifies the file reads back non-empty, logs an
error naming any location that fails, and raises only if *no* location persisted.
`load_last_analysis()` treats a 0-byte file as absent (with a warning) rather than as a parse error.

## Market News Sources

Market news has a **trust hierarchy**, because the account owner trusts Robinhood's own analysis
over other traders' commentary. The two tiers are labelled in the prompt and the system prompt's
`NEWS SOURCE TRUST` rule tells the analysis how to weight each:

1. **Sherwood newsletters (preferred)** — fetched over IMAP from Gmail. Editorial analysis from
   the brokerage the portfolio is held at, not a wire summary. `SHERWOOD_NEWSLETTERS` lists them
   as `(name, sender)`; note the **differing domains**, neither guessable from the other:

   | Newsletter | Sender | Content |
   |---|---|---|
   | Snacks | `hello@snacks.robinhood.com` | Daily market wrap, written takeaway, chart of the day, **next session's earnings calendar** (no other source here provides this) |
   | EntryPoint | `entrypoint@sherwoodmedia.com` | Market insights and technical signals |

   The newest real issue of *each* is ingested, newest first. When any is available the wire feeds
   are not fetched at all. To add Scoreboard or another, append to the list.
2. **Wire feeds (fallback)** — `MARKET_NEWS_FEEDS`, used only when no recent Snacks issue exists.
   Marked in the prompt as a less-trusted source whose headlines are facts, not analysis; the
   analysis is told to keep the macro read tentative and not build a thesis on them.

### How the newsletters are ingested

- **No new credentials.** `fetch_snacks_newsletter()` logs into `imap.gmail.com` with the
  `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` already used for sending. `imaplib` is stdlib.
- **Mailbox order** (`NEWSLETTER_MAILBOXES`): `[Gmail]/All Mail`, `INBOX`, then `[Gmail]/Trash`.
  All Mail holds everything retained, **including archived mail**, which is why archiving is the
  recommended habit — archived issues are found on the first search and are never auto-removed.
  Trash is searched last as a safety net, since a read-and-deleted issue is still the day's
  analysis, but Gmail purges Trash after ~30 days so it is not somewhere to rely on.
- **Subscription mail is filtered out** (`_is_newsletter_issue`). Welcome and confirmation emails
  come from the *same address* as the issues and are frequently the **newest** message from that
  sender — on 2026-10-23 the newest mail from Snacks was "Thank you for subscribing to Snacks!",
  so taking the latest message alone would have fed the analysis a signup receipt as the day's
  market news. Two independent checks: a subject pattern (`NEWSLETTER_SKIP_SUBJECT`) and a
  `NEWSLETTER_MIN_CHARS` (400) floor, since a real issue cleans to ~2KB and a receipt to a few
  hundred characters. Candidates are gathered across all mailboxes and tried newest-first, so a
  confirmation email sitting on top of a real issue is skipped rather than ending the search.
- **The issue is passed to Claude as prose, not parsed into fields.** `_clean_newsletter_text()`
  strips the table scaffolding these emails are built from, the tracking-link remnants, photo
  credits, the repeated preheader line, and the legal footer, then caps the result at
  `NEWSLETTER_MAX_CHARS` (12000, per issue — it was 5000, which cut off the back half of every
  issue: Snacks' "This Week" earnings calendar sits at the very end of a ~7.4k issue and EntryPoint's
  strategist quotes in the last half of ~11.2k; neither ever reached the model before 2026-10-26). Section structure is left intact on purpose: there are no
  per-section regexes to break when the layout changes, and the takeaway reaches the model in
  Sherwood's own words. The *analysis* in the prose is additionally distilled into structured items —
  see Newsletter Analyst Views & Catalysts below.
- **Whitespace-only text nodes are kept as a space** (`_TextExtractor.handle_data`). Snacks is
  HTML-only and often separates inline elements with nothing but `<a> <strong>…`; dropping those nodes
  glued words together ("fromCadence", "EntryPoint last Friday" → "entrypointlast"), which also broke
  exact quote matching.
- **Tickers are extracted mechanically, not linguistically.** The newsletter links every company
  it mentions to `robinhood.com/stocks/<TICKER>`, wrapped as a base64 path segment inside a
  `link.sherwood.news` tracking URL. `_decode_tracking_url()` + `_extract_tickers()` recover them,
  so the ticker list cannot hallucinate a symbol. It feeds both the analysis prompt and the
  ticker-recommendation call.
- **HTML without a dependency.** These emails are usually HTML-only, and a newsletter's
  `text/plain` part is often a "view in browser" stub, so `_message_body()` only trusts it above
  500 chars and otherwise converts the HTML with `_TextExtractor` (stdlib `html.parser`) — no
  BeautifulSoup. `_html_to_text()` also returns the hrefs, which is where the tickers come from.
- **Fails soft, never raises.** Missing credentials, an unreachable mailbox, no matching message,
  or an issue older than `NEWS_MAX_AGE_DAYS` all return `None`, and the run falls through to the
  wire feeds. The reason lands in the log and in the digest's warning block.
- `news.json` stores the issues actually used under `newsletters`.

## News Freshness

**A feed can die while still serving.** Robinhood wound down the Sherwood News website around
mid-2026 and moved the reporting into newsletters (Snacks, Chartr) and the Robinhood app, but
`sherwood.news/rss.xml` kept returning its final pre-shutdown articles. Nothing 404'd and nothing
raised, so for months the digest fed summer headlines to the analysis under the heading
"TODAY'S MARKET NEWS" — and since the prompt rendered only `title`, never the captured
`published` date, neither the model nor the reader could see the age. The 2026-10-23 digest's
newest market headline was from August.

Freshness is now validated rather than assumed:

- `MARKET_NEWS_FEEDS` is an ordered list of `(name, url)` pairs. Each is tried until one yields
  articles inside the window; a feed that errors, returns nothing, or has simply **stopped being
  updated** falls through to the next. To change sources, edit this list — no other code changes.
- `NEWS_MAX_AGE_DAYS` (4) is the cutoff. Four days so a Monday pre-market run still sees Friday's
  close and the weekend's reporting.
- `_fresh_entries()` **sorts newest-first before filtering** — feed order is not publication order,
  so the old `entries[:n]` slice could return old items even from a healthy feed. Undated entries
  are dropped, not trusted: an item whose age can't be established is the exact case this guard
  exists to catch.
- `fetch_market_news()` returns `(items, source_name, problems)`. Every item carries `published`
  and `age_days`, and the prompt renders each headline as `• [today] …` / `• [2d ago] …` with the
  source named, so the model can discount a stale story instead of being told it's current.
- **When no feed has fresh news the prompt says so explicitly** rather than omitting the section,
  and instructs the analysis not to infer a macro narrative or cite headlines it doesn't have — a
  silently absent macro frame is one the model will invent. Both digests show the failure (a red
  block in HTML, a `!! MARKET NEWS UNAVAILABLE` block in plain text) listing each feed's reason,
  so the next dead feed is visible in the email on day one.
- `fetch_ticker_news()` applies the same filter, since stale per-position headlines feed individual
  hold/trim decisions. It previously captured no date at all.
- `news.json` records the winning `source` and the `problems` list for post-hoc debugging.

## Same-Day Rerun Detection

`build_prompt()` compares `prior_analysis['date']` to the current run's date and injects an explicit note: if 0 days have elapsed, the analysis is told this is a same-day rerun (e.g. manual testing) and not to describe any position's price action as new movement since the prior run; if 1+ days have elapsed, it's told a new trading session has genuinely occurred.

## Momentum Scoring

Scores 0–100 across four signals:
- RSI in 55–75 zone: up to 40 pts (penalises overbought >75)
- Volume spike (ratio vs 30-day avg): up to 30 pts
- Today's % price move: up to 20 pts
- Price above MA50 but <20% extended: 10 pts

`TRIM SIGNALS` in `CLAUDE_SYSTEM_PROMPT` uses this same 20% figure as its overextension trim
trigger (see Opportunity Cost Check below) — it used to say 30%, which was inconsistent with
the scanner's own definition of "stretched" and, empirically, was never reached: on a live
snapshot of this portfolio the most-extended holding sat at 18.2% above its MA50. A threshold
the account's own biggest winners can't reach isn't a signal, it's dead code.

## Opportunity Cost Check

Every other trim rule in `CLAUDE_SYSTEM_PROMPT` (`REACTIVE SELLING RULE`, `COST BASIS
DISCIPLINE`, `REPEATED SELL PATTERN`, `PROTECTED SYMBOL RULE`) is a brake — each one exists to
stop a specific bad sell, and none of them exist to build a case *for* acting. That meant "no
rule was triggered" defaulted to HOLDS by construction, regardless of whether the week was good
or bad: a bad week has no broken thesis, a good week has no overextension either (see above), so
both land on the same output. The model was never actually asked to arbitrate "is there something
better to do with this capital right now" — only "did anything go wrong."

`OPPORTUNITY COST CHECK` is a mandatory block (ordered after `SINCE LAST SESSION`, before
`TRIMS/EXITS` — see `apply_watch_verdicts()`'s block-boundary regex, which had to learn this
header too, or it would swallow the block into the watch-item parse) that forces an explicit
comparison every run: name the single best-scoring candidate not already held, name the single
strongest funding candidate among `ELIGIBLE FUNDING SOURCES`, and state plainly whether the
former clears the bar to trim the latter. A "no" requires a specific, falsifiable reason (weak
score, cooling momentum, no catalyst, sector already represented) — "nothing is broken" is
explicitly disallowed as a reason here, since this check is about opportunity cost, not damage
control; those are separate questions and the other trim rules already own the damage-control
side. A field with nothing scoring above ~15 is a legitimate "ride it out," but it has to be
stated as that finding, not skipped.

## Idle Cash Is Deployed — Without Chasing

`POSITION-SIZE-AWARE TRIM SIZING` no longer has a sub-$50 "too small to trim" floor (this is a ~$1k
fractional-share account; $50 is a real holding). `BUYS` defaults to deploying all Available Cash each
session — holding it needs a named exception, "it's only $7" is not one. Because "deploy by default"
made the model grab the easiest target (MU, after a one-day bounce, in the same run that rejected SNPS
as a chase), the same block requires the target to pass the *same* entry standard as any buy: a green
day or price-target headline isn't a reason, and a name rejected as stretched can't be bought in the
same condition. Prompt-only; COST BASIS DISCIPLINE still blocks trimming underwater positions.

## Portfolio Risk Context (gain giveback, concentration, benchmarks)

Added 2026-10-12 after a day when 16/20 positions fell, every one was "held — thesis intact", and
the portfolio had already given back ~4% from its 10-06 peak. The question raised: can the agent
tell when to *bail* on a position before a winner round-trips? The existing rules only authorised a
sell to fund a better buy, for a broken thesis, or for overextension — "sell because the gain is
evaporating" was not a recognised reason. Everything below is computed in Python
(`portfolio_risk.py`), same Python-tracked-state pattern as trim warnings; nothing is auto-sold.

**Gain giveback (the sell trigger).** Per position: peak close since `opened_at` (Robinhood's
position `created_at`, so a pre-purchase spike can't count), `peak_gain` vs `avg_cost`, and what share
of that open gain (`peak - cost`) has been surrendered. `status == "alert"` when peak gain
>= `GIVEBACK_MIN_PEAK_GAIN_PCT` (20), giveback >= `GIVEBACK_ALERT_PCT` (33) **and the position is still
above cost**. Below cost it is `"gone"` (informational) — alerting there would just demand a sale at a
loss, which `COST BASIS DISCIPLINE` separately restrains. Alerts get a tag on the position line, a
`=== GAIN GIVEBACK ALERTS ===` section, and the `GAIN GIVEBACK` hard constraint: for each alert,
TRIMS/EXITS must recommend an exit/trim or name a specific checkable reason to hold ("thesis intact" /
"sector-wide pullback" are explicitly not reasons). A hold-despite-alert must be stated in
TRIMS/EXITS, not only HOLDS (same block-order hazard as below). `OPPORTUNITY COST CHECK` makes the
alerted position the funding candidate by default.

**Exits fund buys, not cash.** `EXITS FUND BUYS` requires giveback proceeds to be redeployed the same
session through the normal no-chasing entry standard, defaulting to a destination *outside* any
CONCENTRATED cluster (selling AMD to buy MU swaps semis for semis). Cash is allowed only when no
candidate clears the standard, and the rejected candidate must be named. Protected symbols keep their
10% cap + reinvestment condition even when alerted (COST alerted on the first live run).

**Why per-position and why flag-only — the backtests (1y, current holdings back-cast, so survivorship
flatters everything; read the comparisons, not the absolutes):**

| Rule | Return | Worst DD |
|---|---|---|
| Buy and hold, 20 positions equal-weight | +89% | -28% |
| Per-position exit at 33% giveback of a >=20% peak gain, final | +94% | -19% |
| Same, re-enter above 50d avg | +91% | -25% (27 exits — churn erodes it) |
| Semis sleeve buy and hold | +114% | -34% |
| Semis sleeve trailing stop 8/10/15% from high-water mark | +38/+46/+77% | -34/-33/-37% |

A portfolio- or sector-level trailing stop gave up half or more of the return and did *not* reduce
the worst drawdown (the sleeve fell 34% Jun 22 – Jul 29 and recovered; stops sold into it and
re-entered late). A per-position giveback exit did better: price was lower 40 sessions after the exit
~80% of the time. So the sell trigger is per position, and concentration is context only.

**Concentration — no hard-coded sector lists.** `correlation_clusters()` does average-linkage
clustering (threshold 0.45) on ~6 months of daily returns. On this portfolio it recovered the real
10-name semis group (47% of equity) plus GOOGL/AMZN, IONQ/RKLB, CRWD/PANW with no labels, so it
adapts to whatever the portfolio becomes. yfinance sector/industry labels were rejected: they split
semis into "Semiconductors" and "Semiconductor Equipment" and lump everything else into
"Technology". Clusters >= `CONCENTRATION_ALERT_PCT` (25) of equity are flagged CONCENTRATED. Each
cluster is compared with the benchmark it *tracks* (highest correlation of QQQ/SOXX/SPY), not a fixed
pairing.

**Benchmarks.** Back-cast of current holdings vs QQQ (Nasdaq-100), SOXX (semiconductors) and SPY
(S&P 500) over 5d/1m/3m, plus 60-day beta/correlation to QQQ and SOXX. The "Portfolio minus X" and
"moves most like X" differences are computed in Python on purpose: the first live run had the model
call a cluster at -6.3% "underperforming" a benchmark at -7.6%. The rule tells it to quote those
lines, not re-derive them.

`PRICE_HISTORY` (module-level dict in `portfolio_monitor.py`) is the side-channel for daily closes —
kept out of `market_data` so no pandas Series reaches the JSON the summary is serialised into. The
risk context is also saved to `last_analysis.json` under `risk_context` and rendered in both digests
as **Portfolio Risk**. Not yet seen on a scheduled cron run — first validated by a live dry run
(real Robinhood + yfinance + Claude, email/git/state writes skipped): AMD (50% given back) and COST
(34%) alerted, both were trimmed, and proceeds went to GOOGL/META outside the semis cluster.

## Newsletter Analyst Views & Catalysts

The newsletters are the one source here carrying *analysis*: Sherwood's own read, strategists and
firms it quotes, Robinhood retail-flow data, and (Snacks) the week's earnings calendar. Passing the
raw issue text was not enough — on 2026-10-26 the analysis trimmed META "for no dated catalyst" while
EntryPoint's lead item was Meta's $95B equity raise, and recommended buying GOOGL without noting that
GOOGL, MSFT and KLAC report Wednesday and AMZN Thursday (the calendar had been truncated away).
Matching tickers to paragraphs was rejected: it is sentiment-blind, and the useful content is the
view, not the mention.

`newsletter_views.extract_all()` runs one Sonnet call per issue (parallel, thinking disabled, fails soft —
the raw issue still reaches the analysis) and returns `views` (`editorial` / `third_party` /
`retail_flow`, each with direction, horizon, claim, reasoning, an invalidation condition only if the
text states one, and a **verbatim quote**), `events` (material facts: offerings, guidance, deals),
`catalysts` (dated earnings/macro, weekday names resolved against the issue date) and the issue's
overall `stance`. Nothing the model emits is trusted unchecked (`validate_extraction`):

- an item whose quote isn't a contiguous span of the issue (whitespace/case/curly-quote
  insensitive) is dropped and counted in the log (`N dropped as unverifiable`); stitched multi-sentence
  quotes with ellipses fail on purpose;
- a ticker not in the issue's own `robinhood.com/stocks/<T>` links is removed, so a symbol can't be
  invented; earnings catalysts keep tickers only; a bad date drops the catalyst.

What it does **not** check is that the claim is *entailed* by the quote — a quote can support only part
of a claim. The prompt tells the extractor the claim must not say more than the quote does, but this
is the residual risk.

How it reaches the analysis:
- `=== ANALYST VIEWS ===` and `=== THIS WEEK'S CATALYSTS ===` sections right after the raw issues, with
  the trust weighting stated (Sherwood editorial > third-party quoted analysts, who are second-hand and
  must be attributed > retail flow, which is behaviour, not analysis). Catalysts are deduplicated across
  issues (earnings merged per date + premarket/after-close slot; macro by word overlap).
- Each held position carries its own `Newsletter event` / `Newsletter view` lines and, if it reports
  within `CATALYST_TAG_DAYS` (4), a `[REPORTS EARNINGS …]` tag — next to the price/RSI data it is
  deciding on rather than in a section it has no reason to reread. This is what actually made the
  META trim cite the equity raise; the section alone did not.
- Hard constraints in `CLAUDE_SYSTEM_PROMPT`: `ANALYST VIEWS` (an **ANALYST VIEWS** block of ≤5 lines
  between `SINCE LAST SESSION` and `OPPORTUNITY COST CHECK`, each AGREE/DISAGREE/PARTLY against this
  prompt's data and ending `changes: <action>` / `changes: nothing`; hedging preserved; events on
  held symbols weighed in that position's reasoning) and `CATALYSTS` (any buy/add/trim of a symbol
  reporting within days must name the event). The TL;DR rule gained: do not state the market mood more
  confidently than the issue's own `stance`, and name only companies/themes present in the news or
  positions (it had coined "AMD-adjacent suppliers").
- An `=== BEFORE YOU WRITE ===` reminder ends the prompt. Without it the model skipped the ANALYST
  VIEWS block outright on a same-day rerun. A missing block also logs a warning. If it keeps being
  skipped, the next step is a post-parse retry, not more prompt text.
- `apply_watch_verdicts()`'s block-boundary regex now stops at `ANALYST VIEWS` too.
- Rendered in both digests (`What Sherwood is saying`); extraction results are saved to `news.json`
  under `newsletter_views`. They are not yet persisted to `last_analysis.json`, and view outcomes are
  not tracked across sessions (deferred: watch items already carry the multi-day thread).

`CLAUDE_MAX_TOKENS` went 4000 → 5000 because the new block lengthens the response, and the TL;DR is
written last — a truncated response would lose it.

Extraction is stochastic: across runs on the same issue the view count varied (3–5 Snacks, 7–12
EntryPoint) and the earnings-slot ticker lists varied in completeness (held names were found every
time). Not a correctness problem, but don't diff two runs expecting identical output.

## No-action lines in TRIMS/EXITS

The analysis was writing `**TRIM COST — no action**` into `TRIMS/EXITS` to explain a blocked protected
symbol. That block is for trades. The prompt now says a restricted position is one line in `HOLDS`, but
the model rewords around the rule (`**HOLD COST — no trim**`, then plain-prose `No action on COST — …`),
so `strip_noop_trim_lines()` enforces it after parsing: it drops no-action TRIM/EXIT/SELL and HOLD
header lines from the block, and plain-prose lines naming a commitment-blocked symbol. It leaves
alone symbols with a gain-giveback alert (a hold-despite-alert must stay written in this block,
as `**HOLD X (giveback alert) — reason**`) and any bold EXIT/TRIM header. If that empties the block it
writes `No trims or exits today.`

## Exits Belong in TRIMS/EXITS (block-order hazard)

The analysis blocks are written in order (`TRIMS/EXITS` → `BUYS` → `HOLDS`), and with thinking
disabled the model only walks the per-position list while writing `HOLDS`. On 2026-09-30 a
`SMALL POSITION CLEANUP` exit for SOLV was decided there: `TRIMS/EXITS` had already said "Nothing to
trim today", and the exit landed in `HOLDS` plus a stray `SMALL POSITION EXIT:` line. The cleanup
rule never said where an exit goes, and `TRIMS/EXITS` said "only list positions … exited" without
saying that overrides anything.

Prompt-only fix: the cleanup rule now requires checking every `[SMALL POSITION]` tag *before*
writing `TRIMS/EXITS` and puts the exit there as a normal bold line; `TRIMS/EXITS` may only say
"nothing to trim" after that check; `HOLDS` may never introduce a sell/exit/trim. Not
mechanically enforced — if it recurs, add a post-parse check for exit language outside
`TRIMS/EXITS` (same pattern as `apply_watch_verdicts()`).

## Robinhood Watchlist Integration

Reads all Robinhood watchlists each run. Tickers not already in `tickers.json` or the portfolio are scored. Those scoring ≥ `WATCHLIST_MIN_SCORE` are passed to Claude as preferred add candidates, split by priority:
- **User lists** (`USER_WATCHLISTS`) — highest priority
- **Robinhood lists** (`ROBINHOOD_WATCHLISTS`) — added only if signals are strong

## Protected Symbols & Small Position Cleanup

**Protected symbols** (`PROTECTED_SYMBOLS`, e.g. Costco) are core long-term holdings that shouldn't get trimmed just for being a consistent winner. They aren't off-limits, but every trim is capped at `PROTECTED_TRIM_MAX_PCT` (10%) of *that symbol's own equity* — far stricter than the normal ~50%-of-position trim rule — and must come with a stated reinvestment condition (buy back at/below the sale price, or a named dip/support level).

That reinvestment condition is **mechanically enforced across runs, not just requested in the prompt**. When the daily analysis *recommends* a protected-symbol trim, a small follow-up Claude call extracts the trim amount and reinvestment price into `protected_commitments.json` — but only as a **pending** commitment, since this script never places trades itself (trading is manual/human-in-the-loop, see the MCP workflow below). A recommendation is not a trade. Every subsequent run:
- Checks each pending commitment against real order history (`get_recent_orders`) for a matching sell — only then is it promoted to **confirmed** and actually enforced. If no matching trade shows up within `PROTECTED_COMMITMENT_PENDING_DAYS` (7 days), the pending commitment is dropped as never executed.
- Resolves each *confirmed* commitment against real order history. It clears on any of: a buy at/below the target price; buys after the trim, **at any price**, adding back ≥ `PROTECTED_COMMITMENT_RESTORE_FRACTION` (60%) of the trimmed dollars (the position is restored); the position being fully exited; or `PROTECTED_COMMITMENT_MAX_DAYS` (14) elapsing since the trim. Nothing the model says clears or confirms a commitment; only real trade data (or the clock) does.
  - **Why the last three exist (2026-10-26):** COST was trimmed at $938.04 and was $1,009 three days later. A confirmed commitment used to live until a buy at/below $938 landed, so a rising core holding was un-trimmable indefinitely and the prompt framed it as "condition not yet met", which in practice meant nobody added to it. The lock covers **trims only**; buying a protected symbol was never forbidden and the prompt now says so (a buy is judged by the normal no-chasing entry standard, and a big enough one clears the commitment). Restoring above the original price is deliberately allowed — the discipline is "don't sell and never come back", not "never pay more than you sold for".
- Injects any confirmed, still-outstanding commitment into the prompt as `=== OUTSTANDING REINVESTMENT COMMITMENTS ===`, and the `PROTECTED SYMBOL REINVESTMENT` hard constraint forbids recommending another partial trim of that symbol until it's gone (a full exit for a specifically broken thesis is the only override). A blocked trim is just a hold: it is not written up in `TRIMS/EXITS` (see No-action lines below). Pending (unconfirmed) commitments don't block anything yet.
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

0. Trend Tracker (breadth history + open watch items with then→now deltas — only when there is something to track)
0b. Portfolio Risk (benchmarks, correlated clusters, gain-protection alerts — same lines the analysis was shown)
1. Pending Orders (table — only rendered when unfilled orders exist)
2. Current Positions (table, colour-coded returns)
3. Technical Indicators (table, colour-coded RSI/MA/vol)
4. Top Momentum Movers (table)
5. Watchlist Updates (add/remove with reasons + linked source articles)
6. Claude Analysis (markdown-rendered)
7. Market News — **What Sherwood is saying** (each issue's overall read, direction-tagged views with `HELD` flags, and the week's catalyst table), then each Sherwood issue as sent (green source banner, full text) with the merged tickers named, or dated wire headlines, or a red warning block if neither was available
8. Ticker News — Yahoo Finance per holding (linked, dated headlines)
9. Abbreviations glossary (footer)

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
2. Open a Claude Code session here and read `last_analysis.json` (in `~/Dropbox/robinhood-monitor/` — Dropbox syncs it automatically, no git pull needed) — it now includes positions with RSI, MAs, volume ratios, top momentum movers, the open `watch_items` (each with the baseline readings it was flagged against) and the `breadth_history` series
3. Pull live quotes via `get_equity_quotes` MCP tool to check if the 6am thesis still holds. Check `portfolio.pending_orders` too — an order placed since the last session may have filled, changing what's actually owned and what cash is free
4. Discuss the recommendation before acting — the pre-trade conversation is the human-in-the-loop filter
5. If a trade is warranted: fund the agentic account manually in the Robinhood app, then use `review_equity_order` + `place_equity_order` MCP tools

**`last_analysis.json` schema** (as of 2026-10-12):
```json
{
  "date": "...", "tldr": "...", "analysis": "...",
  "watch_items": [{ "flagged_date": "...", "restated_date": "...", "text": "...",
                    "symbols": ["..."],
                    "baseline": { "SYM": { "current_price": 0, "rsi": 0,
                                           "price_vs_ma50_pct": 0, "volume_ratio": 0 } } }],
  "breadth_history": [{ "date": "...", "total": 0, "up": 0, "down": 0,
                        "median_pct": 0, "above_ma50": 0, "with_ma50": 0 }],
  "risk_context": { "giveback": { "SYM": { "peak_price": 0, "peak_date": "...", "peak_gain_pct": 0,
                                           "given_back_pct": 0, "status": "ok|alert|gone" } },
                    "concentration": [{ "symbols": ["..."], "pct_of_equity": 0, "ret_5d": 0, "ret_1m": 0,
                                        "below_high_pct": 0, "concentrated": true,
                                        "tracks": "SOXX", "tracks_corr": 0, "tracks_5d": 0 }],
                    "benchmarks": { "returns": { "Portfolio": { "5d": 0, "1m": 0, "3m": 0 } },
                                    "betas": { "QQQ": { "beta": 0, "corr": 0 } }, "coverage_pct": 100 } },
  "portfolio": {
    "total_value": 0.0, "cash": 0.0,
    "committed_cash": 0.0, "uncommitted_cash": 0.0,
    "pending_orders": [{ "symbol": "...", "side": "buy", "quantity": 0, "price": 0,
                         "notional": 0, "date": "...", "state": "queued" }],
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

Pure helpers (`newsletter_views.py`, `strip_noop_trim_lines`, `resolve_protected_commitments`, `_TextExtractor`) are easy to exercise inline without a suite. The validation that has actually caught things here is a **live dry run**: call `main()` with `send_email`, `git_*`, `save_analysis`, `save_news_cache`, `save_protected_commitments` and `save_ethical_exclusions` stubbed, `TICKERS_FILE` pointed at a scratch copy, and the file log handler removed — real Robinhood, yfinance and Claude, nothing written. Capture `build_prompt`'s output and read the prompt, not just the analysis. Model compliance is stochastic, so run it twice before believing a prompt change worked.
