"""
portfolio_monitor.py — Daily Robinhood portfolio digest with Claude AI analysis.

Usage:
    python portfolio_monitor.py
"""

import base64
import email
import imaplib
import json
import os
import re
import socket
import statistics
import subprocess
import sys
import logging
import smtplib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import feedparser
from html.parser import HTMLParser

import pandas as pd
import pytz
import requests
import yfinance as yf
import robin_stocks.robinhood as r
from anthropic import Anthropic
from dotenv import load_dotenv

import newsletter_views
import portfolio_risk

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
# Never repointed by resolve_sync_paths(). save_analysis() writes here as well as to
# the Dropbox copy, and load_last_analysis() falls back to it, because a Dropbox path
# can read back as a 0-byte placeholder even on a run that wrote it successfully.
ANALYSIS_MIRROR_FILE = "last_analysis.json"
MIN_SCREENER_TICKERS = 25
MAX_SCREENER_TICKERS = 40
WATCHLIST_MIN_SCORE = 15  # minimum momentum score to surface a watchlist ticker as a candidate
PROTECTED_SYMBOLS = {"COST"}  # core long-term holdings; see PROTECTED SYMBOL rule in CLAUDE_SYSTEM_PROMPT
PROTECTED_TRIM_MAX_PCT = 0.10  # max fraction of a protected symbol's OWN equity trimmable per action
PROTECTED_COMMITMENT_PENDING_DAYS = 7  # days to wait for a recommended trim to actually execute before dropping it
# A CONFIRMED commitment used to live until a buy at/below its target price landed. A core holding
# that keeps rising (COST: trimmed at $938, back at $1,009 three days later) never comes back to the
# target, so the trim lock was permanent. Two more ways out: restore the position at any price, or
# let it age out.
PROTECTED_COMMITMENT_MAX_DAYS = 14
PROTECTED_COMMITMENT_RESTORE_FRACTION = 0.6  # share of the trimmed $ that must be bought back (loose, like the sell match)
PROTECTED_COMMITMENTS_FILE = "protected_commitments.json"
SMALL_POSITION_THRESHOLD = 10  # equity ($) at/under which a position is a cleanup candidate
ETHICAL_EXCLUSIONS_FILE = "ethical_exclusions.json"
# Continuity tracking (see the "Continuity" section further down): how long a
# "ONE KEY THING TO WATCH" item is carried forward and forcibly re-reported, how
# many can be open at once, and how many sessions of breadth history are kept.
WATCH_ITEM_MAX_AGE_DAYS = 5
WATCH_ITEM_MIN_RESOLVE_AGE_DAYS = 3  # a RESOLVED verdict on a younger item is ignored (one bounce isn't an answer)
WATCH_ITEM_MAX_OPEN = 3
WATCH_ITEM_MAX_SYMBOLS = 8  # a sector-wide watch legitimately names most of a sector
BREADTH_HISTORY_LEN = 5

# ANALYSIS_FILE and PROTECTED_COMMITMENTS_FILE carry real dollar figures from
# the user's account, so — unlike tickers.json/ethical_exclusions.json — they
# stay out of git (this repo is public) and instead get repointed at a
# Dropbox-synced directory by resolve_sync_paths(), since this script runs
# from more than one machine and both need to see the same current state.
SYNC_DIR = os.path.expanduser("~/Dropbox/robinhood-monitor")

