"""
Weekly macro read: the 2-3 week arc of the market, distilled from the newsletters.

Why (2026-11-24): the daily analysis re-read each morning's newsletter wrap as if it were the
whole story, so a 3-week drawdown looked like a fresh tape every day. The newsletters
themselves narrate that arc (EntryPoint: "no single catalyst", AI-credit tightening, the NVDA
accounting inquiry; Snacks: the SaaS "slow-moving apocalypse"), but nothing was accumulating it.

One model call per week reads the last ~3 weeks of issues and returns the phase of the market,
the arc, the themes running through it, the macro backdrop (rates/Fed, policy, earnings, credit,
flows) and dated events ahead. The result is cached (`macro_view.json`) and reused for
`MACRO_MAX_AGE_DAYS`, so the daily run costs nothing extra; a stale cache is still used, labelled
with its age, if the archive can't be fetched.

Same trust discipline as newsletter_views: every theme / macro item / upcoming event must carry a
verbatim quote from one of the issues, checked in Python. Unverifiable items are dropped. The
narrative paragraph is synthesis and is not quote-checked, so it is told to say nothing the
verified items don't support. As there, what is NOT checked is that the claim is *entailed* by
its quote.

Importable without portfolio_monitor (IMAP fetching stays there and is passed in as `issues`).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone

import newsletter_views

log = logging.getLogger(__name__)

MACRO_FILE = "macro_view.json"          # gitignored; market commentary, regenerated weekly
MACRO_WINDOW_DAYS = 21
MACRO_MAX_AGE_DAYS = 7                  # regenerate after this
PER_ISSUE_CHARS = 3500                  # the market wrap + takeaway sit at the top of each issue
MACRO_MAX_TOKENS = 4000
MIN_ISSUES = 4                          # fewer than this is not an arc

PHASES = ("advance", "topping", "pullback", "correction", "capitulation", "stabilizing", "recovery", "range")

MACRO_SYSTEM = """You read about three weeks of market newsletters and write the arc of the market, not today's headlines. Use ONLY what the issues say.

Return one JSON object, no prose before or after:
{"phase": one of advance|topping|pullback|correction|capitulation|stabilizing|recovery|range, "phase_basis": "one sentence on what in the issues places it there",
 "narrative": "4-6 plain sentences: how the three weeks unfolded, oldest to newest, and where they leave the market. Say nothing that the items below do not support.",
 "themes": [{"theme": "short name", "direction": "bullish|bearish|cautionary|mixed", "first_seen": "YYYY-MM-DD", "last_seen": "YYYY-MM-DD", "claim": "what the newsletters say about it, in your words", "quote": "a CONTIGUOUS verbatim span of one issue, 12+ characters", "issue": "Name YYYY-MM-DD"}],
 "macro": [{"area": "rates_fed|inflation|policy_politics|earnings|credit|flows|other", "state": "the backdrop in one sentence", "quote": "verbatim", "issue": "Name YYYY-MM-DD"}],
 "ahead": [{"date": "YYYY-MM-DD or null", "what": "scheduled event or stated risk still to come", "quote": "verbatim", "issue": "Name YYYY-MM-DD"}],
 "view_changes": [{"what": "a view that REVERSED or a theme that faded during the window", "quote": "verbatim", "issue": "Name YYYY-MM-DD"}]}

