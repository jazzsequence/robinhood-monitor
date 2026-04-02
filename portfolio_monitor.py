"""
portfolio_monitor.py — Daily Robinhood portfolio digest with Claude AI analysis.

Usage:
    python portfolio_monitor.py
"""

import json
import os
import re
import socket
import sys
import logging
import smtplib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import feedparser
import pandas as pd
import requests
import yfinance as yf
import robin_stocks.robinhood as r
from anthropic import Anthropic
from dotenv import load_dotenv

# ── Screener watchlist ────────────────────────────────────────────────────────
# Tickers are loaded from tickers.json at runtime and updated daily by Claude.
# Edit tickers.json directly to make manual changes.
TICKERS_FILE = "tickers.json"
NEWS_FILE = "news.json"
MIN_SCREENER_TICKERS = 25
MAX_SCREENER_TICKERS = 40
WATCHLIST_MIN_SCORE = 15  # minimum momentum score to surface a watchlist ticker as a candidate

# ── Config ────────────────────────────────────────────────────────────────────
CLAUDE_MODEL = "claude-sonnet-4-20250514"
CLAUDE_MAX_TOKENS = 1500
CLAUDE_SYSTEM_PROMPT = (
    "You are a stock trading advisor for a high-risk-tolerance retail investor "
    "with ~$700 in a Robinhood play account. The investor cannot make trades "
    "during business hours and will execute after-hours. Goals: aggressive growth, "
    "no pump-and-dump, no unsavory trades, Robinhood platform only. "
    "Robinhood supports fractional share purchases, so small dollar amounts (even $5-20) "
    "can be deployed to open or add to a position.\n\n"
    "CAPITAL AWARENESS: This portfolio is self-funding — no new money is being added from outside. "
    "When recommending purchases, be mindful that available capital comes from: "
    "(a) the Available Cash already in the account, and (b) proceeds from any SELL actions "
    "you recommend in this same analysis. Before recommending buys, note the total capital "
    "available (cash + sell proceeds) so the investor can see whether the purchases are "
    "feasible without adding funds. If recommending new purchases would require capital beyond "
    "what cash and recommended sells provide, flag this clearly so the investor can decide "
    "whether to fund it externally or adjust the plan.\n\n"
    "RECENT POSITIONS — DO NOT SELL PREMATURELY: The prompt includes recent transaction history. "
    "Do not recommend selling any position acquired within the last 7 days unless there is a severe, "
    "specific fundamental reason (e.g. company halted, stop-loss clearly breached, major negative news "
    "that materially changes the thesis). Selling a recently purchased position at a loss is one of the "
    "worst outcomes for this portfolio — avoid it.\n\n"
    "Structure your response in two parts:\n"
    "PART 1 — TL;DR: Write 2-3 sentences framed as a tl;dr of the overall trends or advice given. "
    "This is a high-level, humanistic read on the most important trend, risk, or opportunity "
    "facing the portfolio right now — not a trade recommendation, but the broader context that "
    "should inform every decision today. End this section with exactly the line: ---\n"
    "PART 2 — Full analysis covering: "
    "(1) any positions to EXIT with clear reasoning, "
    "(2) your total buy capacity (cash + sell proceeds) and any positions to ADD TO or new positions "
    "to ENTER within that limit — including small fractional purchases to gradually diversify, "
    "(3) one key thing to watch today. "
    "If conditions do not warrant action, saying so is a valid output — "
    "do not manufacture trades just to fill the format. "
    "Use specific BUY, SELL, or HOLD language. Assume basic but not advanced knowledge "
    "of stock trading and terminology."
)

ROBIN_TOKEN_PATH = ".robin_token"
LOG_FILE = "monitor.log"
RSI_PERIOD = 14
MOMENTUM_TOP_N = 10

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────
def _fmt(val, spec, fallback="N/A"):
    """Format a numeric value with a format spec, returning fallback on None/error."""
    if val is None:
        return fallback
    try:
        return format(val, spec)
    except (TypeError, ValueError):
        return fallback


def calculate_rsi(closes: pd.Series, period: int = RSI_PERIOD) -> float | None:
    if len(closes) < period + 1:
        return None
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 2)


# ── Email ─────────────────────────────────────────────────────────────────────
def send_email(subject: str, body: str, html_body: str | None = None):
    gmail_address = os.getenv("GMAIL_ADDRESS")
    app_password = os.getenv("GMAIL_APP_PASSWORD")

    sender = f"Portfolio Monitor <{gmail_address}>"

    if html_body:
        msg = MIMEMultipart("alternative")
        msg["From"] = sender
        msg["To"] = gmail_address
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        msg.attach(MIMEText(html_body, "html"))
    else:
        msg = MIMEMultipart()
        msg["From"] = sender
        msg["To"] = gmail_address
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_address, app_password)
        server.send_message(msg)

    log.info(f"Email sent: {subject}")


def send_error_email(context: str, exc: Exception):
    """Best-effort error notification — never raises."""
    try:
        subject = f"Portfolio Monitor ERROR - {date.today()}"
        body = f"Script failed during: {context}\n\nError: {type(exc).__name__}: {exc}"
        send_email(subject, body)
    except Exception as e:
        log.error(f"Failed to send error email: {e}")


# ── Robinhood auth ────────────────────────────────────────────────────────────
def robinhood_login():
    """
    Log in to Robinhood with session caching.

    On first run, Robinhood will trigger an MFA/device-approval flow via SMS or
    the app. Follow the prompts in the terminal. The approved session is saved to
    ROBIN_TOKEN_PATH (.robin_token) so subsequent runs are silent.

    r.login() can return without error even when a cached token has expired.
    We validate the session immediately and, if it's dead, delete the stale token
    and perform a fresh credential login. Since the device is already approved,
    this does not trigger MFA.
    """
    def _login():
        r.login(
            username=os.getenv("ROBINHOOD_USERNAME"),
            password=os.getenv("ROBINHOOD_PASSWORD"),
            store_session=True,
            pickle_name=ROBIN_TOKEN_PATH,
        )

    _login()

    try:
        r.load_portfolio_profile()
    except Exception:
        log.warning("Session invalid after login — stale token, retrying with fresh credentials")
        if os.path.exists(ROBIN_TOKEN_PATH):
            os.remove(ROBIN_TOKEN_PATH)
        _login()


