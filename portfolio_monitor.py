"""
portfolio_monitor.py — Daily Robinhood portfolio digest with Claude AI analysis.

Usage:
    python portfolio_monitor.py
"""

import json
import os
import re
import socket
import subprocess
import sys
import logging
import smtplib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import feedparser
import pandas as pd
import pytz
import requests
import yfinance as yf
import robin_stocks.robinhood as r
from anthropic import Anthropic
from dotenv import load_dotenv

# Set a recognizable User-Agent for device-approval prompts in the Robinhood app.
r.helper.update_session("User-Agent", "Robinhood Monitor/1.0")

# robin_stocks 3.4.0 (latest) crashes when the push-approval polling endpoint
# returns 429 (None response) instead of continuing to wait. Patch it here so
# the fix survives venv rebuilds. Upstream bug: jmfernandes/robin_stocks#auth
import robin_stocks.robinhood.authentication as _rh_auth
_orig_validate = _rh_auth._validate_sherrif_id

def _patched_validate(*args, **kwargs):
    _orig_request_get = _rh_auth.request_get
    def _safe_request_get(url, *a, **kw):
        result = _orig_request_get(url, *a, **kw)
        if result is None:
            # 429 — back off hard before the next poll (existing 5s sleep still runs too)
            time.sleep(25)
            return {"challenge_status": "pending"}
        return result
    _rh_auth.request_get = _safe_request_get
    try:
        return _orig_validate(*args, **kwargs)
    finally:
        _rh_auth.request_get = _orig_request_get

_rh_auth._validate_sherrif_id = _patched_validate

# ── Screener watchlist ────────────────────────────────────────────────────────
# Tickers are loaded from tickers.json at runtime and updated daily by Claude.
# Edit tickers.json directly to make manual changes.
TICKERS_FILE = "tickers.json"
NEWS_FILE = "news.json"
ANALYSIS_FILE = "last_analysis.json"
MIN_SCREENER_TICKERS = 25
MAX_SCREENER_TICKERS = 40
WATCHLIST_MIN_SCORE = 15  # minimum momentum score to surface a watchlist ticker as a candidate
PROTECTED_SYMBOLS = {"COST"}  # core long-term holdings; see PROTECTED SYMBOL rule in CLAUDE_SYSTEM_PROMPT
PROTECTED_TRIM_MAX_PCT = 0.10  # max fraction of a protected symbol's OWN equity trimmable per action
PROTECTED_COMMITMENT_PENDING_DAYS = 7  # days to wait for a recommended trim to actually execute before dropping it
PROTECTED_COMMITMENTS_FILE = "protected_commitments.json"
SMALL_POSITION_THRESHOLD = 10  # equity ($) at/under which a position is a cleanup candidate
ETHICAL_EXCLUSIONS_FILE = "ethical_exclusions.json"

# ANALYSIS_FILE and PROTECTED_COMMITMENTS_FILE carry real dollar figures from
# the user's account, so — unlike tickers.json/ethical_exclusions.json — they
# stay out of git (this repo is public) and instead get repointed at a
# Dropbox-synced directory by resolve_sync_paths(), since this script runs
# from more than one machine and both need to see the same current state.
SYNC_DIR = os.path.expanduser("~/Dropbox/robinhood-monitor")

