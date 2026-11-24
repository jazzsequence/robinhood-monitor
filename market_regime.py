"""
Market regime, multi-day dip test, and buy-tranche schedule.

Why this exists (2026-11-24): the daily analysis kept buying on green days and
"waiting" on red ones, inside a drawdown it never saw. The prompt carried per-stock
RSI/MA50 and a 5-session breadth table, but nothing that said where the *indexes* were
(QQQ -16.6% from its high, SOXX -41%, VIX 29), so every morning read as a fresh
chop-with-bounces tape. Three Python-measured pieces fix that, same pattern as trim
warnings and protected commitments — the model reads numbers, it does not form an
impression:

1. REGIME — index drawdown from the 52-week high, index vs its 50/200-day average, VIX vs
   its own history, the portfolio's drawdown from its peak, how many holdings sit under
   their own long averages, and how violent the last 20 sessions were. Classified
   UPTREND / CORRECTION / DOWNTREND from a score of those conditions.
2. STABILIZATION — a dip is not buyable on the strength of today's close. A name must have
   stopped making new lows (no new closing low in the last DIP_BASE_SESSIONS) and must not
   be a one-day relief spike. This replaces the old test (down >= 3% today on heavy volume
   => WAIT, otherwise buy), which fired WAIT on red days and CLEAN on green ones — i.e. it
   bought bounces and waited on drops.
3. BUY TRANCHE — in a CORRECTION or DOWNTREND new money goes in on a schedule (one small
   tranche every N trading sessions) instead of every session the cash allows.

All thresholds are mine, not Chris's, chosen after seeing the Oct-Nov 2026 drawdown
(in-sample). They are named constants so they can be moved without touching the logic.
Everything is non-critical: callers wrap it in try/except and the prompt omits what could
not be computed (which also switches the tranche cap off — no regime, no schedule).
"""
from __future__ import annotations

import logging
from datetime import date

import numpy as np
import pandas as pd
import yfinance as yf

import portfolio_risk
from portfolio_risk import _naive_index, _pct

log = logging.getLogger(__name__)

REGIME_INDEXES = ("SPY", "QQQ", "SOXX")
VIX_SYMBOL = "^VIX"

# ── Regime classification ────────────────────────────────────────────────────
# One point per condition that holds. >= DOWNTREND_SCORE is a DOWNTREND, >= CORRECTION_SCORE
# a CORRECTION, otherwise UPTREND (the normal state, no cap on buys). On the 2026-11-24 data
# this scores 5/5 (SPY and QQQ under their 200-day averages, QQQ -16.6% from its high, VIX
# 28.8 vs 18.4, 15 of 16 holdings under their MA50).
INDEX_DRAWDOWN_POINT_PCT = 10.0   # QQQ (the broad growth proxy) this far under its 52w high
VIX_ELEVATED = 25.0               # absolute level...
VIX_RATIO_POINT = 1.3             # ...or this multiple of its 60-session average
HOLDINGS_BELOW_MA50_SHARE = 0.6   # share of holdings under their own 50-day average
CORRECTION_SCORE = 2
DOWNTREND_SCORE = 4
# "Violent" sessions: portfolio back-cast moved at least this much in a day.
WHIPSAW_DAY_PCT = 3.0
TAPE_WINDOW = 20
# A net move this large over TAPE_WINDOW is a trend, however violent the individual days — which is
# what the 2026-11-24 digests missed: a -14% month with big bounces in it was narrated as "chop".
DIRECTIONAL_NET_PCT = 8.0

# ── Buy tranche schedule ─────────────────────────────────────────────────────
# (tranche as % of total portfolio value, minimum trading sessions between buys).
# 3% of an ~$840 account is ~$25 — the size the buys here have actually been. Applies to the
# session's TOTAL buy dollars whatever funds them (cash or trim proceeds). UPTREND = no cap.
TRANCHE_SCHEDULE = {
    "CORRECTION": (5.0, 2),
    "DOWNTREND": (3.0, 3),
}