# ── Positions ─────────────────────────────────────────────────────────────────
def get_positions() -> list[dict]:
    raw = r.get_open_stock_positions()
    positions = []

    for pos in raw:
        try:
            shares = float(pos["quantity"])
            if shares <= 0:
                continue

            instrument = r.get_instrument_by_url(pos["instrument"])
            symbol = instrument["symbol"]
            avg_cost = float(pos["average_buy_price"])

            price_list = r.get_latest_price(symbol)
            current_price = float(price_list[0]) if price_list else None
            if current_price is None:
                log.warning(f"No price available for {symbol}, skipping")
                continue

            equity = round(shares * current_price, 2)
            total_return_pct = (
                round((current_price - avg_cost) / avg_cost * 100, 2)
                if avg_cost > 0
                else 0.0
            )

            positions.append(
                {
                    "symbol": symbol,
                    "shares": round(shares, 6),
                    "avg_cost": round(avg_cost, 4),
                    "current_price": round(current_price, 4),
                    "equity": equity,
                    "total_return_pct": total_return_pct,
                }
            )
        except Exception as e:
            log.warning(f"Error processing position entry: {e}")

    return positions


def get_cash() -> float:
    try:
        profile = r.load_portfolio_profile()
        return round(float(profile.get("withdrawable_amount", 0)), 2)
    except Exception as e:
        log.warning(f"Could not fetch cash balance: {e}")
        return 0.0


