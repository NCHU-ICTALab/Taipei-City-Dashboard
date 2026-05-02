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
    """Tiered rate breakdown. Returns None when the text contains < 2 rate matches."""
    if not text or is_no_charge(text):
        return None
    matches = list(RATE_RE.finditer(text))
    if len(matches) < 2:
        return None
    tiers: list[dict] = []
    cursor = 0
    for m in matches:
        range_text = text[cursor:m.start()].strip().strip("\n").strip()
        range_text = range_text.rstrip(",，、 \t\r\n")
        tiers.append({"range": range_text or None, "rate": int(m.group(1))})
        cursor = m.end()
    return tiers


def parse_time_range(text: str | None) -> tuple[dtime | None, dtime | None]:
    """Earliest start and latest end as datetime.time. (None, None) on no_charge / no match."""
    if not text or is_no_charge(text):
        return (None, None)
    times = TIME_RE.findall(text)
    if not times:
        return (None, None)
    parsed = [dtime(int(h), int(m)) for h, m in times if 0 <= int(h) <= 23 and 0 <= int(m) <= 59]
    if not parsed:
        return (None, None)
    return (min(parsed), max(parsed))


# ── Step 1: 爬取 ────────────────────────────────────────────────────────────────
def crawl_ntpc(page_size: int = 1000) -> list[dict]:
    """Fetch NTPC parking rate API across all pages. Returns list of dicts tagged _source='NTPC'.
    Returns [] on HTTP/JSON error (logged as error, does not raise)."""
    all_rows: list[dict] = []
    page = 0
    while True:
        url = f"{NTPC_API}?page={page}&size={page_size}"
        log.info("[NTPC] GET page=%d size=%d", page, page_size)
        try:
            res = requests.get(url, timeout=60)
            res.raise_for_status()
            rows = res.json()
        except (requests.RequestException, ValueError) as exc:
            log.error("[NTPC] crawl failed at page %d: %s", page, exc)
            return [] if not all_rows else all_rows
        if not isinstance(rows, list):
            log.error("[NTPC] unexpected response type at page %d: %s", page, type(rows).__name__)
            return all_rows
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < page_size:
            break
        page += 1
    for r in all_rows:
        r["_source"] = "NTPC"
    log.info("[NTPC] fetched %d rows total", len(all_rows))
    return all_rows


def _ods_cell_text(cell: TableCell) -> str:
    """Concatenate all <text:p> contents inside a TableCell."""
    parts: list[str] = []
    for p in cell.getElementsByType(P):
        parts.append("".join(node.data for node in p.childNodes if hasattr(node, "data")))
    return "\n".join(parts).strip()


def _expand_row(tr: TableRow, max_width: int) -> list[str]:
    """Expand <table:table-cell number-columns-repeated="N"/> into N separate values.
    Returns at most max_width entries (trims the trailing 16k-empty padding)."""
    out: list[str] = []
    for cell in tr.getElementsByType(TableCell):
        n = int(cell.getAttribute("numbercolumnsrepeated") or 1)
        text = _ods_cell_text(cell)
        # Cap pathological repeats; if we already have enough columns, stop early
        if len(out) >= max_width:
            break
        n = min(n, max_width - len(out))
        out.extend([text] * n)
    return out


def crawl_tpe() -> list[dict]:
    """Download TPE ODS, parse first sheet. Returns list of dicts tagged _source='TPE'.
    Returns [] on download / parse error."""
    import io

    log.info("[TPE]  GET %s", TPE_ODS_URL)
    try:
        res = requests.get(TPE_ODS_URL, timeout=60)
        res.raise_for_status()
    except requests.RequestException as exc:
        log.error("[TPE] download failed: %s", exc)
        return []

    try:
        doc = load_ods(io.BytesIO(res.content))
    except Exception as exc:
        log.error("[TPE] ODS parse failed: %s", exc)
        return []

    tables = doc.getElementsByType(Table)
    if not tables:
        log.error("[TPE] ODS contains no tables")
        return []

    sheet = tables[0]
    trs = sheet.getElementsByType(TableRow)
    if not trs:
        log.error("[TPE] ODS first sheet has no rows")
        return []

    # Header (row 0) gives us the canonical width
    header_cells = _expand_row(trs[0], max_width=64)
    # Trim trailing blanks in header (the ODS pads to 16k empty cols)
    while header_cells and not header_cells[-1].strip():
        header_cells.pop()
    header = [h.strip() for h in header_cells]
    width = len(header)

    rows: list[dict] = []
    for tr in trs[1:]:
        cells = _expand_row(tr, max_width=width)
        cells = (cells + [""] * width)[:width]
        if not any(c.strip() for c in cells):
            continue
        rec = {k: v.strip() for k, v in zip(header, cells)}
        # Need 路段名稱 to be non-empty (skip blank artefacts)
        if not rec.get("路段名稱"):
            continue
        rec["_source"] = "TPE"
        rows.append(rec)

    log.info("[TPE]  parsed %d rows (header=%s)", len(rows), header)
    return rows


