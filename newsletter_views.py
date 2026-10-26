"""
Analyst views and scheduled catalysts extracted from the Sherwood newsletters.

The newsletters are the one source here that carries *analysis* — Sherwood's own
read of the market, the strategists and firms it quotes, Robinhood retail-flow
data, and (in Snacks) the week's earnings calendar. Handing the model the raw
issue text was not enough: the analysis call runs with thinking disabled and
treats a wall of prose as background colour. On 2026-10-26 it ignored the issue's
lead item (Meta's $95B equity raise, while trimming META) and never mentioned that
four held names report earnings within three sessions, because the calendar sat at
the tail of an issue the old 5,000-char cap had already cut off.

So each issue is distilled once, by a model call, into structured items that are
then *checked mechanically* before they reach the prompt:

- every item carries a verbatim quote, and an item whose quote is not found in the
  issue text is dropped (whitespace-insensitive, so link-spacing artifacts in the
  cleaned text can't fail a faithful quote);
- every ticker must appear among the issue's own robinhood.com/stocks links, the
  same mechanical source `_extract_tickers` uses, so a symbol can't be invented.

The model never sees this as authority over the data: the analysis is told to
weigh each view against price/RSI/breadth and say whether it changes an action.
Pure functions plus one injectable-client call, so it is importable and testable
without the rest of portfolio_monitor.
"""
import html
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date

log = logging.getLogger(__name__)

VIEW_SOURCES = ("editorial", "third_party", "retail_flow")
DIRECTIONS = ("bullish", "bearish", "cautionary", "mixed", "neutral")
CATALYST_KINDS = ("earnings", "macro", "other")
# A held name reporting inside this many calendar days is tagged on its position line.
CATALYST_TAG_DAYS = 4
# Catalysts further out than this are noise for a daily digest.
CATALYST_HORIZON_DAYS = 10
EXTRACTION_MAX_TOKENS = 8000

EXTRACTION_SYSTEM = """You extract the ANALYTICAL CONTENT of a market newsletter for a portfolio analyst who trades a small, aggressive-growth account. Output one JSON object and nothing else.

You are not summarizing the news. You are capturing the VIEWS: what the writers, and the people they quote, think will happen, why they think it, and what they say would change their mind.

"views" are opinions and analysis. source is exactly one of:
  "editorial"   the newsletter's own take: its framing, takeaway, judgment calls
  "third_party" a named analyst, strategist, firm or investor the issue quotes (attributed_to = name + firm)
  "retail_flow" Robinhood customer trading data (net buying, record days). Report what retail is DOING; it is behavior, not a judgment about whether it is right.
Do NOT file a report of what a price did (a stock fell, a market is selling off) as a view; that is an event.

"events" are hard facts the views hang on or that matter to holders: offerings, guidance changes, deals, regulatory action, and a notable price move ONLY when the issue states its cause. Skip bare "stocks fell" reports.

"catalysts" are scheduled, dated things ahead: earnings reports and macro releases or central-bank decisions, from a calendar or from the text. Resolve weekday names to absolute dates using the issue date you are given. Emit ONE earnings catalyst per date and timing slot (premarket / after close), with subjects listing the ticker of EVERY company named in that slot, never a label like "market". Emit each macro release once; its subjects is a short label such as "Fed policy". Do not repeat a catalyst.

Rules
- Stay faithful to hedging. "may", "could", "suggests" stay hedged. Never harden a possibility into a prediction.
- quote: a VERBATIM, CONTIGUOUS span copied exactly from the issue, at most 300 characters, that DIRECTLY supports the claim (the claim must not say more than the quote does). No ellipses, no stitching sentences from different places, nothing that crosses a bracketed editor's note. If you cannot quote it exactly, leave the item out. For a catalyst, quote the shortest span that names it (e.g. the company names or the release).
- reasoning: why they hold the view, in their terms; null if not given.
- invalidation: ONLY a condition the text itself says would change or refute the view. null otherwise. Do not infer one.
- subjects: for a company use its ticker; for anything else a short theme label such as "AI capex" or "Fed policy". subject_type is "symbol", "theme" or "market".
- Relevance: keep items that bear on the market's direction, on the AI / semiconductor / tech complex, or on the symbols listed in the request. Drop entertainment, lifestyle, trivia, and single-company stories with no read-through to markets.
- stance: one sentence giving the issue's overall read of the market, keeping its uncertainty. If it takes no stance, say so.

JSON shape:
{"stance": str,
 "views": [{"source": str, "attributed_to": str|null, "subject_type": str, "subjects": [str], "direction": "bullish|bearish|cautionary|mixed|neutral", "horizon": str|null, "claim": str, "reasoning": str|null, "invalidation": str|null, "quote": str}],
 "events": [{"subjects": [str], "what": str, "quote": str}],
 "catalysts": [{"date": "YYYY-MM-DD", "timing": str|null, "kind": "earnings|macro|other", "subjects": [str], "what": str, "quote": str}]}"""


