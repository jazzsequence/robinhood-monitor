"""
Portfolio-level risk context for the daily digest.

Three pieces of Python-computed state, same pattern as trim warnings and
protected-symbol commitments: the analysis call runs with thinking disabled and
sees one day's snapshot, so anything it has to *reason from* is measured here
rather than left to its impression.

1. Gain giveback — how much of each position's open gain has been surrendered
   since its peak. This is the "don't ride a winner all the way back down" signal.
   A 1-year backtest of the current holdings (exit once a position that peaked
   >=20% above cost gives back 33% of that open gain) beat buy-and-hold
   (+94% vs +89%) and cut the worst drawdown from -28% to -19%; the exit was
   followed by a lower price 40 sessions later in ~80% of cases. A portfolio-level
   trailing stop, by contrast, whipsawed badly (it gave up half or more of the
   return) — which is why this works per position and only flags, never sells.
2. Benchmarks — portfolio vs QQQ (Nasdaq-100), SOXX (semiconductors) and SPY
   (S&P 500), plus beta, so "this is just sector drag" is checked against numbers.
3. Correlation clusters — groups of holdings that actually move together,
   found from return correlations rather than a hard-coded sector list, so any
   concentration (semis today, something else tomorrow) is surfaced.

Everything here is non-critical: callers wrap it in try/except and the digest
simply omits what could not be computed.
"""
from __future__ import annotations

import logging
from datetime import date

import numpy as np
import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

# A position needs to have been this far above cost at its peak for there to be
# a gain worth protecting at all (a +3% blip that fades is not a giveback).
GIVEBACK_MIN_PEAK_GAIN_PCT = 20.0
# Share of that open gain (peak - cost) that, once surrendered, raises an alert.
# 33% is the middle of the 25/33/50 range backtested: 25% fired more often for
# little extra protection, 50% let more of the gain go before flagging.
GIVEBACK_ALERT_PCT = 33.0

BENCHMARKS = ("QQQ", "SOXX", "SPY")
BENCHMARK_WINDOWS = {"5d": 5, "1m": 21, "3m": 63}  # in trading sessions
BETA_WINDOW = 60

# Correlation clustering. Average-linkage at 0.45 over ~6 months of daily returns
# recovered the real semis group (10 names, no sector list) plus the smaller
# natural pairs on this portfolio; single-linkage chained unrelated names together.
CLUSTER_CORR_THRESHOLD = 0.45
CLUSTER_LOOKBACK_SESSIONS = 126
CLUSTER_MIN_HISTORY = 100  # a name with less history than this is left out
CLUSTER_REPORT_MIN_PCT = 10.0  # don't list clusters smaller than this share of equity
CONCENTRATION_ALERT_PCT = 25.0
CLUSTER_HIGH_WINDOW = 60  # sessions for the "below its recent high" figure