# ── Recent order history ──────────────────────────────────────────────────────
def get_recent_orders(days: int = 30) -> list[dict]:
    """
    Return filled stock orders from the last `days` days, newest first.
    Each entry: {symbol, side, quantity, price, date, days_ago}.

    Note: Robinhood returns orders newest-first, so we break early once
    we pass the cutoff rather than scanning the full history.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        all_orders = r.get_all_stock_orders() or []
    except Exception as e:
        log.warning(f"Could not fetch order history: {e}")
        return []

    instrument_cache: dict[str, str] = {}
    recent = []

    for order in all_orders:
        if order.get("state") != "filled":
            continue
        ts_str = order.get("last_transaction_at", "")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        # Orders come back newest-first; stop once past the window
        if ts < cutoff:
            break

        instrument_url = order.get("instrument", "")
        if not instrument_url:
            continue
        if instrument_url not in instrument_cache:
            try:
                instrument = r.get_instrument_by_url(instrument_url)
                instrument_cache[instrument_url] = instrument.get("symbol", "")
            except Exception:
                instrument_cache[instrument_url] = ""

        symbol = instrument_cache[instrument_url]
        if not symbol:
            continue

        try:
            qty = round(float(order.get("quantity") or 0), 4)
            raw_price = order.get("average_price") or order.get("price")
            price = round(float(raw_price), 4) if raw_price else None
        except (TypeError, ValueError):
            continue

        recent.append({
            "symbol": symbol,
            "side": order.get("side", ""),
            "quantity": qty,
            "price": price,
            "date": ts.date().isoformat(),
            "days_ago": (datetime.now(timezone.utc) - ts).days,
        })

    return recent


# ── Watchlists ────────────────────────────────────────────────────────────────
# User-curated lists are prioritised over Robinhood-provided lists in the
# ticker recommendation prompt. Any list name not in either set is treated
# as user-curated by default.
USER_WATCHLISTS = {"My First List", "Gaming", "Tech"}
ROBINHOOD_WATCHLISTS = {"Cannabis", "Software"}


def get_watchlist_tickers(exclude: set[str]) -> dict[str, list[str]]:
    """
    Return tickers from Robinhood watchlists, split by priority.
    Returns {"user": [...], "robinhood": [...]} — both lists exclude symbols
    already in the screener or portfolio.
    """
    try:
        all_watchlists = r.get_all_watchlists()
    except Exception as e:
        log.warning(f"Could not fetch watchlists: {e}")
        return {"user": [], "robinhood": []}

    if isinstance(all_watchlists, dict):
        watchlists = all_watchlists.get("results", [])
    else:
        watchlists = all_watchlists or []

    user_syms: set[str] = set()
    rh_syms: set[str] = set()

    for wl in watchlists:
        name = wl.get("name", "")
        if not name:
            continue
        is_rh = name in ROBINHOOD_WATCHLISTS
        try:
            items = r.get_watchlist_by_name(name) or []
            for item in items:
                sym = item.get("symbol", "").upper()
                if not sym or sym in exclude:
                    continue
                if is_rh:
                    rh_syms.add(sym)
                else:
                    user_syms.add(sym)
        except Exception as e:
            log.warning(f"Could not fetch watchlist '{name}': {e}")

    # A ticker in both user and RH lists counts as user-priority
    rh_syms -= user_syms

    return {"user": sorted(user_syms), "robinhood": sorted(rh_syms)}


# ── Market data ───────────────────────────────────────────────────────────────
def fetch_bulk_market_data(symbols: list[str]) -> dict[str, dict]:
    """
    Download 1y of daily OHLCV for all symbols in one yfinance call.
    Returns a dict keyed by symbol with computed indicators.
    """
    if not symbols:
        return {}

    log.info(f"Downloading market data for: {', '.join(symbols)}")
    raw = yf.download(
        symbols,
        period="1y",
        group_by="ticker",
        auto_adjust=True,
        progress=False,
        threads=True,
    )

    results = {}
    for symbol in symbols:
        try:
            # yfinance returns a flat DataFrame for single-ticker downloads
            hist = raw[symbol] if len(symbols) > 1 else raw
            hist = hist.dropna(subset=["Close", "Volume"])

            if len(hist) < RSI_PERIOD + 2:
                log.warning(f"Not enough history for {symbol}, skipping")
                continue

            closes = hist["Close"]
            volumes = hist["Volume"]

            current_price = round(float(closes.iloc[-1]), 4)
            prev_close = round(float(closes.iloc[-2]), 4)
            pct_change_today = round(
                (current_price - prev_close) / prev_close * 100, 2
            )

            ma50 = round(float(closes.tail(50).mean()), 4) if len(closes) >= 50 else None
            ma200 = round(float(closes.tail(200).mean()), 4) if len(closes) >= 200 else None

            rsi = calculate_rsi(closes)

            avg_vol_30d = float(volumes.tail(30).mean())
            today_volume = int(volumes.iloc[-1])
            volume_ratio = (
                round(today_volume / avg_vol_30d, 2) if avg_vol_30d > 0 else None
            )

            results[symbol] = {
                "current_price": current_price,
                "pct_change_today": pct_change_today,
                "rsi": rsi,
                "ma50": ma50,
                "ma200": ma200,
                "price_vs_ma50_pct": (
                    round((current_price - ma50) / ma50 * 100, 2) if ma50 else None
                ),
                "price_vs_ma200_pct": (
                    round((current_price - ma200) / ma200 * 100, 2) if ma200 else None
                ),
                "today_volume": today_volume,
                "avg_volume_30d": int(avg_vol_30d),
                "volume_ratio": volume_ratio,
            }
        except Exception as e:
            log.warning(f"Error computing indicators for {symbol}: {e}")

    return results


# ── Momentum scan ─────────────────────────────────────────────────────────────
def momentum_score(data: dict) -> float:
    """
    Score a ticker by momentum quality (0–100).
    Weights: RSI in momentum zone (40 pts), volume spike (30 pts),
    today's price move (20 pts), price above MA50 but not stretched (10 pts).
    """
    score = 0.0

    rsi = data.get("rsi")
    if rsi is not None:
        if 55 <= rsi <= 75:
            score += (rsi - 55) / 20 * 40  # sweet spot: up to 40 pts
        elif rsi > 75:
            score += max(0, 40 - (rsi - 75) * 2)  # penalise overbought

    vr = data.get("volume_ratio")
    if vr is not None:
        score += min(vr * 10, 30)  # up to 30 pts

    pct = data.get("pct_change_today")
    if pct is not None:
        score += min(max(pct, 0) * 2, 20)  # up to 20 pts

    vs_ma50 = data.get("price_vs_ma50_pct")
    if vs_ma50 is not None and 0 < vs_ma50 < 20:
        score += 10  # above MA50 but not over-extended

    return round(score, 2)


def run_momentum_scan(
    portfolio_symbols: set[str], market_data: dict[str, dict], screener_tickers: list[str]
) -> list[dict]:
    candidates = []
    for symbol in screener_tickers:
        if symbol in portfolio_symbols:
            continue
        data = market_data.get(symbol)
        if not data:
            continue
        score = momentum_score(data)
        candidates.append({"symbol": symbol, "score": score, **data})

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:MOMENTUM_TOP_N]


# ── Ticker management ─────────────────────────────────────────────────────────
def load_tickers() -> list[str]:
    try:
        with open(TICKERS_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        log.error(f"{TICKERS_FILE} not found — create it from tickers.json.example or README")
        sys.exit(1)
    except json.JSONDecodeError as e:
        log.error(f"{TICKERS_FILE} is invalid JSON: {e}")
        sys.exit(1)


def get_ticker_recommendations(summary: dict, screener_tickers: list[str]) -> dict:
    """
    Ask Claude (Haiku) for incremental watchlist changes based on today's data.
    Returns parsed JSON: {"add": [{"ticker": ..., "reason": ...}], "remove": [...]}
    """
    portfolio_symbols = {p["symbol"] for p in summary["positions"]}
    slots_available = MAX_SCREENER_TICKERS - len(screener_tickers)
    max_adds = min(3, slots_available)

    momentum_lines = "\n".join(
        f"  {m['symbol']}: RSI {_fmt(m.get('rsi'), '.1f')} | "
        f"Today {_fmt(m.get('pct_change_today'), '+.2f')}% | "
        f"Vol {_fmt(m.get('volume_ratio'), '.2f')}x | Score {m['score']}"
        for m in summary["momentum"]
    ) or "  (none)"

    sherwood = summary.get("sherwood_news", [])
    sherwood_lines = "\n".join(
        f"  • {item['title'] if isinstance(item, dict) else item}" for item in sherwood
    ) or "  (none)"

    ticker_news = summary.get("ticker_news", {})
    ticker_news_lines = ""
    for symbol, items in ticker_news.items():
        ticker_news_lines += f"  {symbol}:\n"
        for item in items:
            title = item["title"] if isinstance(item, dict) else item
            ticker_news_lines += f"    • {title}\n"
    ticker_news_lines = ticker_news_lines.strip() or "  (none)"

    watchlist_candidates = summary.get("watchlist_candidates", [])
    user_wl = [c for c in watchlist_candidates if c.get("priority") == "user"]
    rh_wl = [c for c in watchlist_candidates if c.get("priority") == "robinhood"]

    def _wl_lines(candidates):
        if not candidates:
            return "  (none)"
        return "\n".join(
            f"  {c['symbol']}: RSI {_fmt(c.get('rsi'), '.1f')} | "
            f"Today {_fmt(c.get('pct_change_today'), '+.2f')}% | "
            f"Vol {_fmt(c.get('volume_ratio'), '.2f')}x | Score {c['score']}"
            for c in candidates
        )

    prompt = (
        f"Current screener watchlist ({len(screener_tickers)} tickers, max {MAX_SCREENER_TICKERS}):\n"
        f"{', '.join(screener_tickers)}\n\n"
        f"Portfolio positions (do not add these):\n"
        f"{', '.join(portfolio_symbols)}\n\n"
        f"Today's top momentum movers (from the screener scan):\n"
        f"{momentum_lines}\n\n"
        f"Tickers from the USER'S OWN watchlists (Gaming, Tech, My First List) with momentum "
        f"signals — these are highest priority for addition:\n"
        f"{_wl_lines(user_wl)}\n\n"
        f"Tickers from Robinhood-provided watchlists (Cannabis, Software) with momentum "
        f"signals — add only if signals are strong and slots are available:\n"
        f"{_wl_lines(rh_wl)}\n\n"
        f"Today's market news (Sherwood / Robinhood):\n"
        f"{sherwood_lines}\n\n"
        f"Recent news for held positions (Yahoo Finance):\n"
        f"{ticker_news_lines}\n\n"
        f"Recommend incremental changes to the watchlist. Use news and momentum data as primary "
        f"signals. Addition priority order: (1) user's own watchlist tickers with good signals, "
        f"(2) Robinhood watchlist tickers with strong signals, (3) any other tickers with "
        f"exceptional momentum or news coverage. Remove tickers showing persistent weakness.\n\n"
        f"Respond with ONLY valid JSON — no markdown, no explanation outside the JSON:\n"
        f'{{\n'
        f'  "add": [{{"ticker": "SYM", "reason": "one sentence"}}],\n'
        f'  "remove": [{{"ticker": "SYM", "reason": "one sentence"}}]\n'
        f'}}\n\n'
        f"Constraints:\n"
        f"- Max {max_adds} additions (list is at {len(screener_tickers)}/{MAX_SCREENER_TICKERS})\n"
        f"- Max 3 removals (list must not drop below {MIN_SCREENER_TICKERS})\n"
        f"- Do not add tickers already in the portfolio\n"
        f"- Remove tickers showing persistent weakness, low relevance, or delisted/acquired status\n"
        f"- Add tickers with strong momentum signals, sector tailwinds, or high conviction setups\n"
        f"- Return empty arrays if no changes are warranted"
    )

    client = Anthropic()
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        system="You are a quantitative screener curator. Return only valid JSON, no other text.",
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:])
        raw = raw.rsplit("```", 1)[0].strip()

    return json.loads(raw)


def apply_ticker_changes(
    current: list[str], changes: dict
) -> tuple[list[str], list[dict], list[dict]]:
    """
    Apply add/remove changes, write tickers.json, return (new_list, added, removed).
    added/removed are lists of {"ticker": ..., "reason": ...} for what was actually applied.
    """
    add_items = {item["ticker"].upper(): item["reason"] for item in changes.get("add", [])}
    remove_items = {item["ticker"].upper(): item["reason"] for item in changes.get("remove", [])}

    # Enforce minimum: skip removals that would drop the list below MIN_SCREENER_TICKERS
    removed = []
    updated = list(current)
    for t in current:
        if t not in remove_items:
            continue
        if len(updated) - 1 < MIN_SCREENER_TICKERS:
            log.warning(f"Skipping removal of {t}: list would drop below minimum ({MIN_SCREENER_TICKERS})")
            continue
        updated.remove(t)
        removed.append({"ticker": t, "reason": remove_items[t]})

    added = []
    for ticker, reason in add_items.items():
        if ticker in updated:
            continue
        if len(updated) >= MAX_SCREENER_TICKERS:
            log.warning(f"Skipping add {ticker}: list at max ({MAX_SCREENER_TICKERS})")
            continue
        updated.append(ticker)
        added.append({"ticker": ticker, "reason": reason})

    with open(TICKERS_FILE, "w") as f:
        json.dump(sorted(updated), f, indent=2)

    return updated, added, removed


# ── News ──────────────────────────────────────────────────────────────────────
_RSS_HEADERS = {"User-Agent": "portfolio-monitor/1.0"}
_RSS_TIMEOUT = 10  # seconds


def _parse_feed(url: str) -> list:
    """Fetch and parse an RSS feed, returning entries or [] on failure."""
    resp = requests.get(url, headers=_RSS_HEADERS, timeout=_RSS_TIMEOUT)
    resp.raise_for_status()
    return feedparser.parse(resp.text).entries


def fetch_sherwood_news(n: int = 8) -> list[dict]:
    """Return up to n recent articles from Sherwood Media with title, url, and published date."""
    entries = _parse_feed("https://sherwood.news/rss.xml")
    items = []
    for e in entries[:n]:
        title = e.get("title", "").strip()
        if not title:
            continue
        items.append({
            "title": title,
            "url": e.get("link", ""),
            "published": e.get("published", ""),
        })
    return items


def fetch_ticker_news(symbols: list[str], n: int = 3) -> dict[str, list[dict]]:
    """
    Return up to n recent Yahoo Finance articles per symbol with title and url.
    Fetches all symbols in parallel to minimise wall-clock time.
    """
    def _fetch(symbol: str) -> tuple[str, list[dict]]:
        url = (
            f"https://feeds.finance.yahoo.com/rss/2.0/headline"
            f"?s={symbol}&region=US&lang=en-US"
        )
        entries = _parse_feed(url)
        items = []
        for e in entries[:n]:
            title = e.get("title", "").strip()
            if not title:
                continue
            items.append({"title": title, "url": e.get("link", "")})
        return symbol, items

    results = {}
    with ThreadPoolExecutor(max_workers=min(len(symbols), 8)) as pool:
        futures = {pool.submit(_fetch, s): s for s in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                sym, items = future.result()
                if items:
                    results[sym] = items
            except Exception as e:
                log.warning(f"News fetch failed for {symbol}: {e}")

    return results


def save_news_cache(date_str: str, sherwood: list[dict], ticker_news: dict[str, list[dict]]):
    """Persist today's fetched news to NEWS_FILE for email link rendering."""
    with open(NEWS_FILE, "w") as f:
        json.dump({"date": date_str, "sherwood": sherwood, "ticker_news": ticker_news}, f, indent=2)