def _squash(s: str) -> str:
    """Whitespace-free, punctuation-normalised, lowercase form used only for comparison."""
    s = (
        s.replace("’", "'").replace("‘", "'")
        .replace("“", '"').replace("”", '"')
        .replace("—", "-").replace("–", "-").replace(" ", " ")
    )
    return re.sub(r"\s+", "", s).lower()


def quote_in_text(quote: str, text: str) -> bool:
    """True when `quote` is a contiguous span of `text`, ignoring whitespace and case."""
    q = _squash(quote or "")
    return len(q) >= 12 and q in _squash(text)


def _parse_json(raw: str) -> dict | None:
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _clean_subjects(item: dict, linked: set[str]) -> list[str]:
    """Keep theme labels as given; keep a ticker only if the issue links it."""
    out = []
    for s in item.get("subjects") or []:
        if not isinstance(s, str) or not s.strip():
            continue
        s = s.strip()
        looks_like_ticker = bool(re.fullmatch(r"[A-Z][A-Z.\-]{0,9}", s))
        if looks_like_ticker and s not in linked:
            continue  # a ticker the issue never linked: not verifiable, drop it
        out.append(s)
    return out


def validate_extraction(data: dict, issue: dict) -> dict:
    """
    Reduce a raw extraction to what can be verified against the issue. Never raises
    on malformed items; they are dropped and counted in `dropped`.
    """
    text = issue.get("text", "")
    linked = set(issue.get("tickers") or [])
    dropped = 0
    views, events, catalysts = [], [], []

    for v in data.get("views") or []:
        if not isinstance(v, dict) or v.get("source") not in VIEW_SOURCES \
                or v.get("direction") not in DIRECTIONS or not v.get("claim"):
            dropped += 1
            continue
        if not quote_in_text(v.get("quote", ""), text):
            dropped += 1
            continue
        subjects = _clean_subjects(v, linked)
        if not subjects:
            dropped += 1
            continue
        views.append({
            "source": v["source"],
            "attributed_to": v.get("attributed_to") or None,
            "subject_type": v.get("subject_type") or "theme",
            "subjects": subjects,
            "direction": v["direction"],
            "horizon": v.get("horizon") or None,
            "claim": v["claim"].strip(),
            "reasoning": v.get("reasoning") or None,
            "invalidation": v.get("invalidation") or None,
            "quote": v["quote"].strip(),
        })

    for e in data.get("events") or []:
        if not isinstance(e, dict) or not e.get("what") or not quote_in_text(e.get("quote", ""), text):
            dropped += 1
            continue
        events.append({
            "subjects": _clean_subjects(e, linked),
            "what": e["what"].strip(),
            "quote": e["quote"].strip(),
        })

    for c in data.get("catalysts") or []:
        try:
            when = date.fromisoformat(str(c.get("date")))
        except (TypeError, ValueError, AttributeError):
            dropped += 1
            continue
        kind = c.get("kind") if c.get("kind") in CATALYST_KINDS else "other"
        subjects = _clean_subjects(c, linked)
        if kind == "earnings":  # an earnings slot is about companies; a label there is a model slip
            subjects = [x for x in subjects if re.fullmatch(r"[A-Z][A-Z.\-]{0,9}", x)]
        if not quote_in_text(c.get("quote", ""), text) or not c.get("what") \
                or (kind == "earnings" and not subjects):
            dropped += 1
            continue
        catalysts.append({
            "date": when.isoformat(),
            "timing": c.get("timing") or None,
            "kind": kind,
            "subjects": subjects,
            "what": c["what"].strip(),
            "quote": c["quote"].strip(),
        })

    return {
        "name": issue["name"],
        "date": issue["date"],
        "subject": issue.get("subject", ""),
        "stance": (data.get("stance") or "").strip(),
        "views": views,
        "events": events,
        "catalysts": catalysts,
        "dropped": dropped,
    }