# ── Helpers ──────────────────────────────────────────────────────────────────
def _naive_index(closes: pd.Series) -> pd.Series:
    """Daily bars with a tz-naive DatetimeIndex, so date comparisons are safe."""
    idx = pd.DatetimeIndex(closes.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    out = closes.copy()
    out.index = idx
    return out


def _parse_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _pct(a: float, b: float) -> float:
    return (a / b - 1) * 100


# ── 1. Gain giveback ─────────────────────────────────────────────────────────
def compute_giveback(
    avg_cost: float,
    current_price: float,
    closes: pd.Series | None,
    opened_at: str | None = None,
) -> dict | None:
    """
    How much of a position's open gain has been surrendered since its peak.

    The peak is the highest close since the position was opened (`opened_at`,
    Robinhood's position creation timestamp) so a price spike *before* the buy
    can never count as a gain the holder had. Without an open date the 1-year
    window is used. Returns None when there is not enough data to say anything.

    status:
      "alert" — peaked >= GIVEBACK_MIN_PEAK_GAIN_PCT above cost, has surrendered
                >= GIVEBACK_ALERT_PCT of that open gain, and is still above cost.
                Still above cost is deliberate: an alert on a position already
                below cost would just be a recommendation to sell at a loss, which
                COST BASIS DISCIPLINE separately and rightly restrains.
      "gone"  — peaked >= the minimum but has surrendered all of it (now at or
                below cost). Informational: that horse has already left.
      "ok"    — anything else (never had a protectable gain, or has kept most of it).
    """
    if not avg_cost or avg_cost <= 0 or not current_price or closes is None or len(closes) < 2:
        return None

    closes = _naive_index(closes.dropna())
    opened = _parse_date(opened_at)
    window = closes[closes.index >= pd.Timestamp(opened)] if opened else closes
    if len(window) < 2:
        return None

    peak_price = float(window.max())
    peak_date = window.idxmax().date().isoformat()
    if current_price > peak_price:  # live quote above the last daily close
        peak_price, peak_date = float(current_price), date.today().isoformat()

    peak_gain_pct = round(_pct(peak_price, avg_cost), 1)
    result = {
        "peak_price": round(peak_price, 4),
        "peak_date": peak_date,
        "peak_gain_pct": peak_gain_pct,
        "given_back_pct": None,
        "status": "ok",
    }
    if peak_price <= avg_cost:
        return result

    given_back = (peak_price - current_price) / (peak_price - avg_cost) * 100
    result["given_back_pct"] = round(max(given_back, 0.0), 1)
    if peak_gain_pct >= GIVEBACK_MIN_PEAK_GAIN_PCT:
        if current_price <= avg_cost:
            result["status"] = "gone"
        elif given_back >= GIVEBACK_ALERT_PCT:
            result["status"] = "alert"
    return result


# ── 2. Correlation clusters (concentration) ──────────────────────────────────
def correlation_clusters(returns: pd.DataFrame, threshold: float = CLUSTER_CORR_THRESHOLD) -> list[list[str]]:
    """Greedy average-linkage clustering: keep merging the two clusters with the
    highest mean pairwise correlation until none clears `threshold`."""
    cols = list(returns.columns)
    corr = returns.corr().values
    clusters = [[i] for i in range(len(cols))]
    while len(clusters) > 1:
        best, pair = threshold, None
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                avg = corr[np.ix_(clusters[i], clusters[j])].mean()
                if avg >= best:
                    best, pair = avg, (i, j)
        if pair is None:
            break
        i, j = pair
        clusters[i] += clusters[j]
        del clusters[j]
    return [[cols[k] for k in c] for c in clusters]


def _best_tracker(value: pd.Series, bench: dict[str, pd.Series]) -> tuple[str, float, float | None] | None:
    """The benchmark a series moves most like, found from return correlation — so
    a cluster is compared against the yardstick that actually describes it, with
    no hard-coded 'semis go with SOXX'. Returns (name, correlation, benchmark 5d %)."""
    ret = value.pct_change().dropna().tail(BETA_WINDOW)
    best = None
    for name, series in bench.items():
        s = _naive_index(series.dropna())
        joined = pd.concat([ret, s.pct_change().dropna()], axis=1, join="inner").dropna()
        if len(joined) < 30:
            continue
        corr = float(joined.iloc[:, 0].corr(joined.iloc[:, 1]))
        if best is None or corr > best[1]:
            five = round(_pct(float(s.iloc[-1]), float(s.iloc[-6])), 1) if len(s) > 5 else None
            best = (name, round(corr, 2), five)
    return best


def _aligned_closes(symbols: list[str], histories: dict[str, pd.Series], min_len: int) -> pd.DataFrame:
    frames = {
        s: _naive_index(histories[s].dropna())
        for s in symbols
        if s in histories and len(histories[s].dropna()) >= min_len
    }
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, axis=1).ffill().dropna()


def compute_concentration(
    positions: list[dict], histories: dict[str, pd.Series], bench: dict[str, pd.Series] | None = None
) -> list[dict]:
    """Clusters of co-moving holdings, largest first, each with its share of
    equity, recent return, distance below its own recent high, and (given
    benchmark history) the benchmark it tracks and how it did against it."""
    total_equity = sum(p["equity"] for p in positions) or 1
    shares = {p["symbol"]: p["shares"] for p in positions}
    equity = {p["symbol"]: p["equity"] for p in positions}

    closes = _aligned_closes(list(shares), histories, CLUSTER_MIN_HISTORY).tail(CLUSTER_LOOKBACK_SESSIONS + 1)
    if closes.shape[1] < 2 or len(closes) < CLUSTER_MIN_HISTORY:
        return []

    out = []
    for members in correlation_clusters(closes.pct_change().dropna()):
        if len(members) < 2:
            continue
        share = sum(equity[s] for s in members) / total_equity * 100
        if share < CLUSTER_REPORT_MIN_PCT:
            continue
        value = (closes[members] * pd.Series({s: shares[s] for s in members})).sum(axis=1)
        recent_high = float(value.tail(CLUSTER_HIGH_WINDOW).max())
        tracker = _best_tracker(value, bench) if bench else None
        out.append({
            "tracks": tracker[0] if tracker else None,
            "tracks_corr": tracker[1] if tracker else None,
            "tracks_5d": tracker[2] if tracker else None,
            "symbols": sorted(members, key=lambda s: -equity[s]),
            "pct_of_equity": round(share, 1),
            "ret_5d": round(_pct(value.iloc[-1], value.iloc[-6]), 1) if len(value) > 5 else None,
            "ret_1m": round(_pct(value.iloc[-1], value.iloc[-22]), 1) if len(value) > 21 else None,
            "below_high_pct": round(_pct(float(value.iloc[-1]), recent_high), 1),
            "concentrated": share >= CONCENTRATION_ALERT_PCT,
        })
    return sorted(out, key=lambda c: -c["pct_of_equity"])