# ── Config ────────────────────────────────────────────────────────────────────
CLAUDE_MODEL = "claude-sonnet-5"
CLAUDE_MAX_TOKENS = 5000
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
    "NEWS SOURCE TRUST — the market news section at the top of the prompt is one of two kinds, "
    "and it says which:\n"
    "  (1) SNACKS (Robinhood/Sherwood's own daily newsletter) — the PREFERRED source. This is "
    "editorial analysis from the brokerage this portfolio is held at, not a wire summary: it "
    "carries a written takeaway, a chart of the day, and a next-session earnings calendar. Treat "
    "its interpretation as the primary macro read and lean on it over your own framing. When it "
    "names a reason the market moved, cite that reason. Quote or paraphrase its takeaway where it "
    "bears on a position.\n"
    "  (2) A FALLBACK WIRE FEED (CNBC, Yahoo Finance, MarketWatch) — used only when no recent "
    "Snacks issue was available. These are raw headlines with no analysis attached, from outlets "
    "this portfolio's owner explicitly trusts LESS. Use them for facts (what happened, to whom, "
    "when) and do NOT treat a headline's framing or implied conclusion as analysis. Do not build "
    "a confident macro thesis on them; say the macro read is thin when that is all you have.\n"
    "Either way, do not invent a macro narrative that the section does not support.\n\n"
    "ANALYST VIEWS — HARD CONSTRAINT: When an '=== ANALYST VIEWS ===' section appears, it is the "
    "newsletters' analytical content — Sherwood's own read, strategists and firms the issues "
    "quote, and Robinhood retail-flow data — and the account owner wants this feedback, not just "
    "a list of tickers that were mentioned. Open the analysis (after SINCE LAST SESSION, before "
    "OPPORTUNITY COST CHECK) with an **ANALYST VIEWS** block of at most five lines: pick the views "
    "that bear most on what is held, on a CONCENTRATED cluster, or on the market's direction, "
    "plus each issue's overall read. Each line states who said it, what, then AGREE / DISAGREE / "
    "PARTLY measured against the data in THIS prompt (a specific price, RSI, MA50, breadth or "
    "cluster figure), then 'changes: <the specific action>' or 'changes: nothing'. Disagreeing "
    "with a view is fine; ignoring one that bears on a position you act on is not. Weight: "
    "Sherwood's editorial read counts most; a third-party analyst an issue quotes is second-hand "
    "opinion — attribute it by name and never present it as Sherwood's own; retail flow is what "
    "customers are DOING, a behavioural datapoint, not evidence they are right. Keep their "
    "hedging: do not turn a 'may' or 'could' into a 'will'. Never cite a view that is not in the "
    "section. Any EVENT on a [HELD] symbol must be weighed in that position's reasoning if you "
    "trim, exit, buy or hold it with conviction — an equity raise, guidance change or deal is "
    "exactly the sort of fact a technical-only rationale misses.\n\n"
    "CATALYSTS — HARD CONSTRAINT: A held position tagged [REPORTS EARNINGS …] (and the "
    "'=== THIS WEEK'S CATALYSTS ===' section) reports within days. Any BUY, add or trim of that "
    "symbol must acknowledge the event in its reasoning — earnings are binary risk and a buy "
    "into one is a different decision from a buy on a quiet week. This does not forbid acting; "
    "it forbids acting as if the event were not there. Name the macro releases in the same "
    "section (a Fed decision, PCE) when they bear on the tape you describe.\n\n"
    "NEWS INTEGRATION: The market news section appears at the top of the "
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
    "PENDING ORDERS — HARD CONSTRAINT: If a '=== PENDING ORDERS (placed, NOT yet filled) ===' "
    "section appears below, those orders are already committed and simply have not executed yet "
    "(an order placed over a weekend or after hours stays queued until the next session opens). "
    "A pending order holds no shares and is not a completed transaction, so its symbol will be "
    "absent from CURRENT POSITIONS and from RECENT TRANSACTIONS — that absence is NOT evidence "
    "the symbol is unowned. For any symbol with a pending BUY you are FORBIDDEN from "
    "recommending a buy or describing it as an unowned opportunity, a gap in the portfolio, or "
    "missing exposure; it is already bought. For any symbol with a pending SELL, do not "
    "recommend selling or trimming it again and do not count its proceeds as available capital — "
    "that sale is already in flight. Cash committed to unfilled buy orders is likewise NOT "
    "spendable: use only the 'Available Cash' figure at the top of the prompt, which is already "
    "net of those commitments, and never the larger gross total shown beside it.\n\n"
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
    "a genuinely partial amount or recommend a FULL EXIT and call it that explicitly. There is no "
    "minimum position size for a trim: this is a ~$1000 account where a $50 position is a "
    "meaningful holding, not a rounding error, and fractional shares make a $5-25 trim "
    "perfectly executable. Never treat a position as 'too small to trim' — apply the ratio "
    "above and the other trim rules, nothing else. In your one-sentence reasoning, state the position's "
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
    "analysis, so restating the plan again does not clear it — only real trades do: a buy at or "
    "below the target, a buy of roughly the trimmed amount at ANY price, or the commitment aging "
    "out. The lock covers TRIMS only. It never forbids BUYING the symbol: when the price is above "
    "the target and the position is strong, adding to it is a normal buy judged by the usual entry "
    "standard (no chasing), and it also clears the commitment. Do not describe the symbol as "
    "'locked' or 'frozen' for buying purposes, and do not write a TRIMS/EXITS line about it — "
    "a blocked trim is just a hold. The only allowed override of the trim ban is a FULL EXIT for a "
    "specifically broken thesis (adverse news, structural change), named explicitly. If the "
    "current price shown alongside the commitment has already reached the committed level, "
    "prioritize recommending that reinvestment buy this session over other new entries.\n\n"
    "SMALL POSITION CLEANUP RULE: Positions tagged [SMALL POSITION] default to a full-exit "
    "candidate rather than an automatic hold. Recommend a full exit when the position is stale — "
    "no momentum signal (RSI outside the 55-75 momentum zone, price flat against MA50, volume "
    "ratio near 1x, little movement today), no supportive recent news, and not bought in the last "
    "30 days. Keep it only with a specific stated reason — a live catalyst, a still-developing "
    "recent buy, an active momentum signal, or supportive news — named explicitly in its HOLDS "
    "line; if you cannot name one, it is an exit and goes in TRIMS/EXITS instead. This "
    "is an explicit exception to COST BASIS DISCIPLINE: a small stale position may be closed even "
    "at a loss, citing 'SMALL POSITION CLEANUP' instead of a broken-thesis reason, because the "
    "action is portfolio hygiene, not funding a new buy. BEFORE writing TRIMS/EXITS, check every "
    "[SMALL POSITION] tag against this rule — TRIMS/EXITS is written ahead of HOLDS, so a cleanup "
    "exit decided while walking the HOLDS list is one TRIMS/EXITS has already denied exists. An "
    "exit under this rule is a normal exit: it belongs in TRIMS/EXITS as a bold action line "
    "(e.g. **EXIT SOLV — Sell all ($7.15)**) with its 'SMALL POSITION CLEANUP' reason, and "
    "nowhere else. Never invent a separate block or header for it.\n\n"
    "TRIM SIGNALS — consider a partial trim (even without a broken thesis) when a position is "
    "20%+ above its MA50 — the same threshold the momentum scanner itself stops treating as "
    "healthy and starts treating as stretched (see momentum_score) — a single position exceeds "
    "35% of portfolio, or a high-momentum opportunity exists that the portfolio doesn't yet "
    "capture. "
    "A partial trim means taking a fraction of the position off the table, not exiting — size it "
    "per POSITION-SIZE-AWARE TRIM SIZING (at most ~50% of that position's equity), not as a fixed "
    "dollar figure. Riding a winner and taking partial profits are not mutually exclusive.\n\n"
    "GAIN GIVEBACK — HARD CONSTRAINT: If a '=== GAIN GIVEBACK ALERTS ===' section appears, "
    "each position listed peaked well above its cost and has since surrendered a third or "
    "more of that open gain while still showing a profit — the specific way a good position "
    "becomes a round trip. The figures are computed in Python from price history since the "
    "position was opened; they are measurements, not an impression. In a 1-year backtest of "
    "this portfolio, exiting at this threshold modestly beat holding and cut the worst "
    "drawdown by about a third, and the price was lower 40 sessions after the exit in roughly "
    "four cases out of five. So the default for an alerted position is to SELL, and a "
    "decision to hold has to beat that base rate. In TRIMS/EXITS, for every alerted position, "
    "either (1) recommend a full exit (the backtested rule) or a trim sized per "
    "POSITION-SIZE-AWARE TRIM SIZING, or (2) name a specific, checkable reason to hold: a "
    "dated catalyst within days, or a named support level, with its price, that has been "
    "holding. 'Thesis intact', 'sector-wide pullback', 'no negative news' and 'down with "
    "everything else' are NOT reasons — they explain why the price is falling, not why it "
    "will stop, and a giveback inside a sector-wide pullback is still a giveback. State any "
    "hold-despite-alert inside TRIMS/EXITS, even when no trim is recommended, never only in "
    "HOLDS. The other rules still bind: positions bought < 7 days ago, protected symbols (10% "
    "cap plus reinvestment condition), and TRIM COUNT WARNINGS. A position whose 'Gain "
    "protection' line says the entire open gain has been given back is informational only — "
    "the alert does not apply below cost, and COST BASIS DISCIPLINE governs it.\n\n"
    "EXITS FUND BUYS: The proceeds of a giveback exit or trim are not meant to sit in cash. "
    "Redeploy them this same session: put the buy they fund in BUYS and state the pairing. "
    "The target must pass the same entry standard as any other buy (no chasing — see BUYS). "
    "The default destination is outside any cluster flagged CONCENTRATED in PORTFOLIO RISK "
    "CONTEXT: selling one holding in a cluster to buy another in the same cluster swaps an "
    "exposure for the same exposure, and it does nothing about the thing the exit was meant to "
    "protect against. Buying inside a flagged cluster needs a specific, named reason stronger "
    "than 'it is at support'. Cash may be held only when no candidate clears the entry standard; then "
    "say so plainly and name the candidate you rejected and why. That exception is for a thin "
    "field of candidates, not a way to avoid the decision.\n\n"
    "PORTFOLIO RISK CONTEXT: If a '=== PORTFOLIO RISK CONTEXT ===' section appears, reason "
    "from it instead of from impressions. (1) Before attributing a drop to a 'sector-wide "
    "pullback' or a 'broad risk-off day', check the benchmark figures: if the portfolio fell "
    "more than the benchmark it is most exposed to, say the holdings underperformed it and do "
    "not file that under sector drag. Quote the 'Portfolio minus ...' and 'moves most like ...' "
    "lines as given — the differences and their direction are already computed, so do not "
    "re-derive or re-sign them. (2) If a cluster is flagged CONCENTRATED, state it once "
    "in the analysis with its share of equity and say plainly whether it changes any "
    "recommendation — a cluster below its recent high while the benchmark that tracks it is "
    "weak is one exposure falling, not several independent holds. If nothing about it changed "
    "since the PRIOR RUN ANALYSIS, say so in a clause rather than re-describing it. (3) These "
    "figures describe exposure; concentration alone is not a sell trigger. GAIN GIVEBACK is "
    "the sell trigger.\n\n"
    "OPPORTUNITY COST CHECK — HARD CONSTRAINT: Before writing TRIMS/EXITS, explicitly answer "
    "one question: is there a better use of capital right now than what is already held? Name "
    "the single strongest candidate not already owned or pending — the top-scoring symbol from "
    "the momentum scan or watchlist candidates below, whichever scores higher — with its score "
    "and RSI. Then name the funding candidate: if any GAIN GIVEBACK ALERT is open, it is the alerted "
    "position with the largest giveback — that sale is already warranted on its own, so the only "
    "question is what its proceeds buy. Otherwise name the single strongest funding candidate among "
    "ELIGIBLE FUNDING SOURCES: "
    "the position with the largest total_return_pct or the largest price_vs_ma50_pct extension. "
    "State plainly whether the candidate clears the bar to justify trimming that funding source. "
    "'Yes' must lead directly into a TRIMS/EXITS + BUYS pair naming both. 'No' requires a "
    "specific, falsifiable reason — score too weak, momentum cooling versus the prior run, no "
    "supportive catalyst, sector already represented in the portfolio — not 'nothing is broken,' "
    "since this check is about opportunity, not damage control; TRIM SIGNALS, COST BASIS "
    "DISCIPLINE, and the other trim rules already govern whether a specific trim is allowed, so "
    "this check answers a different question and must not be skipped just because a candidate is "
    "not compelling. If no momentum or watchlist candidate scores above roughly 15, say so "
    "explicitly and treat that as this session's actual reason to ride it out — a weak field is "
    "an answer, not a skipped check.\n\n"
    "WATCH ITEM FOLLOW-THROUGH — HARD CONSTRAINT: If an '=== OPEN WATCH ITEMS ===' section "
    "appears in the data below, each numbered item is something a prior run flagged as the one "
    "thing to watch, and the script has been tracking it ever since. Every item comes with "
    "then→now figures computed in Python from real market data — that is the record of what "
    "actually happened, so never say the outcome is unknown, and never substitute your own "
    "impression for those numbers. Open the analysis with a **SINCE LAST SESSION** block "
    "containing exactly one line per open item, in the order given. Each line must (1) name "
    "what was being watched, (2) cite the supplied then→now numbers for at least one of its "
    "symbols, and (3) end with exactly one verdict in caps: RESOLVED, STILL OPEN, or ESCALATED. "
    "RESOLVED means the question has an answer — say which way it went. ESCALATED means it is "
    "now actionable, and requires a matching action line in TRIMS/EXITS or BUYS this session; "
    "without that action line you may not call it escalated. STILL OPEN is only honest while "
    "the data is genuinely ambiguous: if an item has been open for multiple sessions and the "
    "tracked numbers have moved consistently in one direction over that span, that IS the "
    "answer — mark it RESOLVED or ESCALATED and say what the data decided. Repeating 'too early "
    "to tell' for days about numbers that have been trending the whole time is the specific "
    "failure this rule exists to prevent. Never silently drop an item.\n"
    "The verdicts are load-bearing: the script stops tracking an item you mark RESOLVED or "
    "ESCALATED, and brings a STILL OPEN one back on the next run with updated numbers. So do "
    "not mark something resolved to stop hearing about it — if the data is genuinely still "
    "ambiguous, STILL OPEN is the honest answer and seeing it again tomorrow with another "
    "session of movement attached is the point. One session's reversal does not answer a "
    "multi-session trend: a single bounce after days of slippage is noise until it holds. The "
    "script enforces this — a RESOLVED verdict on an item younger than "
    f"{WATCH_ITEM_MIN_RESOLVE_AGE_DAYS} days is ignored and the item stays open — so for a young "
    "item use STILL OPEN and say what the new session did.\n\n"
    "SESSION-OVER-SESSION CONTINUITY — HARD CONSTRAINT: The '=== MARKET BREADTH ===' section "
    "below is computed in Python from the position list over the last several sessions, and the "
    "PRIOR RUN ANALYSIS section is what was said last time. Read both before characterizing "
    "today's market mood, and describe the mood relative to them rather than as if today were "
    "the first day you had looked. If today continues a condition already running for several "
    "sessions, say so and say which session of it this is — do not re-narrate a multi-day "
    "pullback, rotation, or rally as though it started this morning. If the mood has genuinely "
    "changed since the prior run, name the specific thing that changed (a breadth flip, a "
    "sector breaking down, a new catalyst) rather than gesturing at a possible change. And if "
    "your recommendations are materially the same as the prior run's, do not dress them up as a "
    "fresh read: state plainly that nothing has changed enough to act on, and name the "
    "condition that would change it.\n\n"
    "Write the analysis in blocks, in this order: SINCE LAST SESSION (only when an OPEN WATCH "
    "ITEMS section appears below), ANALYST VIEWS (only when an ANALYST VIEWS section appears "
    "below), OPPORTUNITY COST CHECK, TRIMS/EXITS, BUYS, HOLDS. "
    "Do not use --- as dividers between blocks. "
    "Avoid trading jargon — write plainly for someone who trades casually but is not an expert.\n\n"
    "TRIMS/EXITS: Only list positions actually being trimmed or exited — one bold action line "
    "per trim (e.g. **TRIM ARM — Sell $50**) followed by one sentence of reasoning. For a "
    "protected-symbol trim, state the reinvestment condition in that same sentence — this is "
    "required for the commitment to be tracked. "
    "Do not mention positions that are merely being held; those go in HOLDS. Never write a "
    "TRIM/EXIT/SELL action line for a position you are NOT trimming or exiting — no '**TRIM COST — "
    "no action**'. A protected symbol that is blocked by an outstanding commitment, or any other "
    "restricted position, is simply a hold: one line in HOLDS, nothing here. The one exception is "
    "a GAIN GIVEBACK alert you are holding through, which is written as '**HOLD META (giveback "
    "alert) — specific reason**' and not as a trim line. "
    "If nothing to trim, say so in one sentence — but only after checking that no position is "
    "being exited for any reason, including SMALL POSITION CLEANUP, and that every GAIN GIVEBACK "
    "alert has been either acted on or named here with its specific reason for holding. "
    "\"Nothing to trim\" next to "
    "an exit recommended elsewhere in the analysis is a contradiction.\n\n"
    "BUYS: State total capital available (cash + trim proceeds) in one line. Then one bold action "
    "line per buy (e.g. **BUY MRVL — $35**) with one sentence of reasoning. "
    "Do not include a capital math summary — state each buy exactly once. "
    "This account trades exclusively in small dollar amounts via fractional shares by design — "
    "typical entries here are $15-50 on a portfolio worth under $1,000. Never describe an "
    "available amount (cash and/or trim proceeds) as too small, inadequate, or only good for "
    "'a small add' rather than 'a full position' — there is no such thing as a full position "
    "on this account, and that framing is backwards for how it operates. Idle cash earns nothing "
    "and is a drag on an aggressive-growth account: the default for ANY nonzero Available Cash "
    "(including a few dollars of leftover proceeds) is to deploy it into the best available "
    "opportunity this session. Holding cash is an exception that needs a specific, named reason "
    "(e.g. every candidate is overbought or news-damaged, or a pending order already "
    "earmarks it) — 'it's only $7' and 'nothing is broken' are not reasons. If you hold cash, "
    "say which exception applies. Deploying is not a license to chase: the target of a small "
    "deployment must pass the SAME entry standard as any other buy. A one-session bounce, a "
    "green day, or an analyst price-target headline is NOT a reason to buy a name — if you "
    "rejected a candidate as overbought or stretched in the OPPORTUNITY COST CHECK, you may not "
    "then buy a holding that is in the same condition (up sharply today or over the last "
    "session or two, RSI elevated, price extended above its MA50) for the same reason. Prefer, "
    "in order: (1) the best unowned candidate if it is a clean entry, (2) a funding-eligible or "
    "underweight holding that is pulling back toward its MA50 with RSI not stretched, "
    "(3) the position with the strongest thesis that is NOT up on the day. Never put new "
    "money into a name solely because it just bounced, and do not treat a single up session as "
    "resolving a multi-session weakness watch item. "
    "Never comment on whether the dollar figure itself is big enough. "
    "If a move is genuinely compelling but there is no internal funding source (no cash, no "
    "reasonable trim, everything else restricted or a loser), do not just say 'no buys today' — "
    "say explicitly that the opportunity is worth funding with outside cash (a deposit) even "
    "though nothing internal can fund it, and name the ticker and why.\n\n"
    "HOLDS: List each held position on its own line with a brief reason. "
    "Do not group positions into categories. HOLDS lists only positions being kept: a position "
    "being trimmed or exited appears in TRIMS/EXITS and not here, and HOLDS must never introduce "
    "a sell, exit, or trim that TRIMS/EXITS does not already contain.\n\n"
    "End the analysis with **ONE KEY THING TO WATCH TODAY:** followed by one sentence "
    "describing something not yet actionable. If it is actionable, put it in TRIMS or "
    "BUYS instead. It MUST be trackable, because the script parses this line, records the "
    "current readings for the symbols in it, and hands you the then→now movement on every "
    "later run until it is resolved. So it must name at least one specific symbol from the "
    "data above and a condition a later run can actually check against price, RSI, or moving "
    "average data — 'watch whether the pullback continues' is untrackable and invalid; 'watch "
    "whether MU holds its MA50 near $X, which would keep the dip-buy case alive' is valid. If "
    "an item in OPEN WATCH ITEMS is still genuinely the most important thing to watch, restate "
    "that same item deliberately rather than inventing a new one — restating keeps the original "
    "baseline, so the trend keeps being measured from where it was first flagged.\n\n"
    "INTERNAL CONSISTENCY RULE: If you identify a compelling new entry opportunity but have no cash "
    "and recommend no trims, your output is self-contradicting unless you explicitly resolve it one "
    "of three ways: (1) the opportunity IS compelling — identify a trim to fund it and recommend "
    "both together; (2) the opportunity is NOT compelling enough to justify disrupting a winning "
    "position — say so plainly; or (3) the opportunity IS compelling but no internal funding source "
    "exists right now — say so plainly and flag it as worth funding with a deposit instead of letting "
    "it pass silently. Never recommend a new entry alongside 'no cash, no trims' without picking one "
    "of these three. "
    "Use specific BUY, SELL, TRIM, or HOLD language. Be concise — lead with the decision, "
    "one sentence of reasoning per position. Do not output systematic check tables or grids.\n\n"
    "TL;DR — WRITTEN LAST, ON PURPOSE: After the analysis is complete, output a line "
    "containing exactly --- and then write a 3-4 sentence tl;dr. Write it only after the "
    "TRIMS/EXITS, BUYS, and HOLDS blocks already exist above it, because it has to be "
    "consistent with them.\n"
    "It does two jobs, in this order. FIRST, read the broad market mood — what the tape and "
    "the news flow are doing today at the macro and sector level: risk-on or risk-off, which "
    "sectors are leading or breaking down, how broad the move is, what the headlines are "
    "fixated on, and whether this looks like a one-day wobble or the start of something "
    "sustained. Read this from the market news, from the MARKET BREADTH series, and from the "
    "breadth of moves across the whole position list — not from any single ticker. Place it "
    "against the prior run's tl;dr: if this is a continuation, say so and say which session of "
    "it this is; if it has changed, name what changed. Do not describe a condition that has "
    "been running for days as if it were today's news, and do not recycle the prior tl;dr's "
    "phrasing to make an unchanged read sound new. SECOND, connect that mood to the decisions you "
    "actually recommended: the mood is the 'why now' behind the advice, so state the link "
    "explicitly — a sector-wide selloff with intact theses is why almost everything is a hold; "
    "one genuine catalyst breaking today is why the single buy goes where it goes; a risk-off "
    "tape with no eligible funding source is why a good opportunity gets flagged instead of "
    "taken.\n"
    "The mood you describe must be the same one the analysis reasoned from. Do not name a "
    "trend, risk, or opportunity that is absent from the analysis or that points the opposite "
    "way from what you recommended — a tl;dr telling a different story than the advice beneath "
    "it is a failure, not a difference in altitude. When ANALYST VIEWS give an issue's overall "
    "read, the mood you describe may not be stated more confidently or more settled than that "
    "read: if the newsletter calls the picture unresolved and the day's breadth looks green, say "
    "both rather than declaring the worry over. Name only companies, sectors and themes that "
    "appear in the news or the position data — do not coin groupings of your own. If the market mood genuinely argues against "
    "your recommendations, the analysis is what is wrong: go back and fix the recommendations "
    "rather than papering over the gap here. This includes hinting at a shift you did not act "
    "on: if you say the picture may be changing while recommending exactly what you recommended "
    "last session, you must name the specific, checkable trigger that would turn it into an "
    "action — and that trigger belongs in ONE KEY THING TO WATCH TODAY so the next run measures "
    "it. A vague 'this could change the calculus' with nothing tracking it is a failure. "
    "Do not simply restate the action lines. Write it "
    "plainly and humanistically, as the one paragraph someone would read if they read nothing "
    "else."
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
        # robin_stocks opens the pickle with 'wb' *before* reading the login response, so a
        # failed password login (no access_token) leaves a 0-byte file and destroys whatever
        # reauth.py just wrote. Snapshot it and put it back if it comes out empty.
        backup = None
        try:
            with open(ROBIN_TOKEN_PATH, "rb") as f:
                backup = f.read() or None
        except OSError:
            pass
        try:
            r.login(
                username=os.getenv("ROBINHOOD_USERNAME"),
                password=os.getenv("ROBINHOOD_PASSWORD"),
                store_session=True,
                pickle_name=ROBIN_TOKEN_NAME,
            )
        finally:
            try:
                if backup and os.path.getsize(ROBIN_TOKEN_PATH) == 0:
                    with open(ROBIN_TOKEN_PATH, "wb") as f:
                        f.write(backup)
                    log.warning("Fresh login left an empty session file — restored the previous pickle")
            except OSError:
                pass

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
                    # When Robinhood opened this position record — the earliest the
                    # holder could have had a gain, so the gain-giveback peak is
                    # measured from here and not from a pre-purchase price spike.
                    "opened_at": pos.get("created_at"),
                }
            )
        except Exception as e:
            log.warning(f"Error processing position entry: {e}")

    return positions


