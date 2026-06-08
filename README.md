# firestorm-imsr-data

Pipeline that mirrors NICC's daily **Incident Management Situation Report (IMSR)** as machine-readable JSON for the FIRESTORM dashboard.

## What it does

1. Pulls today's IMSR PDF from `https://www.nifc.gov/nicc-files/sitreprt.pdf` every ~15 min via GitHub Actions.
2. Parses the structured sections with `pdfplumber`:
   - National preparedness level + national fire-activity totals
   - Per-GACC Active Incident Resource Summary (page 1 table)
   - Per-GACC narrative + uncontained-large-fire detail (pages 2–4)
   - Yesterday's fires/acres by protection (page 5)
   - Year-to-date fires/acres + 10-year average (page 6)
   - Predictive Services Discussion (page 7)
3. Writes the parsed structure to `data/imsr.json` and commits.
4. Tracks pipeline health in `data/health.json` (consecutive failure count, last success/failure timestamp).
5. Mirrors the raw PDF at `data/imsr.pdf` so the FIRESTORM "open IMSR" link can fall back to GitHub if NIFC is briefly down.

## Why

WFIGS gives FIRESTORM live IRWIN incident geometry, but the IMSR is the authoritative human-curated daily summary that fire managers and ICs read first thing in the morning. Surfacing it directly closes the gap between live map data and the daily ops narrative.

## Schema (`data/imsr.json`)

```jsonc
{
  "generated_utc": "2026-06-08T15:54:10Z",
  "source_url": "https://www.nifc.gov/nicc-files/sitreprt.pdf",
  "pdf_bytes": 203424,
  "fetch": { "fetched_utc": "...", "duration_seconds": 0.42 },

  "summary": {
    "report_date_human": "Monday June 8, 2026",
    "report_time": "0730 MDT",
    "national_preparedness_level": 2,
    "initial_attack_activity": "Light (91 fires)",
    "new_large_incidents": 2,
    "large_fires_contained": 1,
    "uncontained_large_fires": 11,
    "cimts_committed": 1,
    "nimos_committed": 0
  },

  "resource_summary": [
    {
      "gacc": "AICC",
      "incidents": 2, "cumulative_acres": 1466,
      "crews": 3, "engines": 0, "helicopters": 0,
      "total_personnel": 87, "change_in_personnel": 0
    },
    /* ...one row per GACC, plus a final {"gacc":"Total", ...} row */
  ],

  "gacc_sections": [
    {
      "gacc": "SWCC",
      "area": "Southwest Area",
      "preparedness_level": 3,
      "new_fires": 10,
      "new_large_incidents": 0,
      "uncontained_large_fires": 1,
      "cimts_committed": 1,
      "narrative": "Seven Cabins, Lincoln NF, USFS. CIMT (SW Team 2). ..."
    }
    /* IMSR omits sections for GACCs with no activity, so this array
       is typically 5–9 entries — not always all 10. */
  ],

  "yesterday_by_protection": {
    "by_area": [{"area":"Northwest","fires":{"BIA":1,"BLM":14,...,"TOTAL":15},"acres":{...}}, ...],
    "totals": {"fires":{...},"acres":{...}}
  },
  "ytd_by_protection": {
    "by_area": [...],
    "totals": {...},
    "ten_year_average": {"fires": 23015, "acres": 1334280}
  },

  "predictive_services_discussion": "Widespread elevated to critical conditions..."
}
```

## Schema (`data/health.json`)

```jsonc
{
  "last_success_utc": "2026-06-08T15:54:10Z",
  "last_failure_utc": null,
  "last_attempt_utc": "2026-06-08T15:54:10Z",
  "consecutive_failures": 0,
  "status": "ok",            // "ok" | "degraded" (4+ consecutive misses)
  "cadence_minutes": 15,
  "last_notes": "parsed pl=2 large_fires=11"
}
```

The watchdog is the dial-back signal: if `status` flips to `degraded` for an extended period, drop cron from `*/15` to `0 * * * *` and investigate.

## Cadence rationale

- IMSR publishes once daily, ~06:30 MT.
- NICC sometimes re-publishes a corrected version mid-day.
- 15 min catches corrections within an acceptable window.
- 15 min × 96 fetches/day at ~200 KB = ~19 MB/day — trivial for nifc.gov; no rate-limit risk at this volume.
- If NIFC ever asks us to back off, the watchdog signals it and we move to hourly.

## Source attribution

NICC Incident Management Situation Report — public domain (USDA / DOI joint publication, no copyright). Attribute as "NICC IMSR" in any user-facing display. Source URL and `Last-Modified` header preserved in `imsr.json` for audit.

## Running locally

```bash
pip install pdfplumber
python fetch_imsr.py
# Outputs:
#   data/imsr.json
#   data/imsr.pdf
#   data/health.json
```

## Family

Same architecture as the sibling pipelines under `Deasus/firestorm-*-data`:
- `firestorm-ngfs-data` — CIMSS/SSEC fire detections
- `firestorm-goes-fire-data` — GOES-R ABI fire/hot-spot
- `firestorm-lightning-data` — GOES-R GLM lightning
- `firestorm-spread-data` — PyreCast spread-forecast envelopes
- `firestorm-cameras` — AlertWest PTZ camera registry
- `firestorm-aircraft-data` — ADSBx aircraft snapshots
- `firestorm-progression-data` — fire-perimeter progression
- `firestorm-satellite-data` — TLE registry
- `firestorm-hrrr-data` — HRRR 3km surface thermo

Public repo → free GHA minutes → $0/mo to operate.
