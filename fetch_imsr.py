#!/usr/bin/env python3
"""
FIRESTORM IMSR pipeline — pulls today's NICC Incident Management Situation
Report (https://www.nifc.gov/nicc-files/sitreprt.pdf), parses the structured
sections into JSON, and writes data/imsr.json + data/health.json for the
FIRESTORM frontend to consume.

The IMSR is a once-daily PDF published by the National Interagency
Coordination Center, ~06:30 MT. It contains:

  - National Preparedness Level + national fire-activity totals
  - Per-GACC Active Incident Resource Summary table (page 1)
  - Per-GACC narrative + large-fire detail tables (pages 2-4)
  - Yesterday's fires/acres by protection (page 5)
  - Year-to-Date fires/acres + 10-year average (page 6)
  - Predictive Services Discussion (page 7)

Why this matters for FIRESTORM: WFIGS gives us live IRWIN incident geometry,
but the IMSR is the authoritative human-curated daily summary that ICs and
fire-management leadership read. It's the document Scott (USFWS) and his
peers reach for first thing in the morning. Surfacing it directly so the
FIRESTORM AI can answer "what's the national prep level today" / "who's got
a CIMT committed" / "what does Predictive Services say about today's
weather" closes a real situational gap.

Cadence: every 15 min via GHA self-redispatch loop. NIFC publishes once daily
but sometimes re-publishes a corrected version mid-day; 15-min catches that
within an acceptable window. Watchdog `health.json` tracks consecutive fetch
failures so we can spot if NIFC starts rate-limiting or if the URL moves.

Stdlib only — no JWT, no API key. pdfplumber is the one runtime dep
(installed via the GHA workflow). Public IMSR has no auth, no robots
restriction (verified by reading https://www.nifc.gov/robots.txt — sitreprt
is not Disallowed).

Schema is in README.md.
"""

import json
import os
import re
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pdfplumber

PDF_URL = "https://www.nifc.gov/nicc-files/sitreprt.pdf"
USER_AGENT = "firestorm-imsr-data/1.0 (+https://github.com/Deasus/firestorm-imsr-data)"
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
PDF_PATH = DATA_DIR / "imsr.pdf"
JSON_PATH = DATA_DIR / "imsr.json"
HEALTH_PATH = DATA_DIR / "health.json"

GACC_ROW_RE = re.compile(
    # GACC code, then an OPTIONAL per-GACC Preparedness Level column, then the
    # 7 numeric columns (incidents, acres, crews, engines, helicopters,
    # personnel, change-in-personnel).
    #
    # The PL column is optional on purpose. NICC ADDED it to the page-1 table
    # sometime between 2026-06-15 and 2026-07-01 (national PL went 2 -> 4 in
    # that window). The original 7-column regex then matched nothing, and
    # because there was no row-count guard the pipeline reported healthy while
    # `resource_summary` sat empty for ~2 months. Keeping the group optional
    # means both the pre-July (7-col) and current (8-col) layouts parse, so a
    # future NICC format flip in either direction degrades instead of zeroing.
    # Regex backtracking resolves the ambiguity: exactly 7 trailing numeric
    # columns are required, so a 7-number row skips the optional group and an
    # 8-number row binds it.
    #
    # The Total row carries "---" in the PL column rather than a digit.
    r"^(AICC|NWCC|ONCC|OSCC|NRCC|GBCC|SWCC|RMCC|EACC|SACC|Total)\s+"
    r"(?:(\d|---)\s+)?"
    r"([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+(-?[\d,]+)\s*$"
)

# GACC name → human-readable area name (the page-2/3/4 narrative section
# headers don't use the 4-letter codes; they use the full names).
GACC_AREA_NAMES = {
    "Alaska Area": "AICC",
    "Northwest Area": "NWCC",
    "Northern California Area": "ONCC",
    "Southern California Area": "OSCC",
    "Northern Rockies Area": "NRCC",
    "Great Basin Area": "GBCC",
    "Southwest Area": "SWCC",
    "Rocky Mountain Area": "RMCC",
    "Eastern Area": "EACC",
    "Southern Area": "SACC",
}