# ── 3. Benchmarks ────────────────────────────────────────────────────────────
def fetch_benchmark_history() -> dict[str, pd.Series]:
    """1y of daily closes for the benchmark ETFs. Empty dict on any failure."""
    try:
        raw = yf.download(list(BENCHMARKS), period="1y", auto_adjust=True, progress=False)["Close"]
    except Exception as e:
        log.warning(f"Benchmark download failed: {e}")
        return {}
    return {b: raw[b].dropna() for b in BENCHMARKS if b in raw and not raw[b].dropna().empty}


def compute_benchmarks(
    positions: list[dict], histories: dict[str, pd.Series], bench: dict[str, pd.Series]
) -> dict | None:
    """Back-cast of the *current* holdings vs the benchmarks, plus beta/correlation
    to QQQ and SOXX. A back-cast measures the picks, not the timing — it ignores
    trades made along the way — and is only as long as the shortest holding's history."""
    shares = {p["symbol"]: p["shares"] for p in positions}
    closes = _aligned_closes(list(shares), histories, 70)
    if closes.empty or len(closes) < 22 or not bench:
        return None
    value = (closes * pd.Series(shares)[closes.columns]).sum(axis=1)

    def window_returns(series: pd.Series) -> dict:
        s = _naive_index(series.dropna())
        return {
            label: round(_pct(float(s.iloc[-1]), float(s.iloc[-1 - n])), 1)
            for label, n in BENCHMARK_WINDOWS.items()
            if len(s) > n
        }

    rows = {"Portfolio": window_returns(value)}
    rows.update({b: window_returns(bench[b]) for b in BENCHMARKS if b in bench})

    betas = {}
    port_ret = value.pct_change().dropna().tail(BETA_WINDOW)
    for b in ("QQQ", "SOXX"):
        if b not in bench:
            continue
        b_ret = _naive_index(bench[b]).pct_change().dropna()
        joined = pd.concat([port_ret, b_ret], axis=1, join="inner").dropna()
        if len(joined) >= 30 and joined.iloc[:, 1].var() > 0:
            betas[b] = {
                "beta": round(float(joined.iloc[:, 0].cov(joined.iloc[:, 1]) / joined.iloc[:, 1].var()), 2),
                "corr": round(float(joined.iloc[:, 0].corr(joined.iloc[:, 1])), 2),
            }
    covered = sum(p["equity"] for p in positions if p["symbol"] in closes.columns)
    total = sum(p["equity"] for p in positions) or 1
    return {"returns": rows, "betas": betas, "coverage_pct": round(covered / total * 100)}


# ── Orchestration ────────────────────────────────────────────────────────────
def build_risk_context(positions: list[dict], histories: dict[str, pd.Series]) -> dict:
    """Compute all three pieces; each failure is logged and leaves its key empty."""
    ctx: dict = {"giveback": {}, "concentration": [], "benchmarks": None}
    try:
        for p in positions:
            g = compute_giveback(
                p["avg_cost"], p["current_price"], histories.get(p["symbol"]), p.get("opened_at")
            )
            if g:
                ctx["giveback"][p["symbol"]] = g
    except Exception as e:
        log.warning(f"Giveback computation failed: {e}")
    bench = fetch_benchmark_history()
    try:
        ctx["concentration"] = compute_concentration(positions, histories, bench)
    except Exception as e:
        log.warning(f"Concentration computation failed: {e}")
    try:
        ctx["benchmarks"] = compute_benchmarks(positions, histories, bench)
    except Exception as e:
        log.warning(f"Benchmark computation failed: {e}")
    return ctx


# ── Rendering ────────────────────────────────────────────────────────────────
def giveback_alerts(ctx: dict | None) -> dict[str, dict]:
    return {s: g for s, g in ((ctx or {}).get("giveback") or {}).items() if g["status"] == "alert"}