def get_cash() -> float:
    """
    Return the account's cash balance.

    "cash" and "withdrawable_amount" both read $0 here — margin accounts hold
    settled cash against margin, so those fields don't reflect actual
    spendable balance. "portfolio_cash" is the confirmed-correct field:
    verified against a live account by cross-checking
    load_portfolio_profile()'s equity - market_value, which matches
    portfolio_cash exactly.
    """
    try:
        accounts = r.load_account_profile(dataType="results") or []
    except Exception as e:
        log.warning(f"Could not fetch cash balance: {e}")
        return 0.0

    if not accounts:
        log.warning("get_cash(): load_account_profile(dataType='results') returned no accounts")
        return 0.0

    total = round(sum(float(a.get("portfolio_cash", 0) or 0) for a in accounts), 2)
    log.info(f"get_cash() returning {total} (sum of 'portfolio_cash' across {len(accounts)} account(s))")
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


# Robinhood order states meaning "placed, accepted, but not yet executed". An
# order placed over a weekend or after hours sits in one of these until the next
# session opens, which makes it invisible to BOTH sources the script otherwise
# trusts: get_open_stock_positions() (no shares are held yet) and
# get_recent_orders() (which keeps only state == "filled"). That blind spot is
# how a 6am pre-market run can recommend buying a stock the user already
# committed to over the weekend, funded with cash that order has already spent.
PENDING_ORDER_STATES = {"queued", "unconfirmed", "confirmed", "partially_filled"}


def _order_notional(order: dict) -> float | None:
    """
    Best-effort dollar value of an order.

    Robinhood reports this differently depending on how the order was placed:
    dollar-based fractional orders carry a nested amount block and often no
    quantity at all until they fill, while share-based orders carry quantity
    plus a limit price. Try the notional blocks first, then fall back to
    quantity x price.
    """
    for key in ("dollar_based_amount", "total_notional", "executed_notional"):
        block = order.get(key)
        if isinstance(block, dict):
            try:
                amount = float(block.get("amount"))
            except (TypeError, ValueError):
                continue
            if amount > 0:
                return round(amount, 2)

    try:
        qty = float(order.get("quantity") or 0)
        raw_price = order.get("average_price") or order.get("price")
        if qty > 0 and raw_price:
            return round(qty * float(raw_price), 2)
    except (TypeError, ValueError):
        pass
    return None


def get_pending_orders() -> list[dict]:
    """
    Return stock orders that are placed but not yet filled, newest first.
    Each entry: {symbol, side, quantity, price, notional, date, state}.

    See PENDING_ORDER_STATES above for why this matters: without it, a
    weekend-placed order is invisible to every other account read the script
    makes.
    """
    try:
        raw = r.get_all_open_stock_orders() or []
    except Exception as e:
        log.warning(f"Could not fetch open orders: {e}")
        return []

    instrument_cache: dict[str, str] = {}
    pending = []

    for order in raw:
        if order.get("state") not in PENDING_ORDER_STATES:
            continue

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
            qty = round(float(order.get("quantity") or 0), 4) or None
        except (TypeError, ValueError):
            qty = None
        try:
            raw_price = order.get("price")
            price = round(float(raw_price), 4) if raw_price else None
        except (TypeError, ValueError):
            price = None

        created = (order.get("created_at") or "")[:10]

        pending.append({
            "symbol": symbol,
            "side": order.get("side", ""),
            "quantity": qty,
            "price": price,
            "notional": _order_notional(order),
            "date": created,
            "state": order.get("state", ""),
        })

    pending.sort(key=lambda o: o["date"], reverse=True)
    return pending


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
    buy order at/below the committed price lands after the trim, or buys after the
    trim at ANY price add back roughly the trimmed amount (the position is restored —
    insisting on the lower price would make a rising core holding untouchable), or
    PROTECTED_COMMITMENT_MAX_DAYS pass since the trim.
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

        buys_since = [
            o for o in recent_orders
            if o["symbol"] == symbol
            and o["side"] == "buy"
            and o["price"] is not None
            and o["date"] > c["trim_date"]
        ]
        if any(o["price"] <= c["reinvest_price"] for o in buys_since):
            continue
        restored = sum(o["quantity"] * o["price"] for o in buys_since)
        if restored >= c["trim_amount"] * PROTECTED_COMMITMENT_RESTORE_FRACTION:
            log.info(
                f"{symbol} commitment cleared: ${restored:.2f} bought back since "
                f"{c['trim_date']} restores the ${c['trim_amount']:.2f} trim (above the "
                f"${c['reinvest_price']:.2f} target, which is fine)"
            )
            continue
        if _days_between(c["trim_date"], today) > PROTECTED_COMMITMENT_MAX_DAYS:
            log.info(
                f"{symbol} commitment from {c['trim_date']} expired after "
                f"{PROTECTED_COMMITMENT_MAX_DAYS} days without a buy-back — trims unlocked"
            )
            continue
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
# Daily closes per symbol, filled as a side effect of fetch_bulk_market_data. Kept
# out of the returned indicator dicts on purpose: those are serialised into the
# summary/last_analysis.json, and a pandas Series does not belong there. The risk
# context (gain giveback, correlation clusters, benchmarks) reads from here.
PRICE_HISTORY: dict[str, pd.Series] = {}


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
            PRICE_HISTORY[symbol] = closes

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
        f"  • [{item.get('published', 'undated')}] {item['title']}"
        if isinstance(item, dict) else f"  • {item}"
        for item in sherwood
    ) or (
        f"  (no market news available within the last {NEWS_MAX_AGE_DAYS} days "
        f"— judge on momentum data alone)"
    )

    newsletters = summary.get("newsletters") or []
    nl_tickers = sorted({t for n in newsletters for t in n.get("tickers", [])})
    if nl_tickers:
        sherwood_lines += (
            "\n  Tickers named in today's Sherwood newsletters ("
            + ", ".join(n["name"] for n in newsletters)
            + "): " + ", ".join(nl_tickers)
        )

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
        f"Recent market news (published within {NEWS_MAX_AGE_DAYS} days):\n"
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


# ── Snacks newsletter (Robinhood's own analysis) ──────────────────────────────
# Preferred market-news source. Sherwood's website is gone, but the Snacks
# newsletter it was folded into still arrives daily by email, and it carries
# Robinhood's own written analysis ("THE TAKEAWAY") rather than a bare wire
# headline — which is the whole reason to prefer it over a generic feed.
# Sherwood newsletters to ingest, in priority order. Each is (name, sender).
# Snacks is the daily market wrap; EntryPoint covers market insights and
# technical signals. Note the differing domains — these are not interchangeable
# and neither is guessable from the other.
SHERWOOD_NEWSLETTERS = [
    ("Snacks", "hello@snacks.robinhood.com"),
    ("EntryPoint", "entrypoint@sherwoodmedia.com"),
]
NEWSLETTER_IMAP_HOST = "imap.gmail.com"
# Searched in this order. "[Gmail]/All Mail" holds everything kept, including
# ARCHIVED mail, which is why archiving is the recommended habit. Trash is
# searched last as a safety net: a read-and-deleted issue is still the day's
# analysis, but Gmail purges Trash after ~30 days so it is not a place to rely
# on.
NEWSLETTER_MAILBOXES = ('"[Gmail]/All Mail"', "INBOX", '"[Gmail]/Trash"')
# Per-issue cap on what reaches the prompt. 5000 used to be the cap and silently cut
# off the back half of every issue — which is where Snacks puts its "This Week"
# earnings calendar (the issue is ~7.4k) and where EntryPoint's strategist quotes
# live (~11.2k). 12000 holds both whole; two issues are still only ~6k tokens.
NEWSLETTER_MAX_CHARS = 12000
# A real issue cleans to ~2KB; a welcome or confirmation email to a few hundred
# characters. Below this it is not an issue.
NEWSLETTER_MIN_CHARS = 400
# Subscription-lifecycle mail arrives from the same address as the issues and
# is often the NEWEST message from that sender, so picking "latest" alone
# would hand the analysis a welcome email as the day's market news.
NEWSLETTER_SKIP_SUBJECT = re.compile(
    r"\b(welcome|thank(s| you) for subscribing|confirm|verify|"
    r"subscription|unsubscrib|you'?re in|get started)\b",
    re.IGNORECASE,
)


class _TextExtractor(HTMLParser):
    """
    Minimal HTML-to-text for newsletter bodies, stdlib only (no bs4 dependency).

    Collects visible text with block-level breaks preserved, and records every
    href separately so tickers can be recovered from link targets.
    """

    _BLOCK = {
        "p", "div", "br", "tr", "td", "li", "ul", "ol", "table",
        "h1", "h2", "h3", "h4", "h5", "h6", "section", "header", "footer",
    }
    _SKIP = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skipping += 1
        if tag == "a":
            for key, value in attrs:
                if key == "href" and value:
                    self.links.append(value)
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skipping:
            return
        if data.strip():
            self.parts.append(data)
        elif self.parts and not self.parts[-1].endswith((" ", "\n")):
            # A whitespace-only node is often the ONLY separator between inline
            # elements ("from<a> <strong>Cadence</strong></a>"). Dropping it glued
            # words together ("fromCadence", "EntryPoint last Friday" -> "entrypointlast").
            # Block tags still break lines; _clean_newsletter_text collapses the rest.
            self.parts.append(" ")

    def text(self) -> str:
        return "".join(self.parts)