# ── Dip stabilization ────────────────────────────────────────────────────────
DIP_MAX_RSI = 50.0
DIP_HIGH_DRAWDOWN_PCT = 10.0   # or this far under the 60-session high
DIP_SCALE_IN_PCT = 15.0        # more than this far under MA50: commit at most half the tranche
DIP_BASE_SESSIONS = 5          # no new closing low in this many sessions = the low is holding
DIP_LOW_LOOKBACK = 20          # ...measured against the lowest close of the prior N sessions
DIP_BOUNCE_DAY_PCT = 4.0       # a day up this much while still under the short average...
DIP_SHORT_MA = 10              # ...is a relief spike, not a base
HIGH_WINDOW = 60


def _closes(series: pd.Series | None) -> pd.Series | None:
    if series is None:
        return None
    s = _naive_index(series.dropna())
    return s if len(s) else None


# ── 1. Regime ────────────────────────────────────────────────────────────────
def fetch_regime_history() -> dict[str, pd.Series]:
    """~2y of daily closes for the regime indexes and the VIX. Empty dict on failure.

    2y (not the 1y the benchmarks use) so a 52-week high is a real 52-week high even when
    the data window starts mid-rally, and the VIX percentile has a year behind it."""
    symbols = list(REGIME_INDEXES) + [VIX_SYMBOL]
    try:
        raw = yf.download(symbols, period="2y", auto_adjust=True, progress=False)["Close"]
    except Exception as e:
        log.warning(f"Regime index download failed: {e}")
        return {}
    return {s: raw[s].dropna() for s in symbols if s in raw and not raw[s].dropna().empty}


def _index_row(closes: pd.Series) -> dict:
    s = _naive_index(closes.dropna())
    last = float(s.iloc[-1])
    year = s.tail(252)
    hi = float(year.max())

    def vs_avg(n: int) -> float | None:
        return round(_pct(last, float(s.tail(n).mean())), 1) if len(s) >= n else None

    def ret(n: int) -> float | None:
        return round(_pct(last, float(s.iloc[-1 - n])), 1) if len(s) > n else None

    return {
        "last": round(last, 2),
        "high_52w": round(hi, 2),
        "high_date": year.idxmax().date().isoformat(),
        "drawdown_pct": round(_pct(last, hi), 1),
        "vs_ma50_pct": vs_avg(50),
        "vs_ma200_pct": vs_avg(200),
        "ret_1m": ret(21), "ret_3m": ret(63), "ret_6m": ret(126),
    }


def _portfolio_value(positions: list[dict], histories: dict[str, pd.Series]) -> pd.Series | None:
    """Back-cast daily value of the CURRENT holdings (measures the picks, not the timing)."""
    shares = {p["symbol"]: p["shares"] for p in positions if p["symbol"] in histories}
    if not shares:
        return None
    closes = portfolio_risk._aligned_closes(list(shares), histories, 70)
    if closes.empty:
        return None
    return (closes * pd.Series(shares)[closes.columns]).sum(axis=1)