def extract_issue(client, model: str, issue: dict, universe: list[str]) -> dict | None:
    """Extract and verify one issue. Returns None on any failure (callers fall back to raw text)."""
    try:
        weekday = date.fromisoformat(issue["date"]).strftime("%A")
        message = client.messages.create(
            model=model,
            max_tokens=EXTRACTION_MAX_TOKENS,
            thinking={"type": "disabled"},
            system=EXTRACTION_SYSTEM,
            messages=[{
                "role": "user",
                "content": (
                    f"Symbols of interest: {', '.join(universe)}\n\n"
                    f"NEWSLETTER: {issue['name']} — \"{issue['subject']}\"\n"
                    f"ISSUE DATE: {issue['date']} ({weekday})\n\n{issue['text']}"
                ),
            }],
        )
        data = _parse_json(message.content[0].text)
        if data is None:
            log.warning(f"{issue['name']}: view extraction returned unparseable JSON")
            return None
        result = validate_extraction(data, issue)
        log.info(
            f"{issue['name']}: extracted {len(result['views'])} views, "
            f"{len(result['events'])} events, {len(result['catalysts'])} catalysts "
            f"({result['dropped']} dropped as unverifiable)"
        )
        return result
    except Exception as e:
        log.warning(f"{issue.get('name', '?')}: view extraction failed ({type(e).__name__}): {e}")
        return None


def extract_all(client, model: str, issues: list[dict], universe: list[str]) -> list[dict]:
    """Extract every issue in parallel; issues that fail are simply absent from the result."""
    if not issues:
        return []
    with ThreadPoolExecutor(max_workers=len(issues)) as pool:
        results = list(pool.map(lambda i: extract_issue(client, model, i, universe), issues))
    return [r for r in results if r]


# ── Catalysts ─────────────────────────────────────────────────────────────────
def _timing_slot(timing: str | None) -> str:
    t = (timing or "").lower()
    if any(w in t for w in ("pre", "before", "morning")):
        return "premarket"
    if any(w in t for w in ("post", "after", "close")):
        return "after close"
    return t


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower())}


def upcoming_catalysts(extracted: list[dict], today: str) -> list[dict]:
    """
    Deduplicated catalysts dated today..today+horizon, soonest first.

    The same calendar slot arrives more than once (two issues, or one issue split
    across several model-emitted rows), so earnings in one date+slot are merged
    into a single row with the union of their tickers, and a macro release is
    dropped when an already-kept one that day shares most of its words.
    """
    t = date.fromisoformat(today)
    out: list[dict] = []
    for ex in extracted:
        for c in ex.get("catalysts", []):
            days = (date.fromisoformat(c["date"]) - t).days
            if days < 0 or days > CATALYST_HORIZON_DAYS:
                continue
            slot = _timing_slot(c.get("timing"))
            twin = None
            for o in out:
                if o["date"] != c["date"] or o["kind"] != c["kind"]:
                    continue
                if c["kind"] == "earnings" and _timing_slot(o.get("timing")) == slot:
                    twin = o
                elif c["kind"] != "earnings":
                    a, b = _words(o["what"]), _words(c["what"])
                    if a and b and len(a & b) / len(a | b) >= 0.5:
                        twin = o
                if twin:
                    break
            if twin:
                twin["subjects"] = sorted(set(twin["subjects"]) | set(c["subjects"]))
            else:
                out.append({**c, "subjects": list(c["subjects"]), "days": days})
    return sorted(out, key=lambda c: (c["date"], c["kind"]))


def _when_label(c: dict) -> str:
    d = date.fromisoformat(c["date"])
    rel = "today" if c["days"] == 0 else "tomorrow" if c["days"] == 1 else f"in {c['days']} days"
    timing = f" {c['timing']}" if c.get("timing") else ""
    return f"{c['date']} ({d.strftime('%a')}, {rel}){timing}"


