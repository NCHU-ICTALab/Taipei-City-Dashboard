# Static Parking Rate (TPE) Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a single-file Python data pipeline that crawls 雙北 (TPE + NTPC) road-side parking rate data, parses fees and charging hours, geocodes road segments via Overpass (with Nominatim fallback), and writes to PostgreSQL `parking_rate_tpe` table.

**Architecture:** Four-stage functional pipeline (`crawl → preprocess → geocode_all → save_to_postgres`) following the project's existing `static_*.py` style. Single file in `Taipei-City-Dashboard-DE/data_preprocess/`. TRUNCATE+INSERT update mode preserves the table on partial failure. No modification of any existing file.

**Tech Stack:** Python 3.10+, `psycopg2`, `requests`, `geopy` (Nominatim + RateLimiter), `odfpy` (ODS parser), stdlib (`argparse`, `json`, `re`, `time`, `logging`, `datetime`). All packages already in `Taipei-City-Dashboard-DE/docker/develop/requirements.txt`.

**Testing approach:** This project's `data_preprocess/` scripts have no unit test pattern (only `dags/test/` and `cicd/utils/` have pytest files). To match project convention and minimize merge friction (single new file), this plan uses **inline `assert` smoke tests** within each development task and **runtime verification** against the spec's acceptance criteria. No new pytest files, no `tests/` directory created.

**Spec reference:** `docs/superpowers/specs/2026-05-03-static-parking-rate-tpe-design.md`

**Branch:** `feature/static-parking-rate-tpe` (already created from `develop`, currently has the design spec commit)

---

## File Structure

**Single new file:**
- `Taipei-City-Dashboard-DE/data_preprocess/static_parking_rate_tpe.py` — entire pipeline

**Files NOT modified (acceptance criterion 8):**
- `config.py`, `requirements.txt`, all other `static_*.py`, `dags/`, `docker/`, etc.

**Module organization within the file** (top-to-bottom):
1. Module docstring + imports
2. Constants (URLs, regex patterns, User-Agent, time-text mappings)
3. Pure helpers (`parse_rate`, `parse_tiered`, `parse_time_range`, `is_no_charge`)
4. Crawl functions (`crawl_ntpc`, `crawl_tpe`)
5. Preprocess function (`preprocess`)
6. Geocode functions (`query_overpass`, `geocode_one`, `geocode_all`)
7. Persistence (`save_to_postgres`)
8. CLI entry (`__main__` block)

---

## Task 1: Module skeleton + constants + pure helpers

**Files:**
- Create: `Taipei-City-Dashboard-DE/data_preprocess/static_parking_rate_tpe.py`

**Goal of this task:** Build the file scaffold with all imports, constants, and the four pure helper functions. Verify each helper with inline `assert` smoke tests that exit non-zero on failure.

- [ ] **Step 1: Create the file with module docstring, imports, and constants**

Write the file with this initial content:

```python
"""
static_parking_rate_tpe.py - 雙北路邊停車費率資料管線

資料來源：
  - 新北市 OpenData API: https://data.ntpc.gov.tw/api/datasets/d9f18db5-41c7-41d4-b7f0-82a335255b08/json
  - 台北市 OpenData ODS: https://data.taipei/api/frontstage/tpeod/dataset/resource.download?rid=86d2a8b6-c360-4349-956d-dd7771cccf91

Pipeline: crawl_ntpc + crawl_tpe -> preprocess -> geocode_all -> save_to_postgres
更新模式：TRUNCATE + INSERT（保留 schema）
"""
import argparse
import json
import logging
import re
import time
from datetime import time as dtime

import psycopg2
import requests
from geopy.extra.rate_limiter import RateLimiter
from geopy.geocoders import Nominatim
from odf.opendocument import load as load_ods
from odf.table import Table, TableRow, TableCell
from odf.text import P
from psycopg2.extras import execute_batch

from config import PG_DASHBOARD

# ── URLs ───────────────────────────────────────────────────────────────────────
NTPC_API     = "https://data.ntpc.gov.tw/api/datasets/d9f18db5-41c7-41d4-b7f0-82a335255b08/json"
TPE_ODS_URL  = "https://data.taipei/api/frontstage/tpeod/dataset/resource.download?rid=86d2a8b6-c360-4349-956d-dd7771cccf91"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# ── Geocode 設定 ───────────────────────────────────────────────────────────────
USER_AGENT             = "taipei-dashboard-parking-rate/1.0 (charles@j-tcg.com)"
OVERPASS_TIMEOUT       = 25
OVERPASS_DELAY_SEC     = 0.5
NOMINATIM_DELAY_SEC    = 1.1
NOMINATIM_TIMEOUT      = 10
PROGRESS_REPORT_EVERY  = 50

# ── 文字解析常數 ───────────────────────────────────────────────────────────────
RATE_RE   = re.compile(r"(\d+)\s*元\s*/\s*時")
TIME_RE   = re.compile(r"(\d{1,2}):(\d{2})")
NO_CHARGE_TOKENS = ("無收費", "未收費", "不收費")

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("parking_rate")


# ── 純解析 helper ──────────────────────────────────────────────────────────────
def is_no_charge(text: str | None) -> bool:
    """True when the time-text means 'no charge' for that day type."""
    if not text:
        return True
    return any(tok in text for tok in NO_CHARGE_TOKENS)


def parse_rate(text: str | None) -> int | None:
    """First '<N>元/時' → N. None on no match or no_charge."""
    if not text or is_no_charge(text):
        return None
    m = RATE_RE.search(text)
    return int(m.group(1)) if m else None


def parse_tiered(text: str | None) -> list[dict] | None:
    """Tiered rate breakdown. Returns None when the text contains < 2 rate matches.

    Example:
        "前2小時30元/時\\n第3小時以上40元/時"
        -> [{"range":"前2小時","rate":30},{"range":"第3小時以上","rate":40}]
    """
    if not text or is_no_charge(text):
        return None
    matches = list(RATE_RE.finditer(text))
    if len(matches) < 2:
        return None
    tiers: list[dict] = []
    cursor = 0
    for m in matches:
        # range = text from cursor up to the rate number, stripped
        range_text = text[cursor:m.start()].strip().strip("\n").strip()
        # remove trailing punctuation/whitespace and obvious artifacts
        range_text = range_text.rstrip(",，、 \t\r\n")
        tiers.append({"range": range_text or None, "rate": int(m.group(1))})
        cursor = m.end()
    return tiers


def parse_time_range(text: str | None) -> tuple[dtime | None, dtime | None]:
    """Earliest start and latest end as datetime.time. (None, None) on no_charge / no match.

    Examples:
        "07:00~20:00"               -> (07:00, 20:00)
        "07:00-12:00, 14:00-20:00"  -> (07:00, 20:00)   # earliest start, latest end
        "無收費"                    -> (None, None)
    """
    if not text or is_no_charge(text):
        return (None, None)
    times = TIME_RE.findall(text)
    if not times:
        return (None, None)
    parsed = [dtime(int(h), int(m)) for h, m in times if 0 <= int(h) <= 23 and 0 <= int(m) <= 59]
    if not parsed:
        return (None, None)
    return (min(parsed), max(parsed))


# ── (Steps 2-9 to be added in subsequent tasks) ────────────────────────────────


if __name__ == "__main__":
    # NOTE: full CLI added in Task 9. For Task 1 we only run smoke tests.
    pass
```