# ── Config ────────────────────────────────────────────────────────────────────
CLAUDE_MODEL = "claude-sonnet-5"
CLAUDE_MAX_TOKENS = 4000
CLAUDE_SYSTEM_PROMPT = (
    "You are a female stock trading advisor for a high-risk-tolerance retail investor "
    "interested in making ethically responsible investment choices "
    "with ~$1000 in a Robinhood play account. Goals: aggressive growth, "
    "no pump-and-dump, no unsavory trades, Robinhood platform only. "
    "Robinhood supports fractional share purchases, so small dollar amounts (even $5-20) "
    "can be deployed to open or add to a position.\n\n"
    "ETHICAL INVESTMENT SCREEN — HARD CONSTRAINT: This portfolio is screened against a "
    "defined policy, not vague sentiment — see ETHICAL_SCREEN_CRITERIA for the full "
    "exclusion criteria (weapons/defense contractors, mass-surveillance/policing/ICE "
    "contractors, fossil fuel extraction, data center builders/operators, private prisons, "
    "predatory lenders, factory farming; tobacco/gambling/cannabis/vice industries are NOT "
    "excluded). This is enforced mechanically before you ever see a candidate — any symbol "
    "already known to violate the policy is stripped from the screener, watchlist "
    "candidates, and momentum data before this prompt is built, so it will not appear "
    "below as a buy candidate. If a position below is tagged [ETHICAL SCREEN: reason] it is "
    "something you already hold that was screened and found to violate the policy — do not "
    "force an immediate sale, but recommend a full exit and state the ethical reason "
    "explicitly, subject to normal RECENT POSITIONS timing (do not exit something bought "
    "days ago without also naming that tension). This is not optional to address when the "
    "tag is present.\n\n"
    "CAPITAL STRATEGY: This portfolio is self-funding — no new money is added from outside. "
    "External capital injection is possible but should only be flagged when conditions are "
    "compelling enough to justify it, not as a routine suggestion whenever cash is low. "
    "The core growth mechanic is riding winners while actively rotating a portion of gains into "
    "the next high-momentum opportunity. The goal is to accelerate portfolio growth — not to "
    "trim winners for the sake of balance, but to ask each session: is there a better place for "
    "some of this capital right now? Each position shows its % of portfolio — use this as a "
    "signal, not a rule. A large winner is a funding source when a compelling opportunity exists; "
    "it is not a problem to fix on its own. Staying fully invested means staying invested in the "
    "BEST opportunities available. When recommending purchases, state total capital available "
    "(cash + sell/trim proceeds) and confirm the math adds up.\n\n"
    "PROFIT-TAKING AND ROTATION: Each session, actively scan for rotation opportunities: "
    "which positions have strong momentum signals but tiny allocations that deserve more capital? "
    "If a high-momentum mover appears in the screener and a winner has run well beyond its "
    "near-term upside, a partial trim to fund the new position is the right move. "
    "A partial sell (a fraction of a big winner — see POSITION-SIZE-AWARE TRIM SIZING) to seed a "
    "new high-conviction entry is "
    "a valid and encouraged move — you don't have to sell all or nothing. "
    "Selling at a gain to redeploy into a better opportunity is the core mechanic. "
    "Selling at a loss requires clear justification: broken thesis, significant adverse news, "
    "or a technical breakdown with no credible recovery signal.\n\n"
    "NEWS INTEGRATION: The market news section (Sherwood/Robinhood) appears at the top of the "
    "prompt — read it first and let it set the macro frame before evaluating individual positions. "
    "Ask: what sectors or themes are these headlines pointing to? Are any of those themes "
    "underrepresented in the current portfolio? That gap is a rotation candidate. "
    "Each position also includes its own recent headlines — use those to validate or contradict "
    "the hold decision (a guidance cut or competitive threat is a trim signal; a major contract "
    "win is a hold or add signal). Cite specific headlines when making recommendations. "
    "News should drive action, not just appear as background color.\n\n"
    "TIMING AWARENESS: The prompt header includes the day of week, current time, and market "
    "session. Factor this into recommendations — a pre-market analysis runs before prices are "
    "confirmed for the day; a Monday analysis follows a weekend during which no trades could "
    "be made. Advice should reflect what is actionable at the moment it is read.\n\n"
    "REACTIVE SELLING RULE: A single-day price drop is not itself a trim signal. Evaluate "
    "whether the underlying thesis has changed, not whether the price is lower than yesterday. "
    "Sector-wide pullbacks driven by a competitor's news are distinct from company-specific "
    "deterioration — treat them differently. A trim is warranted by broken fundamentals or a "
    "better redeployment opportunity, not by the size of today's move.\n\n"
    "RECENT POSITIONS: Do not recommend selling positions bought < 7 days ago without a severe "
    "specific reason (marked in the transaction history).\n\n"
    "COST BASIS DISCIPLINE — HARD CONSTRAINT: Each position below is tagged FUNDING-ELIGIBLE or "
    "NOT FUNDING-ELIGIBLE, computed from its actual total_return_pct — not from today's price "
    "move, RSI, or distance from its moving averages, all of which describe short-term technical "
    "strength and say nothing about whether the position is a gain or a loss on cost basis. A "
    "position can be up today and above its MA50 while still being NOT FUNDING-ELIGIBLE because "
    "it is underwater on cost basis — that is not a contradiction, and today's move does not "
    "override it. The '=== ELIGIBLE FUNDING SOURCES ===' list is the complete set of positions "
    "you may trim to fund a new buy. Trim from that list, best-first, unless you explicitly state "
    "why you are overriding it. The only valid overrides are: (1) the position's thesis is "
    "specifically broken (adverse news, structural change) and you are selling for that reason, "
    "independent of funding a new buy, or (2) the eligible list is empty or insufficient and the "
    "capital is urgently needed with no other option. If you recommend trimming a NOT "
    "FUNDING-ELIGIBLE position without invoking one of these two overrides by name, your output "
    "is invalid. When you do recommend a trim, state the position's total_return_pct in your "
    "one-sentence reasoning so the funding source is verifiably a winner, not just a mover. "
    "'It has been going down' is not a thesis — price weakness alone does not justify "
    "crystallizing a loss.\n\n"
    "REPEATED SELL PATTERN — HARD CONSTRAINT: If a '=== TRIM COUNT WARNINGS ===' section appears "
    "in the data below, you are FORBIDDEN from recommending another partial trim for any symbol "
    "listed there — no exceptions, even when a new buy looks compelling and that symbol is the "
    "obvious funding source. Your only two allowed recommendations for a flagged symbol are: "
    "(1) HOLD — the thesis is intact, take no action, or (2) FULL EXIT — the thesis is broken, "
    "sell the entire remaining position. Continuing to sell the same stock at progressively lower "
    "prices is capitulation, not active management, and 'this trim only makes sense because it "
    "funds the new opportunity' is precisely the reasoning this rule exists to block — it is not "
    "a valid override. If a new opportunity is genuinely compelling enough to require funding, "
    "find a different funding source that is NOT flagged, or state plainly that no compelling "
    "funding source is currently available and the opportunity should wait for new cash or a "
    "different position's proceeds. Do not treat the warning as advisory — it is a rule, not a "
    "data point to weigh against the buy.\n\n"
    "POSITION-SIZE-AWARE TRIM SIZING — HARD CONSTRAINT: A trim's dollar amount must be sized "
    "relative to that position's current equity (shown per position as 'equity $X'), never picked "
    "as a fixed dollar figure in isolation. A trim by definition leaves the MAJORITY of the "
    "position intact: keep any trim to at most ~50% of the position's equity. Before naming a "
    "dollar amount, look at the position's equity and check the ratio — if the amount you want to "
    "raise would exceed ~50% of the position, you are not trimming, you are exiting, so either take "
    "a genuinely partial amount or recommend a FULL EXIT and call it that explicitly. Positions "
    "under ~$50 of equity are too small to trim meaningfully — never recommend a partial trim of a "
    "sub-$50 position; hold it or exit it fully. In your one-sentence reasoning, state the position's "
    "equity and the trim as a share of it (e.g. 'Sell $20 of a $95 position, ~21%') so the sizing is "
    "verifiable. The fixed-dollar examples elsewhere in this prompt are illustrative only and never "
    "override this ratio.\n\n"
    "PROTECTED SYMBOL RULE: Symbols tagged LIMITED FUNDING SOURCE or FUNDING BLOCKED in the "
    "position data are core long-term holdings and should almost never be trimmed to fund "
    "something else — they are not off-limits, but heavily constrained. A trim of a protected "
    "symbol must not exceed the dollar cap given in the '=== PROTECTED SYMBOLS ===' section (10% "
    "of that position's own equity) — this overrides the normal ~50% POSITION-SIZE-AWARE TRIM "
    "SIZING rule for these symbols specifically. Any trim of a protected symbol must be paired, "
    "in the same recommendation, with an explicit reinvestment condition: a price at or below "
    "the trim's sale price, or a named technical pullback/support level. A plan that would only "
    "buy back at a higher price is not acceptable — recommend HOLD instead. The small mandatory "
    "size plus the required reinvestment plan are intentional: trimming a protected symbol should "
    "be rare, not routine.\n\n"
    "PROTECTED SYMBOL REINVESTMENT — HARD CONSTRAINT: If a symbol appears in the "
    "'=== OUTSTANDING REINVESTMENT COMMITMENTS ===' section, you are FORBIDDEN from recommending "
    "another partial trim of it — no exceptions — until that commitment no longer appears there. "
    "This is tracked by the script from actual order history, not from anything stated in a prior "
    "analysis, so there is no way to satisfy it except a real buy-back — restating the plan again "
    "does not clear it. The only allowed override is a FULL EXIT for a specifically broken thesis "
    "(adverse news, structural change), named explicitly. If the current price shown alongside "
    "the commitment has already reached the committed level, prioritize recommending that "
    "reinvestment buy this session over other new entries.\n\n"
    "SMALL POSITION CLEANUP RULE: Positions tagged [SMALL POSITION] default to a full-exit "
    "candidate rather than an automatic hold. Recommend a full exit when the position is stale — "
    "no momentum signal (RSI outside the 55-75 momentum zone, price flat against MA50, volume "
    "ratio near 1x, little movement today), no supportive recent news, and not bought in the last "
    "30 days. Keep it only with a specific stated reason — a live catalyst, a still-developing "
    "recent buy, an active momentum signal, or supportive news — named explicitly in HOLDS. This "
    "is an explicit exception to COST BASIS DISCIPLINE: a small stale position may be closed even "
    "at a loss, citing 'SMALL POSITION CLEANUP' instead of a broken-thesis reason, because the "
    "action is portfolio hygiene, not funding a new buy.\n\n"
    "First, write 2-3 sentences framed as a tl;dr of the overall trends or advice given. "
    "This is a high-level, humanistic read on the most important trend, risk, or opportunity "
    "facing the portfolio right now — not a trade recommendation, but the broader context that "
    "should inform every decision today. End this paragraph with exactly the line: ---\n"
    "TRIM SIGNALS — consider a partial trim (even without a broken thesis) when a position is "
    "30%+ above its MA50, a single position exceeds 35% of portfolio, or a high-momentum "
    "opportunity exists that the portfolio doesn't yet capture. "
    "A partial trim means taking a fraction of the position off the table, not exiting — size it "
    "per POSITION-SIZE-AWARE TRIM SIZING (at most ~50% of that position's equity), not as a fixed "
    "dollar figure. Riding a winner and taking partial profits are not mutually exclusive.\n\n"
    "Then write a full analysis in three blocks. Do not use --- as dividers between blocks. "
    "Avoid trading jargon — write plainly for someone who trades casually but is not an expert.\n\n"
    "TRIMS/EXITS: Only list positions actually being trimmed or exited — one bold action line "
    "per trim (e.g. **TRIM ARM — Sell $50**) followed by one sentence of reasoning. For a "
    "protected-symbol trim, state the reinvestment condition in that same sentence — this is "
    "required for the commitment to be tracked. "
    "Do not mention positions that are merely being held; those go in HOLDS. "
    "If nothing to trim, say so in one sentence.\n\n"
    "BUYS: State total capital available (cash + trim proceeds) in one line. Then one bold action "
    "line per buy (e.g. **BUY MRVL — $35**) with one sentence of reasoning. "
    "Do not include a capital math summary — state each buy exactly once. "
    "If a move is genuinely compelling but there is no internal funding source (no cash, no "
    "reasonable trim, everything else restricted or a loser), do not just say 'no buys today' — "
    "say explicitly that the opportunity is worth funding with outside cash (a deposit) even "
    "though nothing internal can fund it, and name the ticker and why.\n\n"
    "HOLDS: List each held position on its own line with a brief reason. "
    "Do not group positions into categories.\n\n"
    "End with **ONE KEY THING TO WATCH TODAY:** followed by one sentence describing something "
    "not yet actionable. If it is actionable, put it in TRIMS or BUYS instead.\n\n"
    "INTERNAL CONSISTENCY RULE: If you identify a compelling new entry opportunity but have no cash "
    "and recommend no trims, your output is self-contradicting unless you explicitly resolve it one "
    "of three ways: (1) the opportunity IS compelling — identify a trim to fund it and recommend "
    "both together; (2) the opportunity is NOT compelling enough to justify disrupting a winning "
    "position — say so plainly; or (3) the opportunity IS compelling but no internal funding source "
    "exists right now — say so plainly and flag it as worth funding with a deposit instead of letting "
    "it pass silently. Never recommend a new entry alongside 'no cash, no trims' without picking one "
    "of these three. "
    "Use specific BUY, SELL, TRIM, or HOLD language. Be concise — lead with the decision, "
    "one sentence of reasoning per position. Do not output systematic check tables or grids."
)