def giveback_summary(symbol: str, g: dict, return_pct: float) -> str:
    peak = f"peak +{g['peak_gain_pct']:.1f}% on {g['peak_date']}"
    if g["status"] == "alert":
        return (
            f"{peak}, now {return_pct:+.1f}% — {g['given_back_pct']:.0f}% of the open gain "
            f"given back (alert threshold {GIVEBACK_ALERT_PCT:.0f}%)"
        )
    if g["status"] == "gone":
        return f"{peak}, now {return_pct:+.1f}% — the entire open gain has been given back"
    return f"{peak}, now {return_pct:+.1f}% — {g['given_back_pct']:.0f}% of the open gain given back"


def risk_text_lines(ctx: dict | None, positions: list[dict]) -> list[str]:
    """Benchmark + concentration + giveback block, shared by the prompt and the
    plain-text/HTML digests so the reader sees exactly what the analysis saw."""
    if not ctx:
        return []
    lines: list[str] = []
    bench = ctx.get("benchmarks")
    if bench:
        windows = list(BENCHMARK_WINDOWS)
        lines.append(
            "Performance vs benchmarks (current holdings back-cast; QQQ = Nasdaq-100 ETF, "
            "SOXX = semiconductor ETF, SPY = S&P 500 ETF):"
        )
        lines.append(f"  {'':<10}" + "".join(f"{w:>9}" for w in windows))
        for name, row in bench["returns"].items():
            lines.append(
                f"  {name:<10}" + "".join(
                    f"{row[w]:>+8.1f}%" if w in row else f"{'n/a':>9}" for w in windows
                )
            )
        # Differences are computed here so the model never has to subtract (it
        # once called a -6.3% cluster "underperforming" a -7.6% benchmark).
        port = bench["returns"].get("Portfolio", {})
        for name, row in bench["returns"].items():
            if name == "Portfolio":
                continue
            diffs = ", ".join(
                f"{w} {port[w] - row[w]:+.1f} pts" for w in windows if w in port and w in row
            )
            lines.append(f"  Portfolio minus {name} (+ = portfolio did better): {diffs}")
        for b, v in bench["betas"].items():
            lines.append(f"  {BETA_WINDOW}-day beta to {b}: {v['beta']:.2f} (correlation {v['corr']:.2f})")
        if bench["coverage_pct"] < 95:
            lines.append(f"  (covers {bench['coverage_pct']}% of equity — holdings with short history excluded)")

    clusters = ctx.get("concentration") or []
    if clusters:
        lines.append("Correlated clusters (holdings that actually move together, found from return correlations):")
        for c in clusters:
            flag = "  ** CONCENTRATED **" if c["concentrated"] else ""
            lines.append(
                f"  {c['pct_of_equity']:.0f}% of equity{flag}: {', '.join(c['symbols'])} | "
                f"5d {_fmt_pct(c['ret_5d'])}, 1m {_fmt_pct(c['ret_1m'])}, "
                f"{abs(c['below_high_pct']):.1f}% below its {CLUSTER_HIGH_WINDOW}-session high"
            )
            if c.get("tracks") and c.get("tracks_5d") is not None and c.get("ret_5d") is not None:
                gap = c["ret_5d"] - c["tracks_5d"]
                verdict = "better" if gap > 0 else "worse" if gap < 0 else "level"
                lines.append(
                    f"      moves most like {c['tracks']} (correlation {c['tracks_corr']:.2f}); "
                    f"5d {_fmt_pct(c['ret_5d'])} vs {c['tracks']} {_fmt_pct(c['tracks_5d'])} "
                    f"— {abs(gap):.1f} pts {verdict} than the benchmark that tracks it"
                )

    held = {p["symbol"]: p for p in positions}
    protectable = [
        (s, g) for s, g in (ctx.get("giveback") or {}).items()
        if g["status"] in ("alert", "gone") and s in held
    ]
    if protectable:
        lines.append("Gain protection (positions that peaked >= "
                     f"{GIVEBACK_MIN_PEAK_GAIN_PCT:.0f}% above cost):")
        for s, g in sorted(protectable, key=lambda x: -(x[1]["given_back_pct"] or 0)):
            tag = "ALERT" if g["status"] == "alert" else "gone "
            lines.append(f"  [{tag}] {s}: {giveback_summary(s, g, held[s]['total_return_pct'])}")
    return lines


def _fmt_pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.1f}%"