- [ ] **Step 2: Add inline smoke-test block at the bottom of the file (above `if __name__ == "__main__"`)**

Append (above the `if __name__ == "__main__":` block):

```python
def _smoke_test_helpers() -> None:
    """Asserts pure helpers behave as documented. Run via `python static_parking_rate_tpe.py --smoke-test`."""
    # is_no_charge
    assert is_no_charge("無收費") is True
    assert is_no_charge("") is True
    assert is_no_charge(None) is True
    assert is_no_charge("07:00~20:00") is False

    # parse_rate
    assert parse_rate("30元/時") == 30
    assert parse_rate("前2小時30元/時\n第3小時以上40元/時") == 30
    assert parse_rate("無收費") is None
    assert parse_rate(None) is None
    assert parse_rate("") is None
    assert parse_rate("收費") is None

    # parse_tiered
    assert parse_tiered("30元/時") is None  # single rate -> not tiered
    tiers = parse_tiered("前2小時30元/時\n第3小時以上40元/時")
    assert tiers == [
        {"range": "前2小時", "rate": 30},
        {"range": "第3小時以上", "rate": 40},
    ], f"unexpected tiers: {tiers}"
    assert parse_tiered("無收費") is None
    assert parse_tiered(None) is None

    # parse_time_range
    assert parse_time_range("07:00~20:00") == (dtime(7, 0), dtime(20, 0))
    assert parse_time_range("07:00-12:00, 14:00-20:00") == (dtime(7, 0), dtime(20, 0))
    assert parse_time_range("無收費") == (None, None)
    assert parse_time_range(None) == (None, None)
    assert parse_time_range("") == (None, None)

    print("[OK] All helper smoke tests passed.")
```

Then update the `__main__` block to:

```python
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="雙北路邊停車費率資料管線")
    parser.add_argument("--smoke-test", action="store_true",
                        help="Run inline helper smoke tests and exit.")
    args = parser.parse_args()

    if args.smoke_test:
        _smoke_test_helpers()
        raise SystemExit(0)

    # Full pipeline added in Task 9.
    print("[NOOP] Full pipeline implemented in Task 9 onward.")
```

- [ ] **Step 3: Run smoke tests to verify all four helpers**

Run from the repo root:

```bash
cd Taipei-City-Dashboard-DE/data_preprocess && python static_parking_rate_tpe.py --smoke-test
```

Expected output (last line):
```
[OK] All helper smoke tests passed.
```

If any assert fails, fix the helper logic and re-run until green.

- [ ] **Step 4: Verify the file does not modify any existing file**

Run from repo root:

```bash
git status
```

Expected output (only the new file is added, nothing modified):
```
On branch feature/static-parking-rate-tpe
Untracked files:
  Taipei-City-Dashboard-DE/data_preprocess/static_parking_rate_tpe.py
```

- [ ] **Step 5: Commit**

```bash
git add Taipei-City-Dashboard-DE/data_preprocess/static_parking_rate_tpe.py
git commit -m "$(cat <<'EOF'
feat(parking_rate): add module skeleton and pure-helper smoke tests

- Constants (URLs, regex, geocode timing)
- parse_rate / parse_tiered / parse_time_range / is_no_charge
- --smoke-test CLI flag for inline assertions
EOF
)"
```

---

## Task 2: NTPC crawler

**Files:**
- Modify: `Taipei-City-Dashboard-DE/data_preprocess/static_parking_rate_tpe.py`

**Goal of this task:** Implement `crawl_ntpc()` that fetches the JSON API and returns a list of records tagged with `_source = "NTPC"`. Smoke test by fetching live data and asserting basic shape.

- [ ] **Step 1: Insert `crawl_ntpc()` between the helpers and the smoke-test block**

Add this function:

```python
# ── Step 1: 爬取 ────────────────────────────────────────────────────────────────
def crawl_ntpc() -> list[dict]:
    """Fetch NTPC parking rate API. Returns list of dicts each tagged with _source='NTPC'.
    Returns [] on HTTP/JSON error (logged as error, does not raise)."""
    log.info("[NTPC] GET %s", NTPC_API)
    try:
        res = requests.get(NTPC_API, timeout=60)
        res.raise_for_status()
        rows = res.json()
    except (requests.RequestException, ValueError) as exc:
        log.error("[NTPC] crawl failed: %s", exc)
        return []
    if not isinstance(rows, list):
        log.error("[NTPC] unexpected response type: %s", type(rows).__name__)
        return []
    for r in rows:
        r["_source"] = "NTPC"
    log.info("[NTPC] fetched %d rows", len(rows))
    return rows
```

- [ ] **Step 2: Add an NTPC smoke step inside `_smoke_test_helpers()` guarded by a `--with-network` flag**

Update the `_smoke_test_helpers()` signature to take a `with_network: bool` argument and add the NTPC check:

```python
def _smoke_test_helpers(with_network: bool = False) -> None:
    """Asserts helpers + (optionally) live crawls behave as documented."""
    # ... existing pure-helper asserts unchanged ...

    if with_network:
        rows = crawl_ntpc()
        assert len(rows) > 100, f"NTPC crawl returned too few rows: {len(rows)}"
        assert {"county", "area", "road_name", "rates", "_source"}.issubset(rows[0].keys()), \
            f"NTPC row missing expected keys: {rows[0].keys()}"
        assert rows[0]["_source"] == "NTPC"
        log.info("[OK] NTPC crawl smoke test passed (%d rows).", len(rows))

    print("[OK] All helper smoke tests passed.")
```

Update the `__main__` block to pass the flag through:

```python
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="雙北路邊停車費率資料管線")
    parser.add_argument("--smoke-test", action="store_true",
                        help="Run inline helper smoke tests and exit.")
    parser.add_argument("--with-network", action="store_true",
                        help="Also exercise live HTTP smoke checks.")
    args = parser.parse_args()

    if args.smoke_test:
        _smoke_test_helpers(with_network=args.with_network)
        raise SystemExit(0)

    print("[NOOP] Full pipeline implemented in Task 9 onward.")
```

- [ ] **Step 3: Run smoke test with network enabled**

Run:

```bash
cd Taipei-City-Dashboard-DE/data_preprocess && python static_parking_rate_tpe.py --smoke-test --with-network
```

Expected: NTPC line shows ≥ 100 rows; final `[OK]` line printed; exit code 0.

If the API is unreachable, fix any URL/header issues. Do **not** add retries — this is a one-shot script; manual rerun is acceptable per spec容錯.

- [ ] **Step 4: Commit**

```bash
git add Taipei-City-Dashboard-DE/data_preprocess/static_parking_rate_tpe.py
git commit -m "feat(parking_rate): add NTPC JSON API crawler"
```

---

## Task 3: TPE ODS crawler

**Files:**
- Modify: `Taipei-City-Dashboard-DE/data_preprocess/static_parking_rate_tpe.py`

**Goal of this task:** Implement `crawl_tpe()` that downloads the ODS file and parses it via `odfpy`. Each record tagged `_source = "TPE"`.

- [ ] **Step 1: Add `_ods_cell_text()` helper and `crawl_tpe()` after `crawl_ntpc()`**