# robin_stocks stores pickles in ~/.tokens/robinhood<name>.pickle regardless of path
ROBIN_TOKEN_NAME = ".robin_token"
ROBIN_TOKEN_PATH = os.path.expanduser(f"~/.tokens/robinhood{ROBIN_TOKEN_NAME}.pickle")
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


def get_market_session() -> tuple[str, str, str, bool]:
    """
    Return (day_name, time_str, session_label, is_stale) for the current moment in ET.

    is_stale is True when the most recent daily bar from yfinance reflects the prior
    completed session rather than anything happening right now (pre-market, before
    yfinance has created today's bar, or a weekend with no session at all). Callers
    must relabel "today's" % change figures in that case — they are the prior
    session's already-realized move, not new intraday action, and presenting them
    as fresh momentum causes a single move to be double-counted across daily runs.
    """
    et = pytz.timezone("America/New_York")
    now_et = datetime.now(et)
    day_name = now_et.strftime("%A")
    time_str = now_et.strftime("%I:%M %p ET")
    hour = now_et.hour
    if now_et.weekday() >= 5:
        session = "Market closed (weekend)"
        is_stale = True
    elif hour < 9 or (hour == 9 and now_et.minute < 30):
        session = "Pre-market"
        is_stale = True
    elif hour < 16:
        session = "Market hours"
        is_stale = False
    else:
        session = "After-hours"
        is_stale = False
    return day_name, time_str, session, is_stale


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
        hostname = socket.gethostname()
        subject = f"Portfolio Monitor ERROR ({hostname}) - {date.today()}"
        body = f"Script failed during: {context}\n\nError: {type(exc).__name__}: {exc}"
        send_email(subject, body)
    except Exception as e:
        log.error(f"Failed to send error email: {e}")


# ── Cross-machine state sync ─────────────────────────────────────────────────
def resolve_sync_paths():
    """
    Repoint ANALYSIS_FILE and PROTECTED_COMMITMENTS_FILE at SYNC_DIR (a Dropbox
    folder) so both machines this script runs from read/write the same file,
    instead of each keeping its own local copy that only updates when *that*
    machine happens to run. Falls back to the local repo-relative filename
    (no cross-machine sync) if the Dropbox folder can't be created.
    """
    global ANALYSIS_FILE, PROTECTED_COMMITMENTS_FILE
    try:
        os.makedirs(SYNC_DIR, exist_ok=True)
        ANALYSIS_FILE = os.path.join(SYNC_DIR, "last_analysis.json")
        PROTECTED_COMMITMENTS_FILE = os.path.join(SYNC_DIR, "protected_commitments.json")
    except OSError as e:
        log.warning(f"Dropbox sync dir unavailable ({e}) — using local files, no cross-machine sync")


# ── Git helpers ───────────────────────────────────────────────────────────────
def git_pull():
    """Pull latest changes before running. Non-fatal — logs and continues on failure."""
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        # git 2.35.2+ rejects pulls when the repo owner differs from the running user.
        # Register this directory as safe so cron (potentially a different uid) can pull.
        subprocess.run(
            ["git", "config", "--global", "--add", "safe.directory", repo_dir],
            capture_output=True, check=False
        )
        result = subprocess.run(
            ["git", "pull", "--ff-only"],
            capture_output=True, text=True, check=True
        )
        log.info(f"git pull: {result.stdout.strip() or 'already up to date'}")
    except subprocess.CalledProcessError as e:
        log.warning(f"git pull failed (continuing anyway): {e.stderr.strip()}")


def git_commit_tickers():
    """Commit and push tickers.json if it has changed. Non-fatal."""
    git_commit_file(TICKERS_FILE, "chore: update screener watchlist [skip ci]")


def git_commit_ethical_exclusions():
    """
    Commit and push ethical_exclusions.json if it has changed. Non-fatal.

    This is mechanically-enforced state (excluded symbols are stripped from
    tickers.json, watchlist candidates, and ticker-recommendation output), not
    just a local cache — leaving it unsynced means a second machine re-screens
    every symbol from scratch and could, since screening is an LLM call rather
    than a fixed lookup, reach a different verdict than the first machine did.
    Unlike last_analysis.json/protected_commitments.json, this file carries no
    dollar figures, so it's fine to sync via the (public) git repo rather than
    needing the Dropbox path in resolve_sync_paths().
    """
    git_commit_file(ETHICAL_EXCLUSIONS_FILE, "chore: update ethical exclusions [skip ci]")


def git_commit_file(path: str, message: str):
    """Force-add and commit `path` (even if gitignored) if it has changed, then push. Non-fatal."""
    try:
        # `git diff` alone misses the "never tracked yet" case for gitignored
        # files (nothing to diff against), so check status instead.
        status = subprocess.run(
            ["git", "status", "--porcelain", "--ignored", "--", path],
            capture_output=True, text=True, check=True
        )
        if not status.stdout.strip():
            log.info(f"{path} unchanged — skipping git commit")
            return
        subprocess.run(["git", "add", "-f", path], check=True)
        subprocess.run(
            ["git", "commit", "-m", message],
            check=True, capture_output=True
        )
        subprocess.run(["git", "push"], check=True, capture_output=True)
        log.info(f"{path} committed and pushed")
    except subprocess.CalledProcessError as e:
        log.warning(f"git commit/push of {path} failed: {e.stderr.strip() if e.stderr else e}")


# ── Robinhood auth ────────────────────────────────────────────────────────────
REAUTH_INSTRUCTIONS = (
    "To fix, run reauth.py:\n"
    "  cd ~/Claude/robinhood-monitor && .venv/bin/python reauth.py\n\n"
    "It will prompt you to run this in the Robinhood browser console:\n"
    "  JSON.stringify(JSON.parse(localStorage.getItem('web:auth_state')))"
)


def _is_rate_limited(exc: Exception) -> bool:
    return "429" in str(exc) or "too many" in str(exc).lower()


def robinhood_login():
    """
    Log in to Robinhood, preferring the cached pickle over the full auth flow.

    Loads and injects the pickle session ourselves so we can distinguish a
    real auth failure from a transient 429 — robin_stocks treats both the same
    and would cascade into a re-auth push-notification loop on a rate limit.

    If the pickle is missing or genuinely expired, falls through to r.login()
    which triggers the device-approval flow. On repeated failure, raises with
    instructions to run reauth.py.
    """
    def _do_login():
        r.login(
            username=os.getenv("ROBINHOOD_USERNAME"),
            password=os.getenv("ROBINHOOD_PASSWORD"),
            store_session=True,
            pickle_name=ROBIN_TOKEN_NAME,
        )

    # Load the pickle ourselves so we can handle 429 on validation without
    # misreading it as an expired token and triggering unnecessary re-auth.
    if os.path.exists(ROBIN_TOKEN_PATH):
        try:
            with open(ROBIN_TOKEN_PATH, "rb") as f:
                import pickle as _pickle
                data = _pickle.load(f)
            r.helper.update_session("Authorization", f"{data['token_type']} {data['access_token']}")
            r.authentication.set_login_state(True)
            try:
                profile = r.load_portfolio_profile()
                if profile is None:
                    # robin_stocks returns None (not an exception) on 401 — token is expired.
                    raise ValueError("load_portfolio_profile returned None — token likely expired")
                log.info("Loaded cached Robinhood session")
                return
            except Exception as e:
                if _is_rate_limited(e):
                    # Rate-limited during validation — session is probably still valid.
                    # Proceed and let the actual API calls fail with a clear error if needed.
                    log.warning("Rate-limited during session validation — assuming session still valid")
                    return
                log.warning("Cached session invalid — attempting fresh login")
        except Exception:
            log.warning("Could not load pickle — attempting fresh login")

    # No valid pickle: trigger the push-notification auth flow.
    _do_login()
    try:
        profile = r.load_portfolio_profile()
        if profile is None:
            raise RuntimeError(
                "Robinhood session could not be established.\n\n" + REAUTH_INSTRUCTIONS
            )
    except RuntimeError:
        raise
    except Exception as e:
        if _is_rate_limited(e):
            raise RuntimeError(
                "Robinhood API is rate-limiting this account. Wait and retry, or:\n\n"
                + REAUTH_INSTRUCTIONS
            ) from e
        raise RuntimeError(
            "Robinhood session could not be established.\n\n" + REAUTH_INSTRUCTIONS
        ) from e