# ── Step 2: 預處理 ─────────────────────────────────────────────────────────────
DAY_KEYS = ("weekday", "saturday", "sunday", "holiday")


def _build_rate_schedule(per_day: dict[str, dict]) -> dict:
    """Compose rate_schedule JSONB from per-day dict {hours_text, rate, tiers}."""
    sched: dict = {}
    for d in DAY_KEYS:
        slot = per_day.get(d) or {}
        ht = slot.get("hours_text") or ""
        charging = bool(ht) and not is_no_charge(ht)
        sched[d] = {
            "charging":   charging,
            "hours_text": ht,
            "rate":       slot.get("rate") if charging else None,
            "tiers":      slot.get("tiers") if charging else None,
        }
    return sched


def _assemble_record(
    source: str,
    city: str,
    district: str | None,
    road_segment: str,
    segment_endpoints: str | None,
    rate_text: str | None,
    per_day: dict[str, dict],
) -> dict:
    """Build the unified record dict (geometry placeholders left None)."""
    schedule = _build_rate_schedule(per_day)
    main_rate = next(
        (per_day[d].get("rate") for d in DAY_KEYS
         if per_day.get(d) and per_day[d].get("rate") is not None),
        None,
    )
    main_tiers = next(
        (per_day[d].get("tiers") for d in DAY_KEYS
         if per_day.get(d) and per_day[d].get("tiers") is not None),
        None,
    )
    rec: dict = {
        "source":            source,
        "city":              city,
        "district":          district,
        "road_segment":      road_segment,
        "segment_endpoints": segment_endpoints,
        "rate_text":         rate_text or None,
        "tiered_rates":      main_tiers,
        "rate_schedule":     schedule,
        "start_lat":         None,
        "start_lon":         None,
        "geometry_path":     None,
        "geom_source":       None,
    }
    for d in DAY_KEYS:
        slot = per_day.get(d) or {}
        ht = slot.get("hours_text") or ""
        charging = bool(ht) and not is_no_charge(ht)
        rec[f"{d}_rate"]       = slot.get("rate") if charging else None
        rec[f"{d}_hours_text"] = ht or None
        rec[f"{d}_start_time"] = slot.get("start_time")
        rec[f"{d}_end_time"]   = slot.get("end_time")
    # Hint: main rate fallback, used by acceptance criteria query
    if rec["weekday_rate"] is None and main_rate is not None and per_day.get("weekday", {}).get("hours_text"):
        rec["weekday_rate"] = main_rate
    return rec


# ── NTPC parser ────────────────────────────────────────────────────────────────
def _normalise_ntpc(r: dict) -> dict | None:
    """Convert one NTPC raw row into the unified record (no geometry yet)."""
    road_segment = (r.get("road_name") or "").strip()
    district     = (r.get("area") or "").strip()
    if not road_segment or not district:
        return None

    rate_text = (r.get("rates") or "").strip()
    main_rate  = parse_rate(rate_text)
    main_tiers = parse_tiered(rate_text)

    per_day_hours = {
        "weekday":  (r.get("weekdays_time") or "").strip(),
        "saturday": (r.get("sat._charging_time") or "").strip(),
        "sunday":   (r.get("sun._charging_time") or "").strip(),
        "holiday":  (r.get("national_holidays_charging_time") or "").strip(),
    }
    per_day = {}
    for d, ht in per_day_hours.items():
        charging = not is_no_charge(ht)
        start_t, end_t = parse_time_range(ht)
        per_day[d] = {
            "hours_text": ht,
            "start_time": start_t,
            "end_time":   end_t,
            "rate":       main_rate if charging else None,
            "tiers":      main_tiers if charging else None,
        }
    return _assemble_record(
        source="NTPC",
        city=(r.get("county") or "新北市").strip(),
        district=district,
        road_segment=road_segment,
        segment_endpoints=None,
        rate_text=rate_text,
        per_day=per_day,
    )


# ── TPE parser ─────────────────────────────────────────────────────────────────
def _parse_tpe_time_range(text: str) -> tuple[dtime | None, dtime | None, str | None]:
    """'9-17' -> (09:00, 17:00, '09:00-17:00'). '0-24' -> 24h. Empty -> (None,None,None)."""
    text = (text or "").strip()
    if not text or is_no_charge(text):
        return (None, None, None)
    m = re.match(r"^\s*(\d{1,2})\s*[\-~～]\s*(\d{1,2})\s*$", text)
    if not m:
        return (None, None, None)
    h1, h2 = int(m.group(1)), int(m.group(2))
    if not (0 <= h1 <= 24 and 0 <= h2 <= 24):
        return (None, None, None)
    end_time = dtime(0, 0) if h2 == 24 else dtime(h2, 0)
    start_time = dtime(h1 if h1 < 24 else 0, 0)
    norm = f"{h1:02d}:00-{h2:02d}:00"
    return (start_time, end_time, norm)