```python
def _ods_cell_text(cell: TableCell) -> str:
    """Concatenate all <text:p> contents inside a TableCell (handles repeated cols)."""
    parts: list[str] = []
    for p in cell.getElementsByType(P):
        # odfpy: walk child text nodes
        parts.append("".join(node.data for node in p.childNodes if hasattr(node, "data")))
    text = "\n".join(parts).strip()
    # Honour table:number-columns-repeated by NOT repeating; we read each column once and
    # let downstream count alignment by header order.
    return text


def crawl_tpe() -> list[dict]:
    """Download TPE ODS, parse first sheet. Returns list of dicts tagged _source='TPE'.
    Returns [] on download / parse error."""
    log.info("[TPE]  GET %s", TPE_ODS_URL)
    try:
        res = requests.get(TPE_ODS_URL, timeout=60)
        res.raise_for_status()
    except requests.RequestException as exc:
        log.error("[TPE] download failed: %s", exc)
        return []

    # Save to temp path (odfpy needs a path or file-like)
    import io
    try:
        doc = load_ods(io.BytesIO(res.content))
    except Exception as exc:  # odfpy may raise generic Exception
        log.error("[TPE] ODS parse failed: %s", exc)
        return []

    tables = doc.getElementsByType(Table)
    if not tables:
        log.error("[TPE] ODS contains no tables")
        return []

    sheet = tables[0]
    raw_rows: list[list[str]] = []
    for tr in sheet.getElementsByType(TableRow):
        row_cells = [_ods_cell_text(c) for c in tr.getElementsByType(TableCell)]
        # Skip fully-blank rows
        if any(cell.strip() for cell in row_cells):
            raw_rows.append(row_cells)

    if len(raw_rows) < 2:
        log.error("[TPE] ODS has no data rows (got %d rows)", len(raw_rows))
        return []

    header = [h.strip() for h in raw_rows[0]]
    rows: list[dict] = []
    for r in raw_rows[1:]:
        # Pad / truncate to header length
        cells = (r + [""] * len(header))[:len(header)]
        rec = {k: v.strip() for k, v in zip(header, cells)}
        rec["_source"] = "TPE"
        rows.append(rec)

    log.info("[TPE]  parsed %d rows (header: %s)", len(rows), header)
    return rows
```

- [ ] **Step 2: Add a TPE smoke check inside `_smoke_test_helpers()` (under the existing `if with_network:` block)**

```python
        rows_tpe = crawl_tpe()
        assert len(rows_tpe) > 50, f"TPE crawl returned too few rows: {len(rows_tpe)}"
        # Spec lists these Chinese headers; assert at least the four critical ones exist
        critical = {"路段名稱", "收費時間", "費率（元）", "收費日（星期）"}
        first_keys = set(rows_tpe[0].keys())
        missing = critical - first_keys
        # If naming drift occurs, log keys to aid debugging
        assert not missing, f"TPE missing critical headers {missing}; got {first_keys}"
        assert rows_tpe[0]["_source"] == "TPE"
        log.info("[OK] TPE crawl smoke test passed (%d rows).", len(rows_tpe))
```

- [ ] **Step 3: Run smoke test**

```bash
cd Taipei-City-Dashboard-DE/data_preprocess && python static_parking_rate_tpe.py --smoke-test --with-network
```

Expected: Both `[OK] NTPC crawl ...` and `[OK] TPE crawl ...` lines, then final `[OK] All helper smoke tests passed.`

If TPE header names differ from spec (e.g., parens style or whitespace), update the `critical` set and the column lookup keys you'll use in Task 4. Log the actual headers on assertion failure to make this easy to discover.

- [ ] **Step 4: Commit**

```bash
git add Taipei-City-Dashboard-DE/data_preprocess/static_parking_rate_tpe.py
git commit -m "feat(parking_rate): add TPE ODS downloader and parser"
```

---

## Task 4: Preprocess function

**Files:**
- Modify: `Taipei-City-Dashboard-DE/data_preprocess/static_parking_rate_tpe.py`

**Goal of this task:** Implement `preprocess(rows)` that normalises both NTPC and TPE shapes into the unified record schema (28 logical fields, geometry filled in Task 7).

- [ ] **Step 1: Add NTPC- and TPE-specific normalise helpers, then `preprocess()`**

Insert after `crawl_tpe()`:

```python
# ── Step 2: 預處理 ─────────────────────────────────────────────────────────────
DAY_KEYS = ("weekday", "saturday", "sunday", "holiday")


def _build_rate_schedule(per_day_hours_text: dict[str, str], rate_text: str) -> dict:
    """Compose rate_schedule JSONB structure from per-day hours_text + raw rate text."""
    main_rate = parse_rate(rate_text)
    tiers     = parse_tiered(rate_text)
    sched: dict = {}
    for d in DAY_KEYS:
        ht = per_day_hours_text.get(d, "")
        charging = not is_no_charge(ht)
        sched[d] = {
            "charging":   charging,
            "hours_text": ht,
            "rate":       main_rate if charging else None,
            "tiers":      tiers     if charging else None,
        }
    return sched


def _normalise_ntpc(r: dict) -> dict | None:
    """Convert one NTPC raw row into the unified record (no geometry yet)."""
    road_segment = (r.get("road_name") or "").strip()
    district     = (r.get("area") or "").strip()
    if not road_segment or not district:
        return None

    per_day_hours_text = {
        "weekday":  (r.get("weekdays_time") or "").strip(),
        "saturday": (r.get("sat._charging_time") or "").strip(),
        "sunday":   (r.get("sun._charging_time") or "").strip(),
        "holiday":  (r.get("national_holidays_charging_time") or "").strip(),
    }
    rate_text = (r.get("rates") or "").strip()
    return _assemble_record(
        source="NTPC",
        city=(r.get("county") or "新北市").strip(),
        district=district,
        road_segment=road_segment,
        segment_endpoints=None,
        per_day_hours_text=per_day_hours_text,
        rate_text=rate_text,
    )


# Map TPE 收費日（星期） tokens to which DAY_KEYS receive the rate
_TPE_DAY_TOKEN_MAP = {
    "週一": "weekday", "週二": "weekday", "週三": "weekday",
    "週四": "weekday", "週五": "weekday",
    "週六": "saturday", "週日": "sunday",
    "國定假日": "holiday", "假日": "holiday",
}


def _expand_tpe_day_text(day_text: str) -> set[str]:
    """'週一至週五' -> {weekday}; '週一至週日' -> {weekday, saturday, sunday}; etc."""
    if not day_text:
        return set()
    days: set[str] = set()
    # Range patterns "X至Y"
    for m in re.finditer(r"(週.|國定假日|假日)\s*至\s*(週.|國定假日|假日)", day_text):
        a, b = m.group(1), m.group(2)
        order = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
        if a in order and b in order:
            for d in order[order.index(a):order.index(b) + 1]:
                if d in _TPE_DAY_TOKEN_MAP:
                    days.add(_TPE_DAY_TOKEN_MAP[d])
        else:
            days.add(_TPE_DAY_TOKEN_MAP.get(a, ""))
            days.add(_TPE_DAY_TOKEN_MAP.get(b, ""))
    # Singletons
    for tok, key in _TPE_DAY_TOKEN_MAP.items():
        if tok in day_text:
            days.add(key)
    days.discard("")
    return days


def _normalise_tpe(r: dict) -> dict | None:
    """Convert one TPE raw row into the unified record (no geometry yet)."""
    road_segment = (r.get("路段名稱") or "").strip()
    district     = (r.get("行政區") or "").strip()
    if not road_segment or not district:
        return None

    hours_text   = (r.get("收費時間") or "").strip()
    day_text     = (r.get("收費日（星期）") or "").strip()
    rate_text    = (r.get("費率（元）") or "").strip()
    endpoints    = (r.get("起迄路段") or "").strip() or None

    charging_days = _expand_tpe_day_text(day_text)
    per_day_hours_text = {
        d: (hours_text if d in charging_days else "無收費")
        for d in DAY_KEYS
    }
    return _assemble_record(
        source="TPE",
        city="臺北市",
        district=district,
        road_segment=road_segment,
        segment_endpoints=endpoints,
        per_day_hours_text=per_day_hours_text,
        rate_text=rate_text,
    )


def _assemble_record(
    source: str,
    city: str,
    district: str,
    road_segment: str,
    segment_endpoints: str | None,
    per_day_hours_text: dict[str, str],
    rate_text: str,
) -> dict:
    """Build the unified record dict (geometry placeholders left None)."""
    schedule  = _build_rate_schedule(per_day_hours_text, rate_text)
    main_rate = parse_rate(rate_text)
    tiers     = parse_tiered(rate_text)

    rec: dict = {
        "source":            source,
        "city":              city,
        "district":          district,
        "road_segment":      road_segment,
        "segment_endpoints": segment_endpoints,
        "rate_text":         rate_text or None,
        "tiered_rates":      tiers,
        "rate_schedule":     schedule,
        # geometry placeholders (filled in Task 7)
        "start_lat":         None,
        "start_lon":         None,
        "geometry_path":     None,
        "geom_source":       None,
    }
    for d in DAY_KEYS:
        ht = per_day_hours_text.get(d, "")
        charging = not is_no_charge(ht)
        rec[f"{d}_rate"]       = main_rate if charging else None
        rec[f"{d}_hours_text"] = ht or None
        start_t, end_t = parse_time_range(ht)
        rec[f"{d}_start_time"] = start_t
        rec[f"{d}_end_time"]   = end_t
    return rec


def preprocess(rows: list[dict]) -> list[dict]:
    """Normalise NTPC + TPE rows into unified records. Drops rows missing road/district."""
    log.info("[預處理] %d raw rows", len(rows))
    out: list[dict] = []
    skipped = 0
    for r in rows:
        src = r.get("_source")
        if src == "NTPC":
            rec = _normalise_ntpc(r)
        elif src == "TPE":
            rec = _normalise_tpe(r)
        else:
            rec = None
        if rec is None:
            skipped += 1
            continue
        out.append(rec)
    log.info("[預處理] kept %d, dropped %d (missing road / district)", len(out), skipped)
    return out
```