def compute_regime(
    positions: list[dict],
    histories: dict[str, pd.Series],
    index_hist: dict[str, pd.Series],
) -> dict | None:
    """Measure the market and portfolio state and classify it. None when no index data."""
    idx = {s: _index_row(index_hist[s]) for s in REGIME_INDEXES if s in index_hist}
    if "QQQ" not in idx and "SPY" not in idx:
        return None

    vix = None
    if VIX_SYMBOL in index_hist:
        v = _naive_index(index_hist[VIX_SYMBOL].dropna())
        avg60 = float(v.tail(60).mean())
        vix = {
            "last": round(float(v.iloc[-1]), 1),
            "avg_60d": round(avg60, 1),
            "ratio": round(float(v.iloc[-1]) / avg60, 2) if avg60 else None,
            "high_1y": round(float(v.tail(252).max()), 1),
            "high_1y_date": v.tail(252).idxmax().date().isoformat(),
        }

    below50 = below200 = with50 = with200 = 0
    for p in positions:
        ind = p.get("indicators") or {}
        if ind.get("price_vs_ma50_pct") is not None:
            with50 += 1
            below50 += ind["price_vs_ma50_pct"] < 0
        if ind.get("price_vs_ma200_pct") is not None:
            with200 += 1
            below200 += ind["price_vs_ma200_pct"] < 0

    port = {"drawdown_pct": None}
    tape = None
    value = _portfolio_value(positions, histories)
    if value is not None and len(value) > TAPE_WINDOW:
        value = _naive_index(value)
        peak = value.max()
        port = {
            "drawdown_pct": round(_pct(float(value.iloc[-1]), float(peak)), 1),
            "peak_date": value.idxmax().date().isoformat(),
            "ret_20d": round(_pct(float(value.iloc[-1]), float(value.iloc[-1 - TAPE_WINDOW])), 1),
        }
        moves = value.pct_change().dropna().tail(TAPE_WINDOW) * 100
        big = moves[moves.abs() >= WHIPSAW_DAY_PCT]
        # A reversal is a >= WHIPSAW_DAY_PCT day whose sign differs from the previous big day's.
        reversals = int(((np.sign(big).diff().abs()) == 2).sum()) if len(big) > 1 else 0
        net = round(_pct(float(value.iloc[-1]), float(value.iloc[-1 - TAPE_WINDOW])), 1)
        character = (
            "DIRECTIONAL DECLINE" if net <= -DIRECTIONAL_NET_PCT
            else "DIRECTIONAL ADVANCE" if net >= DIRECTIONAL_NET_PCT
            else "CHOPPY" if len(big) >= 3
            else "QUIET"
        )
        tape = {
            "character": character,
            "sessions": int(len(moves)),
            "up_days": int((moves > 0).sum()),
            "down_days": int((moves < 0).sum()),
            "big_days": int(len(big)),
            "big_up": int((big > 0).sum()),
            "big_down": int((big < 0).sum()),
            "reversals": reversals,
            "net_pct": net,
            "weekly": _weekly_returns(value),
        }

    score, reasons = 0, []
    for name in ("SPY", "QQQ"):
        row = idx.get(name)
        if row and row["vs_ma200_pct"] is not None and row["vs_ma200_pct"] < 0:
            score += 1
            reasons.append(f"{name} under its 200-day average ({row['vs_ma200_pct']:+.1f}%)")
    q = idx.get("QQQ")
    if q and q["drawdown_pct"] <= -INDEX_DRAWDOWN_POINT_PCT:
        score += 1
        reasons.append(f"QQQ {q['drawdown_pct']:.1f}% under its 52-week high")
    if vix and (vix["last"] >= VIX_ELEVATED or (vix["ratio"] or 0) >= VIX_RATIO_POINT):
        score += 1
        reasons.append(f"VIX {vix['last']} vs its 60-day average {vix['avg_60d']}")
    if with50 and below50 / with50 >= HOLDINGS_BELOW_MA50_SHARE:
        score += 1
        reasons.append(f"{below50} of {with50} holdings under their own 50-day average")

    label = "DOWNTREND" if score >= DOWNTREND_SCORE else "CORRECTION" if score >= CORRECTION_SCORE else "UPTREND"
    return {
        "label": label, "score": score, "max_score": 5, "reasons": reasons,
        "indexes": idx, "vix": vix, "portfolio": port, "tape": tape,
        "holdings": {"below_ma50": below50, "with_ma50": with50, "below_ma200": below200, "with_ma200": with200},
    }