# ── Positions ─────────────────────────────────────────────────────────────────
def get_positions() -> list[dict]:
    raw = r.get_open_stock_positions()
    if not raw:
        log.warning("get_open_stock_positions returned empty/None — session may be invalid")
        return []
    positions = []

    for pos in (p for p in raw if p is not None):
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
    """
    Return the account's cash balance.

    Two guesses at the right field/account have both still read $0
    (withdrawable_amount, then a single account's "cash") — rather than
    guess a third time, this logs every cash-adjacent field from every
    account and endpoint robin_stocks exposes, so the next mismatch is
    diagnosable from monitor.log's "CASH DIAGNOSTIC" lines instead of
    another blind guess. The returned value (sum of "cash" across
    whatever load_account_profile(dataType="results") returns) is a
    best-effort current guess, not a confirmed-correct one yet.
    """
    try:
        accounts = r.load_account_profile(dataType="results") or []
    except Exception as e:
        log.warning(f"Could not fetch cash balance: {e}")
        return 0.0

    if not accounts:
        log.warning("CASH DIAGNOSTIC: load_account_profile(dataType='results') returned no accounts")
        return 0.0

    cash_fields = (
        "cash", "cash_available_for_withdrawal", "unsettled_funds",
        "uncleared_deposits", "cash_held_for_orders", "buying_power",
        "portfolio_cash", "sma", "sma_held_for_orders",
    )
    for a in accounts:
        fields_str = " ".join(f"{f}={a.get(f)}" for f in cash_fields)
        log.info(
            f"CASH DIAGNOSTIC account={a.get('account_number')} "
            f"type={a.get('type')} {fields_str}"
        )

    try:
        phoenix = r.load_phoenix_account() or {}
        log.info(
            "CASH DIAGNOSTIC phoenix "
            f"uninvested_cash={phoenix.get('uninvested_cash')} "
            f"withdrawable_cash={phoenix.get('withdrawable_cash')} "
            f"cash_held_for_orders={phoenix.get('cash_held_for_orders')} "
            f"account_buying_power={phoenix.get('account_buying_power')}"
        )
    except Exception as e:
        log.info(f"CASH DIAGNOSTIC phoenix fetch failed: {e}")

    try:
        portfolio_profile = r.load_portfolio_profile() or {}
        log.info(
            "CASH DIAGNOSTIC portfolio_profile "
            f"withdrawable_amount={portfolio_profile.get('withdrawable_amount')} "
            f"excess_margin={portfolio_profile.get('excess_margin')}"
        )
    except Exception as e:
        log.info(f"CASH DIAGNOSTIC portfolio_profile fetch failed: {e}")

    total = round(sum(float(a.get("cash", 0) or 0) for a in accounts), 2)
    log.info(f"get_cash() returning {total} (sum of 'cash' across {len(accounts)} account(s))")
    return total


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


def compute_trim_warnings(recent_orders: list[dict]) -> dict[str, str]:
    """
    Flag symbols sold 2+ times in the last 30 days where the most recent sale
    undercut every prior sale in the window (a capitulation pattern).

    This grounds the REPEATED SELL PATTERN rule in actual order data rather than
    leaving Claude to infer the pattern from prose — the trim count and price
    trail are computed here and injected as an explicit, unmissable warning.

    Uses "latest <= min(all prior)" rather than requiring every consecutive pair
    to be non-increasing — a single mid-sequence uptick (e.g. a brief bounce
    between two otherwise-declining trims) would otherwise silently defeat
    detection of an overall declining/capitulation pattern.
    """
    sells_by_symbol: dict[str, list[dict]] = {}
    for o in recent_orders:
        if o["side"] == "sell" and o["price"] is not None:
            sells_by_symbol.setdefault(o["symbol"], []).append(o)

    warnings = {}
    for symbol, sells in sells_by_symbol.items():
        if len(sells) < 2:
            continue
        chronological = sorted(sells, key=lambda o: o["date"])
        prices = [s["price"] for s in chronological]
        if prices[-1] <= min(prices[:-1]):
            price_trail = " → ".join(f"${p:.2f}" for p in prices)
            warnings[symbol] = (
                f"{symbol} has been sold {len(sells)}x in the last 30 days at "
                f"declining prices ({price_trail}). Only HOLD or FULL EXIT are "
                f"permitted for {symbol} today — no further partial trims."
            )
    return warnings


def load_protected_commitments() -> list[dict]:
    """Load outstanding protected-symbol reinvestment commitments. [] if missing/invalid."""
    try:
        with open(PROTECTED_COMMITMENTS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_protected_commitments(commitments: list[dict]) -> None:
    with open(PROTECTED_COMMITMENTS_FILE, "w") as f:
        json.dump(commitments, f, indent=2)


def resolve_protected_commitments(
    commitments: list[dict], recent_orders: list[dict], held_symbols: set[str], today: str
) -> list[dict]:
    """
    Advance each commitment through pending -> confirmed -> cleared, using only
    real order history — never anything the model says in a later run's analysis.
    This is what makes the PROTECTED SYMBOL REINVESTMENT hard constraint actually
    enforceable instead of an honor-system request repeated each run.

    Pending commitments (a trim Claude's analysis *recommended* but this script
    never executes itself — trades happen manually, later) are only promoted to
    confirmed once a real matching sell order shows up. If nothing matches within
    PROTECTED_COMMITMENT_PENDING_DAYS, the recommendation was evidently never
    acted on and the commitment is dropped rather than permanently blocking
    future trims of that symbol.

    Confirmed commitments are dropped once the symbol is fully exited, or a real
    buy order at/below the committed price lands after the trim.
    """
    still_outstanding = []
    for c in commitments:
        symbol = c["symbol"]
        if symbol not in held_symbols:
            continue  # fully exited — nothing left to enforce a reinvestment into

        if c.get("status", "confirmed") == "pending":
            match = next(
                (
                    o for o in recent_orders
                    if o["symbol"] == symbol
                    and o["side"] == "sell"
                    and o["price"] is not None
                    and o["date"] >= c["recommended_date"]
                    and _dollar_amount_matches(o["quantity"] * o["price"], c["trim_amount"])
                ),
                None,
            )
            if match:
                still_outstanding.append({
                    **c,
                    "status": "confirmed",
                    "trim_amount": round(match["quantity"] * match["price"], 2),
                    "trim_date": match["date"],
                })
            elif _days_between(c["recommended_date"], today) <= PROTECTED_COMMITMENT_PENDING_DAYS:
                still_outstanding.append(c)  # still within the grace window — keep waiting
            else:
                log.info(
                    f"Dropping pending {symbol} commitment recommended on "
                    f"{c['recommended_date']}: no matching trade in "
                    f"{PROTECTED_COMMITMENT_PENDING_DAYS} days — never executed"
                )
            continue

        fulfilled = any(
            o["symbol"] == symbol
            and o["side"] == "buy"
            and o["price"] is not None
            and o["price"] <= c["reinvest_price"]
            and o["date"] > c["trim_date"]
            for o in recent_orders
        )
        if not fulfilled:
            still_outstanding.append(c)
    return still_outstanding


def _days_between(start_iso: str, end_iso: str) -> int:
    return (date.fromisoformat(end_iso) - date.fromisoformat(start_iso)).days


def _dollar_amount_matches(actual: float, expected: float, tolerance: float = 0.4) -> bool:
    """Loose match — the recommended trim $ amount and the eventual manual trade's
    $ amount will differ (share rounding, days-later price movement), so this only
    needs to rule out an unrelated sell, not require an exact figure."""
    return abs(actual - expected) <= max(5.0, expected * tolerance)


def extract_protected_commitment(
    analysis_text: str, protected_held: list[str], today: str
) -> list[dict]:
    """
    Pull any protected-symbol trim RECOMMENDATION + its stated reinvestment
    condition out of today's analysis text, so it can be watched for on future
    runs. This script never places trades itself (see CLAUDE.md's manual,
    human-in-the-loop trading workflow) — the analysis text only says a trim
    was recommended, not that one happened. So these are persisted as "pending"
    and only promoted to an enforced commitment by resolve_protected_commitments
    once a matching real sell order actually appears in order history.
    Returns [] (and logs a warning) on any failure — never blocks the digest.
    """
    prompt = (
        f"Protected symbols: {', '.join(protected_held)}\n\n"
        f"Today's portfolio analysis:\n{analysis_text}\n\n"
        f"Did this analysis recommend a TRIM (partial sell) of any protected symbol "
        f"listed above? For each one, extract the trim dollar amount and the stated "
        f"reinvestment condition (a target price to buy back at or below). "
        f"Respond with ONLY valid JSON — no markdown, no explanation:\n"
        f'{{"trims": [{{"symbol": "SYM", "trim_amount": 0.0, "reinvest_price": 0.0}}]}}\n\n'
        f"Return an empty list if no protected symbol was trimmed today, or if no "
        f"explicit reinvestment price/level was stated."
    )
    try:
        client = Anthropic()
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system="You extract structured trim commitments from prose. Return only valid JSON.",
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:])
            raw = raw.rsplit("```", 1)[0].strip()
        trims = json.loads(raw).get("trims", [])
        commitments = []
        for t in trims:
            if t.get("symbol") not in protected_held or not t.get("reinvest_price") or not t.get("trim_amount"):
                continue
            try:
                commitments.append({
                    "symbol": t["symbol"],
                    "trim_amount": float(t["trim_amount"]),
                    "reinvest_price": float(t["reinvest_price"]),
                    "recommended_date": today,
                    "status": "pending",
                })
            except (TypeError, ValueError):
                log.warning(f"Skipping malformed trim extraction for {t.get('symbol')}: {t}")
        return commitments
    except Exception as e:
        log.warning(f"Protected commitment extraction failed: {e}")
        return []