- [ ] **Step 2: Extend `_smoke_test_helpers()` with preprocess assertions on synthetic inputs**

Add right before the `if with_network:` block (so it runs offline):

```python
    # preprocess — NTPC tiered case
    ntpc_row = {
        "_source": "NTPC",
        "county": "新北市", "area": "板橋區", "road_name": "大同街",
        "weekdays_time": "07:00~20:00",
        "sat._charging_time": "無收費",
        "sun._charging_time": "無收費",
        "national_holidays_charging_time": "無收費",
        "rates": "前2小時30元/時\n第3小時以上40元/時",
    }
    rec = preprocess([ntpc_row])
    assert len(rec) == 1, rec
    r0 = rec[0]
    assert r0["source"] == "NTPC" and r0["city"] == "新北市"
    assert r0["road_segment"] == "大同街" and r0["district"] == "板橋區"
    assert r0["weekday_rate"] == 30
    assert r0["saturday_rate"] is None
    assert r0["weekday_start_time"] == dtime(7, 0)
    assert r0["weekday_end_time"]   == dtime(20, 0)
    assert r0["tiered_rates"] == [
        {"range": "前2小時", "rate": 30},
        {"range": "第3小時以上", "rate": 40},
    ]
    assert r0["rate_schedule"]["weekday"]["charging"] is True
    assert r0["rate_schedule"]["saturday"]["charging"] is False

    # preprocess — TPE all-week case
    tpe_row = {
        "_source": "TPE",
        "路段名稱": "信義路四段", "行政區": "大安區",
        "起迄路段": "信義路一段~信義路五段",
        "收費時間": "08:00-20:00",
        "收費日（星期）": "週一至週日",
        "費率（元）": "60元/時",
    }
    rec2 = preprocess([tpe_row])
    assert len(rec2) == 1
    t0 = rec2[0]
    assert t0["source"] == "TPE" and t0["city"] == "臺北市"
    assert t0["weekday_rate"]  == 60
    assert t0["saturday_rate"] == 60
    assert t0["sunday_rate"]   == 60
    assert t0["holiday_rate"]  is None       # 國定假日 not in 週一至週日 expansion
    assert t0["segment_endpoints"] == "信義路一段~信義路五段"
    assert t0["tiered_rates"] is None
```

- [ ] **Step 3: Run offline smoke test**

```bash
cd Taipei-City-Dashboard-DE/data_preprocess && python static_parking_rate_tpe.py --smoke-test
```

Expected: `[OK] All helper smoke tests passed.` (no network needed for preprocess block).

If any assertion fails, fix the corresponding helper (`_normalise_ntpc`, `_expand_tpe_day_text`, `_build_rate_schedule`) and re-run.

- [ ] **Step 4: Commit**

```bash
git add Taipei-City-Dashboard-DE/data_preprocess/static_parking_rate_tpe.py
git commit -m "feat(parking_rate): add preprocess to unified record schema"
```

---

## Task 5: Overpass query function

**Files:**
- Modify: `Taipei-City-Dashboard-DE/data_preprocess/static_parking_rate_tpe.py`

**Goal of this task:** Implement `query_overpass(road, city, district)` returning either a `list[[lat, lon]]` polyline or `None` on miss/error. No fallback yet — that's Task 6.

- [ ] **Step 1: Add `query_overpass()` after `preprocess()`**

```python
# ── Step 3: Geocode ────────────────────────────────────────────────────────────
OVERPASS_QUERY_TEMPLATE = """
[out:json][timeout:{timeout}];
area["name"="{city}"]["admin_level"="4"]->.city;
area(area.city)["name"="{district}"]["admin_level"="7"]->.district;
way(area.district)["highway"]["name"="{road}"];
out geom;
"""


def _dedupe_consecutive(coords: list[list[float]]) -> list[list[float]]:
    """Remove only adjacent duplicates (keeps the polyline shape)."""
    out: list[list[float]] = []
    for c in coords:
        if not out or out[-1] != c:
            out.append(c)
    return out


def query_overpass(road: str, city: str, district: str) -> list[list[float]] | None:
    """Return [[lat, lon], ...] for the named highway way(s), or None on miss/error."""
    query = OVERPASS_QUERY_TEMPLATE.format(
        timeout=OVERPASS_TIMEOUT,
        city=city,
        district=district,
        road=road,
    )
    try:
        res = requests.post(
            OVERPASS_URL,
            data={"data": query},
            headers={"User-Agent": USER_AGENT},
            timeout=OVERPASS_TIMEOUT + 5,
        )
        res.raise_for_status()
        payload = res.json()
    except (requests.RequestException, ValueError) as exc:
        log.warning("[Overpass] %s/%s/%s error: %s", city, district, road, exc)
        return None

    elements = payload.get("elements", [])
    if not elements:
        return None

    coords: list[list[float]] = []
    for el in elements:
        for node in el.get("geometry", []):
            coords.append([node["lat"], node["lon"]])
    coords = _dedupe_consecutive(coords)
    return coords or None
```