# ── Claude analysis ───────────────────────────────────────────────────────────
def build_prompt(summary: dict) -> str:
    lines = [
        f"Portfolio Summary as of {summary['date']}",
        f"Total Portfolio Value: ${summary['total_value']:.2f}",
        f"Available Cash: ${summary['cash']:.2f}",
        "",
        "=== CURRENT POSITIONS ===",
    ]

    for pos in summary["positions"]:
        ind = pos.get("indicators", {})
        lines += [
            f"\n{pos['symbol']}: {pos['shares']} shares @ ${pos['current_price']} "
            f"(avg cost ${pos['avg_cost']}, return {pos['total_return_pct']:+.1f}%, "
            f"equity ${pos['equity']})",
            f"  RSI: {_fmt(ind.get('rsi'), '.1f')} | "
            f"MA50: ${_fmt(ind.get('ma50'), '.2f')} "
            f"({_fmt(ind.get('price_vs_ma50_pct'), '+.1f')}%) | "
            f"MA200: ${_fmt(ind.get('ma200'), '.2f')} "
            f"({_fmt(ind.get('price_vs_ma200_pct'), '+.1f')}%)",
            f"  Today: {_fmt(ind.get('pct_change_today'), '+.2f')}% | "
            f"Volume ratio: {_fmt(ind.get('volume_ratio'), '.2f')}x",
        ]

    recent_orders = summary.get("recent_orders", [])
    if recent_orders:
        lines += ["", "=== RECENT TRANSACTIONS (last 30 days) ==="]
        lines.append(
            "  (Do not recommend selling positions bought within 7 days without a severe reason)"
        )
        for o in recent_orders:
            flag = "  ← RECENT BUY — do not sell" if o["side"] == "buy" and o["days_ago"] < 7 else ""
            price_str = f"${o['price']:.4f}" if o["price"] is not None else "N/A"
            lines.append(
                f"  {o['side'].upper():<4} {o['symbol']:<8} "
                f"{o['quantity']:.4f} shares @ {price_str}  "
                f"{o['date']} ({o['days_ago']}d ago){flag}"
            )

    lines += ["", "=== TOP MOMENTUM MOVERS (not in portfolio) ==="]
    for m in summary["momentum"]:
        lines.append(
            f"\n{m['symbol']}: RSI {_fmt(m.get('rsi'), '.1f')} | "
            f"Today {_fmt(m.get('pct_change_today'), '+.2f')}% | "
            f"Vol ratio {_fmt(m.get('volume_ratio'), '.2f')}x | "
            f"Score {m['score']}"
        )

    sherwood = summary.get("sherwood_news", [])
    if sherwood:
        lines += ["", "=== MARKET NEWS (Sherwood / Robinhood) ==="]
        for item in sherwood:
            lines.append(f"• {item['title'] if isinstance(item, dict) else item}")

    ticker_news = summary.get("ticker_news", {})
    if ticker_news:
        lines += ["", "=== TICKER NEWS (Yahoo Finance) ==="]
        for symbol, items in ticker_news.items():
            lines.append(f"\n{symbol}:")
            for item in items:
                title = item["title"] if isinstance(item, dict) else item
                lines.append(f"  • {title}")

    return "\n".join(lines)