# ── Ethical screen ───────────────────────────────────────────────────────────
# Symbols are screened once (LLM judgment call) and the verdict is persisted to
# ETHICAL_EXCLUSIONS_FILE, then enforced mechanically on every subsequent run —
# same pattern as protected-symbol commitments above. The user never hand-curates
# the exclusion list; Claude populates it against this fixed rubric.
ETHICAL_SCREEN_CRITERIA = (
    "Exclude a company if it: (1) manufactures weapons or is a defense contractor "
    "(prime or major subcontractor), (2) builds or sells mass-surveillance, policing, "
    "or ICE/immigration-enforcement technology, (3) is primarily a fossil fuel "
    "extraction company (oil, gas, or coal production/exploration), (4) is primarily a "
    "data center builder or operator — data center REITs, colocation/hosting providers, "
    "data center construction/engineering firms, or data center cooling/power "
    "infrastructure specialists (NOT chipmakers, cloud hyperscalers, or general "
    "hardware/software companies whose products merely run inside data centers), "
    "(5) operates private prisons or immigration detention facilities, (6) is a "
    "predatory lender (payday loans, extreme-APR consumer credit), or (7) is primarily "
    "a factory-farming / industrial animal agriculture operator. Tobacco, gambling, "
    "cannabis, alcohol, and other vice industries are NOT grounds for exclusion on "
    "their own."
)


def load_ethical_exclusions() -> dict:
    """{'excluded': {SYM: {reason, added}}, 'screened': [SYM, ...]}. Empty shell if missing/invalid."""
    try:
        with open(ETHICAL_EXCLUSIONS_FILE) as f:
            data = json.load(f)
        data.setdefault("excluded", {})
        data.setdefault("screened", [])
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {"excluded": {}, "screened": []}


def save_ethical_exclusions(state: dict) -> None:
    with open(ETHICAL_EXCLUSIONS_FILE, "w") as f:
        json.dump(state, f, indent=2)


def screen_ethical_exclusions(candidates: set[str], state: dict, today: str) -> dict:
    """
    Screen any candidate symbol not already screened against ETHICAL_SCREEN_CRITERIA.
    Mutates and returns `state` with newly-excluded symbols recorded and all candidates
    marked screened (so passing symbols aren't re-asked about every run). Never blocks
    the run — on failure, leaves the new candidates unscreened so they're retried next run.
    """
    new = sorted(candidates - set(state["screened"]))
    if not new:
        return state

    prompt = (
        f"{ETHICAL_SCREEN_CRITERIA}\n\n"
        f"Screen these ticker symbols against the criteria above: {', '.join(new)}\n\n"
        f"For each symbol that should be EXCLUDED, give a one-sentence reason citing "
        f"which criterion applies. Only exclude when you have reasonable confidence the "
        f"company's actual business fits a criterion — do not exclude on a guess.\n\n"
        f"Respond with ONLY valid JSON — no markdown, no explanation:\n"
        f'{{"excluded": [{{"symbol": "SYM", "reason": "one sentence"}}]}}\n\n'
        f"Return an empty list if none of these symbols should be excluded."
    )
    try:
        client = Anthropic()
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            system="You screen stock tickers against an ethical investment policy. Return only valid JSON.",
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:])
            raw = raw.rsplit("```", 1)[0].strip()
        result = json.loads(raw)
        for item in result.get("excluded", []):
            sym = item.get("symbol", "").upper()
            if sym in new:
                state["excluded"][sym] = {"reason": item["reason"], "added": today}
        state["screened"] = sorted(set(state["screened"]) | set(new))
    except Exception as e:
        log.warning(f"Ethical screen failed, will retry unscreened symbols next run: {e}")
    return state


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
    _, _, _, is_stale = get_market_session()
    change_label = "Last session" if is_stale else "Today"

    momentum_lines = "\n".join(
        f"  {m['symbol']}: RSI {_fmt(m.get('rsi'), '.1f')} | "
        f"{change_label} {_fmt(m.get('pct_change_today'), '+.2f')}% | "
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
            f"{change_label} {_fmt(c.get('pct_change_today'), '+.2f')}% | "
            f"Vol {_fmt(c.get('volume_ratio'), '.2f')}x | Score {c['score']}"
            for c in candidates
        )

    at_minimum = len(screener_tickers) <= MIN_SCREENER_TICKERS
    removal_note = (
        f"IMPORTANT: The list is at its minimum size ({len(screener_tickers)}/{MIN_SCREENER_TICKERS}). "
        f"Removals are blocked unless paired with additions. If you want to remove a ticker, you MUST "
        f"also recommend at least one addition — or skip the removal and only add."
        if at_minimum else
        f"The list has {len(screener_tickers) - MIN_SCREENER_TICKERS} tickers above the minimum; "
        f"removals without paired additions are allowed."
    )

    prior = summary.get("prior_analysis")
    prior_tldr_line = (
        f"Prior analysis TL;DR ({prior['date']}): {prior['tldr']}\n\n"
        if prior and prior.get("tldr") else ""
    )

    stale_note = (
        "NOTE: No new trading session has started yet, so every "
        f'"{change_label}" percentage below is the prior completed session\'s '
        "already-known move, not new intraday action — do not treat it as fresh "
        "momentum on top of what the prior analysis TL;DR above already reported.\n\n"
        if is_stale else ""
    )

    prompt = (
        f"{prior_tldr_line}"
        f"{stale_note}"
        f"Current screener watchlist ({len(screener_tickers)} tickers, max {MAX_SCREENER_TICKERS}):\n"
        f"{', '.join(screener_tickers)}\n\n"
        f"{removal_note}\n\n"
        f"Portfolio positions (do not add these):\n"
        f"{', '.join(portfolio_symbols)}\n\n"
        f"{change_label}'s top momentum movers (from the screener scan):\n"
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
        f"Aim for sector diversity across the watchlist — consider opportunities in energy, "
        f"healthcare/biotech, financials, industrials, and consumer sectors when the news or "
        f"momentum data supports them, not only the sectors already well-represented.\n\n"
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
        f"- Return empty arrays only if truly nothing warrants a change"
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
    current: list[str], changes: dict, excluded: set[str] | None = None
) -> tuple[list[str], list[dict], list[dict]]:
    """
    Apply add/remove changes, write tickers.json, return (new_list, added, removed).
    added/removed are lists of {"ticker": ..., "reason": ...} for what was actually applied.

    `excluded` (ethically-screened symbols) is enforced here regardless of what the
    model proposed — the same "Python-tracked state blocks it" pattern as other hard
    constraints in this file, so a model suggestion can't route around the screen.
    """
    excluded = excluded or set()
    blocked = [
        item["ticker"].upper() for item in changes.get("add", [])
        if item["ticker"].upper() in excluded
    ]
    if blocked:
        log.warning(f"Blocked ethically-excluded ticker(s) from screener add: {blocked}")
    add_items = {
        item["ticker"].upper(): item["reason"]
        for item in changes.get("add", [])
        if item["ticker"].upper() not in excluded
    }
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