def _html_to_text(html: str) -> tuple[str, list[str]]:
    """Return (visible_text, hrefs) for an HTML body."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception as e:
        log.warning(f"HTML parse failed, falling back to tag stripping: {e}")
        return re.sub(r"<[^>]+>", " ", html), []
    return parser.text(), parser.links


def _decode_tracking_url(url: str) -> str:
    """
    Recover the destination from a link.sherwood.news tracking URL.

    These wrap the real target as a base64 path segment, so the ticker in
    robinhood.com/stocks/<T> is invisible until decoded. Returns the input
    unchanged when no segment decodes.
    """
    for segment in url.split("/"):
        if len(segment) < 16 or not re.fullmatch(r"[A-Za-z0-9_\-]+", segment):
            continue
        try:
            decoded = base64.urlsafe_b64decode(
                segment + "=" * (-len(segment) % 4)
            ).decode("utf-8")
        except Exception:
            continue
        if decoded.startswith("http"):
            return decoded
    return url


def _extract_tickers(links: list[str]) -> list[str]:
    """
    Tickers named in the issue, taken from its robinhood.com/stocks/<T> links.

    Mechanical rather than linguistic: the newsletter links every company it
    mentions to its Robinhood quote page, so this needs no entity extraction
    and cannot hallucinate a symbol.
    """
    found = set()
    for raw in links:
        url = _decode_tracking_url(raw)
        m = re.search(r"robinhood\.com/stocks/([A-Z][A-Z.\-]{0,9})(?:[/?#]|$)", url)
        if m:
            found.add(m.group(1))
    return sorted(found)


# Everything from here on is legal boilerplate, not market content.
_SNACKS_CUTOFFS = (
    "Sherwood Media, LLC produces",
    "Sherwood Terms and Conditions",
    "Craving more insights in your inbox?",
)


def _clean_newsletter_text(text: str) -> str:
    """
    Reduce a newsletter body to readable prose for the prompt.

    Strips the empty table-cell scaffolding these emails are built from, drops
    the legal footer, and collapses whitespace. The section structure (THE
    TAKEAWAY, CHART OF THE DAY, the next-session calendar) is left intact and
    deliberately NOT parsed into fields: Claude reads the issue directly, so
    there are no brittle per-section regexes to break when the layout changes.
    """
    # Markdown-ish link remnants: "Microsoft[](http...)" -> "Microsoft"
    text = re.sub(r"\[\]\((?:https?|mailto)[^)]*\)", "", text)
    text = re.sub(r"<[^>]*>", "", text)

    lines = []
    seen = set()
    for raw in text.splitlines():
        line = raw.strip().strip("|").strip()
        line = re.sub(r"[ \t]+", " ", line)
        if not line or set(line) <= {"|", " ", "#", "-", "*", "\u00a0"}:
            continue
        # Photo credits ride along as their own block, e.g. "# (Kent
        # Nishimura/Getty Images)" — pure noise in a text prompt.
        if re.fullmatch(r"#*\s*\([^()]{0,80}\)", line):
            continue
        # The preheader repeats the lede verbatim one block later.
        key = line.lstrip("#* ").strip()
        if key in seen:
            continue
        seen.add(key)
        lines.append(line)

    cleaned = []
    for line in lines:
        if any(line.startswith(c) or line == c for c in _SNACKS_CUTOFFS):
            break
        cleaned.append(line)

    out = "\n".join(cleaned)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    if len(out) > NEWSLETTER_MAX_CHARS:
        out = out[:NEWSLETTER_MAX_CHARS].rsplit("\n", 1)[0] + "\n[... issue truncated]"
    return out


def _message_body(msg) -> str:
    """Prefer a real text/plain part; fall back to converting text/html."""
    plain, html = "", ""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            body = payload.decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if ctype == "text/plain" and not plain:
            plain = body
        elif ctype == "text/html" and not html:
            html = body

    # A newsletter's text/plain part is often a stub ("view in browser"), so
    # only trust it when it is substantial enough to be the real issue.
    if len(plain.strip()) > 500:
        return plain
    if html:
        text, links = _html_to_text(html)
        _message_body.last_links = links  # type: ignore[attr-defined]
        return text
    return plain


def _is_newsletter_issue(subject: str, text: str) -> tuple[bool, str]:
    """
    Whether a message is an actual issue rather than subscription-lifecycle mail.

    Returns (is_issue, reason_if_not). Both checks matter: the subject pattern
    catches welcome/confirmation mail by name, and the length floor catches
    anything transactional that the pattern misses, since a real issue is an
    order of magnitude longer than a signup receipt.
    """
    if NEWSLETTER_SKIP_SUBJECT.search(subject or ""):
        return False, f"subscription email, not an issue ({subject!r})"
    if len(text) < NEWSLETTER_MIN_CHARS:
        return False, f"body too short to be an issue ({len(text)} chars)"
    return True, ""


def _decode_subject(msg) -> str:
    try:
        return str(make_header(decode_header(msg.get("Subject", "")))).strip()
    except Exception:
        return (msg.get("Subject") or "").strip()


def _message_sent_at(msg) -> datetime | None:
    try:
        sent = email.utils.parsedate_to_datetime(msg.get("Date", ""))
    except (TypeError, ValueError):
        return None
    if sent is None:
        return None
    return sent.replace(tzinfo=timezone.utc) if sent.tzinfo is None else sent


def _newsletter_from_message(name: str, raw: bytes, sent: datetime,
                             max_age_days: int) -> tuple[dict | None, str]:
    """Parse one fetched message into an issue dict, or explain why not."""
    msg = email.message_from_bytes(raw)
    _message_body.last_links = []  # type: ignore[attr-defined]
    body = _message_body(msg)
    links = list(getattr(_message_body, "last_links", []) or [])
    links += re.findall(r"https?://[^\s)\]<>\"]+", body)

    text = _clean_newsletter_text(body)
    subject = _decode_subject(msg)
    is_issue, reason = _is_newsletter_issue(subject, text)
    if not is_issue:
        return None, reason

    age_days = (datetime.now(timezone.utc) - sent).days
    if age_days > max_age_days:
        return None, (
            f"newest issue is {age_days}d old ({sent.date().isoformat()}), "
            f"outside the {max_age_days}d window"
        )

    return {
        "name": name,
        "date": sent.date().isoformat(),
        "subject": subject,
        "text": text,
        "tickers": _extract_tickers(links),
        "age_days": age_days,
    }, ""


def fetch_sherwood_newsletters(max_age_days: int | None = None) -> tuple[list[dict], list[str]]:
    """
    Return (issues, problems) for the configured Sherwood newsletters.

    One issue per newsletter at most — the newest real issue inside the
    freshness window — newest first. Uses the Gmail app password already
    configured for sending, over IMAP, so no new credential is required.
    Never raises: an unreachable mailbox or a newsletter with no recent issue
    is reported in `problems` and the caller falls back to the wire feeds.
    """
    max_age_days = NEWS_MAX_AGE_DAYS if max_age_days is None else max_age_days
    address = os.getenv("GMAIL_ADDRESS")
    password = os.getenv("GMAIL_APP_PASSWORD")
    if not address or not password:
        return [], ["GMAIL_ADDRESS/GMAIL_APP_PASSWORD not set"]

    since = (datetime.now(timezone.utc) - timedelta(days=max_age_days + 1)).strftime("%d-%b-%Y")
    issues: list[dict] = []
    problems: list[str] = []

    try:
        with imaplib.IMAP4_SSL(NEWSLETTER_IMAP_HOST) as imap:
            imap.login(address, password)
            for name, sender in SHERWOOD_NEWSLETTERS:
                # Candidates are gathered across mailboxes first, then tried
                # newest-first, so a welcome email sitting on top of a real
                # issue is skipped rather than ending the search.
                candidates: list[tuple[datetime, bytes]] = []
                for mailbox in NEWSLETTER_MAILBOXES:
                    try:
                        status, _ = imap.select(mailbox, readonly=True)
                        if status != "OK":
                            continue
                        status, data = imap.search(None, "FROM", f'"{sender}"', "SINCE", since)
                        if status != "OK" or not data or not data[0]:
                            continue
                        for num in data[0].split()[-8:]:
                            status, fetched = imap.fetch(num, "(RFC822)")
                            if status != "OK" or not fetched or not fetched[0]:
                                continue
                            raw = fetched[0][1]
                            if not isinstance(raw, bytes):
                                continue
                            sent = _message_sent_at(email.message_from_bytes(raw))
                            if sent is not None:
                                candidates.append((sent, raw))
                    except Exception as e:
                        log.warning(f"{name}: mailbox {mailbox} unusable: {e}")
                        continue

                if not candidates:
                    problems.append(f"{name}: no email from {sender} since {since}")
                    log.warning(f"{name}: no email from {sender} since {since}")
                    continue

                candidates.sort(key=lambda pair: pair[0], reverse=True)
                seen_reasons = []
                for sent, raw in candidates:
                    issue, reason = _newsletter_from_message(name, raw, sent, max_age_days)
                    if issue:
                        issues.append(issue)
                        log.info(
                            f"{name}: using issue {issue['date']} ({issue['age_days']}d old) "
                            f"\"{issue['subject']}\" — {len(issue['text'])} chars, "
                            f"tickers: {issue['tickers'] or 'none'}"
                        )
                        break
                    if reason and reason not in seen_reasons:
                        seen_reasons.append(reason)
                        log.info(f"{name}: skipped a message — {reason}")
                else:
                    detail = seen_reasons[0] if seen_reasons else "no usable issue"
                    problems.append(f"{name}: {detail}")
                    log.warning(f"{name}: no usable issue — {detail}")
    except Exception as e:
        log.warning(f"Newsletter IMAP fetch failed ({type(e).__name__}): {e}")
        return [], [f"IMAP fetch failed ({type(e).__name__})"]

    issues.sort(key=lambda i: i["date"], reverse=True)
    return issues, problems


# ── News ──────────────────────────────────────────────────────────────────────
_RSS_HEADERS = {"User-Agent": "portfolio-monitor/1.0"}
_RSS_TIMEOUT = 10  # seconds

# Market-news feeds, tried in order until one yields articles inside the freshness
# window. A feed that 404s, errors, OR has simply stopped being updated falls
# through to the next. The fallthrough exists because a publication can die while
# its feed keeps serving: Robinhood wound down the Sherwood News website around
# mid-2026 and moved the reporting into newsletters and the Robinhood app, but
# sherwood.news/rss.xml kept returning its final pre-shutdown articles. Nothing
# errored, so the digest went on presenting summer headlines as "TODAY'S MARKET
# NEWS" for months. Freshness is therefore validated, never assumed.
MARKET_NEWS_FEEDS = [
    ("Sherwood", "https://sherwood.news/rss.xml"),
    ("CNBC Markets", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
]

# Max age for a headline to count as current. 4 days so a Monday pre-market run
# still sees Friday's close and the weekend's reporting.
NEWS_MAX_AGE_DAYS = 4


def _parse_feed(url: str) -> list:
    """Fetch and parse an RSS feed, returning entries or [] on failure."""
    resp = requests.get(url, headers=_RSS_HEADERS, timeout=_RSS_TIMEOUT)
    resp.raise_for_status()
    return feedparser.parse(resp.text).entries


def _entry_published(entry) -> datetime | None:
    """
    Timezone-aware publish time for a feedparser entry, or None if absent.

    feedparser normalises the many RSS/Atom date spellings into a UTC struct_time
    under published_parsed/updated_parsed, so prefer those over the raw strings.
    """
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _fresh_entries(entries: list, max_age_days: int) -> tuple[list, int, int]:
    """
    Sort entries newest-first and keep only those published within max_age_days.

    Returns (fresh, stale_count, undated_count). Feed order is not publication
    order in practice, so sorting is required before any "most recent" claim.
    Undated entries are dropped rather than trusted: an item whose age cannot be
    established is exactly the case this guard exists to catch.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    dated, undated = [], 0
    for e in entries:
        published = _entry_published(e)
        if published is None:
            undated += 1
            continue
        dated.append((published, e))

    dated.sort(key=lambda pair: pair[0], reverse=True)
    fresh = [(pub, e) for pub, e in dated if pub >= cutoff]
    return fresh, len(dated) - len(fresh), undated


def fetch_market_news(n: int = 8) -> tuple[list[dict], str, list[str]]:
    """
    Return (items, source_name, problems) for the first MARKET_NEWS_FEEDS entry
    that yields articles inside the freshness window.

    Each item carries title, url, published (display string) and age_days, so both
    the prompt and the digest can show how old a headline is instead of implying
    it is today's. `problems` records why each rejected feed was skipped, for the
    log and for the digest's warning line — a silently empty news section is how
    this went unnoticed for months.
    """
    problems = []

    for source, url in MARKET_NEWS_FEEDS:
        try:
            entries = _parse_feed(url)
        except Exception as e:
            problems.append(f"{source}: fetch failed ({type(e).__name__})")
            log.warning(f"Market news feed {source} ({url}) failed: {e}")
            continue

        if not entries:
            problems.append(f"{source}: feed empty")
            log.warning(f"Market news feed {source} returned no entries")
            continue

        fresh, stale, undated = _fresh_entries(entries, NEWS_MAX_AGE_DAYS)
        if not fresh:
            newest = max((p for p, _ in [(_entry_published(e), e) for e in entries] if p),
                         default=None)
            newest_str = newest.date().isoformat() if newest else "unknown"
            problems.append(
                f"{source}: no articles newer than {NEWS_MAX_AGE_DAYS}d "
                f"(newest {newest_str}, {stale} stale, {undated} undated)"
            )
            log.warning(
                f"Market news feed {source} is stale — newest article {newest_str}, "
                f"nothing within {NEWS_MAX_AGE_DAYS} days; trying next feed"
            )
            continue

        now = datetime.now(timezone.utc)
        items = []
        for published, e in fresh[:n]:
            title = e.get("title", "").strip()
            if not title:
                continue
            items.append({
                "title": title,
                "url": e.get("link", ""),
                "published": published.strftime("%Y-%m-%d"),
                "age_days": (now - published).days,
            })

        if not items:
            problems.append(f"{source}: fresh entries had no titles")
            continue

        if stale or undated:
            log.info(f"Market news from {source}: dropped {stale} stale, {undated} undated")
        log.info(f"Market news source: {source} ({len(items)} fresh articles)")
        return items, source, problems

    log.error(f"No market news feed returned fresh articles: {'; '.join(problems)}")
    return [], "", problems


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
        # Same freshness contract as market news: a per-ticker feed that stops
        # updating would otherwise keep feeding months-old headlines into
        # individual hold/trim decisions.
        fresh, _stale, _undated = _fresh_entries(entries, NEWS_MAX_AGE_DAYS)
        now = datetime.now(timezone.utc)
        items = []
        for published, e in fresh[:n]:
            title = e.get("title", "").strip()
            if not title:
                continue
            items.append({
                "title": title,
                "url": e.get("link", ""),
                "published": published.strftime("%Y-%m-%d"),
                "age_days": (now - published).days,
            })
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


def save_news_cache(
    date_str: str,
    sherwood: list[dict],
    ticker_news: dict[str, list[dict]],
    source: str = "",
    problems: list[str] | None = None,
    newsletters: list[dict] | None = None,
    newsletter_views_data: list[dict] | None = None,
):
    """Persist today's fetched news to NEWS_FILE for email link rendering."""
    with open(NEWS_FILE, "w") as f:
        json.dump(
            {
                "date": date_str,
                "source": source,
                "problems": problems or [],
                "newsletters": newsletters or [],
                "newsletter_views": newsletter_views_data or [],
                "sherwood": sherwood,
                "ticker_news": ticker_news,
            },
            f,
            indent=2,
        )


def _read_analysis_file(path: str) -> tuple[dict, float] | None:
    """Read one analysis file, returning (payload, mtime) or None if unusable."""
    try:
        if os.path.getsize(path) == 0:
            # A Dropbox-hosted path can report 0 bytes (dehydrated placeholder or a
            # sync that lost the contents) even though the run that wrote it logged
            # success — treat that as absent rather than as a parse error.
            log.warning(f"{path} is 0 bytes — ignoring this copy")
            return None
        with open(path) as f:
            return json.load(f), os.path.getmtime(path)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"Could not read {path}: {e}")
        return None


def load_last_analysis() -> dict | None:
    """
    Load the prior run's analysis, preferring whichever of the Dropbox copy and the
    local mirror is intact and most recent. Returns None if neither is usable.
    """
    candidates = []
    for path in dict.fromkeys([ANALYSIS_FILE, ANALYSIS_MIRROR_FILE]):
        got = _read_analysis_file(path)
        if got:
            candidates.append((got[1], path, got[0]))
    if not candidates:
        log.warning("No usable prior analysis found — continuity context unavailable")
        return None
    mtime, path, payload = max(candidates)
    log.info(f"Loaded prior analysis from {path} (date: {payload.get('date', 'unknown')})")
    return payload