def held_catalyst_tags(extracted: list[dict], held: set[str], today: str) -> dict[str, str]:
    """{symbol: tag} for held symbols with a dated catalyst in the next CATALYST_TAG_DAYS days."""
    tags: dict[str, list[str]] = {}
    for c in upcoming_catalysts(extracted, today):
        if c["days"] > CATALYST_TAG_DAYS:
            continue
        for s in c["subjects"]:
            if s in held:
                kind = "REPORTS EARNINGS" if c["kind"] == "earnings" else c["what"].upper()
                tag = f"{kind} {_when_label(c)}"
                if tag not in tags.setdefault(s, []):
                    tags[s].append(tag)
    return {s: "; ".join(v) for s, v in tags.items()}


def symbol_notes(extracted: list[dict], symbol: str, limit: int = 3) -> list[str]:
    """
    Newsletter items that name `symbol`, as short lines for that position's block.

    Putting them beside the position's own price/RSI lines is the point: on
    2026-10-26 the analysis trimmed META for "no dated catalyst" while Meta's $95B
    equity raise sat in a separate section it had no reason to reread at decision time.
    Events first (hard facts), then views.
    """
    notes = []
    for ex in extracted:
        for e in ex.get("events", []):
            if symbol in e["subjects"]:
                notes.append(f"  Newsletter event ({ex['name']}): {e['what']}")
    for ex in extracted:
        for v in ex.get("views", []):
            if symbol in v["subjects"]:
                who = v["attributed_to"] or ("Sherwood" if v["source"] == "editorial" else v["source"])
                notes.append(f"  Newsletter view ({ex['name']}, {who}): [{v['direction']}] {v['claim']}")
    return notes[:limit]


# ── Prompt / digest rendering ─────────────────────────────────────────────────
def _held_note(subjects: list[str], held: set[str]) -> str:
    hit = [s for s in subjects if s in held]
    return f" [HELD: {', '.join(hit)}]" if hit else ""


def _symbols_with_held(subjects: list[str], held: set[str]) -> str:
    return ", ".join(f"{s} [HELD]" if s in held else s for s in subjects)


def prompt_lines(extracted: list[dict], held: set[str], today: str) -> list[str]:
    """The ANALYST VIEWS and CATALYSTS sections for build_prompt. [] when nothing was extracted."""
    if not extracted:
        return []
    lines = [
        "",
        "=== ANALYST VIEWS — extracted from the Sherwood newsletters above ===",
        "Every quote below was checked verbatim against its issue and every ticker against the "
        "issue's own links. Weight: Sherwood's editorial read first; a third-party analyst or firm "
        "quoted in an issue is second-hand opinion (attribute it, do not present it as Sherwood's); "
        "retail flow is what Robinhood customers are doing, not a judgment about whether it is right. "
        "The analysis must engage with these (see ANALYST VIEWS — HARD CONSTRAINT).",
    ]
    for ex in extracted:
        lines += ["", f"--- {ex['name']} ({ex['date']}) — overall read: "
                      f"{ex['stance'] or 'no stance stated'}"]
        for i, v in enumerate(ex["views"], 1):
            who = v["attributed_to"] or ("Sherwood" if v["source"] == "editorial" else v["source"])
            horizon = f", {v['horizon']}" if v["horizon"] else ""
            lines.append(
                f"{i}. [{v['source']} — {who}] {', '.join(v['subjects'])}"
                f"{_held_note(v['subjects'], held)} — {v['direction']}{horizon}"
            )
            lines.append(f"   Claim: {v['claim']}")
            if v["reasoning"]:
                lines.append(f"   Why: {v['reasoning']}")
            if v["invalidation"]:
                lines.append(f"   Would change if: {v['invalidation']}")
            lines.append(f"   Quote: \"{v['quote']}\"")
        for e in ex["events"]:
            lines.append(f"• EVENT{_held_note(e['subjects'], held)}: {e['what']}")

    catalysts = upcoming_catalysts(extracted, today)
    if catalysts:
        lines += ["", "=== THIS WEEK'S CATALYSTS (from the newsletters' own calendars) ==="]
        for c in catalysts:
            who = f" — {_symbols_with_held(c['subjects'], held)}" if c["subjects"] else ""
            lines.append(f"• {_when_label(c)} [{c['kind']}]{who}: {c['what']}")
    return lines