def _weekly_returns(value: pd.Series, weeks: int = 8) -> list[dict]:
    """Back-cast portfolio return per calendar week, newest last — a multi-week anchor that
    one day's bounce cannot move."""
    weekly = value.resample("W-FRI").last().dropna()
    rets = weekly.pct_change().dropna().tail(weeks) * 100
    last = value.index[-1]
    # The newest bucket is a partial week whenever the data ends before that Friday.
    return [
        {"week_ending": d.date().isoformat(), "pct": round(float(r), 1), "partial": bool(d > last)}
        for d, r in rets.items()
    ]


def regime_lines(regime: dict | None) -> list[str]:
    """The REGIME block — shared by the prompt and both digests."""
    if not regime:
        return []
    lines = [
        f"Classification: {regime['label']} (score {regime['score']}/{regime['max_score']}: "
        + ("; ".join(regime["reasons"]) if regime["reasons"] else "no stress conditions hold") + ")",
        "Indexes (drawdown is from the 52-week closing high; avg = simple moving average):",
        f"  {'':<6}{'last':>9}{'from high':>11}{'vs MA50':>9}{'vs MA200':>10}{'1m':>8}{'3m':>8}{'6m':>8}",
    ]
    for name, r in regime["indexes"].items():
        def f(v, spec="+.1f"):
            return f"{format(v, spec)}%" if v is not None else "n/a"
        lines.append(
            f"  {name:<6}{r['last']:>9.2f}{f(r['drawdown_pct']):>11}{f(r['vs_ma50_pct']):>9}"
            f"{f(r['vs_ma200_pct']):>10}{f(r['ret_1m']):>8}{f(r['ret_3m']):>8}{f(r['ret_6m']):>8}"
            f"   (high {r['high_date']})"
        )
    v = regime.get("vix")
    if v:
        lines.append(
            f"VIX: {v['last']} (60-day average {v['avg_60d']}, {v['ratio']:.2f}x; "
            f"1y high {v['high_1y']} on {v['high_1y_date']})"
        )
    h = regime["holdings"]
    if h["with_ma50"]:
        lines.append(
            f"Holdings under their own 50-day average: {h['below_ma50']}/{h['with_ma50']}; "
            f"under their 200-day: {h['below_ma200']}/{h['with_ma200']}"
        )
    p = regime.get("portfolio") or {}
    if p.get("drawdown_pct") is not None:
        lines.append(
            f"Portfolio (current holdings back-cast): {p['drawdown_pct']:+.1f}% from its peak "
            f"on {p['peak_date']}; {p['ret_20d']:+.1f}% over the last {TAPE_WINDOW} sessions"
        )
    t = regime.get("tape")
    if t:
        lines.append(
            f"Last {t['sessions']} sessions: {t['up_days']} up / {t['down_days']} down; "
            f"{t['big_days']} days moved >= {WHIPSAW_DAY_PCT:.0f}% ({t['big_up']} up, {t['big_down']} down), "
            f"{t['reversals']} sign reversals between them; net {t['net_pct']:+.1f}%"
        )
        lines.append(
            f"Tape character: {t['character']}"
            + (" — big days inside a trend are bounces or drops within it, not chop"
               if t["character"].startswith("DIRECTIONAL") else "")
        )
        if t.get("weekly"):
            lines.append(
                "Weekly portfolio returns, oldest to newest: "
                + ", ".join(
                    f"{w['pct']:+.1f}% (wk {w['week_ending'][5:]}{', so far' if w.get('partial') else ''})"
                    for w in t["weekly"]
                )
            )
    return lines


# ── 3. Buy tranche schedule ──────────────────────────────────────────────────
def _trading_days_since(then: date, today: date) -> int:
    return int(np.busday_count(then.isoformat(), today.isoformat()))