def save_analysis(date: str, tldr: str, analysis: str, summary: dict | None = None):
    """Persist today's Claude analysis so tomorrow's run has continuity."""
    payload: dict = {"date": date, "tldr": tldr, "analysis": analysis}

    if summary:
        # Continuity state for the next run: the watch items still being tracked
        # (today's, merged onto any still-open earlier ones) and the rolling
        # breadth series. Both are read back by build_prompt via
        # load_last_analysis() — see the Continuity section above.
        payload["watch_items"] = summary.get("watch_items_next", summary.get("watch_items") or [])
        payload["breadth_history"] = summary.get("breadth_history") or []
        payload["portfolio"] = {
            "total_value": summary.get("total_value"),
            "cash": summary.get("cash"),
            "committed_cash": summary.get("committed_cash", 0.0),
            "uncommitted_cash": summary.get("uncommitted_cash", summary.get("cash")),
            # Carried so an interactive MCP session reading this file sees the
            # same in-flight orders the digest did — a pending buy's symbol is
            # absent from "positions" and would otherwise look unowned there too.
            "pending_orders": summary.get("pending_orders", []),
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
        # Compact copy of the risk context for MCP sessions reading this file.
        risk = summary.get("risk_context") or {}
        payload["risk_context"] = {
            "giveback": risk.get("giveback") or {},
            "concentration": risk.get("concentration") or [],
            "benchmarks": risk.get("benchmarks"),
        }
        payload["momentum"] = [
            {
                k: m.get(k)
                for k in ("symbol", "score", "rsi", "ma50", "ma200",
                          "price_vs_ma50_pct", "volume_ratio", "pct_change_today")
            }
            for m in summary.get("momentum", [])
        ]

    # Write the Dropbox copy (cross-machine sync) and the local mirror (survives
    # Dropbox dehydrating or zeroing the synced copy). Verify each landed non-empty:
    # the 08-17 and 08-19 runs both logged a successful save and left 0-byte files,
    # which went unnoticed until an MCP session tried to read one.
    blob = json.dumps(payload, indent=2)
    wrote_any = False
    for path in dict.fromkeys([ANALYSIS_FILE, ANALYSIS_MIRROR_FILE]):
        try:
            with open(path, "w") as f:
                f.write(blob)
                f.flush()
                os.fsync(f.fileno())
            if os.path.getsize(path) == 0:
                log.error(f"Wrote {len(blob)} bytes to {path} but it reads back as 0 bytes")
                continue
            wrote_any = True
        except OSError as e:
            log.error(f"Failed writing analysis to {path}: {e}")
    if not wrote_any:
        raise OSError("analysis could not be persisted to any location")


# ── Continuity: market breadth + watch-item tracking ─────────────────────────
# Two more pieces of Python-tracked state, same pattern as trim warnings and
# protected-symbol commitments: the model does not get to decide whether a
# multi-day trend is still worth mentioning, and it cannot flag something as
# "the one thing to watch" and then quietly never look at it again.
#
# The symptom that motivated this: several consecutive digests opened with a
# near-identical tl;dr about the same chip-sector pullback, each written as if
# it were that morning's discovery, and each ending with a "watch whether this
# is a one-day wobble or something broader" line that the next run never
# followed up on. Both halves are unfixable by prompt wording alone — with
# thinking disabled the model sees one day's snapshot plus the prior run's prose
# and has no measured record of what the thing it flagged actually did.
#
# Both ride on last_analysis.json rather than a new file: it is already
# dual-written with a local mirror, already gitignored (real dollar figures,
# public repo), and already loaded before the prompt is built.
def compute_breadth(positions: list[dict], today: str) -> dict:
    """Session-level breadth of the position list — advancers/decliners, median
    move, and how many are above their MA50. Cheap, and it is the only factual
    basis for calling today's mood a continuation or a change."""
    moves = [
        (p.get("indicators") or {}).get("pct_change_today") for p in positions
    ]
    moves = [m for m in moves if m is not None]
    vs_ma50 = [
        (p.get("indicators") or {}).get("price_vs_ma50_pct") for p in positions
    ]
    vs_ma50 = [v for v in vs_ma50 if v is not None]
    return {
        "date": today,
        "total": len(moves),
        "up": sum(1 for m in moves if m > 0),
        "down": sum(1 for m in moves if m < 0),
        "median_pct": round(float(statistics.median(moves)), 2) if moves else None,
        "above_ma50": sum(1 for v in vs_ma50 if v > 0),
        "with_ma50": len(vs_ma50),
    }


def build_breadth_history(prior_analysis: dict | None, breadth: dict) -> list[dict]:
    """Prior sessions' breadth plus today's, oldest first, capped at
    BREADTH_HISTORY_LEN. A same-day rerun replaces its own earlier entry rather
    than stacking a second one for the same date."""
    history = list((prior_analysis or {}).get("breadth_history") or [])
    history = [h for h in history if h.get("date") != breadth["date"]]
    history.append(breadth)
    return history[-BREADTH_HISTORY_LEN:]


def describe_breadth_streak(history: list[dict]) -> str:
    """One line naming the current run of down- or up-breadth sessions, so a
    multi-session condition is stated as a fact rather than left to be inferred
    from the rows."""
    if len(history) < 2:
        return ""
    def direction(h: dict) -> str:
        if h.get("up") is None or h.get("down") is None:
            return "flat"
        if h["up"] > h["down"]:
            return "up"
        if h["down"] > h["up"]:
            return "down"
        return "flat"
    latest = direction(history[-1])
    if latest == "flat":
        return ""
    streak = 0
    for h in reversed(history):
        if direction(h) != latest:
            break
        streak += 1
    if streak < 2:
        return ""
    word = "decliners than advancers" if latest == "down" else "advancers than decliners"
    return (
        f"Streak: {streak} consecutive sessions with more {word} "
        f"(this is session {streak} of the current condition, not a new development)."
    )


_WATCH_MARKER_RE = re.compile(
    r"ONE\s+KEY\s+THING\s+TO\s+WATCH(?:\s+TODAY)?\s*\**\s*:?\**\s*", re.IGNORECASE
)
_WATCH_READING_KEYS = ("current_price", "rsi", "price_vs_ma50_pct", "volume_ratio")


def _watch_reading(data: dict | None) -> dict:
    return {k: (data or {}).get(k) for k in _WATCH_READING_KEYS}


def extract_watch_item(
    analysis_text: str, universe: set[str], today: str, market_data: dict[str, dict]
) -> dict | None:
    """
    Pull today's "ONE KEY THING TO WATCH TODAY" line out of the analysis and
    snapshot the current readings for every symbol it names, so later runs can
    report what those symbols actually did rather than re-reading the sentence.
    Pure string work — no extra API call. Returns None if the marker is absent.
    """
    m = _WATCH_MARKER_RE.search(analysis_text or "")
    if not m:
        return None
    tail = analysis_text[m.end():].strip()
    # The marker is the last thing in the analysis, but stop at a blank line or a
    # separator anyway in case anything follows it.
    text = re.split(r"\n\s*\n|\n-{3,}", tail)[0].strip()
    text = re.sub(r"\*\*|__", "", text).strip()
    if not text:
        return None
    seen: list[str] = []
    for token in re.findall(r"\b[A-Z][A-Z0-9.\-]{0,5}\b", text):
        if token in universe and token not in seen:
            seen.append(token)
    symbols = seen[:WATCH_ITEM_MAX_SYMBOLS]
    return {
        "flagged_date": today,
        "restated_date": today,
        "text": text,
        "symbols": symbols,
        "baseline": {s: _watch_reading(market_data.get(s)) for s in symbols},
    }


def carry_forward_watch_items(prior_analysis: dict | None, today: str) -> list[dict]:
    """
    Prior runs' watch items that are still open: younger than
    WATCH_ITEM_MAX_AGE_DAYS and not flagged today. An item flagged today has
    nothing to report yet (this happens on a same-day rerun), and an item that
    ages out is dropped rather than nagged about forever — if it still matters,
    the model is told to restate it, which starts a fresh baseline.
    """
    items = []
    for item in (prior_analysis or {}).get("watch_items") or []:
        flagged = item.get("flagged_date")
        if not flagged or not item.get("text"):
            continue
        try:
            age = _days_between(flagged, today)
        except ValueError:
            continue
        if age <= 0 or age > WATCH_ITEM_MAX_AGE_DAYS:
            continue
        items.append(item)
    items.sort(key=lambda i: i["flagged_date"], reverse=True)
    return items[:WATCH_ITEM_MAX_OPEN]


_VERDICT_RE = re.compile(r"\b(RESOLVED|ESCALATED)\b")


def apply_watch_verdicts(
    analysis_text: str, open_items: list[dict], today: str | None = None
) -> tuple[list[dict], list[tuple[dict, str]]]:
    """
    Close out any watch item today's SINCE LAST SESSION block marked RESOLVED or
    ESCALATED, so a question that has been answered stops being re-asked (and
    the tracker doesn't silt up with zombie lines, which would be its own kind of
    daily repetition). Returns (still_open, [(item, verdict), ...]).

    Deliberately conservative: an item is only closed when a line naming one of
    its own symbols carries the verdict word. Anything unmatched or ambiguous
    stays open and comes back tomorrow with updated numbers — the failure mode
    to avoid here is dropping a trend nobody looked at, not carrying one an
    extra session.

    RESOLVED is additionally age-gated when `today` is given: an item younger
    than WATCH_ITEM_MIN_RESOLVE_AGE_DAYS stays open even if the model says
    RESOLVED. A MU "cushion compressing for 5 sessions" watch was closed as
    "resolved bullish" the morning after it was flagged, off a single up day,
    and the cash was then deployed into that same bounce. ESCALATED is not
    gated — it already requires a matching action line in the analysis.
    """
    if not open_items:
        return [], []
    m = re.search(r"SINCE\s+LAST\s+SESSION", analysis_text or "", re.IGNORECASE)
    if not m:
        return open_items, []
    block = analysis_text[m.end():]
    # The block ends at the next analysis header.
    end = re.search(
        r"\n[\s#*]*(ANALYST VIEWS|OPPORTUNITY COST|TRIMS|EXITS|BUYS|HOLDS)\b", block, re.IGNORECASE
    )
    if end:
        block = block[: end.start()]

    kept, closed = [], []
    for item in open_items:
        symbols = item.get("symbols") or []
        verdict = None
        for line in block.splitlines():
            if not line.strip():
                continue
            if symbols and not any(
                re.search(rf"\b{re.escape(s)}\b", line) for s in symbols
            ):
                continue
            found = _VERDICT_RE.search(line)
            if found:
                verdict = found.group(1)
                break
        if verdict == "RESOLVED" and today and item.get("flagged_date"):
            try:
                age = _days_between(item["flagged_date"], today)
            except ValueError:
                age = WATCH_ITEM_MIN_RESOLVE_AGE_DAYS
            if age < WATCH_ITEM_MIN_RESOLVE_AGE_DAYS:
                log.info(
                    f"Watch item from {item['flagged_date']} marked RESOLVED at {age}d old "
                    f"(< {WATCH_ITEM_MIN_RESOLVE_AGE_DAYS}d) — keeping it open: "
                    f"{item.get('symbols') or '(no symbol)'}"
                )
                verdict = None
        if verdict:
            closed.append((item, verdict))
        else:
            kept.append(item)
    return kept, closed


def merge_watch_item(open_items: list[dict], new_item: dict | None) -> list[dict]:
    """
    Fold today's watch item into the open list for persistence. A restatement of
    an existing item (any symbol overlap) updates that item's text in place and
    KEEPS its original flagged_date and baseline — that is what makes a trend
    measurable across sessions instead of resetting to zero every morning.
    """
    items = [dict(i) for i in open_items]
    if not new_item:
        return items[:WATCH_ITEM_MAX_OPEN]
    new_syms = set(new_item.get("symbols") or [])
    for item in items:
        if new_syms and new_syms & set(item.get("symbols") or []):
            item["text"] = new_item["text"]
            item["restated_date"] = new_item["restated_date"]
            # Union the symbol sets so a widened watch keeps tracking the
            # originals, and baseline any symbol that has no reading yet.
            for sym in new_item["symbols"]:
                if sym in (item.get("symbols") or []):
                    continue
                if len(item.get("symbols") or []) >= WATCH_ITEM_MAX_SYMBOLS:
                    break
                item.setdefault("symbols", []).append(sym)
                item.setdefault("baseline", {})[sym] = new_item["baseline"][sym]
            return items[:WATCH_ITEM_MAX_OPEN]
    return ([new_item] + items)[:WATCH_ITEM_MAX_OPEN]


def watch_item_deltas(item: dict, market_data: dict[str, dict]) -> list[dict]:
    """Per-symbol then→now readings for one watch item. Shared by the prompt and
    the email so both quote the same numbers."""
    rows = []
    for sym in item.get("symbols") or []:
        then = (item.get("baseline") or {}).get(sym) or {}
        now = _watch_reading(market_data.get(sym))
        p0, p1 = then.get("current_price"), now.get("current_price")
        rows.append({
            "symbol": sym,
            "then": then,
            "now": now,
            "price_change_pct": (
                round((p1 - p0) / p0 * 100, 2) if p0 and p1 else None
            ),
            "has_data": p1 is not None,
        })
    return rows


def watch_items_prompt_lines(
    items: list[dict], today: str, market_data: dict[str, dict]
) -> list[str]:
    if not items:
        return []
    lines = [
        "",
        "=== OPEN WATCH ITEMS (flagged in prior runs — you MUST report on each) ===",
        "Each item is what a prior run named as the one thing to watch. The then→now "
        "figures are computed in Python from the same market data feeding this prompt, "
        "so they are the record of what actually happened since it was flagged — not a "
        "recollection. Resolve every item explicitly in the SINCE LAST SESSION block "
        "(see WATCH ITEM FOLLOW-THROUGH).",
    ]
    for n, item in enumerate(items, 1):
        age = _days_between(item["flagged_date"], today)
        day_word = "day" if age == 1 else "days"
        restated = item.get("restated_date")
        restated_note = (
            f", restated {restated}"
            if restated and restated != item["flagged_date"] else ""
        )
        lines.append(
            f"\n  [{n}] flagged {item['flagged_date']} ({age} {day_word} ago{restated_note}): "
            f"{item['text']}"
        )
        rows = watch_item_deltas(item, market_data)
        if not rows:
            lines.append(
                "      No symbol was named in this item, so there is nothing tracked for "
                "it — answer it from the market news and MARKET BREADTH data, or say "
                "plainly that it cannot be answered and replace it."
            )
            continue
        for row in rows:
            if not row["has_data"]:
                lines.append(f"      {row['symbol']:<8} no market data today")
                continue
            then, now = row["then"], row["now"]
            lines.append(
                f"      {row['symbol']:<8} "
                f"price ${_fmt(then.get('current_price'), '.2f')} → "
                f"${_fmt(now.get('current_price'), '.2f')} "
                f"({_fmt(row['price_change_pct'], '+.1f')}%) | "
                f"RSI {_fmt(then.get('rsi'), '.1f')} → {_fmt(now.get('rsi'), '.1f')} | "
                f"vs MA50 {_fmt(then.get('price_vs_ma50_pct'), '+.1f')}% → "
                f"{_fmt(now.get('price_vs_ma50_pct'), '+.1f')}%"
            )
    return lines


def breadth_prompt_lines(history: list[dict]) -> list[str]:
    if not history:
        return []
    lines = [
        "",
        "=== MARKET BREADTH — THIS PORTFOLIO, SESSION OVER SESSION ===",
        "Computed in Python from the position list, one row per run. Use this — not a "
        "fresh impression of today's headlines — to decide whether today's mood is a "
        "continuation of the last few sessions or a genuine change (see "
        "SESSION-OVER-SESSION CONTINUITY).",
    ]
    for n, h in enumerate(history):
        marker = "  ← today" if n == len(history) - 1 else ""
        ma50 = (
            f"above MA50 {h['above_ma50']}/{h['with_ma50']}"
            if h.get("with_ma50") else "above MA50 n/a"
        )
        lines.append(
            f"  {h.get('date', '?'):<12} up {h.get('up', 0)} / down {h.get('down', 0)} "
            f"of {h.get('total', 0)}   median {_fmt(h.get('median_pct'), '+.2f')}%   "
            f"{ma50}{marker}"
        )
    streak = describe_breadth_streak(history)
    if streak:
        lines.append(f"  {streak}")
    return lines


# ── Claude analysis ───────────────────────────────────────────────────────────
def build_prompt(summary: dict) -> str:
    day_name, time_str, session, is_stale = get_market_session()
    change_label = "Last session" if is_stale else "Today"

    pending_orders = summary.get("pending_orders", [])
    pending_buy_symbols = {o["symbol"] for o in pending_orders if o["side"] == "buy"}
    pending_sell_symbols = {o["symbol"] for o in pending_orders if o["side"] == "sell"}
    committed_cash = summary.get("committed_cash", 0.0)
    uncommitted_cash = summary.get("uncommitted_cash", summary["cash"])

    lines = [
        f"Portfolio Summary as of {summary['date']} ({day_name}, {time_str} — {session})",
        f"Total Portfolio Value: ${summary['total_value']:.2f}",
        f"Available Cash: ${uncommitted_cash:.2f}",
    ]
    if committed_cash:
        lines.append(
            f"  (${summary['cash']:.2f} total cash, less ${committed_cash:.2f} already "
            f"committed to unfilled buy orders — see PENDING ORDERS. Only the "
            f"${uncommitted_cash:.2f} figure above is deployable; do not treat the "
            f"total as spendable.)"
        )
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

    newsletters = summary.get("newsletters") or []
    if newsletters:
        names = ", ".join(n["name"] for n in newsletters)
        lines += [
            "",
            f"=== TODAY'S MARKET NEWS — SHERWOOD NEWSLETTERS ({names}) ===",
            "PREFERRED SOURCE (see NEWS SOURCE TRUST): Robinhood/Sherwood's own newsletters, "
            "reproduced below as sent. These carry their written analysis — and, in Snacks, "
            "the next session's earnings calendar. Let them set the macro frame and cite their "
            "reasoning where it bears on a position.",
        ]
        for n in newsletters:
            when = (
                "today" if n["age_days"] == 0
                else "yesterday" if n["age_days"] == 1
                else f"{n['age_days']} days ago"
            )
            lines += [
                "",
                f"--- {n['name']} — {n['date']} ({when}) — \"{n['subject']}\" ---",
                n["text"],
            ]
        all_tickers = sorted({t for n in newsletters for t in n["tickers"]})
        if all_tickers:
            lines += [
                "",
                "Tickers named across today's issues (taken from their own quote links, "
                "not inferred): " + ", ".join(all_tickers),
            ]
        lines += newsletter_views.prompt_lines(
            summary.get("newsletter_views") or [],
            {p["symbol"] for p in summary["positions"]} | pending_buy_symbols,
            summary["date"],
        )

    sherwood = summary.get("sherwood_news", [])
    news_source = summary.get("news_source", "")
    if newsletters:
        pass  # Newsletters rendered above; skip the fallback feed section entirely.
    elif sherwood:
        source_label = f" — source: {news_source}" if news_source else ""
        lines += ["", f"=== RECENT MARKET NEWS{source_label} ===",
                  "FALLBACK WIRE FEED (see NEWS SOURCE TRUST) — no recent Snacks issue was "
                  "available, so these are bare headlines from a less-trusted outlet. Use them "
                  "for facts, not for their framing, and keep the macro read tentative.",
                  f"Every headline below was published within the last {NEWS_MAX_AGE_DAYS} days "
                  "and is dated. Use them to identify macro themes, sector trends, and rotation "
                  "opportunities, and ask what they imply for positions not yet in this "
                  "portfolio. Weight a headline by its date — a 3-day-old story may already be "
                  "priced in."]
        for item in sherwood:
            if not isinstance(item, dict):
                lines.append(f"• {item}")
                continue
            age = item.get("age_days")
            if age == 0:
                when = "today"
            elif age == 1:
                when = "yesterday"
            elif age is not None:
                when = f"{age}d ago"
            else:
                when = item.get("published", "undated")
            lines.append(f"• [{when}] {item['title']}")
    else:
        # Say this out loud rather than omitting the section. A missing macro
        # frame that the model cannot see is a missing macro frame it will
        # invent, or silently substitute stale recollection for.
        problems = summary.get("news_problems") or []
        newsletter_problems = summary.get("newsletter_problems") or []
        if newsletter_problems:
            problems = [f"Sherwood {np}" for np in newsletter_problems] + list(problems)
        detail = f" ({'; '.join(problems)})" if problems else ""
        lines += [
            "",
            "=== RECENT MARKET NEWS — NONE AVAILABLE ===",
            f"No market-news feed returned an article published within the last "
            f"{NEWS_MAX_AGE_DAYS} days{detail}. You therefore have NO macro news for "
            "today. Do not infer or assert a macro narrative, sector rotation theme, "
            "or market driver from anything other than the price and indicator data "
            "below, and do not cite headlines — you have none. Say plainly in the "
            "analysis that today's macro read is based on price action alone.",
        ]

    lines += breadth_prompt_lines(summary.get("breadth_history") or [])

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
                    "headline appears above. This script never places trades itself — a prior "
                    "recommendation is NOT evidence it was executed. The prior analysis's text is "
                    "not a record of what happened; the RECENT TRANSACTIONS section below (real "
                    "order history) is the only source of truth for what was actually executed "
                    "today. Check it before describing any position as trimmed or exited — do not "
                    "infer execution from the prior recommendation, and do not assume NO action was "
                    "taken just because a symbol still appears in CURRENT POSITIONS, since a partial "
                    "trim leaves a smaller position rather than removing it. If RECENT TRANSACTIONS "
                    "shows no matching trade, treat the prior recommendation as not yet acted on and "
                    "repeat it rather than describing it as already done.\n"
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

    lines += watch_items_prompt_lines(
        summary.get("watch_items") or [],
        summary["date"],
        summary.get("market_data") or {},
    )

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
    catalyst_tags = newsletter_views.held_catalyst_tags(
        summary.get("newsletter_views") or [],
        {p["symbol"] for p in summary["positions"]},
        summary["date"],
    )
    for pos in summary["positions"]:
        ind = pos.get("indicators", {})
        alloc_pct = pos["equity"] / total_value * 100
        symbol = pos["symbol"]
        is_winner = pos["total_return_pct"] > 0
        if symbol in pending_sell_symbols:
            funding_tag = (
                "NOT FUNDING-ELIGIBLE — an unfilled sell order is already placed on "
                "this position (see PENDING ORDERS); its proceeds are already spoken for"
            )
        elif not is_winner:
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
        giveback = ((summary.get("risk_context") or {}).get("giveback") or {}).get(symbol)
        giveback_tag = (
            f"  [GAIN GIVEBACK ALERT — {giveback['given_back_pct']:.0f}% of the open gain given back]"
            if giveback and giveback["status"] == "alert" else ""
        )
        catalyst_tag = (
            f"  [{catalyst_tags[symbol]} — see THIS WEEK'S CATALYSTS]"
            if symbol in catalyst_tags else ""
        )
        lines += [
            f"\n{symbol}: {pos['shares']} shares @ ${pos['current_price']} "
            f"(avg cost ${pos['avg_cost']}, return {pos['total_return_pct']:+.1f}%, "
            f"equity ${pos['equity']}, {alloc_pct:.1f}% of portfolio)  [{funding_tag}]{small_tag}{ethical_tag}{giveback_tag}{catalyst_tag}",
            f"  RSI: {_fmt(ind.get('rsi'), '.1f')} | "
            f"MA50: ${_fmt(ind.get('ma50'), '.2f')} "
            f"({_fmt(ind.get('price_vs_ma50_pct'), '+.1f')}%) | "
            f"MA200: ${_fmt(ind.get('ma200'), '.2f')} "
            f"({_fmt(ind.get('price_vs_ma200_pct'), '+.1f')}%)",
            f"  {change_label}: {_fmt(ind.get('pct_change_today'), '+.2f')}% | "
            f"Volume ratio: {_fmt(ind.get('volume_ratio'), '.2f')}x",
        ]
        if giveback and giveback["status"] in ("alert", "gone"):
            lines.append(
                "  Gain protection: "
                + portfolio_risk.giveback_summary(symbol, giveback, pos["total_return_pct"])
            )
        sym_news = ticker_news.get(symbol, [])
        if sym_news:
            for item in sym_news:
                title = item["title"] if isinstance(item, dict) else item
                lines.append(f"  News: {title}")
        lines += newsletter_views.symbol_notes(summary.get("newsletter_views") or [], symbol)

    eligible = sorted(
        (p for p in summary["positions"]
         if p["total_return_pct"] > 0
         and p["symbol"] not in trim_warnings
         and p["symbol"] not in recent_buy_symbols
         and p["symbol"] not in pending_sell_symbols
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
        and p["symbol"] not in pending_sell_symbols
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
            expires = (
                date.fromisoformat(c["trim_date"]) + timedelta(days=PROTECTED_COMMITMENT_MAX_DAYS)
            ).isoformat()
            met = (
                "condition MET — reinvest now"
                if current_price is not None and current_price <= c["reinvest_price"]
                else (
                    f"price is above the target. TRIMS stay locked, but buying {c['symbol']} is "
                    f"NOT blocked: judge it like any other buy (normal entry standard). A purchase "
                    f"of about ${c['trim_amount'] * PROTECTED_COMMITMENT_RESTORE_FRACTION:.0f}+ at "
                    f"ANY price restores the position and clears this commitment; it also expires "
                    f"{expires}"
                )
            )
            lines.append(
                f"  {c['symbol']:<8} trimmed ${c['trim_amount']:.2f} on {c['trim_date']}, "
                f"reinvest at/below ${c['reinvest_price']:.2f} "
                f"(current ${_fmt(current_price, '.2f')}) — {met}"
            )

    risk_context = summary.get("risk_context")
    risk_alerts = portfolio_risk.giveback_alerts(risk_context)
    if risk_alerts:
        lines += ["", "=== GAIN GIVEBACK ALERTS ==="]
        position_by_symbol = {p["symbol"]: p for p in summary["positions"]}
        for sym, g in sorted(risk_alerts.items(), key=lambda kv: -kv[1]["given_back_pct"]):
            pos = position_by_symbol.get(sym)
            if pos:
                lines.append(
                    f"⚠ {sym}: "
                    + portfolio_risk.giveback_summary(sym, g, pos["total_return_pct"])
                    + f"; equity ${pos['equity']:.2f}"
                )
    risk_lines = portfolio_risk.risk_text_lines(risk_context, summary["positions"])
    if risk_lines:
        lines += ["", "=== PORTFOLIO RISK CONTEXT (computed in Python) ==="] + risk_lines

    if pending_orders:
        lines += [
            "",
            "=== PENDING ORDERS (placed, NOT yet filled) ===",
            "These orders are already committed but have not executed — typically "
            "placed over a weekend or after hours and queued until the next session "
            "opens. They hold no shares yet, so the buys below do NOT appear in "
            "CURRENT POSITIONS and their symbols are NOT unowned. Treat a pending buy "
            "as already bought: do not recommend buying that symbol again, and do not "
            "describe it as a gap in the portfolio.",
        ]
        for o in pending_orders:
            qty_str = f"{o['quantity']:.4f} shares" if o["quantity"] else "fractional"
            notional_str = f"${o['notional']:.2f}" if o["notional"] is not None else "amount unknown"
            price_str = f" @ ${o['price']:.4f}" if o["price"] is not None else ""
            lines.append(
                f"  {o['side'].upper():<4} {o['symbol']:<8} {qty_str}{price_str} "
                f"({notional_str})  placed {o['date']} — state: {o['state']}"
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

    # Final guard on the "(not in portfolio)" claim in this header. The momentum
    # scan already excludes held and pending-buy symbols upstream, but this
    # section is the one place the prompt asserts a symbol is unowned, so filter
    # again here rather than trusting that the upstream set was complete.
    held_or_pending = {p["symbol"] for p in summary["positions"]} | pending_buy_symbols
    lines += ["", "=== TOP MOMENTUM MOVERS (not in portfolio) ==="]
    for m in summary["momentum"]:
        if m["symbol"] in held_or_pending:
            continue
        lines.append(
            f"\n{m['symbol']}: RSI {_fmt(m.get('rsi'), '.1f')} | "
            f"{change_label} {_fmt(m.get('pct_change_today'), '+.2f')}% | "
            f"Vol ratio {_fmt(m.get('volume_ratio'), '.2f')}x | "
            f"Score {m['score']}"
        )
    if summary.get("newsletter_views"):
        # Last thing the model reads before writing. The system prompt carries the full
        # rules, but on a long prompt the block was skipped outright on a rerun.
        lines += [
            "",
            "=== BEFORE YOU WRITE ===",
            "The newsletters' analysis is in the ANALYST VIEWS section above. Your analysis MUST "
            "contain an **ANALYST VIEWS** block (agree/disagree against this prompt's data, "
            "ending 'changes: <action>' or 'changes: nothing') ahead of OPPORTUNITY COST CHECK. "
            "Any position with a 'Newsletter event' line gets that event weighed in its own "
            "trim/buy/hold reasoning, and a position tagged [REPORTS EARNINGS …] gets the "
            "earnings named in any buy or trim of it.",
        ]
    return "\n".join(lines)


_NOOP_TRIM_RE = re.compile(
    r"^\s*[-*\u2022]*\s*\*\*\s*(?:"
    # "TRIM COST — no action"
    r"(?:TRIM|EXIT|SELL)\s+(?-i:[A-Z][A-Z.\-]{0,9})\s*[\u2014\u2013-]+\s*"
    r"(?:no action|no trim|no change|hold(?:ing)?|not trimming|skipp?(?:ing)?)\b"
    # ...or its rewording "HOLD COST — no trim" (the rule against the first just moves it)
    r"|HOLD\s+(?-i:[A-Z][A-Z.\-]{0,9})\b"
    r")[^*\n]*\*\*",
    re.IGNORECASE,
)
_TRIMS_HEADER_RE = re.compile(r"^[\s#*]*TRIMS\s*/\s*EXITS\b", re.IGNORECASE)
_AFTER_TRIMS_RE = re.compile(
    r"^[\s#*]*(?:BUYS|HOLDS|OPPORTUNITY COST|ANALYST VIEWS|SINCE LAST SESSION|ONE KEY THING)\b",
    re.IGNORECASE,
)


def strip_noop_trim_lines(
    analysis: str,
    keep_symbols: set[str] = frozenset(),
    blocked_symbols: set[str] = frozenset(),
) -> str:
    """
    Remove '**TRIM COST — no action**' (and '**HOLD COST — no trim**') lines from the TRIMS/EXITS block.

    TRIMS/EXITS is for trades. A restricted position (a protected symbol under an
    outstanding commitment, say) gets a bold TRIM header only so the model can say it
    is not trimming it — noise in the email, and it reads like an action. The prompt
    asks the model not to, but the model is the thing that did it, so it is enforced
    here too. `keep_symbols` (symbols with a gain-giveback alert) are left alone: a
    hold-despite-alert must stay written in this block. `blocked_symbols` (protected
    symbols under an outstanding commitment) also lose any plain-prose line that names
    them — the model's second try at the same explanation after being told not to write
    a TRIM line is "No action on COST — it's protected…" — while a bold EXIT/TRIM header
    for one is untouched. If removing lines empties the block, a one-line "nothing"
    statement takes their place.
    """
    lines = analysis.split("\n")
    start = next((i for i, ln in enumerate(lines) if _TRIMS_HEADER_RE.match(ln)), None)
    if start is None:
        return analysis
    end = next(
        (i for i in range(start + 1, len(lines)) if _AFTER_TRIMS_RE.match(lines[i])), len(lines)
    )
    kept, removed = [], []
    for ln in lines[start + 1:end]:
        m = _NOOP_TRIM_RE.match(ln)
        sym = re.search(r"\b(?:TRIM|EXIT|SELL|HOLD)\s+([A-Z][A-Z.\-]{0,9})\b", ln) if m else None
        if m and not (sym and sym.group(1) in keep_symbols):
            removed.append(sym.group(1) if sym else "?")
            continue
        named = next(
            (b for b in blocked_symbols if re.search(rf"\b{re.escape(b)}\b", ln)), None
        )
        if named and ln.strip() and not re.match(r"^\s*[-\u2022]*\s*\*\*", ln):
            removed.append(named)
            continue
        kept.append(ln)
    if not removed:
        return analysis
    log.info(f"Dropped no-action line(s) from TRIMS/EXITS: {removed}")
    if not any(ln.strip() for ln in kept):
        kept = ["", "No trims or exits today.", ""]
    return "\n".join(lines[:start + 1] + kept + lines[end:])


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
    # The TL;DR is generated *after* the analysis blocks (see CLAUDE_SYSTEM_PROMPT) so
    # it summarizes recommendations the model has already committed to, rather than a
    # theme it guessed at before reasoning about any of them — thinking is disabled, so
    # a TL;DR emitted first is literally the model's first tokens on the portfolio, and
    # it used to routinely tell a different story than the advice below it.
    raw = message.content[0].text.strip()
    # Drop a trailing separator the model sometimes appends after the TL;DR; left in
    # place it would make the rsplit below hand back an empty TL;DR.
    raw = re.sub(r"\n-{3,}[ \t]*$", "", raw).strip()
    if "\n---\n" in raw:
        # rsplit, not split: the last separator is the TL;DR boundary, so a stray ---
        # inside the analysis can't steal it.
        analysis_part, tldr = raw.rsplit("\n---\n", 1)
        tldr = tldr.strip()
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
    analysis_part = re.sub(r"\n-{3,}[ \t]*$", "", analysis_part).strip()

    analysis_part = strip_noop_trim_lines(
        analysis_part,
        set(portfolio_risk.giveback_alerts(summary.get("risk_context")) or {}),
        {c["symbol"] for c in summary.get("protected_commitments", [])},
    )
    if summary.get("newsletter_views") and not re.search(r"ANALYST\s+VIEWS", analysis_part, re.IGNORECASE):
        log.warning(
            "Analysis omitted the ANALYST VIEWS block even though "
            f"{sum(len(v['views']) for v in summary['newsletter_views'])} newsletter views were supplied"
        )

    return tldr, analysis_part


# ── Email digest ──────────────────────────────────────────────────────────────
def _held_symbols(summary: dict) -> set[str]:
    """Symbols owned or about to be (unfilled buys), for flagging newsletter items."""
    pending_buys = {o["symbol"] for o in summary.get("pending_orders", []) if o["side"] == "buy"}
    return {p["symbol"] for p in summary["positions"]} | pending_buys


def format_digest(summary: dict, analysis: str) -> str:
    SEP = "=" * 64
    sep = "-" * 64

    tldr = summary.get("tldr", "")
    committed_cash = summary.get("committed_cash", 0.0)
    uncommitted_cash = summary.get("uncommitted_cash", summary["cash"])
    cash_str = f"${uncommitted_cash:.2f}"
    if committed_cash:
        cash_str += f" (of ${summary['cash']:.2f}; ${committed_cash:.2f} on unfilled orders)"
    lines = [
        SEP,
        f"PORTFOLIO DIGEST — {summary['date']}",
        f"Total Value: ${summary['total_value']:.2f}  |  Cash: {cash_str}",
        SEP,
    ]

    newsletters = summary.get("newsletters") or []
    if newsletters:
        lines += ["", "MARKET NEWS — SHERWOOD NEWSLETTERS", sep]
        for n in newsletters:
            lines.append(f"{n['name']} ({n['date']}): {n['subject']}")
        all_tickers = sorted({t for n in newsletters for t in n["tickers"]})
        if all_tickers:
            lines.append(f"Tickers named: {', '.join(all_tickers)}")
        lines += newsletter_views.plain_lines(
            summary.get("newsletter_views") or [], _held_symbols(summary), summary["date"]
        )

    news_problems = summary.get("news_problems") or []
    if not newsletters and not summary.get("sherwood_news") and news_problems:
        lines += [
            "",
            "!! MARKET NEWS UNAVAILABLE",
            sep,
            f"No feed returned an article newer than {NEWS_MAX_AGE_DAYS} days. "
            "Today's analysis had no macro news to work from:",
        ]
        lines += [f"  - {p}" for p in news_problems]

    pending_orders = summary.get("pending_orders", [])
    if pending_orders:
        lines += ["", "PENDING ORDERS (placed, not yet filled)", sep]
        for o in pending_orders:
            qty_str = f"{o['quantity']:.4f} sh" if o["quantity"] else "fractional"
            notional_str = f"${o['notional']:.2f}" if o["notional"] is not None else "—"
            lines.append(
                f"{o['side'].upper():<4} {o['symbol']:<8} {qty_str:>14} {notional_str:>10}  "
                f"placed {o['date']} ({o['state']})"
            )

    if tldr:
        lines += [
            "",
            "TL;DR",
            sep,
            tldr,
            "",
        ]

    watch_items = summary.get("watch_items") or []
    breadth_history = summary.get("breadth_history") or []
    market_data = summary.get("market_data") or {}
    if watch_items or len(breadth_history) > 1:
        lines += ["", "TREND TRACKER", sep]
        if breadth_history:
            b = breadth_history[-1]
            ma50 = (
                f"above MA50 {b['above_ma50']}/{b['with_ma50']}"
                if b.get("with_ma50") else "above MA50 n/a"
            )
            lines.append(
                f"Breadth today: up {b.get('up', 0)} / down {b.get('down', 0)} of "
                f"{b.get('total', 0)} | median {_fmt(b.get('median_pct'), '+.2f')}% | {ma50}"
            )
            for h in breadth_history[:-1]:
                lines.append(
                    f"  {h.get('date', '?')}: up {h.get('up', 0)} / down {h.get('down', 0)} "
                    f"of {h.get('total', 0)}, median {_fmt(h.get('median_pct'), '+.2f')}%"
                )
            streak = describe_breadth_streak(breadth_history)
            if streak:
                lines.append(streak)
        for item in watch_items:
            age = _days_between(item["flagged_date"], summary["date"])
            day_word = "day" if age == 1 else "days"
            lines += ["", f"Watching since {item['flagged_date']} ({age} {day_word} ago):",
                      f"  {item['text']}"]
            for row in watch_item_deltas(item, market_data):
                if not row["has_data"]:
                    lines.append(f"  {row['symbol']:<8} no market data today")
                    continue
                then, now = row["then"], row["now"]
                lines.append(
                    f"  {row['symbol']:<8} "
                    f"${_fmt(then.get('current_price'), '.2f')} → "
                    f"${_fmt(now.get('current_price'), '.2f')} "
                    f"({_fmt(row['price_change_pct'], '+.1f')}%) | "
                    f"RSI {_fmt(then.get('rsi'), '.1f')} → {_fmt(now.get('rsi'), '.1f')} | "
                    f"vs MA50 {_fmt(then.get('price_vs_ma50_pct'), '+.1f')}% → "
                    f"{_fmt(now.get('price_vs_ma50_pct'), '+.1f')}%"
                )

    risk_lines = portfolio_risk.risk_text_lines(summary.get("risk_context"), summary["positions"])
    if risk_lines:
        lines += ["", "PORTFOLIO RISK", sep] + risk_lines

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
    # Headline cash is what's actually deployable — gross cash less anything
    # unfilled buy orders have already committed.
    committed_cash = summary.get("committed_cash", 0.0)
    uncommitted_cash = summary.get("uncommitted_cash", summary["cash"])
    committed_cash_note = (
        f'<span style="display:block;color:#94a3b8;font-size:11px;font-weight:600;'
        f'font-family:Arial,Helvetica,sans-serif;padding-top:4px;">'
        f'of ${summary["cash"]:.2f} — ${committed_cash:.2f} on unfilled orders</span>'
        if committed_cash else ""
    )

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

    # ── Pending orders ───────────────────────────────────────────────────────
    pending_orders = summary.get("pending_orders", [])
    pending_header = "".join([
        _th("Side", align="left"),
        _th("Symbol", align="left"),
        _th("Quantity"),
        _th("Amount"),
        _th("Placed", align="left"),
        _th("State", align="left"),
    ])
    pending_rows = ""
    for o in pending_orders:
        is_buy = o["side"] == "buy"
        pending_rows += (
            "<tr>"
            + _td(_h(o["side"].upper()), align="left", bold=True,
                  color="#16a34a" if is_buy else "#dc2626")
            + _td(_h(o["symbol"]), align="left", bold=True, color="#0f172a")
            + _td(f"{o['quantity']:.4f}" if o["quantity"] else "fractional", mono=True)
            + _td(f"${o['notional']:.2f}" if o["notional"] is not None else "—",
                  mono=True, bold=True, color="#0f172a")
            + _td(_h(o["date"]), align="left", mono=True)
            + _td(_h(o["state"]), align="left")
            + "</tr>"
        )
    pending_content = (
        '<p style="margin:0 0 10px;font-size:13px;color:#64748b;line-height:1.5;">'
        "Already committed but not yet executed — these hold no shares and spend no "
        "cash until they fill, so they do not appear in Current Positions.</p>"
        + _table(pending_header, pending_rows)
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

    # ── Market news ───────────────────────────────────────────────────────────
    news_source = summary.get("news_source", "")
    newsletters = summary.get("newsletters") or []
    sherwood_items = summary.get("sherwood_news", [])
    if newsletters:
        blocks = []
        for n in newsletters:
            when = (
                "today" if n["age_days"] == 0
                else "yesterday" if n["age_days"] == 1
                else f"{n['age_days']}d ago"
            )
            body_html = "".join(
                f'<p style="margin:0 0 8px;font-size:13px;line-height:1.6;'
                f'color:#334155;">{_h(ln)}</p>'
                for ln in n["text"].splitlines() if ln.strip()
            )
            blocks.append(
                '<div style="background:#f0fdf4;border-left:4px solid #16a34a;'
                'padding:12px 16px;border-radius:0 4px 4px 0;margin:0 0 12px;">'
                '<p style="margin:0;font-size:12px;font-weight:700;color:#166534;">'
                f'{_h(n["name"])} — {_h(n["date"])} ({_h(when)})</p>'
                '<p style="margin:4px 0 0;font-size:13px;color:#166534;">'
                f'{_h(n["subject"])}</p></div>'
                f'{body_html}'
            )
        all_tickers = sorted({t for n in newsletters for t in n["tickers"]})
        tickers_html = (
            '<p style="margin:10px 0 0;font-size:12px;color:#475569;">'
            f'<strong>Tickers named:</strong> {_h(", ".join(all_tickers))}</p>'
            if all_tickers else ""
        )
        views_html = newsletter_views.html_block(
            summary.get("newsletter_views") or [], _held_symbols(summary), summary["date"]
        )
        if views_html:
            views_html = (
                '<p style="margin:0 0 4px;font-size:12px;font-weight:700;color:#166534;'
                'text-transform:uppercase;letter-spacing:.04em;">What Sherwood is saying</p>'
                + views_html
                + '<p style="margin:16px 0 4px;font-size:12px;font-weight:700;color:#166534;'
                  'text-transform:uppercase;letter-spacing:.04em;">The issues, as sent</p>'
            )
        sherwood_content = views_html + "".join(blocks) + tickers_html
    elif sherwood_items:
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
        problems = list(summary.get("news_problems") or [])
        nl_problems = summary.get("newsletter_problems") or []
        if nl_problems:
            problems = [f"Sherwood {np}" for np in nl_problems] + problems
        detail = (
            "".join(
                f'<li style="margin:2px 0;">{_h(p)}</li>' for p in problems
            )
            if problems else ""
        )
        sherwood_content = (
            '<div style="background:#fef2f2;border-left:4px solid #dc2626;'
            'padding:12px 16px;border-radius:0 4px 4px 0;">'
            '<p style="margin:0 0 6px;font-size:13px;font-weight:700;color:#991b1b;">'
            f'No market news newer than {NEWS_MAX_AGE_DAYS} days</p>'
            '<p style="margin:0;font-size:12px;color:#7f1d1d;line-height:1.5;">'
            "Today's analysis ran without any macro news. A feed that has stopped "
            "publishing still serves its final articles, so this is checked rather "
            "than assumed.</p>"
            + (f'<ul style="margin:6px 0 0;padding-left:18px;font-size:12px;'
               f'color:#7f1d1d;">{detail}</ul>' if detail else "")
            + "</div>"
        )

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

    # ── Portfolio risk ───────────────────────────────────────────────────────
    # The same lines the analysis was shown, so the reader sees what it saw.
    risk_text = portfolio_risk.risk_text_lines(summary.get("risk_context"), summary["positions"])
    risk_content = (
        '<pre style="margin:0;font-size:12px;line-height:1.5;color:#334155;'
        'font-family:Menlo,Consolas,monospace;white-space:pre-wrap;">'
        + _h("\n".join(risk_text))
        + "</pre>"
    ) if risk_text else ""

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

    # ── Trend tracker ────────────────────────────────────────────────────────
    # What prior runs said they would watch, and what those symbols actually did
    # since — so the follow-through is visible in the digest even if the analysis
    # text is terse about it.
    watch_items = summary.get("watch_items") or []
    breadth_history = summary.get("breadth_history") or []
    watch_market_data = summary.get("market_data") or {}
    trend_content = ""
    if breadth_history:
        b = breadth_history[-1]
        ma50 = (
            f"{b['above_ma50']}/{b['with_ma50']} above MA50"
            if b.get("with_ma50") else "MA50 n/a"
        )
        prior_rows = " &nbsp;·&nbsp; ".join(
            f"{_h(h.get('date', '?'))}: {h.get('up', 0)}↑/{h.get('down', 0)}↓ "
            f"({_fmt(h.get('median_pct'), '+.2f')}%)"
            for h in breadth_history[:-1]
        )
        streak = describe_breadth_streak(breadth_history)
        trend_content += (
            '<p style="margin:0 0 4px;font-size:13px;color:#0f172a;">'
            f'<strong>Breadth today:</strong> {b.get("up", 0)} up / {b.get("down", 0)} down '
            f'of {b.get("total", 0)} &nbsp;·&nbsp; median '
            f'{_fmt(b.get("median_pct"), "+.2f")}% &nbsp;·&nbsp; {_h(ma50)}</p>'
        )
        if prior_rows:
            trend_content += (
                f'<p style="margin:0 0 4px;font-size:11px;color:#6b7280;'
                f'font-family:monospace;">{prior_rows}</p>'
            )
        if streak:
            trend_content += (
                f'<p style="margin:0 0 4px;font-size:12px;color:#b45309;">{_h(streak)}</p>'
            )
    for item in watch_items:
        age = _days_between(item["flagged_date"], summary["date"])
        day_word = "day" if age == 1 else "days"
        trend_content += (
            '<div style="margin:14px 0 0;padding:12px 14px;background:#f8fafc;'
            'border-left:3px solid #f59e0b;border-radius:0 4px 4px 0;">'
            f'<p style="margin:0 0 2px;font-size:10px;font-weight:700;color:#b45309;'
            f'text-transform:uppercase;letter-spacing:0.08em;">Watching since '
            f'{_h(item["flagged_date"])} — {age} {day_word} ago</p>'
            f'<p style="margin:0 0 8px;font-size:13px;color:#334155;line-height:1.5;">'
            f'{_h(item["text"])}</p>'
        )
        rows = watch_item_deltas(item, watch_market_data)
        if not rows:
            trend_content += (
                '<p style="margin:0;font-size:12px;color:#6b7280;">'
                "No symbol named — nothing tracked for this item.</p>"
            )
        else:
            watch_header = "".join([
                _th("Symbol", align="left"),
                _th("Price then &rarr; now"),
                _th("Change"),
                _th("RSI then &rarr; now"),
                _th("vs MA50 then &rarr; now"),
            ])
            watch_rows = ""
            for row in rows:
                then, now = row["then"], row["now"]
                if not row["has_data"]:
                    watch_rows += (
                        "<tr>"
                        + _td(_h(row["symbol"]), align="left", bold=True, color="#0f172a")
                        + '<td colspan="4" style="padding:8px 12px;font-size:12px;'
                          'color:#6b7280;border-bottom:1px solid #f1f5f9;">'
                          "no market data today</td>"
                        + "</tr>"
                    )
                    continue
                chg = row["price_change_pct"]
                watch_rows += (
                    "<tr>"
                    + _td(_h(row["symbol"]), align="left", bold=True, color="#0f172a")
                    + _td(
                        f"${_fmt(then.get('current_price'), '.2f')} &rarr; "
                        f"${_fmt(now.get('current_price'), '.2f')}",
                        mono=True,
                    )
                    + _td(f"{_fmt(chg, '+.1f')}%", color=_color_pct(chg), bold=True, mono=True)
                    + _td(
                        f"{_fmt(then.get('rsi'), '.1f')} &rarr; {_fmt(now.get('rsi'), '.1f')}",
                        color=_color_rsi(now.get("rsi")), mono=True,
                    )
                    + _td(
                        f"{_fmt(then.get('price_vs_ma50_pct'), '+.1f')}% &rarr; "
                        f"{_fmt(now.get('price_vs_ma50_pct'), '+.1f')}%",
                        color=_color_pct(now.get("price_vs_ma50_pct")), mono=True,
                    )
                    + "</tr>"
                )
            trend_content += _table(watch_header, watch_rows)
        trend_content += "</div>"

    # ── Assemble ─────────────────────────────────────────────────────────────
    body_content = (
        tldr_block
        + (_section("Trend Tracker", trend_content)
           if (watch_items or len(breadth_history) > 1) else "")
        + (_section("Pending Orders", pending_content) if pending_orders else "")
        + (_section("Portfolio Risk", risk_content) if risk_content else "")
        + _section("Current Positions", _table(pos_header, pos_rows))
        + _section("Technical Indicators", _table(ind_header, ind_rows))
        + _section("Top Momentum Movers", _table(mom_header, mom_rows))
        + _section("Watchlist Updates", watchlist_content)
        + _section(
            "Claude Analysis",
            f'<div style="font-size:14px;line-height:1.7;color:#1e293b;">{analysis_html}</div>',
        )
        + _section(
            f"Market News{f' — {news_source}' if news_source else ''}",
            sherwood_content,
        )
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
            ${uncommitted_cash:.2f}{committed_cash_note}</td>
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

    # 3c. Fetch orders that are placed but not yet filled. An order placed over a
    # weekend or after hours holds no shares and is not "filled", so without this
    # it is invisible to both reads above — the symbol looks unowned and the cash
    # it has already committed looks spendable.
    try:
        pending_orders = get_pending_orders()
        log.info(f"Fetched {len(pending_orders)} pending (unfilled) orders")
    except Exception as e:
        log.warning(f"Could not fetch pending orders: {e}")
        pending_orders = []

    pending_buy_symbols = {o["symbol"] for o in pending_orders if o["side"] == "buy"}
    pending_sell_symbols = {o["symbol"] for o in pending_orders if o["side"] == "sell"}
    committed_cash = round(
        sum(o["notional"] or 0 for o in pending_orders if o["side"] == "buy"), 2
    )
    if pending_orders:
        log.info(
            f"Pending buys: {sorted(pending_buy_symbols) or 'none'} "
            f"(${committed_cash:.2f} committed) | "
            f"pending sells: {sorted(pending_sell_symbols) or 'none'}"
        )

    # Cash the analysis may actually deploy: gross cash minus what open buy
    # orders have already spoken for. Robinhood does not always deduct a queued
    # order from portfolio_cash, so this has to be netted here.
    uncommitted_cash = round(max(cash - committed_cash, 0.0), 2)
    if committed_cash:
        log.info(
            f"Cash ${cash:.2f} gross, ${committed_cash:.2f} committed to open "
            f"buys, ${uncommitted_cash:.2f} deployable"
        )

    # A symbol with an open buy is already bought as far as any new
    # recommendation is concerned — treat it exactly like a held position so it
    # can never resurface as an "unowned" candidate.
    committed_symbols = portfolio_symbols | pending_buy_symbols

    # 4. Bulk fetch market data for portfolio + screener in two calls
    # Every symbol's readings are also accumulated into one dict so watch-item
    # tracking can look up today's numbers for whatever a prior run flagged,
    # whether or not it is currently held.
    market_data_by_symbol: dict[str, dict] = {}
    try:
        portfolio_data = fetch_bulk_market_data(sorted(portfolio_symbols))
        for pos in positions:
            pos["indicators"] = portfolio_data.get(pos["symbol"], {})
        market_data_by_symbol.update(portfolio_data)
    except Exception as e:
        log.error(f"Portfolio market data failed: {e}")
        send_error_email("fetching portfolio market data", e)
        sys.exit(1)

    try:
        screener_data = fetch_bulk_market_data(
            [t for t in screener_tickers if t not in committed_symbols]
        )
    except Exception as e:
        log.error(f"Screener market data failed: {e}")
        screener_data = {}
    market_data_by_symbol.update(screener_data)

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
        if t not in committed_symbols and t not in screener_data
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
            exclude=committed_symbols | set(screener_tickers)
        )
        all_watchlist_syms = watchlist_syms["user"] + watchlist_syms["robinhood"]
        if all_watchlist_syms:
            log.info(
                f"Fetching market data for {len(all_watchlist_syms)} watchlist tickers "
                f"({len(watchlist_syms['user'])} user, {len(watchlist_syms['robinhood'])} RH)"
            )
            watchlist_market_data = fetch_bulk_market_data(all_watchlist_syms)
            market_data_by_symbol.update(watchlist_market_data)
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
        momentum = run_momentum_scan(committed_symbols, screener_data, screener_tickers)
        log.info(f"Momentum scan complete: {len(momentum)} candidates")
    except Exception as e:
        log.error(f"Momentum scan failed: {e}")
        momentum = []

    # 6b. Portfolio-level risk context — gain giveback per position, correlation
    # clusters (concentration), and performance vs QQQ/SOXX/SPY. Non-critical: on
    # failure the prompt and digest simply omit it. See portfolio_risk.py.
    try:
        risk_context = portfolio_risk.build_risk_context(positions, PRICE_HISTORY)
        alerts = portfolio_risk.giveback_alerts(risk_context)
        if alerts:
            log.info(f"Gain giveback alerts: {sorted(alerts)}")
        concentrated = [c for c in risk_context["concentration"] if c["concentrated"]]
        if concentrated:
            log.info(
                "Concentrated clusters: "
                + "; ".join(f"{c['pct_of_equity']:.0f}% {c['symbols']}" for c in concentrated)
            )
    except Exception as e:
        log.warning(f"Risk context failed: {e}")
        risk_context = None

    # 7. Build summary
    total_equity = sum(p["equity"] for p in positions)
    total_value = round(total_equity + cash, 2)
    # load_last_analysis() already logs which copy it used and that copy's date.
    prior_analysis = load_last_analysis()

    # Continuity state — measured, not remembered. Breadth gives the prompt a
    # multi-session record of the tape so a days-old condition isn't re-narrated
    # as today's discovery; the watch items are what prior runs said they would
    # watch, which the prompt now forces to be reported on with real then→now
    # numbers instead of quietly dropped.
    breadth = compute_breadth(positions, today)
    breadth_history = build_breadth_history(prior_analysis, breadth)
    open_watch_items = carry_forward_watch_items(prior_analysis, today)
    log.info(
        f"Breadth: up {breadth['up']} / down {breadth['down']} of {breadth['total']}, "
        f"median {_fmt(breadth['median_pct'], '+.2f')}% "
        f"({len(breadth_history)} sessions tracked)"
    )
    if open_watch_items:
        log.info(
            "Open watch items: "
            + "; ".join(
                f"{i['flagged_date']} {i.get('symbols') or ['(no symbol)']}"
                for i in open_watch_items
            )
        )

    summary = {
        "date": today,
        "hostname": hostname,
        "total_value": total_value,
        "cash": cash,
        "committed_cash": committed_cash,
        "uncommitted_cash": uncommitted_cash,
        "positions": positions,
        "momentum": momentum,
        "watchlist_candidates": watchlist_candidates,
        "recent_orders": recent_orders,
        "pending_orders": pending_orders,
        "prior_analysis": prior_analysis,
        "ethical_held": ethical_held,
        "market_data": market_data_by_symbol,
        "breadth": breadth,
        "breadth_history": breadth_history,
        "watch_items": open_watch_items,
        "risk_context": risk_context,
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
        log.info("Fetching Sherwood newsletters (preferred market-news source)")
        summary["newsletters"], summary["newsletter_problems"] = fetch_sherwood_newsletters()
    except Exception as e:
        log.warning(f"Newsletter fetch failed: {e}")
        summary["newsletters"], summary["newsletter_problems"] = [], [
            f"fetch raised {type(e).__name__}: {e}"
        ]

    summary["newsletter_views"] = []
    if summary["newsletters"]:
        # These carry Robinhood's own analysis, so the wire feeds are not
        # fetched at all when one is available — they exist only as a fallback.
        summary["sherwood_news"] = []
        summary["news_source"] = " + ".join(n["name"] for n in summary["newsletters"])
        summary["news_problems"] = []
        # Distill the analysis out of the prose (views, retail flow, the earnings
        # calendar). Non-critical: on failure the analysis still gets the raw issues.
        try:
            summary["newsletter_views"] = newsletter_views.extract_all(
                Anthropic(), CLAUDE_MODEL, summary["newsletters"],
                sorted(committed_symbols | set(screener_tickers)),
            )
        except Exception as e:
            log.warning(f"Newsletter view extraction failed: {e}")
    else:
        try:
            log.info("No Sherwood newsletter available — falling back to wire feeds")
            news_items, news_source, news_problems = fetch_market_news()
            summary["sherwood_news"] = news_items
            summary["news_source"] = news_source
            summary["news_problems"] = news_problems
        except Exception as e:
            log.warning(f"Market news fetch failed: {e}")
            summary["sherwood_news"] = []
            summary["news_source"] = ""
            summary["news_problems"] = [f"fetch raised {type(e).__name__}: {e}"]

    try:
        log.info(f"Fetching ticker news for {sorted(portfolio_symbols)}")
        summary["ticker_news"] = fetch_ticker_news(sorted(portfolio_symbols))
    except Exception as e:
        log.warning(f"Ticker news fetch failed: {e}")
        summary["ticker_news"] = {}

    try:
        save_news_cache(
            today,
            summary["sherwood_news"],
            summary["ticker_news"],
            source=summary.get("news_source", ""),
            problems=summary.get("news_problems"),
            newsletters=summary.get("newsletters"),
            newsletter_views_data=summary.get("newsletter_views"),
        )
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

        # Snapshot today's "ONE KEY THING TO WATCH" before saving, so tomorrow's
        # run gets both the sentence and the readings it was flagged against. A
        # restatement of an already-open item keeps that item's original
        # baseline (see merge_watch_item) — that is what makes a multi-session
        # trend measurable instead of resetting to zero every morning.
        try:
            still_open, closed = apply_watch_verdicts(analysis, open_watch_items, today)
            for item, verdict in closed:
                log.info(
                    f"Watch item from {item['flagged_date']} closed as {verdict}: "
                    f"{item.get('symbols') or '(no symbol)'}"
                )
            new_watch = extract_watch_item(
                analysis,
                set(market_data_by_symbol) | committed_symbols,
                today,
                market_data_by_symbol,
            )
            summary["watch_items_next"] = merge_watch_item(still_open, new_watch)
            if new_watch:
                log.info(
                    f"Watch item recorded: {new_watch['symbols'] or '(no symbol named)'} "
                    f"— {new_watch['text'][:120]}"
                )
            else:
                log.warning("No ONE KEY THING TO WATCH line found in analysis — nothing new to track")
        except Exception as watch_err:
            log.warning(f"Watch item extraction failed: {watch_err}")
            summary["watch_items_next"] = open_watch_items

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