def plain_lines(extracted: list[dict], held: set[str], today: str) -> list[str]:
    """Plain-text digest section."""
    if not extracted:
        return []
    lines = ["", "WHAT SHERWOOD IS SAYING", "-" * 64]
    for ex in extracted:
        lines.append(f"{ex['name']} — {ex['stance'] or 'no stance stated'}")
        for v in ex["views"]:
            who = v["attributed_to"] or ("Sherwood" if v["source"] == "editorial" else v["source"])
            lines.append(
                f"  [{v['direction'].upper()}] {', '.join(v['subjects'])}"
                f"{_held_note(v['subjects'], held)} ({who}): {v['claim']}"
            )
    catalysts = upcoming_catalysts(extracted, today)
    if catalysts:
        lines += ["", "CATALYSTS THIS WEEK"]
        for c in catalysts:
            who = f" — {_symbols_with_held(c['subjects'], held)}" if c["subjects"] else ""
            lines.append(f"  {_when_label(c)}{who}: {c['what']}")
    return lines


_DIRECTION_STYLE = {
    "bullish": ("#166534", "#dcfce7"),
    "bearish": ("#991b1b", "#fee2e2"),
    "cautionary": ("#92400e", "#fef3c7"),
    "mixed": ("#475569", "#e2e8f0"),
    "neutral": ("#475569", "#f1f5f9"),
}


def html_block(extracted: list[dict], held: set[str], today: str) -> str:
    """HTML digest section, styled to match the existing Market News block."""
    if not extracted:
        return ""
    h = lambda s: html.escape(str(s), quote=True)  # noqa: E731
    parts = []
    for ex in extracted:
        parts.append(
            '<p style="margin:12px 0 6px;font-size:13px;font-weight:700;color:#0f172a;">'
            f'{h(ex["name"])} — overall read</p>'
            f'<p style="margin:0 0 8px;font-size:13px;line-height:1.6;color:#334155;">'
            f'{h(ex["stance"] or "No stance stated.")}</p>'
        )
        for v in ex["views"]:
            fg, bg = _DIRECTION_STYLE[v["direction"]]
            who = v["attributed_to"] or ("Sherwood" if v["source"] == "editorial" else v["source"])
            held_hit = [s for s in v["subjects"] if s in held]
            held_html = (
                f'<span style="margin-left:6px;font-size:10px;font-weight:700;color:#1d4ed8;">'
                f'HELD: {h(", ".join(held_hit))}</span>' if held_hit else ""
            )
            why = (
                f'<br><span style="color:#64748b;">Why: {h(v["reasoning"])}</span>'
                if v["reasoning"] else ""
            )
            parts.append(
                '<div style="padding:8px 0;border-bottom:1px solid #f1f5f9;font-size:13px;'
                'line-height:1.55;color:#0f172a;">'
                f'<span style="background:{bg};color:{fg};font-size:10px;font-weight:700;'
                f'padding:1px 6px;border-radius:3px;text-transform:uppercase;">{h(v["direction"])}</span> '
                f'<strong>{h(", ".join(v["subjects"]))}</strong>{held_html} '
                f'<span style="color:#64748b;">({h(who)})</span><br>{h(v["claim"])}{why}</div>'
            )
    catalysts = upcoming_catalysts(extracted, today)
    if catalysts:
        rows = "".join(
            '<tr>'
            f'<td style="padding:4px 10px 4px 0;font-size:12px;color:#475569;white-space:nowrap;">'
            f'{h(_when_label(c))}</td>'
            f'<td style="padding:4px 0;font-size:12px;color:#0f172a;">'
            f'{h(_symbols_with_held(c["subjects"], held) or c["what"])}'
            f'{"" if not c["subjects"] else " — " + h(c["what"])}</td></tr>'
            for c in catalysts
        )
        parts.append(
            '<p style="margin:14px 0 4px;font-size:13px;font-weight:700;color:#0f172a;">'
            f'Catalysts this week</p><table style="border-collapse:collapse;">{rows}</table>'
        )
    return "".join(parts)