def _expand_tpe_day_range(text: str) -> set[str]:
    """'1-6' -> {weekday, saturday}; '1-5' -> {weekday}; '1-7' -> {weekday, saturday, sunday}.
    Single digit '3' -> {weekday}. '6,7' -> {saturday, sunday} (rare)."""
    text = (text or "").strip()
    if not text:
        return set()
    days: set[int] = set()
    # range
    m = re.match(r"^\s*(\d)\s*[\-~～]\s*(\d)\s*$", text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if 1 <= a <= 7 and 1 <= b <= 7 and a <= b:
            days.update(range(a, b + 1))
    else:
        # comma-separated singletons
        for tok in re.findall(r"\d", text):
            n = int(tok)
            if 1 <= n <= 7:
                days.add(n)
    out: set[str] = set()
    if any(1 <= d <= 5 for d in days):
        out.add("weekday")
    if 6 in days:
        out.add("saturday")
    if 7 in days:
        out.add("sunday")
    return out


def _normalise_tpe(r: dict) -> dict | None:
    """Convert one TPE raw row into the unified record (no geometry / no district yet)."""
    road_segment = (r.get("路段名稱") or "").strip()
    if not road_segment:
        return None

    endpoints = (r.get("起迄路段") or "").strip() or None
    rate_text = (r.get("費率（元）") or "").strip()
    time_text = (r.get("收費時間") or "").strip()
    day_text  = (r.get("收費日（星期）") or "").strip()

    # If no rate AND no time AND no day, this row is structural noise — drop
    if not rate_text and not time_text and not day_text:
        return None

    # TPE rate is bare integer like "50"; fall back to NTPC-style "N元/時" just in case
    rate: int | None = None
    if rate_text.isdigit():
        rate = int(rate_text)
    else:
        rate = parse_rate(rate_text)

    start_t, end_t, norm_hours = _parse_tpe_time_range(time_text)
    charging_days = _expand_tpe_day_range(day_text)

    per_day: dict = {}
    for d in DAY_KEYS:
        if d in charging_days and norm_hours and rate is not None:
            per_day[d] = {
                "hours_text": norm_hours,
                "start_time": start_t,
                "end_time":   end_t,
                "rate":       rate,
                "tiers":      None,
            }
        else:
            per_day[d] = {
                "hours_text": "無收費",
                "start_time": None,
                "end_time":   None,
                "rate":       None,
                "tiers":      None,
            }

    return _assemble_record(
        source="TPE",
        city="臺北市",
        district=None,                    # filled by reverse-geocode in geocode_one
        road_segment=road_segment,
        segment_endpoints=endpoints,
        rate_text=rate_text or None,
        per_day=per_day,
    )


def preprocess(rows: list[dict]) -> list[dict]:
    """Normalise NTPC + TPE rows into unified records. Drops structurally invalid rows."""
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
    log.info("[預處理] kept %d, dropped %d", len(out), skipped)
    return out


# ── Step 3: Geocode ────────────────────────────────────────────────────────────
OVERPASS_QUERY_WITH_DISTRICT = """
[out:json][timeout:{timeout}];
area["name"="{city}"]["admin_level"="4"]->.city;
area(area.city)["name"="{district}"]["admin_level"="7"]->.district;
way(area.district)["highway"]["name"="{road}"];
out geom;
""".strip()

OVERPASS_QUERY_CITY_ONLY = """
[out:json][timeout:{timeout}];
area["name"="{city}"]["admin_level"="4"]->.city;
way(area.city)["highway"]["name"="{road}"];
out geom;
""".strip()


def _dedupe_consecutive(coords: list[list[float]]) -> list[list[float]]:
    """Remove only adjacent duplicates (keeps the polyline shape)."""
    out: list[list[float]] = []
    for c in coords:
        if not out or out[-1] != c:
            out.append(c)
    return out


def query_overpass(road: str, city: str, district: str | None) -> list[list[float]] | None:
    """Return [[lat, lon], ...] for the named highway way(s), or None on miss/error.
    If district is None, search the whole city (used for TPE before reverse geocoding)."""
    if not road or not city:
        return None

    if district:
        query = OVERPASS_QUERY_WITH_DISTRICT.format(
            timeout=OVERPASS_TIMEOUT, city=city, district=district, road=road
        )
    else:
        query = OVERPASS_QUERY_CITY_ONLY.format(
            timeout=OVERPASS_TIMEOUT, city=city, road=road
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
        log.warning("[Overpass] %s/%s/%s error: %s", city, district or "*", road, exc)
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


# ── Smoke tests ────────────────────────────────────────────────────────────────
def _smoke_test_helpers(with_network: bool = False) -> None:
    """Asserts pure helpers behave as documented."""
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
    assert parse_tiered("30元/時") is None
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

    # _parse_tpe_time_range
    assert _parse_tpe_time_range("9-17") == (dtime(9, 0), dtime(17, 0), "09:00-17:00")
    assert _parse_tpe_time_range("0-24") == (dtime(0, 0), dtime(0, 0), "00:00-24:00")
    assert _parse_tpe_time_range("") == (None, None, None)
    assert _parse_tpe_time_range("無收費") == (None, None, None)

    # _expand_tpe_day_range
    assert _expand_tpe_day_range("1-5") == {"weekday"}
    assert _expand_tpe_day_range("1-6") == {"weekday", "saturday"}
    assert _expand_tpe_day_range("1-7") == {"weekday", "saturday", "sunday"}
    assert _expand_tpe_day_range("6-7") == {"saturday", "sunday"}
    assert _expand_tpe_day_range("3") == {"weekday"}
    assert _expand_tpe_day_range("") == set()

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

    # preprocess — TPE typical 1-6 / 9-17 / 50
    tpe_row = {
        "_source": "TPE",
        "路段名稱": "七星街", "起迄路段": "育仁路-光明路",
        "收費時間": "9-17", "費率（元）": "50", "收費日（星期）": "1-6",
    }
    rec2 = preprocess([tpe_row])
    assert len(rec2) == 1
    t0 = rec2[0]
    assert t0["source"] == "TPE" and t0["city"] == "臺北市"
    assert t0["district"] is None        # filled later by reverse geocode
    assert t0["road_segment"] == "七星街"
    assert t0["segment_endpoints"] == "育仁路-光明路"
    assert t0["weekday_rate"]  == 50
    assert t0["saturday_rate"] == 50
    assert t0["sunday_rate"]   is None
    assert t0["holiday_rate"]  is None
    assert t0["weekday_start_time"] == dtime(9, 0)
    assert t0["weekday_end_time"]   == dtime(17, 0)
    assert t0["weekday_hours_text"] == "09:00-17:00"
    assert t0["tiered_rates"] is None

    # preprocess — TPE empty data row (just spots count, no rate/time/day) should drop
    empty_tpe = {
        "_source": "TPE",
        "路段名稱": "X路", "起迄路段": "A-B",
        "收費時間": "", "費率（元）": "", "收費日（星期）": "",
    }
    assert preprocess([empty_tpe]) == [], "empty TPE row should be dropped"

    if with_network:
        rows = crawl_ntpc()
        assert len(rows) > 100, f"NTPC crawl returned too few rows: {len(rows)}"
        assert {"county", "area", "road_name", "rates", "_source"}.issubset(rows[0].keys()), \
            f"NTPC row missing expected keys: {rows[0].keys()}"
        assert rows[0]["_source"] == "NTPC"
        log.info("[OK] NTPC crawl smoke test passed (%d rows).", len(rows))

        rows_tpe = crawl_tpe()
        assert len(rows_tpe) > 50, f"TPE crawl returned too few rows: {len(rows_tpe)}"
        critical = {"路段名稱", "收費時間", "費率（元）", "收費日（星期）"}
        first_keys = set(rows_tpe[0].keys())
        missing = critical - first_keys
        assert not missing, f"TPE missing critical headers {missing}; got {first_keys}"
        assert rows_tpe[0]["_source"] == "TPE"
        log.info("[OK] TPE crawl smoke test passed (%d rows).", len(rows_tpe))

        # Overpass with district (NTPC-style query)
        coords = query_overpass("信義路四段", "臺北市", "大安區")
        assert coords is not None and len(coords) >= 2, \
            f"Overpass should return polyline for 信義路四段 in 大安區, got {coords}"
        assert all(120 < c[1] < 122 and 24 < c[0] < 26 for c in coords), \
            "Overpass coords outside Taiwan bbox"
        log.info("[OK] Overpass with-district smoke test passed (%d nodes).", len(coords))
        time.sleep(OVERPASS_DELAY_SEC)

        # Overpass city-only (TPE-style without district)
        coords2 = query_overpass("信義路四段", "臺北市", None)
        assert coords2 is not None and len(coords2) >= 2, \
            f"Overpass city-only should return polyline for 信義路四段, got {coords2}"
        log.info("[OK] Overpass city-only smoke test passed (%d nodes).", len(coords2))
        time.sleep(OVERPASS_DELAY_SEC)

    print("[OK] All helper smoke tests passed.")


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