def get_claude_analysis(summary: dict) -> tuple[str, str]:
    """Return (tldr, analysis) parsed from Claude's structured response."""
    client = Anthropic()
    prompt = build_prompt(summary)
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=CLAUDE_MAX_TOKENS,
        system=CLAUDE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text
    if "\n---\n" in raw:
        tldr_part, analysis_part = raw.split("\n---\n", 1)
        tldr = tldr_part.strip()
    else:
        tldr = ""
        analysis_part = raw
    return tldr, analysis_part.strip()


# ── Email digest ──────────────────────────────────────────────────────────────
def format_digest(summary: dict, analysis: str) -> str:
    SEP = "=" * 64
    sep = "-" * 64

    tldr = summary.get("tldr", "")
    lines = [
        SEP,
        f"PORTFOLIO DIGEST — {summary['date']}",
        f"Total Value: ${summary['total_value']:.2f}  |  Cash: ${summary['cash']:.2f}",
        SEP,
    ]

    if tldr:
        lines += [
            "",
            "TL;DR",
            sep,
            tldr,
            "",
        ]

    lines += [
        "",
        "CURRENT POSITIONS",
        sep,
        f"{'Symbol':<8} {'Shares':>10} {'Price':>10} {'Avg Cost':>10} "
        f"{'Return%':>9} {'Equity':>10}",
        sep,
    ]

    for pos in summary["positions"]:
        lines.append(
            f"{pos['symbol']:<8} {pos['shares']:>10.4f} "
            f"{pos['current_price']:>10.4f} {pos['avg_cost']:>10.4f} "
            f"{pos['total_return_pct']:>8.1f}% {pos['equity']:>10.2f}"
        )

    lines += ["", "TECHNICAL INDICATORS", sep]
    for pos in summary["positions"]:
        ind = pos.get("indicators", {})
        lines.append(
            f"{pos['symbol']:<8} "
            f"RSI {_fmt(ind.get('rsi'), '.1f'):>5} | "
            f"MA50 ${_fmt(ind.get('ma50'), '.2f')} "
            f"({_fmt(ind.get('price_vs_ma50_pct'), '+.1f')}%) | "
            f"MA200 ${_fmt(ind.get('ma200'), '.2f')} "
            f"({_fmt(ind.get('price_vs_ma200_pct'), '+.1f')}%) | "
            f"Today {_fmt(ind.get('pct_change_today'), '+.2f')}% | "
            f"Vol {_fmt(ind.get('volume_ratio'), '.2f')}x"
        )

    lines += ["", "TOP MOMENTUM MOVERS (not in portfolio)", sep]
    for m in summary["momentum"]:
        lines.append(
            f"{m['symbol']:<8} "
            f"RSI {_fmt(m.get('rsi'), '.1f'):>5} | "
            f"Today {_fmt(m.get('pct_change_today'), '+.2f'):>7}% | "
            f"Vol {_fmt(m.get('volume_ratio'), '.2f'):>5}x | "
            f"Score {m['score']:>5}"
        )

    ticker_changes = summary.get("ticker_changes", {})
    added = ticker_changes.get("added", [])
    removed = ticker_changes.get("removed", [])

    lines += ["", "WATCHLIST UPDATES", sep]
    if not added and not removed:
        lines.append("No watchlist changes today.")
    if added:
        lines.append(f"Added ({len(added)}):")
        for item in added:
            lines.append(f"  {item['ticker']:<8} — {item['reason']}")
    if removed:
        lines.append(f"Removed ({len(removed)}):")
        for item in removed:
            lines.append(f"  {item['ticker']:<8} — {item['reason']}")

    lines += [
        "",
        SEP,
        "CLAUDE ANALYSIS",
        SEP,
        "",
        analysis,
        "",
        SEP,
        f"Sent from {summary.get('hostname', 'unknown')}",
    ]

    return "\n".join(lines)


# ── HTML email digest ─────────────────────────────────────────────────────────
def _color_pct(val: float | None, *, neutral: str = "#6b7280") -> str:
    """Return green/red/neutral hex based on sign of val."""
    if val is None:
        return neutral
    return "#16a34a" if val >= 0 else "#dc2626"


def _color_rsi(rsi: float | None) -> str:
    if rsi is None:
        return "#6b7280"
    if rsi > 75:
        return "#d97706"  # amber — overbought
    if rsi < 30:
        return "#dc2626"  # red — oversold
    if 55 <= rsi <= 75:
        return "#16a34a"  # green — momentum zone
    return "#6b7280"      # gray — neutral


def _h(val) -> str:
    """HTML-escape a value for safe insertion."""
    return str(val).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _md_to_html(text: str) -> str:
    """Convert the subset of markdown Claude uses in analysis to HTML."""
    text = _h(text)
    # **bold** → <strong>
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    # ### heading lines → styled div
    text = re.sub(
        r"^###\s+(.+)$",
        r'<div style="font-size:13px;font-weight:700;color:#0f172a;'
        r'margin:16px 0 6px;text-transform:uppercase;letter-spacing:0.05em;">\1</div>',
        text,
        flags=re.MULTILINE,
    )
    # ## heading lines
    text = re.sub(
        r"^##\s+(.+)$",
        r'<div style="font-size:14px;font-weight:700;color:#0f172a;margin:20px 0 8px;">\1</div>',
        text,
        flags=re.MULTILINE,
    )
    # - bullet lines → indented rows
    text = re.sub(
        r"^[-*]\s+(.+)$",
        r'<div style="padding:3px 0 3px 16px;border-left:3px solid #e2e8f0;'
        r'margin:4px 0;color:#374151;">\1</div>',
        text,
        flags=re.MULTILINE,
    )
    # Blank lines → spacing div
    text = re.sub(r"\n{2,}", '<div style="height:10px;"></div>', text)
    # Remaining single newlines → <br>
    text = text.replace("\n", "<br>")
    return text


def _td(content, *, align="right", color=None, bold=False, mono=False) -> str:
    styles = [
        "padding:8px 12px",
        "border-bottom:1px solid #f1f5f9",
        f"text-align:{align}",
        "font-size:13px",
    ]
    if color:
        styles.append(f"color:{color}")
    if bold:
        styles.append("font-weight:600")
    if mono:
        styles.append("font-family:monospace")
    return f'<td style="{";".join(styles)}">{content}</td>'


def _th(label, *, align="right") -> str:
    return (
        f'<th style="padding:8px 12px;text-align:{align};font-size:11px;'
        f'font-weight:600;color:#94a3b8;text-transform:uppercase;'
        f'letter-spacing:0.05em;border-bottom:2px solid #e2e8f0;">'
        f"{label}</th>"
    )