def buy_budget(
    regime: dict | None,
    total_value: float,
    recent_orders: list[dict],
    pending_orders: list[dict],
    today: str,
) -> dict | None:
    """The session's regime buy cap, or None when no cap applies (UPTREND / no regime).

    {regime, tranche_usd, spacing, last_buy, sessions_since, due, budget_usd, next_due}
    budget_usd is the cap on NET NEW money (buy dollars beyond same-session sale proceeds)
    this session: the tranche when one is due, else 0. The last buy is the newest filled or pending buy of ANY symbol — including ones
    made by hand, which is what the order history reports."""
    if not regime or regime["label"] not in TRANCHE_SCHEDULE:
        return None
    pct, spacing = TRANCHE_SCHEDULE[regime["label"]]
    tranche = round(total_value * pct / 100, 2)
    now = date.fromisoformat(today)
    if any(o["side"] == "buy" for o in pending_orders):
        last, since = now, 0
    else:
        buys = [date.fromisoformat(o["date"]) for o in recent_orders if o["side"] == "buy" and o.get("date")]
        last = max(buys) if buys else None
        since = _trading_days_since(last, now) if last else None
    due = since is None or since >= spacing
    next_due = None
    if not due and last is not None:
        next_due = np.busday_offset(last.isoformat(), spacing, roll="forward").astype(str)
    return {
        "regime": regime["label"], "tranche_usd": tranche, "tranche_pct": pct, "spacing": spacing,
        "last_buy": last.isoformat() if last else None, "sessions_since": since,
        "due": due, "budget_usd": tranche if due else 0.0, "next_due": str(next_due) if next_due else None,
    }


def buy_budget_lines(b: dict | None) -> list[str]:
    if not b:
        return []
    last = f"last buy {b['last_buy']} ({b['sessions_since']} trading sessions ago)" if b["last_buy"] else "no buy in the order history"
    head = (
        f"REGIME BUY CAP: {b['regime']} — caps NET NEW MONEY only (buys beyond the proceeds of sales "
        f"written in TRIMS/EXITS this session); a buy funded by trimming a winner is a rotation and is "
        f"not capped. New money comes in one tranche per {b['spacing']} trading sessions, "
        f"{b['tranche_pct']:.0f}% of the portfolio (${b['tranche_usd']:.2f}) at a time; {last}."
    )
    if b["due"]:
        return [head, f"A tranche IS due: net new money this session may not exceed ${b['budget_usd']:.2f}, "
                      f"and adding it needs a very good, named reason."]
    return [head, f"NO tranche is due today (next one {b['next_due']}): net new money this session is $0.00 — "
                  f"total BUYS may not exceed the sale proceeds in TRIMS/EXITS."]


# ── 2. Stabilization / dip candidates ────────────────────────────────────────
def stabilization(closes: pd.Series | None) -> dict | None:
    """Has this name stopped making new lows? Pure function of the close history.

    new_low_recent: some close in the last DIP_BASE_SESSIONS undercut the lowest close of the
    DIP_LOW_LOOKBACK sessions before them — it is still making lows, whatever today did.
    relief_spike: up >= DIP_BOUNCE_DAY_PCT today while under its DIP_SHORT_MA-day average — a
    one-day squeeze, not a base."""
    s = _closes(closes)
    need = DIP_BASE_SESSIONS + DIP_LOW_LOOKBACK + 1
    if s is None or len(s) < need:
        return None
    recent = s.iloc[-DIP_BASE_SESSIONS:]
    prior_low = float(s.iloc[-(DIP_BASE_SESSIONS + DIP_LOW_LOOKBACK):-DIP_BASE_SESSIONS].min())
    new_low = float(recent.min()) < prior_low
    low_date = recent.idxmin().date().isoformat() if new_low else None
    today_pct = _pct(float(s.iloc[-1]), float(s.iloc[-2]))
    under_short = float(s.iloc[-1]) < float(s.tail(DIP_SHORT_MA).mean())
    return {
        "new_low_recent": new_low,
        "new_low_date": low_date,
        "relief_spike": today_pct >= DIP_BOUNCE_DAY_PCT and under_short,
        "today_pct": round(today_pct, 2),
    }