- [ ] **Step 2: Add an Overpass smoke check (network-only)**

Inside the `if with_network:` block of `_smoke_test_helpers()`, append:

```python
        coords = query_overpass("信義路四段", "臺北市", "大安區")
        assert coords is not None and len(coords) >= 2, \
            f"Overpass should return polyline for 信義路四段, got {coords}"
        assert all(120 < c[1] < 122 and 24 < c[0] < 26 for c in coords), \
            "Overpass coords outside Taiwan bbox"
        log.info("[OK] Overpass smoke test passed (%d nodes for 信義路四段).", len(coords))
        time.sleep(OVERPASS_DELAY_SEC)  # respect rate limit before next test
```

- [ ] **Step 3: Run network smoke test**

```bash
cd Taipei-City-Dashboard-DE/data_preprocess && python static_parking_rate_tpe.py --smoke-test --with-network
```

Expected: `[OK] Overpass smoke test passed (N nodes for 信義路四段).` line where N ≥ 2.

If `None` is returned, the OSM way name format may differ. Try a different known road (e.g., `大同街` in `板橋區`, `新北市`); update the smoke test accordingly. If Overpass repeatedly times out, retry once before failing the task — Overpass public servers occasionally throttle.

- [ ] **Step 4: Commit**

```bash
git add Taipei-City-Dashboard-DE/data_preprocess/static_parking_rate_tpe.py
git commit -m "feat(parking_rate): add Overpass API polyline query"
```

---

## Task 6: Nominatim fallback + `geocode_one`

**Files:**
- Modify: `Taipei-City-Dashboard-DE/data_preprocess/static_parking_rate_tpe.py`

**Goal of this task:** Wire Nominatim as the fallback path and provide `geocode_one(rec, nominatim_geocode)` that mutates a record in place with geometry fields, returning `True` on success / `False` on total failure.

- [ ] **Step 1: Add `_make_nominatim_geocoder()` and `geocode_one()` after `query_overpass()`**

```python
def _make_nominatim_geocoder():
    """Build a rate-limited Nominatim geocode callable."""
    geolocator = Nominatim(user_agent=USER_AGENT, timeout=NOMINATIM_TIMEOUT)
    return RateLimiter(geolocator.geocode, min_delay_seconds=NOMINATIM_DELAY_SEC,
                       error_wait_seconds=2.0, max_retries=2, swallow_exceptions=True)


def geocode_one(rec: dict, nominatim_geocode) -> bool:
    """Fill rec['start_lat'/'start_lon'/'geometry_path'/'geom_source'] in place.

    Returns True if geometry obtained (Overpass or Nominatim), False if both failed."""
    coords = query_overpass(rec["road_segment"], rec["city"], rec["district"])
    if coords:
        rec["geometry_path"] = coords
        rec["start_lat"]     = coords[0][0]
        rec["start_lon"]     = coords[0][1]
        rec["geom_source"]   = "OVERPASS"
        return True

    # Nominatim fallback
    query = f"{rec['road_segment']}, {rec['district']}, {rec['city']}, Taiwan"
    location = nominatim_geocode(query)
    if location is not None:
        lat, lon = location.latitude, location.longitude
        rec["geometry_path"] = [[lat, lon]]
        rec["start_lat"]     = lat
        rec["start_lon"]     = lon
        rec["geom_source"]   = "NOMINATIM_FALLBACK"
        return True

    return False
```

- [ ] **Step 2: Add a geocode_one smoke check (network-only)**

Inside the `if with_network:` block of `_smoke_test_helpers()`, append:

```python
        nomi = _make_nominatim_geocoder()
        sample = {
            "road_segment": "信義路四段", "district": "大安區", "city": "臺北市",
        }
        ok = geocode_one(sample, nomi)
        assert ok, "geocode_one failed for 信義路四段"
        assert sample["geom_source"] in ("OVERPASS", "NOMINATIM_FALLBACK")
        assert isinstance(sample["geometry_path"], list) and sample["geometry_path"]
        assert sample["start_lat"] and sample["start_lon"]
        log.info("[OK] geocode_one smoke test passed (source=%s, points=%d).",
                 sample["geom_source"], len(sample["geometry_path"]))
        time.sleep(OVERPASS_DELAY_SEC)
```

- [ ] **Step 3: Run network smoke test**

```bash
cd Taipei-City-Dashboard-DE/data_preprocess && python static_parking_rate_tpe.py --smoke-test --with-network
```

Expected: `[OK] geocode_one smoke test passed (source=OVERPASS, points=N).`

- [ ] **Step 4: Commit**

```bash
git add Taipei-City-Dashboard-DE/data_preprocess/static_parking_rate_tpe.py
git commit -m "feat(parking_rate): add Nominatim fallback and geocode_one orchestrator"
```

---

## Task 7: `geocode_all` with progress reporting

**Files:**
- Modify: `Taipei-City-Dashboard-DE/data_preprocess/static_parking_rate_tpe.py`

**Goal of this task:** Loop over records, call `geocode_one`, drop on total failure, log progress every 50 records with Overpass / Nominatim_fb / Failed counts and ETA.

- [ ] **Step 1: Add `geocode_all()` after `geocode_one()`**

```python
def geocode_all(records: list[dict]) -> list[dict]:
    """Geocode all records sequentially. Returns the subset that succeeded."""
    nomi = _make_nominatim_geocoder()
    total = len(records)
    log.info("[Geom] 開始：總計 %d 筆待處理", total)

    started = time.time()
    kept: list[dict] = []
    n_overpass = n_nominatim = n_failed = 0

    for idx, rec in enumerate(records, start=1):
        ok = geocode_one(rec, nomi)
        if ok:
            if rec["geom_source"] == "OVERPASS":
                n_overpass += 1
            else:
                n_nominatim += 1
            kept.append(rec)
        else:
            n_failed += 1
            log.warning("[Geom] drop %s / %s / %s (no Overpass nor Nominatim hit)",
                        rec["city"], rec["district"], rec["road_segment"])

        # Respect Overpass rate limit between records (Nominatim is throttled by RateLimiter)
        time.sleep(OVERPASS_DELAY_SEC)

        if idx % PROGRESS_REPORT_EVERY == 0 or idx == total:
            elapsed = time.time() - started
            rate    = idx / elapsed if elapsed > 0 else 0
            remaining_sec = (total - idx) / rate if rate > 0 else 0
            log.info(
                "[Geom] %4d/%-4d  Overpass %d  Nominatim_fb %d  Failed %d  進度 %5.1f%%  剩餘 ~%dm%02ds",
                idx, total, n_overpass, n_nominatim, n_failed,
                100.0 * idx / total,
                int(remaining_sec // 60), int(remaining_sec % 60),
            )

    elapsed = time.time() - started
    keep_pct = 100.0 * len(kept) / total if total else 0.0
    log.info(
        "[Geom] 完成：Overpass %d / Nominatim_fb %d / Failed %d (保留率 %.1f%%)，耗時 %dm%02ds",
        n_overpass, n_nominatim, n_failed, keep_pct,
        int(elapsed // 60), int(elapsed % 60),
    )
    return kept
```