def load_last_analysis() -> dict | None:
    """Load the prior run's analysis. Returns None if not available or malformed."""
    try:
        with open(ANALYSIS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_analysis(date: str, tldr: str, analysis: str, summary: dict | None = None):
    """Persist today's Claude analysis so tomorrow's run has continuity."""
    payload: dict = {"date": date, "tldr": tldr, "analysis": analysis}

    if summary:
        payload["portfolio"] = {
            "total_value": summary.get("total_value"),
            "cash": summary.get("cash"),
            "positions": [
                {
                    "symbol": pos["symbol"],
                    "shares": pos.get("shares"),
                    "avg_cost": pos.get("avg_cost"),
                    "current_price": pos.get("current_price"),
                    "equity": pos.get("equity"),
                    "total_return_pct": pos.get("total_return_pct"),
                    **{
                        k: pos["indicators"].get(k)
                        for k in ("rsi", "ma50", "ma200", "price_vs_ma50_pct",
                                  "price_vs_ma200_pct", "volume_ratio", "pct_change_today")
                        if pos.get("indicators")
                    },
                }
                for pos in summary.get("positions", [])
            ],
        }
        payload["momentum"] = [
            {
                k: m.get(k)
                for k in ("symbol", "score", "rsi", "ma50", "ma200",
                          "price_vs_ma50_pct", "volume_ratio", "pct_change_today")
            }
            for m in summary.get("momentum", [])
        ]

    with open(ANALYSIS_FILE, "w") as f:
        json.dump(payload, f, indent=2)


# ── Claude analysis ───────────────────────────────────────────────────────────
def build_prompt(summary: dict) -> str:
    day_name, time_str, session, is_stale = get_market_session()
    change_label = "Last session" if is_stale else "Today"

    lines = [
        f"Portfolio Summary as of {summary['date']} ({day_name}, {time_str} — {session})",
        f"Total Portfolio Value: ${summary['total_value']:.2f}",
        f"Available Cash: ${summary['cash']:.2f}",
    ]
    if is_stale:
        lines.append(
            "NOTE: No new trading session has started yet, so every "
            f'"{change_label}" percentage below is the prior completed session\'s '
            "move — it already happened and was already knowable in the PRIOR RUN "
            "ANALYSIS section (if one appears further down). Do not narrate it as "
            "happening 'this morning' or as an additional day of momentum on top "
            "of what the prior run already reported — check the prior run's date "
            "against today's date before treating a move as new."
        )

    sherwood = summary.get("sherwood_news", [])
    if sherwood:
        lines += ["", "=== TODAY'S MARKET NEWS (Sherwood / Robinhood) ===",
                  "Use these headlines to identify macro themes, sector trends, and rotation "
                  "opportunities. Ask what they imply for positions not yet in this portfolio."]
        for item in sherwood:
            lines.append(f"• {item['title'] if isinstance(item, dict) else item}")

    prior = summary.get("prior_analysis")
    if prior:
        elapsed_note = ""
        try:
            days_elapsed = (
                date.fromisoformat(summary["date"]) - date.fromisoformat(prior["date"])
            ).days
            if days_elapsed <= 0:
                elapsed_note = (
                    f"NOTE: This prior analysis ({prior['date']}) is from EARLIER TODAY, not a "
                    "prior trading day — this run is a same-day rerun (e.g. manual testing), not "
                    "a new trading session. No new day has passed: do not describe any position's "
                    "price action, RSI, or other indicator as new movement since that run — the "
                    "portfolio data below is essentially the same snapshot unless a genuinely new "
                    "headline appears above. Do not repeat recommendations already acted on above "
                    "as if they're fresh.\n"
                )
            else:
                day_word = "day" if days_elapsed == 1 else "days"
                elapsed_note = (
                    f"NOTE: {days_elapsed} calendar {day_word} since the prior run "
                    f"({prior['date']}) — a new trading session has occurred, so today's price "
                    "action and indicators reflect genuinely new movement.\n"
                )
        except (ValueError, TypeError, KeyError):
            pass
        lines += [
            "",
            f"=== PRIOR RUN ANALYSIS ({prior['date']}) ===",
            elapsed_note,
            f"TL;DR: {prior['tldr']}" if prior.get("tldr") else "",
            prior.get("analysis", ""),
        ]

    lines += [
        "",
        "=== CURRENT POSITIONS ===",
    ]

    # Compute these up front (not just at the transactions section further down)
    # so each position can be tagged with its funding eligibility inline. Leaving
    # Claude to eyeball total_return_pct in prose was how a loser (MRVL, -13.2%)
    # got described as "not a loser" and recommended as a funding source — this
    # makes the winner/loser and restricted/unrestricted status an explicit,
    # unmissable field per position instead of an inference the model has to make.
    recent_orders = summary.get("recent_orders", [])
    trim_warnings = compute_trim_warnings(recent_orders)
    recent_buy_symbols = {
        o["symbol"] for o in recent_orders
        if o["side"] == "buy" and o["days_ago"] <= 7
    }
    outstanding_by_symbol = {c["symbol"]: c for c in summary.get("protected_commitments", [])}
    ethical_reason_by_symbol = {h["symbol"]: h["reason"] for h in summary.get("ethical_held", [])}

    total_value = summary["total_value"] or 1  # avoid div/0 if somehow zero
    ticker_news = summary.get("ticker_news", {})
    for pos in summary["positions"]:
        ind = pos.get("indicators", {})
        alloc_pct = pos["equity"] / total_value * 100
        symbol = pos["symbol"]
        is_winner = pos["total_return_pct"] > 0
        if not is_winner:
            funding_tag = "NOT FUNDING-ELIGIBLE — losing position (see COST BASIS DISCIPLINE)"
        elif symbol in trim_warnings:
            funding_tag = "NOT FUNDING-ELIGIBLE — repeated-sell hard stop (see TRIM COUNT WARNINGS)"
        elif symbol in recent_buy_symbols:
            funding_tag = "NOT FUNDING-ELIGIBLE — bought within the last 7 days"
        elif symbol in PROTECTED_SYMBOLS:
            if symbol in outstanding_by_symbol:
                c = outstanding_by_symbol[symbol]
                funding_tag = (
                    f"FUNDING BLOCKED — protected symbol has an unresolved reinvestment "
                    f"commitment from {c['trim_date']} (see OUTSTANDING REINVESTMENT COMMITMENTS)"
                )
            else:
                cap = pos["equity"] * PROTECTED_TRIM_MAX_PCT
                funding_tag = (
                    f"LIMITED FUNDING SOURCE — protected symbol, max ${cap:.2f} "
                    f"({PROTECTED_TRIM_MAX_PCT:.0%} of equity) per trim, reinvestment plan required"
                )
        else:
            funding_tag = "FUNDING-ELIGIBLE — winner, unrestricted"
        small_tag = (
            f"  [SMALL POSITION — equity ${pos['equity']:.2f}, evaluate for cleanup per "
            f"SMALL POSITION CLEANUP rule]"
            if pos["equity"] <= SMALL_POSITION_THRESHOLD else ""
        )
        ethical_tag = (
            f"  [ETHICAL SCREEN: {ethical_reason_by_symbol[symbol]}]"
            if symbol in ethical_reason_by_symbol else ""
        )
        lines += [
            f"\n{symbol}: {pos['shares']} shares @ ${pos['current_price']} "
            f"(avg cost ${pos['avg_cost']}, return {pos['total_return_pct']:+.1f}%, "
            f"equity ${pos['equity']}, {alloc_pct:.1f}% of portfolio)  [{funding_tag}]{small_tag}{ethical_tag}",
            f"  RSI: {_fmt(ind.get('rsi'), '.1f')} | "
            f"MA50: ${_fmt(ind.get('ma50'), '.2f')} "
            f"({_fmt(ind.get('price_vs_ma50_pct'), '+.1f')}%) | "
            f"MA200: ${_fmt(ind.get('ma200'), '.2f')} "
            f"({_fmt(ind.get('price_vs_ma200_pct'), '+.1f')}%)",
            f"  {change_label}: {_fmt(ind.get('pct_change_today'), '+.2f')}% | "
            f"Volume ratio: {_fmt(ind.get('volume_ratio'), '.2f')}x",
        ]
        sym_news = ticker_news.get(symbol, [])
        if sym_news:
            for item in sym_news:
                title = item["title"] if isinstance(item, dict) else item
                lines.append(f"  News: {title}")

    eligible = sorted(
        (p for p in summary["positions"]
         if p["total_return_pct"] > 0
         and p["symbol"] not in trim_warnings
         and p["symbol"] not in recent_buy_symbols
         and p["symbol"] not in PROTECTED_SYMBOLS),
        key=lambda p: p["total_return_pct"],
        reverse=True,
    )
    lines += ["", "=== ELIGIBLE FUNDING SOURCES (winners, unrestricted, best first) ==="]
    if eligible:
        for p in eligible:
            lines.append(
                f"  {p['symbol']:<8} return {p['total_return_pct']:+.1f}%  "
                f"equity ${p['equity']:.2f}"
            )
    else:
        lines.append("  None — every position is currently either a loser or restricted.")

    protected_held = [
        p for p in summary["positions"]
        if p["symbol"] in PROTECTED_SYMBOLS
        and p["total_return_pct"] > 0
        and p["symbol"] not in trim_warnings
        and p["symbol"] not in recent_buy_symbols
        and p["symbol"] not in outstanding_by_symbol
    ]
    if protected_held:
        lines += ["", "=== PROTECTED SYMBOLS (limited funding sources) ==="]
        for p in protected_held:
            cap = p["equity"] * PROTECTED_TRIM_MAX_PCT
            lines.append(
                f"  {p['symbol']:<8} return {p['total_return_pct']:+.1f}%  "
                f"equity ${p['equity']:.2f}  max trim this action: ${cap:.2f} "
                f"({PROTECTED_TRIM_MAX_PCT:.0%})"
            )

    outstanding = summary.get("protected_commitments", [])
    if outstanding:
        lines += ["", "=== OUTSTANDING REINVESTMENT COMMITMENTS ==="]
        price_by_symbol = {p["symbol"]: p["current_price"] for p in summary["positions"]}
        for c in outstanding:
            current_price = price_by_symbol.get(c["symbol"])
            met = (
                "condition MET — reinvest now"
                if current_price is not None and current_price <= c["reinvest_price"]
                else "condition not yet met"
            )
            lines.append(
                f"  {c['symbol']:<8} trimmed ${c['trim_amount']:.2f} on {c['trim_date']}, "
                f"reinvest at/below ${c['reinvest_price']:.2f} "
                f"(current ${_fmt(current_price, '.2f')}) — {met}"
            )

    if recent_orders:
        lines += ["", "=== RECENT TRANSACTIONS (last 30 days) ==="]
        for o in recent_orders:
            flag = "  ← RECENT BUY — do not sell without strong justification" if (
                o["side"] == "buy" and o["days_ago"] <= 7
            ) else ""
            price_str = f"${o['price']:.4f}" if o["price"] is not None else "N/A"
            lines.append(
                f"  {o['side'].upper():<4} {o['symbol']:<8} "
                f"{o['quantity']:.4f} shares @ {price_str}  "
                f"{o['date']} ({o['days_ago']}d ago){flag}"
            )

    if trim_warnings:
        lines += ["", "=== TRIM COUNT WARNINGS ==="]
        for warning in trim_warnings.values():
            lines.append(f"⚠ {warning}")

    ethical_held = summary.get("ethical_held", [])
    if ethical_held:
        lines += ["", "=== ETHICAL SCREEN — HELD POSITIONS FLAGGED ==="]
        for h in ethical_held:
            lines.append(
                f"⚠ {h['symbol']}: {h['reason']} — recommend a full exit and state this "
                f"reason explicitly (see ETHICAL INVESTMENT SCREEN)."
            )

    lines += ["", "=== TOP MOMENTUM MOVERS (not in portfolio) ==="]
    for m in summary["momentum"]:
        lines.append(
            f"\n{m['symbol']}: RSI {_fmt(m.get('rsi'), '.1f')} | "
            f"{change_label} {_fmt(m.get('pct_change_today'), '+.2f')}% | "
            f"Vol ratio {_fmt(m.get('volume_ratio'), '.2f')}x | "
            f"Score {m['score']}"
        )
    return "\n".join(lines)


def get_claude_analysis(summary: dict) -> tuple[str, str]:
    """Return (tldr, analysis) parsed from Claude's structured response."""
    client = Anthropic()
    prompt = build_prompt(summary)
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=CLAUDE_MAX_TOKENS,
        thinking={"type": "disabled"},
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

    # Strip model formatting artifacts that Claude sometimes echoes but we don't want
    # rendered — e.g. a stray "PART 1"/"TL;DR:" label, whether it's glued onto the
    # start of the real content on the same line ("PART 1 — TL;DR: Chips are...") or
    # sitting alone on its own line ("PART 2\n\nTRIMS/EXITS..."). Only strip when the
    # matched prefix actually contains one of these labels, so legitimate leading
    # markdown (e.g. "**TRIM ARM**") is never touched.
    _FILLER = r"[\s#*\-—:]*"
    _label_re = re.compile(
        rf"^{_FILLER}(?:PART\s+[12]\b{_FILLER})?(?:TL;DR{_FILLER})?",
        re.IGNORECASE,
    )

    def _strip_label(text: str) -> str:
        m = _label_re.match(text)
        if m and re.search(r"PART\s+[12]\b|TL;DR", m.group(0), re.IGNORECASE):
            return text[m.end():].lstrip()
        return text

    # TL;DR: strip any leading heading line, then a leading "PART 1"/"TL;DR:" label.
    tldr = re.sub(r"^[#\s]*#[^\n]+\n+", "", tldr).strip()  # leading # heading
    tldr = _strip_label(tldr).strip()

    # Analysis: strip leading separator lines, a "PART 2" label, and TRIM SIGNALS
    # block. Iterate because they can appear in any order.
    analysis_part = analysis_part.strip()
    for _ in range(4):
        analysis_part = re.sub(r"^---[ \t]*\n+", "", analysis_part).strip()
        analysis_part = _strip_label(analysis_part).strip()
    analysis_part = re.sub(
        r"^(?:[#*\s]*)TRIM SIGNALS\s*[-—].*?(?=\n\n|\Z)",
        "",
        analysis_part,
        flags=re.IGNORECASE | re.DOTALL,
    ).strip()
    analysis_part = re.sub(r"^---[ \t]*\n+", "", analysis_part).strip()

    return tldr, analysis_part


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


def _pipe_table_to_html(block: str) -> str:
    """Convert a pre-escaped GFM pipe table block to an HTML table.
    _h() must have already run on the text before this is called."""
    rows = [r.strip() for r in block.strip().splitlines()]
    if len(rows) < 2:
        return block
    def parse_row(r):
        return [c.strip() for c in r.strip("|").split("|")]
    headers = parse_row(rows[0])
    html = (
        '<table width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;width:100%;margin:12px 0;">'
        "<thead><tr>"
    )
    for h in headers:
        html += (
            f'<th style="padding:6px 10px;text-align:left;font-size:11px;'
            f'font-weight:600;color:#94a3b8;text-transform:uppercase;'
            f'letter-spacing:0.05em;border-bottom:2px solid #e2e8f0;">{h}</th>'
        )
    html += "</tr></thead><tbody>"
    for row in rows[2:]:  # skip separator line
        if not row.strip("|").strip():
            continue
        cells = parse_row(row)
        html += "<tr>"
        for cell in cells:
            html += (
                f'<td style="padding:6px 10px;border-bottom:1px solid #f1f5f9;'
                f'font-size:13px;">{cell}</td>'
            )
        html += "</tr>"
    html += "</tbody></table>"
    return html


def _md_to_html(text: str) -> str:
    """Convert the subset of markdown Claude uses in analysis to HTML."""
    # HTML-escape first so _pipe_table_to_html doesn't get its output re-escaped
    text = _h(text)
    # Convert pipe tables (content already escaped by _h above)
    text = re.sub(
        r"(^\|.+\|[ \t]*$\n^\|[-| :]+\|[ \t]*$(?:\n^\|.+\|[ \t]*$)*)",
        lambda m: _pipe_table_to_html(m.group(0)),
        text,
        flags=re.MULTILINE,
    )
    # **bold** → <strong>
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    # # heading lines (H1) — single hash only
    text = re.sub(
        r"^#(?!#)\s+(.+)$",
        r'<div style="font-size:16px;font-weight:700;color:#0f172a;margin:24px 0 10px;">\1</div>',
        text,
        flags=re.MULTILINE,
    )
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
    git_pull()
    resolve_sync_paths()
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
        if "logged in" in str(e).lower():
            subject = f"Portfolio Monitor — Re-authentication Required ({hostname}) - {date.today()}"
            body = (
                f"The script could not run on {hostname} because the Robinhood session has expired.\n\n"
                f"Error: {e}\n\n"
                f"To fix, run reauth.py on {hostname}:\n"
                f"  cd ~/Claude/robinhood-monitor && .venv/bin/python reauth.py\n\n"
                f"It will prompt you to run this in the Robinhood browser console:\n"
                f"  JSON.stringify(JSON.parse(localStorage.getItem('web:auth_state')))\n\n"
                f"Paste the output into the terminal and the session will be restored."
            )
            try:
                send_email(subject, body)
            except Exception as mail_err:
                log.error(f"Failed to send login error email: {mail_err}")
        else:
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

    # Auto-remove screener tickers that returned no data (likely delisted/halted).
    # Bypass Claude for these — there's nothing to analyse. The MIN_SCREENER_TICKERS
    # floor deliberately does NOT apply here: it exists to protect a pool of
    # scannable candidates, and a delisted symbol was never contributing to that
    # pool in the first place (fetch_bulk_market_data can never return data for
    # it) — keeping it around just to satisfy the count is pure dead weight, not
    # a real candidate. If this drops the list below the floor, that's a signal
    # for the ticker-recommendation step below to prioritize backfilling it, not
    # a reason to leave a permanently-broken ticker stuck in the watchlist.
    no_data_tickers = [
        t for t in screener_tickers
        if t not in portfolio_symbols and t not in screener_data
    ]
    if no_data_tickers:
        auto_removed = []
        for t in no_data_tickers:
            screener_tickers.remove(t)
            auto_removed.append(t)
            log.warning(f"Auto-removed {t} from screener: no market data returned (possible delisting)")
        if len(screener_tickers) < MIN_SCREENER_TICKERS:
            log.warning(
                f"Screener list now below minimum ({len(screener_tickers)}/{MIN_SCREENER_TICKERS}) "
                f"after removing delisted ticker(s) — ticker recommendations below should backfill it"
            )
        if auto_removed:
            with open(TICKERS_FILE, "w") as _f:
                json.dump(sorted(screener_tickers), _f, indent=2)
            log.info(f"Wrote updated tickers.json after auto-removing: {auto_removed}")

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

    # 5b. Ethical screen — screen any new candidate symbol, then mechanically strip
    # anything already known to violate the policy before it can reach the momentum
    # scan, the ticker-recommendation prompt, or the analysis prompt.
    ethical_state = load_ethical_exclusions()
    try:
        candidates = set(screener_tickers) | {c["symbol"] for c in watchlist_candidates} | portfolio_symbols
        ethical_state = screen_ethical_exclusions(candidates, ethical_state, today)
        save_ethical_exclusions(ethical_state)
    except Exception as e:
        log.warning(f"Ethical screen failed: {e}")
    excluded_syms = set(ethical_state["excluded"].keys())

    ethically_removed = [t for t in screener_tickers if t in excluded_syms]
    if ethically_removed:
        screener_tickers = [t for t in screener_tickers if t not in excluded_syms]
        with open(TICKERS_FILE, "w") as _f:
            json.dump(sorted(screener_tickers), _f, indent=2)
        log.warning(f"Removed ethically-excluded ticker(s) from screener: {ethically_removed}")

    watchlist_candidates = [c for c in watchlist_candidates if c["symbol"] not in excluded_syms]

    ethical_held = [
        {"symbol": s, "reason": ethical_state["excluded"][s]["reason"]}
        for s in sorted(portfolio_symbols)
        if s in excluded_syms
    ]
    if ethical_held:
        log.warning(f"Currently-held position(s) flagged by ethical screen: {[h['symbol'] for h in ethical_held]}")

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
    prior_analysis = load_last_analysis()
    if prior_analysis:
        log.info(f"Loaded prior analysis from {prior_analysis['date']}")
    summary = {
        "date": today,
        "hostname": hostname,
        "total_value": total_value,
        "cash": cash,
        "positions": positions,
        "momentum": momentum,
        "watchlist_candidates": watchlist_candidates,
        "recent_orders": recent_orders,
        "prior_analysis": prior_analysis,
        "ethical_held": ethical_held,
    }

    # 7b. Resolve protected-symbol reinvestment commitments against real order history
    all_commitments: list[dict] = []
    try:
        all_commitments = resolve_protected_commitments(
            load_protected_commitments(), recent_orders, portfolio_symbols, today
        )
        save_protected_commitments(all_commitments)
        # Only confirmed commitments (a real matching trade was found) are enforced
        # in the prompt — pending ones are still waiting to see if the recommended
        # trim actually gets executed, and shouldn't block funding decisions yet.
        summary["protected_commitments"] = [
            c for c in all_commitments if c.get("status", "confirmed") == "confirmed"
        ]
        if summary["protected_commitments"]:
            log.info(
                f"Outstanding protected-symbol commitments: "
                f"{[c['symbol'] for c in summary['protected_commitments']]}"
            )
        pending = [c for c in all_commitments if c.get("status") == "pending"]
        if pending:
            log.info(f"Pending (unconfirmed) protected-symbol commitments: {[c['symbol'] for c in pending]}")
    except Exception as e:
        log.warning(f"Protected commitment resolution failed: {e}")
        summary["protected_commitments"] = []

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
        _, added, removed = apply_ticker_changes(screener_tickers, changes, excluded=excluded_syms)
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
    analysis_ok = False
    try:
        log.info("Requesting Claude analysis")
        tldr, analysis = get_claude_analysis(summary)
        summary["tldr"] = tldr
        analysis_ok = True
        try:
            save_analysis(today, tldr, analysis, summary)
            log.info(f"Analysis saved to {ANALYSIS_FILE}")
        except Exception as save_err:
            log.warning(f"Failed to save analysis: {save_err}")
    except Exception as e:
        log.error(f"Claude analysis failed: {e}")
        send_error_email("Claude API analysis", e)
        tldr = ""
        analysis = f"[Claude analysis unavailable: {e}]"
        summary["tldr"] = ""

    # 10b. Extract & persist any new protected-symbol reinvestment commitment.
    # These start "pending" — see extract_protected_commitment — and only become
    # enforced once a matching real trade is found by resolve_protected_commitments
    # on a later run.
    protected_held_syms = sorted(PROTECTED_SYMBOLS & portfolio_symbols)
    if analysis_ok and protected_held_syms:
        try:
            already_tracked = {c["symbol"] for c in all_commitments}
            new_commitments = [
                c for c in extract_protected_commitment(analysis, protected_held_syms, today)
                if c["symbol"] not in already_tracked
            ]
            if new_commitments:
                save_protected_commitments(all_commitments + new_commitments)
                log.info(
                    f"New pending protected-symbol commitments recorded: "
                    f"{[c['symbol'] for c in new_commitments]}"
                )
        except Exception as e:
            log.warning(f"Protected commitment extraction failed: {e}")

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

    # 12. Commit and push tickers.json if Claude updated the watchlist, and
    # ethical_exclusions.json if any symbol was newly screened. last_analysis.json
    # and protected_commitments.json are synced separately via Dropbox (see
    # resolve_sync_paths) rather than git, since this repo is public and both
    # carry real dollar figures from the user's account.
    git_commit_tickers()
    git_commit_ethical_exclusions()

    log.info("Portfolio monitor complete")


if __name__ == "__main__":
    main()