Rules:
- A theme must appear in at least two different issues, or be a major event; a single day's headline is not a theme. first_seen/last_seen are issue dates.
- Quotes are copied exactly from one issue. Never stitch two passages, never use an ellipsis, never fix wording.
- Do not give investment advice and do not name any portfolio. Attribute third-party analysts to the newsletter that quoted them.
- If the issues do not say something (rates, the Fed, politics), leave that area out; do not fill it from general knowledge.
- 3-7 themes, 0-5 macro items, 0-6 ahead items, 0-3 view_changes. Newest evidence outweighs older."""


# ── Cache ────────────────────────────────────────────────────────────────────
def load_cache(path: str = MACRO_FILE) -> dict | None:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) and data.get("generated") else None
    except (OSError, json.JSONDecodeError):
        return None


def save_cache(data: dict, path: str = MACRO_FILE) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def age_days(cache: dict | None, today: str) -> int | None:
    if not cache:
        return None
    try:
        return (date.fromisoformat(today) - date.fromisoformat(cache["generated"])).days
    except (KeyError, ValueError):
        return None


def needs_refresh(cache: dict | None, today: str) -> bool:
    a = age_days(cache, today)
    return a is None or a >= MACRO_MAX_AGE_DAYS


# ── Generation ───────────────────────────────────────────────────────────────
def build_corpus(issues: list[dict]) -> str:
    """The exact text the model sees AND quotes are verified against, oldest issue first."""
    parts = []
    for i in sorted(issues, key=lambda x: (x["date"], x["name"])):
        body = i["text"][:PER_ISSUE_CHARS]
        parts.append(f"##### {i['name']} {i['date']} — {i['subject']}\n{body}")
    return "\n\n".join(parts)


def validate(data: dict, corpus: str, issue_labels: set[str]) -> dict:
    """Keep only items whose quote is verbatim in the corpus; coerce the rest of the shape."""
    dropped = 0

    def keep(items, required: tuple[str, ...]):
        nonlocal dropped
        out = []
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict) or not all(it.get(k) for k in required):
                dropped += 1
                continue
            if not newsletter_views.quote_in_text(it.get("quote", ""), corpus):
                dropped += 1
                continue
            if it.get("issue") not in issue_labels:
                it["issue"] = None  # the quote is real; the attribution label was not — don't print a wrong one
            out.append(it)
        return out

    phase = data.get("phase")
    return {
        "phase": phase if phase in PHASES else None,
        "phase_basis": str(data.get("phase_basis") or "")[:400],
        "narrative": str(data.get("narrative") or "")[:1500],
        "themes": keep(data.get("themes"), ("theme", "claim", "quote")),
        "macro": keep(data.get("macro"), ("state", "quote")),
        "ahead": keep(data.get("ahead"), ("what", "quote")),
        "view_changes": keep(data.get("view_changes"), ("what", "quote")),
        "dropped": dropped,
    }


def generate(client, model: str, issues: list[dict], today: str) -> dict | None:
    """One call over the archive. None on any failure or too few issues (callers keep the old cache)."""
    if len(issues) < MIN_ISSUES:
        log.warning(f"Macro read: only {len(issues)} issues in the archive (need {MIN_ISSUES}) — not generating")
        return None
    corpus = build_corpus(issues)
    labels = {f"{i['name']} {i['date']}" for i in issues}
    try:
        message = client.messages.create(
            model=model,
            max_tokens=MACRO_MAX_TOKENS,
            thinking={"type": "disabled"},
            system=MACRO_SYSTEM,
            messages=[{"role": "user", "content": f"TODAY: {today}\nISSUES ({len(issues)}), oldest first:\n\n{corpus}"}],
        )
        data = newsletter_views._parse_json(message.content[0].text)
        if data is None:
            log.warning("Macro read: model returned unparseable JSON")
            return None
        result = validate(data, corpus, labels)
    except Exception as e:
        log.warning(f"Macro read generation failed ({type(e).__name__}): {e}")
        return None
    if not result["themes"]:
        log.warning("Macro read: no theme survived quote verification — discarding")
        return None
    dates = sorted(i["date"] for i in issues)
    result.update({
        "generated": today,
        "window": [dates[0], dates[-1]],
        "n_issues": len(issues),
        "sources": sorted({i["name"] for i in issues}),
    })
    log.info(
        f"Macro read generated from {len(issues)} issues ({dates[0]}..{dates[-1]}): phase={result['phase']}, "
        f"{len(result['themes'])} themes, {len(result['macro'])} macro, {len(result['ahead'])} ahead "
        f"({result['dropped']} dropped as unverifiable)"
    )
    return result


def ensure(client, model: str, fetch_issues, today: str, path: str = MACRO_FILE) -> dict | None:
    """Return the current macro read, regenerating weekly. `fetch_issues(days)` -> (issues, problems).

    Never raises. A failed refresh falls back to whatever is cached (even stale — the prompt
    shows its age) rather than to nothing."""
    cache = load_cache(path)
    if not needs_refresh(cache, today):
        return cache
    try:
        issues, problems = fetch_issues(MACRO_WINDOW_DAYS)
        if problems:
            log.info(f"Macro read archive problems: {problems}")
        fresh = generate(client, model, issues, today)
        if fresh:
            save_cache(fresh, path)
            return fresh
    except Exception as e:
        log.warning(f"Macro read refresh failed ({type(e).__name__}): {e}")
    return cache


# ── Rendering ────────────────────────────────────────────────────────────────
def _issue_tag(it: dict) -> str:
    return f", {it['issue']}" if it.get("issue") else ""


def prompt_lines(macro: dict | None, today: str) -> list[str]:
    if not macro:
        return []
    a = age_days(macro, today)
    stale = f" — {a} days old, treat as dated" if a is not None and a > MACRO_MAX_AGE_DAYS else ""
    lines = [
        "",
        f"=== WEEKLY MACRO READ — THE 3-WEEK ARC (generated {macro['generated']} from {macro['n_issues']} "
        f"{'/'.join(macro.get('sources') or [])} issues, {macro['window'][0]} to {macro['window'][1]}{stale}) ===",
        "The backdrop that today's newsletter is the latest installment of. Place today's news "
        "against it (extends / contradicts which theme) instead of reading it as a fresh story. "
        "Items carry a verified quote; the arc paragraph is synthesis.",
    ]
    if macro.get("phase"):
        lines.append(f"Phase: {macro['phase'].upper()} — {macro.get('phase_basis', '')}".rstrip(" —"))
    if macro.get("narrative"):
        lines.append(f"Arc: {macro['narrative']}")
    if macro.get("themes"):
        lines.append("Themes running through the window:")
        for t in macro["themes"]:
            span = f"{t.get('first_seen', '?')} to {t.get('last_seen', '?')}"
            lines.append(f"  [{t.get('direction', '?')}] {t['theme']} ({span}{_issue_tag(t)}): {t['claim']} — \"{t['quote']}\"")
    if macro.get("macro"):
        lines.append("Macro backdrop:")
        for m in macro["macro"]:
            lines.append(f"  [{m.get('area', 'other')}] {m['state']} — \"{m['quote']}\"{_issue_tag(m)}")
    if macro.get("view_changes"):
        lines.append("Views that reversed or faded:")
        for v in macro["view_changes"]:
            lines.append(f"  {v['what']} — \"{v['quote']}\"{_issue_tag(v)}")
    if macro.get("ahead"):
        lines.append("Still ahead:")
        for x in macro["ahead"]:
            lines.append(f"  {x.get('date') or 'undated'}: {x['what']}")
    return lines


def plain_lines(macro: dict | None, today: str) -> list[str]:
    if not macro:
        return []
    a = age_days(macro, today)
    out = [f"MACRO READ — 3-week arc ({macro['window'][0]} to {macro['window'][1]}, generated {macro['generated']}"
           + (f", {a}d old" if a else "") + ")"]
    if macro.get("phase"):
        out.append(f"  Phase: {macro['phase']} — {macro.get('phase_basis', '')}")
    if macro.get("narrative"):
        out.append(f"  {macro['narrative']}")
    for t in macro.get("themes") or []:
        out.append(f"  [{t.get('direction', '?')}] {t['theme']}: {t['claim']}")
    return out


def html_block(macro: dict | None, today: str, esc=lambda s: s) -> str:
    if not macro:
        return ""
    a = age_days(macro, today)
    rows = "".join(
        f"<li><b>{esc(t['theme'])}</b> <i>({esc(str(t.get('direction', '?')))})</i> — {esc(t['claim'])}</li>"
        for t in macro.get("themes") or []
    )
    ahead = "".join(
        f"<li>{esc(str(x.get('date') or 'undated'))}: {esc(x['what'])}</li>" for x in macro.get("ahead") or []
    )
    return (
        '<div style="margin:16px 0;padding:12px 16px;border-left:4px solid #6b7280;background:#f9fafb">'
        f'<div style="font-weight:600">Macro read — the 3-week arc '
        f'<span style="font-weight:400;color:#6b7280">({esc(macro["window"][0])} to {esc(macro["window"][1])}'
        f'{f", {a}d old" if a else ""})</span></div>'
        + (f'<div style="margin-top:6px"><b>Phase:</b> {esc(macro["phase"])} — {esc(macro.get("phase_basis", ""))}</div>' if macro.get("phase") else "")
        + (f'<div style="margin-top:6px">{esc(macro["narrative"])}</div>' if macro.get("narrative") else "")
        + (f'<ul style="margin:8px 0 0 18px;padding:0">{rows}</ul>' if rows else "")
        + (f'<div style="margin-top:8px"><b>Still ahead</b></div><ul style="margin:4px 0 0 18px;padding:0">{ahead}</ul>' if ahead else "")
        + "</div>"
    )