def _section(title: str, content: str) -> str:
    return (
        f'<div style="margin:0 0 28px;">'
        f'<h2 style="margin:0 0 12px;font-size:13px;font-weight:700;'
        f'color:#0f172a;text-transform:uppercase;letter-spacing:0.08em;'
        f'padding-bottom:8px;border-bottom:2px solid #0f172a;">{_h(title)}</h2>'
        f"{content}"
        f"</div>"
    )


def _table(header_row: str, body_rows: str) -> str:
    return (
        '<table width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;width:100%;">'
        f"<thead><tr>{header_row}</tr></thead>"
        f"<tbody>{body_rows}</tbody>"
        "</table>"
    )


def format_digest_html(summary: dict, analysis: str) -> str:
    # ── Positions table ──────────────────────────────────────────────────────
    pos_header = "".join([
        _th("Symbol", align="left"),
        _th("Shares"),
        _th("Price"),
        _th("Avg Cost"),
        _th("Return"),
        _th("Equity"),
    ])
    pos_rows = ""
    for pos in summary["positions"]:
        ret = pos["total_return_pct"]
        pos_rows += (
            "<tr>"
            + _td(_h(pos["symbol"]), align="left", bold=True, color="#0f172a")
            + _td(f"{pos['shares']:.4f}", mono=True)
            + _td(f"${pos['current_price']:.4f}", mono=True)
            + _td(f"${pos['avg_cost']:.4f}", mono=True)
            + _td(f"{ret:+.1f}%", color=_color_pct(ret), bold=True, mono=True)
            + _td(f"${pos['equity']:.2f}", mono=True, bold=True)
            + "</tr>"
        )

    # ── Indicators table ─────────────────────────────────────────────────────
    ind_header = "".join([
        _th("Symbol", align="left"),
        _th("RSI"),
        _th("MA50"),
        _th("vs MA50"),
        _th("MA200"),
        _th("vs MA200"),
        _th("Today"),
        _th("Vol Ratio"),
    ])
    ind_rows = ""
    for pos in summary["positions"]:
        ind = pos.get("indicators", {})
        rsi = ind.get("rsi")
        today = ind.get("pct_change_today")
        vs50 = ind.get("price_vs_ma50_pct")
        vs200 = ind.get("price_vs_ma200_pct")
        ind_rows += (
            "<tr>"
            + _td(_h(pos["symbol"]), align="left", bold=True, color="#0f172a")
            + _td(_fmt(rsi, ".1f"), color=_color_rsi(rsi), bold=True, mono=True)
            + _td(f"${_fmt(ind.get('ma50'), '.2f')}", mono=True)
            + _td(f"{_fmt(vs50, '+.1f')}%", color=_color_pct(vs50), mono=True)
            + _td(f"${_fmt(ind.get('ma200'), '.2f')}", mono=True)
            + _td(f"{_fmt(vs200, '+.1f')}%", color=_color_pct(vs200), mono=True)
            + _td(f"{_fmt(today, '+.2f')}%", color=_color_pct(today), mono=True)
            + _td(_fmt(ind.get("volume_ratio"), ".2f") + "x", mono=True)
            + "</tr>"
        )

    # ── Momentum table ───────────────────────────────────────────────────────
    mom_header = "".join([
        _th("Symbol", align="left"),
        _th("RSI"),
        _th("Today"),
        _th("Vol Ratio"),
        _th("Score"),
    ])
    mom_rows = ""
    for m in summary["momentum"]:
        rsi = m.get("rsi")
        today = m.get("pct_change_today")
        mom_rows += (
            "<tr>"
            + _td(_h(m["symbol"]), align="left", bold=True, color="#0f172a")
            + _td(_fmt(rsi, ".1f"), color=_color_rsi(rsi), bold=True, mono=True)
            + _td(f"{_fmt(today, '+.2f')}%", color=_color_pct(today), mono=True)
            + _td(_fmt(m.get("volume_ratio"), ".2f") + "x", mono=True)
            + _td(str(m["score"]), bold=True, mono=True, color="#0f172a")
            + "</tr>"
        )

    # ── Watchlist updates ────────────────────────────────────────────────────
    ticker_changes = summary.get("ticker_changes", {})
    added = ticker_changes.get("added", [])
    removed = ticker_changes.get("removed", [])
    ticker_news = summary.get("ticker_news", {})

    if not added and not removed:
        watchlist_content = '<p style="color:#6b7280;font-size:13px;margin:0;">No watchlist changes today.</p>'
    else:
        watchlist_content = ""
        for label, items, bg, fg in [
            ("Added", added, "#dcfce7", "#15803d"),
            ("Removed", removed, "#fee2e2", "#b91c1c"),
        ]:
            if not items:
                continue
            watchlist_content += f'<p style="margin:0 0 6px;font-size:12px;font-weight:600;color:#6b7280;">{label}</p>'
            for item in items:
                sym = item["ticker"]
                watchlist_content += (
                    f'<div style="margin:0 0 10px;">'
                    f'<div style="display:flex;align-items:baseline;gap:10px;margin-bottom:4px;">'
                    f'<span style="background:{bg};color:{fg};font-size:12px;font-weight:700;'
                    f'padding:2px 8px;border-radius:4px;font-family:monospace;white-space:nowrap;">'
                    f'{_h(sym)}</span>'
                    f'<span style="font-size:13px;color:#374151;">{_h(item["reason"])}</span>'
                    f"</div>"
                )
                # Link to any ticker-specific news articles that were available
                sym_news = ticker_news.get(sym, [])
                if sym_news:
                    for article in sym_news:
                        url = article.get("url", "")
                        title = _h(article.get("title", ""))
                        if url:
                            watchlist_content += (
                                f'<div style="padding-left:16px;font-size:11px;color:#6b7280;margin:2px 0;">'
                                f'&#8594; <a href="{url}" style="color:#3b82f6;text-decoration:none;">{title}</a>'
                                f"</div>"
                            )
                watchlist_content += "</div>"

    # ── Sherwood news ─────────────────────────────────────────────────────────
    sherwood_items = summary.get("sherwood_news", [])
    if sherwood_items:
        sherwood_content = ""
        for item in sherwood_items:
            url = item.get("url", "") if isinstance(item, dict) else ""
            title = _h(item.get("title", item) if isinstance(item, dict) else item)
            pub = item.get("published", "") if isinstance(item, dict) else ""
            pub_str = f'<span style="color:#9ca3af;font-size:10px;margin-left:6px;">{_h(pub)}</span>' if pub else ""
            if url:
                sherwood_content += (
                    f'<div style="padding:6px 0;border-bottom:1px solid #f1f5f9;">'
                    f'<a href="{url}" style="color:#0f172a;text-decoration:none;font-size:13px;'
                    f'font-weight:500;">{title}</a>{pub_str}'
                    f"</div>"
                )
            else:
                sherwood_content += (
                    f'<div style="padding:6px 0;border-bottom:1px solid #f1f5f9;'
                    f'font-size:13px;color:#0f172a;">{title}{pub_str}</div>'
                )
    else:
        sherwood_content = '<p style="color:#6b7280;font-size:13px;margin:0;">No news fetched.</p>'

    # ── Per-ticker news ───────────────────────────────────────────────────────
    all_ticker_news = summary.get("ticker_news", {})
    if all_ticker_news:
        ticker_news_content = ""
        for symbol, articles in all_ticker_news.items():
            ticker_news_content += (
                f'<p style="margin:12px 0 4px;font-size:12px;font-weight:700;'
                f'color:#0f172a;font-family:monospace;">{_h(symbol)}</p>'
            )
            for article in articles:
                url = article.get("url", "")
                title = _h(article.get("title", ""))
                if url:
                    ticker_news_content += (
                        f'<div style="padding:3px 0 3px 12px;font-size:12px;">'
                        f'<a href="{url}" style="color:#374151;text-decoration:none;">{title}</a>'
                        f"</div>"
                    )
                else:
                    ticker_news_content += (
                        f'<div style="padding:3px 0 3px 12px;font-size:12px;color:#374151;">{title}</div>'
                    )
    else:
        ticker_news_content = '<p style="color:#6b7280;font-size:13px;margin:0;">No ticker news fetched.</p>'

    # ── Analysis block ───────────────────────────────────────────────────────
    analysis_html = _md_to_html(analysis)

    # ── TL;DR block ──────────────────────────────────────────────────────────
    tldr_text = summary.get("tldr", "")
    if tldr_text:
        tldr_block = (
            '<div style="background:#0f172a;border-left:4px solid #f59e0b;'
            'padding:20px 24px;margin:0 0 28px;border-radius:0 6px 6px 0;">'
            '<p style="margin:0 0 6px;font-size:10px;font-weight:700;color:#f59e0b;'
            'text-transform:uppercase;letter-spacing:0.1em;">TL;DR</p>'
            f'<p style="margin:0;font-size:15px;line-height:1.6;color:#f1f5f9;">{_md_to_html(tldr_text)}</p>'
            '</div>'
        )
    else:
        tldr_block = ""

    # ── Assemble ─────────────────────────────────────────────────────────────
    body_content = (
        tldr_block
        + _section("Current Positions", _table(pos_header, pos_rows))
        + _section("Technical Indicators", _table(ind_header, ind_rows))
        + _section("Top Momentum Movers", _table(mom_header, mom_rows))
        + _section("Watchlist Updates", watchlist_content)
        + _section(
            "Claude Analysis",
            f'<div style="font-size:14px;line-height:1.7;color:#1e293b;">{analysis_html}</div>',
        )
        + _section("Market News (Sherwood)", sherwood_content)
        + _section("Ticker News", ticker_news_content)
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;">
<tr><td style="padding:20px 0;">
<table width="700" align="center" cellpadding="0" cellspacing="0"
  style="background:#ffffff;margin:0 auto;border-radius:10px;
         overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.08);">

  <!-- Header -->
  <tr>
    <td style="background:#0f172a;padding:24px 32px;">
      <p style="margin:0;color:#94a3b8;font-size:11px;font-weight:600;
                text-transform:uppercase;letter-spacing:0.1em;">Daily Digest</p>
      <h1 style="margin:4px 0 0;color:#f8fafc;font-size:22px;font-weight:700;">
        Portfolio Monitor</h1>
      <p style="margin:4px 0 0;color:#64748b;font-size:13px;">{_h(summary["date"])}</p>
    </td>
  </tr>

  <!-- Summary bar -->
  <tr>
    <td style="background:#1e293b;padding:16px 32px;">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="color:#94a3b8;font-size:11px;font-weight:600;
                     text-transform:uppercase;letter-spacing:0.05em;padding-bottom:4px;">
            Total Value</td>
          <td style="color:#94a3b8;font-size:11px;font-weight:600;
                     text-transform:uppercase;letter-spacing:0.05em;padding-bottom:4px;">
            Available Cash</td>
        </tr>
        <tr>
          <td style="color:#f8fafc;font-size:24px;font-weight:700;font-family:monospace;">
            ${summary["total_value"]:.2f}</td>
          <td style="color:#f8fafc;font-size:24px;font-weight:700;font-family:monospace;">
            ${summary["cash"]:.2f}</td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- Body -->
  <tr>
    <td style="padding:28px 32px;">
      {body_content}
    </td>
  </tr>

  <!-- Glossary -->
  <tr>
    <td style="background:#f8fafc;padding:20px 32px;border-top:1px solid #e2e8f0;">
      <p style="margin:0 0 8px;font-size:10px;font-weight:700;color:#94a3b8;
                text-transform:uppercase;letter-spacing:0.08em;">Abbreviations</p>
      <p style="margin:0;font-size:11px;color:#94a3b8;line-height:1.8;">
        <strong style="color:#64748b;">RSI</strong> &mdash;
          Relative Strength Index (0&ndash;100). Momentum oscillator:
          <span style="color:#16a34a;">55&ndash;75 = momentum zone</span>,
          <span style="color:#d97706;">&gt;75 = overbought</span>,
          <span style="color:#dc2626;">&lt;30 = oversold</span>.
        &nbsp;&nbsp;
        <strong style="color:#64748b;">MA50 / MA200</strong> &mdash;
          50-day and 200-day simple moving averages of closing price.
        &nbsp;&nbsp;
        <strong style="color:#64748b;">vs MA50 / vs MA200</strong> &mdash;
          How far the current price is above or below the moving average, as a percentage.
        &nbsp;&nbsp;
        <strong style="color:#64748b;">Vol Ratio</strong> &mdash;
          Today&rsquo;s volume divided by the 30-day average volume.
          1.0&times; = normal activity; &gt;2&times; = unusually high.
        &nbsp;&nbsp;
        <strong style="color:#64748b;">Today</strong> &mdash;
          Price change from previous close, as a percentage.
        &nbsp;&nbsp;
        <strong style="color:#64748b;">Score</strong> &mdash;
          Momentum score (0&ndash;100) combining RSI zone (40 pts),
          volume spike (30 pts), daily move (20 pts),
          and price position vs MA50 (10 pts).
      </p>
    </td>
  </tr>

  <!-- Footer -->
  <tr>
    <td style="background:#f8fafc;padding:12px 32px;border-top:1px solid #e2e8f0;
               text-align:center;">
      <p style="margin:0;font-size:11px;color:#94a3b8;">
        Generated by portfolio_monitor.py &middot; {_h(summary["date"])}
        &middot; sent from {_h(summary.get("hostname", "unknown"))}
      </p>
    </td>
  </tr>