- [ ] **Step 2: Manually verify the progress reporting on a tiny live batch**

Add a temporary `--geocode-demo` flag in the `__main__` block (this stays in the script — it's a useful debug entry):

```python
    parser.add_argument("--geocode-demo", action="store_true",
                        help="Run preprocess on first 5 NTPC rows + geocode + print result.")
    # ...
    if args.geocode_demo:
        rows = crawl_ntpc()[:5]
        recs = preprocess(rows)
        out  = geocode_all(recs)
        for r in out:
            print(f"  {r['source']} {r['district']} {r['road_segment']} -> "
                  f"{r['geom_source']} {len(r['geometry_path'])} pts "
                  f"start=({r['start_lat']:.4f},{r['start_lon']:.4f})")
        raise SystemExit(0)
```

- [ ] **Step 3: Run the demo**

```bash
cd Taipei-City-Dashboard-DE/data_preprocess && python static_parking_rate_tpe.py --geocode-demo
```

Expected (partial):
```
[NTPC] fetched ~1058 rows
[預處理] kept 5, dropped 0 ...
[Geom] 開始：總計 5 筆待處理
[Geom]    5/5    Overpass X  Nominatim_fb Y  Failed Z  進度 100.0%  剩餘 ~0m00s
[Geom] 完成：...
  NTPC 板橋區 大同街 -> OVERPASS NN pts start=(25.xxxx,121.xxxx)
  ...
```

If 0 rows survive geocoding, debug the Overpass query (likely admin_level mismatch). Update query template until ≥ 60 % of the 5-row sample succeeds.

- [ ] **Step 4: Commit**

```bash
git add Taipei-City-Dashboard-DE/data_preprocess/static_parking_rate_tpe.py
git commit -m "feat(parking_rate): add geocode_all with progress reporting"
```

---

## Task 8: `save_to_postgres`

**Files:**
- Modify: `Taipei-City-Dashboard-DE/data_preprocess/static_parking_rate_tpe.py`

**Goal of this task:** Implement `save_to_postgres(records)` that creates the table if absent, TRUNCATEs, then inserts in batches. JSONB fields serialised via `json.dumps(...)`.

- [ ] **Step 1: Add `save_to_postgres()` after `geocode_all()`**

```python
# ── Step 4: 寫入 PostgreSQL ────────────────────────────────────────────────────
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS public.parking_rate_tpe (
    ogc_fid              SERIAL           PRIMARY KEY,
    source               VARCHAR(10)      NOT NULL,
    city                 VARCHAR(20)      NOT NULL,
    district             VARCHAR(20)      NOT NULL,
    road_segment         TEXT             NOT NULL,
    segment_endpoints    TEXT,
    weekday_rate         INTEGER,
    saturday_rate        INTEGER,
    sunday_rate          INTEGER,
    holiday_rate         INTEGER,
    weekday_start_time   TIME,
    weekday_end_time     TIME,
    saturday_start_time  TIME,
    saturday_end_time    TIME,
    sunday_start_time    TIME,
    sunday_end_time      TIME,
    holiday_start_time   TIME,
    holiday_end_time     TIME,
    weekday_hours_text   TEXT,
    saturday_hours_text  TEXT,
    sunday_hours_text    TEXT,
    holiday_hours_text   TEXT,
    tiered_rates         JSONB,
    rate_schedule        JSONB,
    rate_text            TEXT,
    start_lat            DOUBLE PRECISION NOT NULL,
    start_lon            DOUBLE PRECISION NOT NULL,
    geometry_path        JSONB            NOT NULL,
    geom_source          VARCHAR(25)      NOT NULL,
    data_time            TIMESTAMPTZ      DEFAULT CURRENT_TIMESTAMP,
    _ctime               TIMESTAMPTZ      DEFAULT CURRENT_TIMESTAMP,
    _mtime               TIMESTAMPTZ      DEFAULT CURRENT_TIMESTAMP
)
"""

INSERT_SQL = """
INSERT INTO public.parking_rate_tpe (
    source, city, district, road_segment, segment_endpoints,
    weekday_rate, saturday_rate, sunday_rate, holiday_rate,
    weekday_start_time, weekday_end_time,
    saturday_start_time, saturday_end_time,
    sunday_start_time, sunday_end_time,
    holiday_start_time, holiday_end_time,
    weekday_hours_text, saturday_hours_text, sunday_hours_text, holiday_hours_text,
    tiered_rates, rate_schedule, rate_text,
    start_lat, start_lon, geometry_path, geom_source,
    data_time, _mtime
) VALUES (
    %(source)s, %(city)s, %(district)s, %(road_segment)s, %(segment_endpoints)s,
    %(weekday_rate)s, %(saturday_rate)s, %(sunday_rate)s, %(holiday_rate)s,
    %(weekday_start_time)s, %(weekday_end_time)s,
    %(saturday_start_time)s, %(saturday_end_time)s,
    %(sunday_start_time)s, %(sunday_end_time)s,
    %(holiday_start_time)s, %(holiday_end_time)s,
    %(weekday_hours_text)s, %(saturday_hours_text)s, %(sunday_hours_text)s, %(holiday_hours_text)s,
    %(tiered_rates_json)s, %(rate_schedule_json)s, %(rate_text)s,
    %(start_lat)s, %(start_lon)s, %(geometry_path_json)s, %(geom_source)s,
    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
)
"""


def _to_db_row(rec: dict) -> dict:
    """Convert in-memory record into the param dict for INSERT_SQL (JSONB serialised)."""
    out = dict(rec)
    out["tiered_rates_json"]   = json.dumps(rec.get("tiered_rates"),   ensure_ascii=False) if rec.get("tiered_rates")   is not None else None
    out["rate_schedule_json"]  = json.dumps(rec.get("rate_schedule"),  ensure_ascii=False) if rec.get("rate_schedule")  is not None else None
    out["geometry_path_json"]  = json.dumps(rec.get("geometry_path"),  ensure_ascii=False)
    return out


def save_to_postgres(records: list[dict]) -> None:
    log.info("[PostgreSQL] 連線中 ...")
    conn = psycopg2.connect(**PG_DASHBOARD)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(CREATE_TABLE_SQL)
                cur.execute("TRUNCATE TABLE public.parking_rate_tpe RESTART IDENTITY")
                payload = [_to_db_row(r) for r in records]
                execute_batch(cur, INSERT_SQL, payload, page_size=500)
        log.info("[PostgreSQL] 寫入完成（覆蓋 %d 筆）", len(records))
    finally:
        conn.close()
```

- [ ] **Step 2: Add a save smoke check (DB-only) gated by `--with-db`**

Update the `__main__` parser:

```python
    parser.add_argument("--with-db", action="store_true",
                        help="Smoke-test save_to_postgres against PG_DASHBOARD (writes 1 fake row, then truncates).")
```

Add a smoke block under it (before the existing `--smoke-test`/`--geocode-demo` handling):

```python
    if args.with_db:
        fake = [{
            "source": "NTPC", "city": "新北市", "district": "板橋區",
            "road_segment": "__SMOKE_TEST__", "segment_endpoints": None,
            "weekday_rate": 30, "saturday_rate": None, "sunday_rate": None, "holiday_rate": None,
            "weekday_start_time": dtime(7, 0), "weekday_end_time": dtime(20, 0),
            "saturday_start_time": None, "saturday_end_time": None,
            "sunday_start_time": None,   "sunday_end_time": None,
            "holiday_start_time": None,  "holiday_end_time": None,
            "weekday_hours_text": "07:00~20:00", "saturday_hours_text": None,
            "sunday_hours_text": None,   "holiday_hours_text": None,
            "tiered_rates": None,
            "rate_schedule": {"weekday": {"charging": True, "hours_text": "07:00~20:00", "rate": 30, "tiers": None}},
            "rate_text": "30元/時",
            "start_lat": 25.0, "start_lon": 121.5,
            "geometry_path": [[25.0, 121.5]],
            "geom_source": "NOMINATIM_FALLBACK",
        }]
        save_to_postgres(fake)
        # Verify the fake row landed
        with psycopg2.connect(**PG_DASHBOARD) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*), min(road_segment) FROM public.parking_rate_tpe")
                count, road = cur.fetchone()
        assert count == 1 and road == "__SMOKE_TEST__", (count, road)
        log.info("[OK] save_to_postgres smoke test passed.")
        raise SystemExit(0)
```

- [ ] **Step 3: Run the DB smoke test**

```bash
cd Taipei-City-Dashboard-DE/data_preprocess && python static_parking_rate_tpe.py --with-db
```

Expected: `[OK] save_to_postgres smoke test passed.` and exit 0.

If the connection fails, verify `PG_DASHBOARD` host/port — `data_preprocess/config.py` defaults to `localhost:5433`. Make sure the dashboard Postgres docker container is running.

- [ ] **Step 4: Commit**

```bash
git add Taipei-City-Dashboard-DE/data_preprocess/static_parking_rate_tpe.py
git commit -m "feat(parking_rate): add save_to_postgres with TRUNCATE+INSERT"
```

---

## Task 9: CLI orchestration + main entry

**Files:**
- Modify: `Taipei-City-Dashboard-DE/data_preprocess/static_parking_rate_tpe.py`

**Goal of this task:** Replace the stub `__main__` with the full pipeline orchestration (`--sources`, `--dry-run`, `--limit`) per the spec's CLI interface.

- [ ] **Step 1: Replace the entire `if __name__ == "__main__":` block**

Replace the current block with:

```python
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="雙北路邊停車費率資料管線")
    parser.add_argument("--sources", nargs="+", choices=["ntpc", "tpe"],
                        default=["ntpc", "tpe"],
                        help="哪些來源要跑（預設兩者皆跑）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只跑 crawl + preprocess + geocode，不寫入 DB")
    parser.add_argument("--limit", type=int, default=None,
                        help="處理前 N 筆（debug 用）")
    # debug / smoke flags retained from earlier tasks
    parser.add_argument("--smoke-test", action="store_true",
                        help="Run inline helper smoke tests and exit.")
    parser.add_argument("--with-network", action="store_true",
                        help="Also exercise live HTTP smoke checks (use with --smoke-test).")
    parser.add_argument("--with-db", action="store_true",
                        help="Smoke-test save_to_postgres and exit.")
    parser.add_argument("--geocode-demo", action="store_true",
                        help="Crawl NTPC[:5] + preprocess + geocode + print, then exit.")
    args = parser.parse_args()

    # Debug shortcuts (retained as useful)
    if args.smoke_test:
        _smoke_test_helpers(with_network=args.with_network)
        raise SystemExit(0)
    if args.with_db:
        fake = [{
            "source": "NTPC", "city": "新北市", "district": "板橋區",
            "road_segment": "__SMOKE_TEST__", "segment_endpoints": None,
            "weekday_rate": 30, "saturday_rate": None, "sunday_rate": None, "holiday_rate": None,
            "weekday_start_time": dtime(7, 0), "weekday_end_time": dtime(20, 0),
            "saturday_start_time": None, "saturday_end_time": None,
            "sunday_start_time": None,   "sunday_end_time": None,
            "holiday_start_time": None,  "holiday_end_time": None,
            "weekday_hours_text": "07:00~20:00", "saturday_hours_text": None,
            "sunday_hours_text": None,   "holiday_hours_text": None,
            "tiered_rates": None,
            "rate_schedule": {"weekday": {"charging": True, "hours_text": "07:00~20:00", "rate": 30, "tiers": None}},
            "rate_text": "30元/時",
            "start_lat": 25.0, "start_lon": 121.5,
            "geometry_path": [[25.0, 121.5]],
            "geom_source": "NOMINATIM_FALLBACK",
        }]
        save_to_postgres(fake)
        with psycopg2.connect(**PG_DASHBOARD) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*), min(road_segment) FROM public.parking_rate_tpe")
                count, road = cur.fetchone()
        assert count == 1 and road == "__SMOKE_TEST__", (count, road)
        log.info("[OK] save_to_postgres smoke test passed.")
        raise SystemExit(0)

    if args.geocode_demo:
        rows = crawl_ntpc()[:5]
        recs = preprocess(rows)
        out  = geocode_all(recs)
        for r in out:
            print(f"  {r['source']} {r['district']} {r['road_segment']} -> "
                  f"{r['geom_source']} {len(r['geometry_path'])} pts "
                  f"start=({r['start_lat']:.4f},{r['start_lon']:.4f})")
        raise SystemExit(0)

    # ── Full pipeline ────────────────────────────────────────────────────────
    log.info("=" * 60)
    log.info("Parking Rate Pipeline | sources=%s dry_run=%s limit=%s",
             args.sources, args.dry_run, args.limit)
    log.info("=" * 60)

    rows: list[dict] = []
    if "ntpc" in args.sources:
        rows.extend(crawl_ntpc())
    if "tpe" in args.sources:
        rows.extend(crawl_tpe())
    if not rows:
        log.error("[錯誤] 兩個來源都失敗，未動 DB，結束。")
        raise SystemExit(1)

    records = preprocess(rows)
    if args.limit:
        records = records[: args.limit]
        log.info("[限制] --limit %d 套用後剩 %d 筆", args.limit, len(records))

    geocoded = geocode_all(records)
    if not geocoded:
        log.error("[錯誤] geocode 後無有效資料，未動 DB，結束。")
        raise SystemExit(1)

    if args.dry_run:
        log.info("[DRY-RUN] %d 筆已備妥（未寫入 DB）", len(geocoded))
        raise SystemExit(0)

    save_to_postgres(geocoded)

    log.info("=" * 60)
    log.info("完成！%d 筆寫入 parking_rate_tpe", len(geocoded))
    log.info("=" * 60)
```

- [ ] **Step 2: Quick CLI sanity — `--help` works**

```bash
cd Taipei-City-Dashboard-DE/data_preprocess && python static_parking_rate_tpe.py --help
```

Expected: `--sources`, `--dry-run`, `--limit`, plus the debug flags listed.

- [ ] **Step 3: End-to-end dry run on small sample**

```bash
cd Taipei-City-Dashboard-DE/data_preprocess && python static_parking_rate_tpe.py --sources ntpc --limit 5 --dry-run
```

Expected: Pipeline runs through to geocode_all; logs `[DRY-RUN] 5 筆已備妥`. No DB writes.

- [ ] **Step 4: Commit**

```bash
git add Taipei-City-Dashboard-DE/data_preprocess/static_parking_rate_tpe.py
git commit -m "feat(parking_rate): wire full pipeline orchestration in main"
```

---

## Task 10: Acceptance criteria validation + final cleanup

**Files:**
- (No file modifications expected; validation and any final tweaks only.)

**Goal of this task:** Walk through the spec's 10 acceptance criteria one by one and produce evidence each is met. Fix anything that fails before declaring done.

- [ ] **Step 1: Acceptance #1 — Successful first run writes ≥ 1 row**

Run a constrained pipeline so this finishes in ~ 1 minute (full geocode would take 22 minutes; `--limit 30` is enough to validate the schema):

```bash
cd Taipei-City-Dashboard-DE/data_preprocess && python static_parking_rate_tpe.py --limit 30
```

Then verify:

```bash
psql -h localhost -p 5433 -U postgres -d dashboard \
  -c "SELECT count(*) FROM public.parking_rate_tpe;"
```

Expected: count > 0 (typically 25-30 depending on geocode hit rate).

- [ ] **Step 2: Acceptance #2 — Re-run TRUNCATEs and refreshes**

Re-run the same command:

```bash
cd Taipei-City-Dashboard-DE/data_preprocess && python static_parking_rate_tpe.py --limit 30
```

Verify count still ≤ 30 (no accumulation):

```bash
psql -h localhost -p 5433 -U postgres -d dashboard \
  -c "SELECT count(*) FROM public.parking_rate_tpe;"
```

Expected: count ≤ 30 and equal to (or close to) the previous run's count. If it doubled, TRUNCATE failed.

- [ ] **Step 3: Acceptance #3 — Single-source run works**

```bash
cd Taipei-City-Dashboard-DE/data_preprocess && python static_parking_rate_tpe.py --sources tpe --limit 10
```

Verify:

```bash
psql -h localhost -p 5433 -U postgres -d dashboard \
  -c "SELECT DISTINCT source FROM public.parking_rate_tpe;"
```

Expected: only `TPE` (NTPC rows replaced).

- [ ] **Step 4: Acceptance #4 — Both sources fail → exit 1, table preserved**

Temporarily break both URLs by patching constants in a one-shot Python invocation (do **not** modify the source file):

```bash
cd Taipei-City-Dashboard-DE/data_preprocess && python -c "
import static_parking_rate_tpe as m
m.NTPC_API = 'https://invalid.invalid/x'
m.TPE_ODS_URL = 'https://invalid.invalid/y'
import sys
sys.argv = ['p', '--limit', '5']
exec(open('static_parking_rate_tpe.py').read())
"
echo "exit code: $?"
```

Expected: exit code `1`, error log line about `兩個來源都失敗`.

Verify table not cleared (still has rows from Step 3):

```bash
psql -h localhost -p 5433 -U postgres -d dashboard \
  -c "SELECT count(*) FROM public.parking_rate_tpe;"
```

Expected: count == count from Step 3 (not zero).

- [ ] **Step 5: Acceptance #5 — Progress log every 50 records**

```bash
cd Taipei-City-Dashboard-DE/data_preprocess && python static_parking_rate_tpe.py --sources ntpc --limit 110 2>&1 | grep "\[Geom\]"
```

Expected: At least three `[Geom]` lines — start banner, mid-progress at 50, mid-progress at 100, completion banner; lines include Overpass / Nominatim_fb / Failed counts and ETA.

- [ ] **Step 6: Acceptance #6 — Tiered rate parsed for 板橋大同街**

Find that record (filter NTPC + 板橋區 + 大同街):

```bash
psql -h localhost -p 5433 -U postgres -d dashboard -c \
  "SELECT road_segment, weekday_rate, tiered_rates
   FROM public.parking_rate_tpe
   WHERE source='NTPC' AND district='板橋區' AND road_segment='大同街';"
```

Expected: `weekday_rate = 30` and `tiered_rates` is a non-NULL JSON array with two objects (前2小時 / 第3小時以上).

If the row isn't present (geocode dropped it), rerun with `--limit 200` to increase the chance of including it, or temporarily run without `--limit` if time permits.

- [ ] **Step 7: Acceptance #7 — At least one Overpass-sourced polyline**

```bash
psql -h localhost -p 5433 -U postgres -d dashboard -c \
  "SELECT count(*) FROM public.parking_rate_tpe
   WHERE geom_source='OVERPASS' AND jsonb_array_length(geometry_path) > 1;"
```

Expected: count > 0.

- [ ] **Step 8: Acceptance #8 — git diff is clean (only the new file + spec + plan)**

```bash
git status
git log --oneline develop..HEAD
git diff develop --stat
```

Expected:
- `git status` shows clean working tree
- `git log` shows the spec commit + plan commit + 9 implementation commits (one per task) — all on `feature/static-parking-rate-tpe`
- `git diff` only mentions `docs/superpowers/specs/...`, `docs/superpowers/plans/...`, and `Taipei-City-Dashboard-DE/data_preprocess/static_parking_rate_tpe.py` — no other paths

- [ ] **Step 9: Acceptance #9 — `--dry-run` does not touch DB**

```bash
psql -h localhost -p 5433 -U postgres -d dashboard \
  -c "SELECT count(*), max(_mtime) FROM public.parking_rate_tpe;" \
  > /tmp/before.txt
cd Taipei-City-Dashboard-DE/data_preprocess && python static_parking_rate_tpe.py --sources ntpc --limit 5 --dry-run
psql -h localhost -p 5433 -U postgres -d dashboard \
  -c "SELECT count(*), max(_mtime) FROM public.parking_rate_tpe;" \
  > /tmp/after.txt
diff /tmp/before.txt /tmp/after.txt && echo "[OK] dry-run did not touch DB"
```

Expected: `[OK] dry-run did not touch DB` (no diff in count/max(_mtime)).

- [ ] **Step 10: Acceptance #10 — `--limit 10` runs in seconds**

```bash
time (cd Taipei-City-Dashboard-DE/data_preprocess && python static_parking_rate_tpe.py --limit 10)
```

Expected: real time < 30 seconds (depends on Overpass latency; 10 records × ~0.6s + Nominatim fallbacks).

- [ ] **Step 11: Final commit (only if last-mile fixes were needed)**

If any acceptance criterion needed a fix, commit it:

```bash
git add Taipei-City-Dashboard-DE/data_preprocess/static_parking_rate_tpe.py
git commit -m "fix(parking_rate): address acceptance criterion <N>"
```

If everything passed cleanly: nothing to commit; just announce done.

---

## Done

The branch `feature/static-parking-rate-tpe` should now contain:

1. `docs/superpowers/specs/2026-05-03-static-parking-rate-tpe-design.md` (already committed)
2. `docs/superpowers/plans/2026-05-03-static-parking-rate-tpe.md` (this file)
3. `Taipei-City-Dashboard-DE/data_preprocess/static_parking_rate_tpe.py` (built across Tasks 1-9, validated in Task 10)

Push and open a PR against `develop`:

```bash
git push -u origin feature/static-parking-rate-tpe
gh pr create --base develop --title "feat: 雙北路邊停車費率資料管線 (static_parking_rate_tpe)" \
  --body "依據 docs/superpowers/specs/2026-05-03-static-parking-rate-tpe-design.md 實作。單檔，不改任何既有程式。"
```