def dip_candidates(
    positions: list[dict],
    histories: dict[str, pd.Series],
    exclude: set[str] = frozenset(),
) -> list[dict]:
    """Held names meeting the numeric DIP definition, best first.

    A dip is RSI below DIP_MAX_RSI AND either at/under its MA50 or DIP_HIGH_DRAWDOWN_PCT
    under its 60-session high. status is one of
      WAIT_LOWS — still making new closing lows (any in the last DIP_BASE_SESSIONS sessions)
      BOUNCE    — a one-day relief spike under its short average; do not buy the squeeze
      SCALE_IN  — stabilized but > DIP_SCALE_IN_PCT under MA50: commit at most half the tranche
      CLEAN     — stabilized
    The old 1-day knife check is gone: it said WAIT on red days and CLEAN on green ones."""
    out = []
    for p in positions:
        sym = p["symbol"]
        ind = p.get("indicators") or {}
        rsi, vs50 = ind.get("rsi"), ind.get("price_vs_ma50_pct")
        if sym in exclude or rsi is None or vs50 is None:
            continue
        closes = _closes(histories.get(sym))
        below_high = None
        if closes is not None and len(closes) >= 2:
            below_high = round(_pct(float(closes.iloc[-1]), float(closes.tail(HIGH_WINDOW).max())), 1)
        if rsi >= DIP_MAX_RSI or not (vs50 <= 0 or (below_high is not None and below_high <= -DIP_HIGH_DRAWDOWN_PCT)):
            continue
        stab = stabilization(histories.get(sym)) or {}
        if stab.get("new_low_recent"):
            status = "WAIT_LOWS"
        elif stab.get("relief_spike"):
            status = "BOUNCE"
        elif vs50 <= -DIP_SCALE_IN_PCT:
            status = "SCALE_IN"
        else:
            status = "CLEAN"
        out.append({
            "symbol": sym, "rsi": round(rsi, 1), "vs_ma50_pct": round(vs50, 1),
            "below_high_pct": below_high, "today_pct": ind.get("pct_change_today"),
            "return_pct": p.get("total_return_pct"), "status": status,
            "new_low_date": stab.get("new_low_date"), "stabilization_known": bool(stab),
        })
    order = {"CLEAN": 0, "SCALE_IN": 1, "BOUNCE": 2, "WAIT_LOWS": 3}
    return sorted(out, key=lambda d: (order[d["status"]], d["vs_ma50_pct"]))


def dip_candidate_lines(cands: list[dict]) -> list[str]:
    notes = {
        "CLEAN": "has stopped making new lows — passes the numeric dip test",
        "SCALE_IN": f"has stopped making new lows, but is more than {DIP_SCALE_IN_PCT:.0f}% under MA50 — "
                    "commit at most about half this session's tranche",
        "BOUNCE": f"one-day relief spike (up >= {DIP_BOUNCE_DAY_PCT:.0f}% today, still under its "
                  f"{DIP_SHORT_MA}-day average) — do not buy the squeeze",
        "WAIT_LOWS": "still making new closing lows — wait for a higher low (no new low for "
                     f"{DIP_BASE_SESSIONS} sessions)",
    }
    lines = []
    for c in cands:
        high = f", {abs(c['below_high_pct']):.1f}% under its 60-session high" if c["below_high_pct"] is not None else ""
        today = f", today {c['today_pct']:+.2f}%" if c["today_pct"] is not None else ""
        low = f" (last new low {c['new_low_date']})" if c.get("new_low_date") else ""
        lines.append(
            f"  {c['symbol']:<7} RSI {c['rsi']}, {c['vs_ma50_pct']:+.1f}% vs MA50{high}{today}, "
            f"{c['return_pct']:+.1f}% on cost — {notes[c['status']]}{low}"
        )
    return lines