</table>
</td></tr>
</table>
</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    load_dotenv()
    log.info("Starting portfolio monitor")
    today = date.today().isoformat()
    hostname = socket.gethostname()

    # 1. Load screener tickers
    screener_tickers = load_tickers()
    log.info(f"Loaded {len(screener_tickers)} screener tickers from {TICKERS_FILE}")

    # 2. Robinhood auth
    try:
        robinhood_login()
        log.info("Robinhood login successful")
    except Exception as e:
        log.error(f"Robinhood auth failed: {e}")
        send_error_email("Robinhood authentication", e)
        sys.exit(1)

    # 3. Pull positions and cash
    try:
        positions = get_positions()
        cash = get_cash()
        log.info(f"Fetched {len(positions)} positions, cash: ${cash}")
    except Exception as e:
        log.error(f"Failed to fetch positions: {e}")
        send_error_email("fetching Robinhood positions", e)
        sys.exit(1)

    portfolio_symbols = {p["symbol"] for p in positions}

    # 3b. Fetch recent order history (for capital-consistency checks in Claude prompt)
    try:
        recent_orders = get_recent_orders(days=30)
        log.info(f"Fetched {len(recent_orders)} recent filled orders")
    except Exception as e:
        log.warning(f"Could not fetch recent orders: {e}")
        recent_orders = []

    # 4. Bulk fetch market data for portfolio + screener in two calls
    try:
        portfolio_data = fetch_bulk_market_data(sorted(portfolio_symbols))
        for pos in positions:
            pos["indicators"] = portfolio_data.get(pos["symbol"], {})
    except Exception as e:
        log.error(f"Portfolio market data failed: {e}")
        send_error_email("fetching portfolio market data", e)
        sys.exit(1)

    try:
        screener_data = fetch_bulk_market_data(
            [t for t in screener_tickers if t not in portfolio_symbols]
        )
    except Exception as e:
        log.error(f"Screener market data failed: {e}")
        screener_data = {}

    # 5. Score watchlist tickers as preferred add candidates
    watchlist_candidates = []
    try:
        watchlist_syms = get_watchlist_tickers(
            exclude=portfolio_symbols | set(screener_tickers)
        )
        all_watchlist_syms = watchlist_syms["user"] + watchlist_syms["robinhood"]
        if all_watchlist_syms:
            log.info(
                f"Fetching market data for {len(all_watchlist_syms)} watchlist tickers "
                f"({len(watchlist_syms['user'])} user, {len(watchlist_syms['robinhood'])} RH)"
            )
            watchlist_market_data = fetch_bulk_market_data(all_watchlist_syms)
            for priority, syms in [("user", watchlist_syms["user"]), ("robinhood", watchlist_syms["robinhood"])]:
                for sym in syms:
                    data = watchlist_market_data.get(sym)
                    if not data:
                        continue
                    score = momentum_score(data)
                    if score >= WATCHLIST_MIN_SCORE:
                        watchlist_candidates.append({"symbol": sym, "score": score, "priority": priority, **data})
            watchlist_candidates.sort(key=lambda x: (x["priority"] != "user", -x["score"]))
            log.info(
                f"Watchlist candidates (score >= {WATCHLIST_MIN_SCORE}): "
                f"{[c['symbol'] for c in watchlist_candidates] or 'none'}"
            )
    except Exception as e:
        log.warning(f"Watchlist evaluation failed: {e}")

    # 6. Momentum scan (screener tickers only — watchlist candidates handled separately above)
    try:
        momentum = run_momentum_scan(portfolio_symbols, screener_data, screener_tickers)
        log.info(f"Momentum scan complete: {len(momentum)} candidates")
    except Exception as e:
        log.error(f"Momentum scan failed: {e}")
        momentum = []

    # 7. Build summary
    total_equity = sum(p["equity"] for p in positions)
    total_value = round(total_equity + cash, 2)
    summary = {
        "date": today,
        "hostname": hostname,
        "total_value": total_value,
        "cash": cash,
        "positions": positions,
        "momentum": momentum,
        "watchlist_candidates": watchlist_candidates,
        "recent_orders": recent_orders,
    }

    # 8. Fetch market and ticker news
    try:
        log.info("Fetching Sherwood news")
        summary["sherwood_news"] = fetch_sherwood_news()
    except Exception as e:
        log.warning(f"Sherwood news fetch failed: {e}")
        summary["sherwood_news"] = []

    try:
        log.info(f"Fetching ticker news for {sorted(portfolio_symbols)}")
        summary["ticker_news"] = fetch_ticker_news(sorted(portfolio_symbols))
    except Exception as e:
        log.warning(f"Ticker news fetch failed: {e}")
        summary["ticker_news"] = {}

    try:
        save_news_cache(today, summary["sherwood_news"], summary["ticker_news"])
        log.info(f"News cache saved to {NEWS_FILE}")
    except Exception as e:
        log.warning(f"Failed to save news cache: {e}")

    # 9. Update screener watchlist
    try:
        log.info("Requesting ticker recommendations")
        changes = get_ticker_recommendations(summary, screener_tickers)
        _, added, removed = apply_ticker_changes(screener_tickers, changes)
        summary["ticker_changes"] = {"added": added, "removed": removed}
        if added:
            log.info(f"Tickers added: {[x['ticker'] for x in added]}")
        if removed:
            log.info(f"Tickers removed: {[x['ticker'] for x in removed]}")
        if not added and not removed:
            log.info("No ticker changes today")
    except Exception as e:
        log.error(f"Ticker recommendation failed: {e}")
        summary["ticker_changes"] = {"added": [], "removed": []}

    # 10. Claude analysis
    try:
        log.info("Requesting Claude analysis")
        tldr, analysis = get_claude_analysis(summary)
        summary["tldr"] = tldr
    except Exception as e:
        log.error(f"Claude analysis failed: {e}")
        send_error_email("Claude API analysis", e)
        tldr = ""
        analysis = f"[Claude analysis unavailable: {e}]"
        summary["tldr"] = ""

    # 11. Format and send email digest
    try:
        body = format_digest(summary, analysis)
        html_body = format_digest_html(summary, analysis)
        subject = f"Portfolio Digest - {today} - Total: ${total_value:.2f}"
        send_email(subject, body, html_body=html_body)
        log.info("Digest sent successfully")
    except Exception as e:
        log.error(f"Failed to send digest email: {e}")
        send_error_email("sending email digest", e)
        sys.exit(1)

    log.info("Portfolio monitor complete")


if __name__ == "__main__":
    main()