# Page 5/6 protection-area names (slightly different from GACC narrative
# headers — "Northern California" vs "Northern California Area")
PROTECTION_AREAS = [
    "Alaska", "Northwest", "Northern California", "Southern California",
    "Northern Rockies", "Great Basin", "Southwest", "Rocky Mountain",
    "Eastern Area", "Southern Area",
]


def fetch_pdf() -> bytes:
    ctx = ssl.create_default_context()
    req = urllib.request.Request(PDF_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
        return r.read()


def parse_top_summary(p1_text: str) -> dict:
    """Page 1 header — date, prep level, national totals."""
    out = {}
    # Date line: "Monday June 8, 2026 – 0730 MDT"
    m = re.search(r"^(\w+ \w+ \d{1,2}, \d{4})\s*[–-]\s*(\d{4} \w{2,4})", p1_text, re.MULTILINE)
    if m:
        out["report_date_human"] = m.group(1)
        out["report_time"] = m.group(2)
    # Prep level: "National Preparedness Level 2"
    m = re.search(r"National Preparedness Level (\d)", p1_text)
    if m:
        out["national_preparedness_level"] = int(m.group(1))
    # Activity ladder
    for label, key in [
        ("Initial attack activity", "initial_attack_activity"),
        ("New large incidents", "new_large_incidents"),
        ("Large fires contained", "large_fires_contained"),
        ("Uncontained large fires", "uncontained_large_fires"),
        ("CIMTs committed", "cimts_committed"),
        ("NIMOs committed", "nimos_committed"),
    ]:
        # Initial attack carries free text; the rest are integers.
        if key == "initial_attack_activity":
            m = re.search(rf"{label}:\s*([^\n]+)", p1_text)
            if m:
                out[key] = m.group(1).strip()
        else:
            m = re.search(rf"{label}:\s*(\d+)", p1_text)
            if m:
                out[key] = int(m.group(1))
    return out


def _to_int(s: str) -> int:
    return int(s.replace(",", ""))


def parse_resource_summary(p1_text: str) -> list:
    """Page 1 GACC table — Active Incident Resource Summary.

    `pl` is the per-GACC Preparedness Level (1-5), or None when the column is
    absent (pre-July-2026 layout) or not applicable (the Total row, which
    prints "---"). This is the label the GACC-level PL index trains on — see
    ~/Projects/firestorm-research/PREPAREDNESS_LEVEL_2026-08-27.md.

    NOTE: the Total row IS included in this list (it always has been, despite
    an earlier comment claiming otherwise). Consumers filter by `gacc`, so
    leaving it in place keeps the shape consumers already expect.
    """
    out = []
    for line in p1_text.splitlines():
        m = GACC_ROW_RE.match(line.strip())
        if not m:
            continue
        pl_raw = m.group(2)
        out.append({
            "gacc": m.group(1),
            "pl": int(pl_raw) if pl_raw and pl_raw.isdigit() else None,
            "incidents": _to_int(m.group(3)),
            "cumulative_acres": _to_int(m.group(4)),
            "crews": _to_int(m.group(5)),
            "engines": _to_int(m.group(6)),
            "helicopters": _to_int(m.group(7)),
            "total_personnel": _to_int(m.group(8)),
            "change_in_personnel": _to_int(m.group(9)),
        })
    return out


def parse_gacc_sections(text: str) -> list:
    """Pages 2-4 — per-GACC narrative + uncontained-large-fire table."""
    out = []
    # Find each "<Area> Area (PL N)" header, collect the block until the next
    # area header (or the "Fires and Acres Yesterday" header, which starts the
    # protection table on page 5).
    headers = []
    for area, gacc_code in GACC_AREA_NAMES.items():
        for m in re.finditer(rf"^{re.escape(area)} \(PL (\d)\)", text, re.MULTILINE):
            headers.append({
                "area": area,
                "gacc": gacc_code,
                "preparedness_level": int(m.group(1)),
                "start": m.start(),
            })
    headers.sort(key=lambda h: h["start"])

    # Sentinel end positions
    end_marker = re.search(r"^Fires and Acres Yesterday", text, re.MULTILINE)
    text_end = end_marker.start() if end_marker else len(text)

    for i, h in enumerate(headers):
        block_start = h["start"]
        block_end = headers[i + 1]["start"] if i + 1 < len(headers) else text_end
        block = text[block_start:block_end]
        section = {
            "gacc": h["gacc"],
            "area": h["area"],
            "preparedness_level": h["preparedness_level"],
        }
        # New fires / new large incidents / uncontained large fires / CIMTs
        for label, key in [
            ("New fires", "new_fires"),
            ("New large incidents", "new_large_incidents"),
            ("Uncontained large fires", "uncontained_large_fires"),
            ("CIMTs Committed", "cimts_committed"),
        ]:
            m = re.search(rf"{label}:\s*(\d+)", block)
            if m:
                section[key] = int(m.group(1))
        # Narrative paragraphs — anything between the activity totals and the
        # incident detail table that's wrapped in flowing prose. Heuristic:
        # grab lines that aren't header rows, table column rows, or empty.
        lines = block.splitlines()
        narrative_lines = []
        in_table = False
        for ln in lines:
            s = ln.strip()
            if not s:
                continue
            # Stop at table column header
            if "Incident Name" in s and "% Ctn" in s:
                in_table = True
                continue
            if in_table:
                continue
            if s.startswith(h["area"]):
                continue
            if any(s.startswith(prefix) for prefix in [
                "Fire Activity", "New fires:", "New large incidents:",
                "Uncontained large fires:", "CIMTs Committed:", "Total Chge",
                "Acres Acres",
            ]):
                continue
            narrative_lines.append(s)
        section["narrative"] = " ".join(narrative_lines).strip() or None
        out.append(section)
    return out


def parse_yesterday_table(text: str) -> dict:
    """Page 5 — Fires and Acres Yesterday by Protection."""
    out = {"by_area": [], "totals": {}}
    # Stretch from "Fires and Acres Yesterday" header to the next "Fires and
    # Acres Year-to-Date" header.
    yest_m = re.search(r"^Fires and Acres Yesterday \(by Protection\):", text, re.MULTILINE)
    ytd_m = re.search(r"^Fires and Acres Year-to-Date \(by Protection\):", text, re.MULTILINE)
    if not yest_m:
        return out
    block = text[yest_m.end():ytd_m.start() if ytd_m else len(text)]
    cur = {}
    for area in PROTECTION_AREAS:
        # Match "Northern California FIRES 0 0 0 0 14 2 16" then "Northern California ACRES …"
        for kind in ("FIRES", "ACRES"):
            m = re.search(
                rf"^{re.escape(area)}\s+{kind}\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s*$",
                block, re.MULTILINE,
            )
            if not m:
                continue
            row = {
                "BIA": _to_int(m.group(1)),
                "BLM": _to_int(m.group(2)),
                "FWS": _to_int(m.group(3)),
                "NPS": _to_int(m.group(4)),
                "ST_OT": _to_int(m.group(5)),
                "USFS": _to_int(m.group(6)),
                "TOTAL": _to_int(m.group(7)),
            }
            if area not in cur:
                cur[area] = {}
            cur[area][kind.lower()] = row
    for area, kinds in cur.items():
        out["by_area"].append({
            "area": area,
            "fires": kinds.get("fires", {}),
            "acres": kinds.get("acres", {}),
        })
    # Totals
    m = re.search(
        r"TOTAL FIRES:\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)",
        block,
    )
    if m:
        out["totals"]["fires"] = {
            "BIA": _to_int(m.group(1)), "BLM": _to_int(m.group(2)),
            "FWS": _to_int(m.group(3)), "NPS": _to_int(m.group(4)),
            "ST_OT": _to_int(m.group(5)), "USFS": _to_int(m.group(6)),
            "TOTAL": _to_int(m.group(7)),
        }
    m = re.search(
        r"TOTAL ACRES:\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)",
        block,
    )
    if m:
        out["totals"]["acres"] = {
            "BIA": _to_int(m.group(1)), "BLM": _to_int(m.group(2)),
            "FWS": _to_int(m.group(3)), "NPS": _to_int(m.group(4)),
            "ST_OT": _to_int(m.group(5)), "USFS": _to_int(m.group(6)),
            "TOTAL": _to_int(m.group(7)),
        }
    return out


def parse_ytd_table(text: str) -> dict:
    """Page 6 — Year-to-Date by Protection + 10-year averages."""
    out = {"by_area": [], "totals": {}, "ten_year_average": {}}
    ytd_m = re.search(r"^Fires and Acres Year-to-Date \(by Protection\):", text, re.MULTILINE)
    avg_m = re.search(r"^Ten Year Average Quantity", text, re.MULTILINE)
    if not ytd_m:
        return out
    block = text[ytd_m.end():avg_m.start() if avg_m else len(text)]
    cur = {}
    for area in PROTECTION_AREAS:
        for kind in ("FIRES", "ACRES"):
            m = re.search(
                rf"^{re.escape(area)}\s+{kind}\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s*$",
                block, re.MULTILINE,
            )
            if not m:
                continue
            row = {
                "BIA": _to_int(m.group(1)), "BLM": _to_int(m.group(2)),
                "FWS": _to_int(m.group(3)), "NPS": _to_int(m.group(4)),
                "ST_OT": _to_int(m.group(5)), "USFS": _to_int(m.group(6)),
                "TOTAL": _to_int(m.group(7)),
            }
            if area not in cur:
                cur[area] = {}
            cur[area][kind.lower()] = row
    for area, kinds in cur.items():
        out["by_area"].append({
            "area": area,
            "fires": kinds.get("fires", {}),
            "acres": kinds.get("acres", {}),
        })
    m = re.search(
        r"TOTAL FIRES:\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)",
        block,
    )
    if m:
        out["totals"]["fires"] = {
            "BIA": _to_int(m.group(1)), "BLM": _to_int(m.group(2)),
            "FWS": _to_int(m.group(3)), "NPS": _to_int(m.group(4)),
            "ST_OT": _to_int(m.group(5)), "USFS": _to_int(m.group(6)),
            "TOTAL": _to_int(m.group(7)),
        }
    m = re.search(
        r"TOTAL ACRES:\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)",
        block,
    )
    if m:
        out["totals"]["acres"] = {
            "BIA": _to_int(m.group(1)), "BLM": _to_int(m.group(2)),
            "FWS": _to_int(m.group(3)), "NPS": _to_int(m.group(4)),
            "ST_OT": _to_int(m.group(5)), "USFS": _to_int(m.group(6)),
            "TOTAL": _to_int(m.group(7)),
        }
    # 10-year averages — separator can be hyphen, en-dash, em-dash, or
    # surrounding whitespace variations; match liberally.
    m = re.search(r"Fires \(\d{4}\s*[-–—]\s*\d{4}\s*as of today\)\s+([\d,]+)", text)
    if m:
        out["ten_year_average"]["fires"] = _to_int(m.group(1))
    m = re.search(r"Acres \(\d{4}\s*[-–—]\s*\d{4}\s*as of today\)\s+([\d,]+)", text)
    if m:
        out["ten_year_average"]["acres"] = _to_int(m.group(1))
    return out


def parse_predictive_services(text: str) -> str:
    """Page 7 — Predictive Services Discussion (free text)."""
    m = re.search(r"^Predictive Services Discussion:\s*$", text, re.MULTILINE)
    if not m:
        return ""
    end_m = re.search(
        r"^When a Predictive Services Discussion is not available", text, re.MULTILINE,
    )
    body = text[m.end():end_m.start() if end_m else len(text)]
    # Collapse line wraps in the source PDF — paragraphs are flowed across
    # multiple lines; rejoin whitespace so the AI gets clean prose.
    paragraphs = []
    cur = []
    for ln in body.splitlines():
        s = ln.strip()
        if not s:
            if cur:
                paragraphs.append(" ".join(cur))
                cur = []
            continue
        cur.append(s)
    if cur:
        paragraphs.append(" ".join(cur))
    return "\n\n".join(paragraphs).strip()


def parse_imsr_pdf(pdf_bytes: bytes) -> dict:
    tmp = DATA_DIR / "_tmp.pdf"
    tmp.write_bytes(pdf_bytes)
    try:
        with pdfplumber.open(tmp) as pdf:
            full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
            page1 = pdf.pages[0].extract_text() or ""
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return {
        "summary": parse_top_summary(page1),
        "resource_summary": parse_resource_summary(page1),
        "gacc_sections": parse_gacc_sections(full_text),
        "yesterday_by_protection": parse_yesterday_table(full_text),
        "ytd_by_protection": parse_ytd_table(full_text),
        "predictive_services_discussion": parse_predictive_services(full_text),
    }


def write_outputs(parsed: dict, raw_bytes: bytes, fetch_meta: dict) -> None:
    now = datetime.now(timezone.utc)
    parsed["generated_utc"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    parsed["source_url"] = PDF_URL
    parsed["pdf_bytes"] = len(raw_bytes)
    parsed["fetch"] = fetch_meta
    JSON_PATH.write_text(json.dumps(parsed, indent=2, ensure_ascii=False))
    # Also keep a copy of the most recent raw PDF so the frontend "open IMSR"
    # link can serve from raw.githubusercontent if NIFC is briefly down.
    PDF_PATH.write_bytes(raw_bytes)


def update_health(success: bool, notes: str = "") -> None:
    """Watchdog tracking — consecutive failure count is what we care about.
    If consecutive_failures crosses a threshold we want to dial cron back."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if HEALTH_PATH.exists():
        try:
            health = json.loads(HEALTH_PATH.read_text())
        except Exception:
            health = {}
    else:
        health = {}
    if success:
        health["last_success_utc"] = now
        health["consecutive_failures"] = 0
        health["status"] = "ok"
    else:
        health["last_failure_utc"] = now
        health["consecutive_failures"] = int(health.get("consecutive_failures", 0)) + 1
        health["status"] = "degraded" if health["consecutive_failures"] >= 4 else "ok"
        # 4 consecutive 15-min misses = ~1 hour silent; that's the dial-back signal.
    health["last_attempt_utc"] = now
    health["last_notes"] = notes
    health["cadence_minutes"] = 15
    HEALTH_PATH.write_text(json.dumps(health, indent=2))


def main() -> int:
    fetch_started = datetime.now(timezone.utc)
    try:
        raw = fetch_pdf()
    except Exception as e:
        print(f"FETCH FAIL: {e}", file=sys.stderr)
        update_health(success=False, notes=f"fetch error: {e}")
        return 1
    fetch_meta = {
        "fetched_utc": fetch_started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_seconds": round((datetime.now(timezone.utc) - fetch_started).total_seconds(), 2),
    }
    try:
        parsed = parse_imsr_pdf(raw)
    except Exception as e:
        print(f"PARSE FAIL: {e}", file=sys.stderr)
        update_health(success=False, notes=f"parse error: {e}")
        return 1
    if not parsed.get("summary"):
        print("PARSE WARN: empty summary — header parser missed", file=sys.stderr)
        update_health(success=False, notes="empty summary block")
        return 1
    # Row-count guards. The failure mode that actually bit us was NOT an
    # exception or a dead URL — it was a structurally-valid PDF whose page-1
    # table silently stopped matching after NICC added a column. The pipeline
    # reported healthy with resource_summary == [] for ~2 months. Never again:
    # write the payload anyway (the other sections stay useful) but record a
    # FAILURE so data/health.json degrades and `firestorm-health` catches it.
    warnings = []
    if not parsed.get("resource_summary"):
        warnings.append("resource_summary EMPTY — page-1 GACC table matched no rows (NICC layout change?)")
    if not parsed.get("gacc_sections"):
        warnings.append("gacc_sections EMPTY — page 2-4 narrative parser matched no sections")
    parsed["parse_warnings"] = warnings

    write_outputs(parsed, raw, fetch_meta)

    if warnings:
        print("PARSE WARN: " + " | ".join(warnings), file=sys.stderr)
        update_health(success=False, notes="; ".join(warnings))
        return 1

    update_health(success=True, notes=f"parsed pl={parsed['summary'].get('national_preparedness_level')} large_fires={parsed['summary'].get('uncontained_large_fires')}")
    print(f"OK: PL={parsed['summary'].get('national_preparedness_level')} "
          f"uncontained_large={parsed['summary'].get('uncontained_large_fires')} "
          f"gacc_sections={len(parsed['gacc_sections'])} "
          f"resource_rows={len(parsed['resource_summary'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
