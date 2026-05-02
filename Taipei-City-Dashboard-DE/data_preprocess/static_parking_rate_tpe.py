"""
static_parking_rate_tpe.py - 雙北路邊停車費率資料管線

資料來源：
  - 新北市 OpenData API: https://data.ntpc.gov.tw/api/datasets/d9f18db5-41c7-41d4-b7f0-82a335255b08/json
  - 台北市 OpenData ODS: https://data.taipei/api/frontstage/tpeod/dataset/resource.download?rid=86d2a8b6-c360-4349-956d-dd7771cccf91

Pipeline: crawl_ntpc + crawl_tpe -> preprocess -> geocode_all -> save_to_postgres
更新模式：TRUNCATE + INSERT（保留 schema）
"""
import argparse
import csv
import json
import logging
import re
import time
from datetime import time as dtime
from pathlib import Path

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


# ── Nominatim forward + reverse ───────────────────────────────────────────────
def _make_nominatim() -> tuple:
    """Return (forward_fn, reverse_fn) — both rate-limited Nominatim callables."""
    geolocator = Nominatim(user_agent=USER_AGENT, timeout=NOMINATIM_TIMEOUT)
    forward = RateLimiter(
        geolocator.geocode,
        min_delay_seconds=NOMINATIM_DELAY_SEC,
        error_wait_seconds=2.0,
        max_retries=2,
        swallow_exceptions=True,
    )
    reverse = RateLimiter(
        geolocator.reverse,
        min_delay_seconds=NOMINATIM_DELAY_SEC,
        error_wait_seconds=2.0,
        max_retries=2,
        swallow_exceptions=True,
    )
    return forward, reverse


_DISTRICT_ADDRESS_KEYS = ("city_district", "suburb", "district", "borough", "town", "village")


def _extract_district(address: dict | None) -> str | None:
    """Pick first value ending with '區' from a Nominatim address dict."""
    if not address:
        return None
    for key in _DISTRICT_ADDRESS_KEYS:
        v = address.get(key)
        if v and isinstance(v, str) and v.endswith("區"):
            return v
    # Fallback: any value ending with 區
    for v in address.values():
        if isinstance(v, str) and v.endswith("區"):
            return v
    return None


def reverse_geocode_district(lat: float, lon: float, reverse_fn) -> str | None:
    """Reverse geocode (lat, lon) -> Taiwanese district name like '大安區', or None."""
    try:
        loc = reverse_fn((lat, lon), language="zh-TW", exactly_one=True)
    except Exception as exc:
        log.warning("[Reverse] (%s,%s) error: %s", lat, lon, exc)
        return None
    if loc is None or not getattr(loc, "raw", None):
        return None
    return _extract_district(loc.raw.get("address"))


def _nominatim_forward(query: str, forward_fn) -> tuple[float | None, float | None, dict | None]:
    """Run forward geocode with addressdetails. Returns (lat, lon, address_dict) or (None, None, None)."""
    try:
        loc = forward_fn(query, addressdetails=True, exactly_one=True, language="zh-TW")
    except Exception as exc:
        log.warning("[Nominatim] forward %s error: %s", query, exc)
        return (None, None, None)
    if loc is None:
        return (None, None, None)
    return (loc.latitude, loc.longitude, (loc.raw or {}).get("address"))


def geocode_one(rec: dict, forward_fn, reverse_fn) -> bool:
    """Fill rec['start_lat'/'start_lon'/'geometry_path'/'geom_source'] in place.
    For TPE records (district is None on entry), also fills rec['district'].

    NTPC strategy (district known): Overpass(district) → fallback forward Nominatim.
    TPE strategy (district unknown): forward Nominatim first → extract district →
        refine with Overpass(district) if district found.

    Returns True if geometry + district both obtained, False otherwise."""
    city = rec["city"]
    road = rec["road_segment"]
    district = rec.get("district")

    if district:
        # NTPC path
        coords = query_overpass(road, city, district)
        geom_source = "OVERPASS" if coords else None
        if coords is None:
            query = f"{road}, {district}, {city}, Taiwan"
            lat, lon, _addr = _nominatim_forward(query, forward_fn)
            if lat is None:
                return False
            coords = [[lat, lon]]
            geom_source = "NOMINATIM_FALLBACK"
    else:
        # TPE path: forward Nominatim first to discover district + cheap coords
        query = f"{road}, {city}, Taiwan"
        lat, lon, address = _nominatim_forward(query, forward_fn)
        if lat is None:
            return False

        derived_district = _extract_district(address)
        if derived_district is None:
            # Reverse geocode as a backup if forward did not give district
            derived_district = reverse_geocode_district(lat, lon, reverse_fn)
        if derived_district is None:
            log.warning("[Geocode] cannot resolve district for %s / %s", city, road)
            return False
        district = derived_district
        rec["district"] = district

        # Now refine geometry with Overpass(district) — best effort
        refined = query_overpass(road, city, district)
        if refined and len(refined) >= 2:
            coords = refined
            geom_source = "OVERPASS"
        else:
            coords = [[lat, lon]]
            geom_source = "NOMINATIM_FALLBACK"

    rec["start_lat"]     = coords[0][0]
    rec["start_lon"]     = coords[0][1]
    rec["geometry_path"] = coords
    rec["geom_source"]   = geom_source
    return True


# ── CSV checkpoint helpers ────────────────────────────────────────────────────
CSV_DEFAULT_PATH = Path(__file__).parent / "parking_rate_tpe_geocoded.csv"

CSV_FIELDS = [
    "source", "city", "district", "road_segment", "segment_endpoints",
    "weekday_rate", "saturday_rate", "sunday_rate", "holiday_rate",
    "weekday_start_time", "weekday_end_time",
    "saturday_start_time", "saturday_end_time",
    "sunday_start_time", "sunday_end_time",
    "holiday_start_time", "holiday_end_time",
    "weekday_hours_text", "saturday_hours_text", "sunday_hours_text", "holiday_hours_text",
    "tiered_rates", "rate_schedule", "rate_text",
    "start_lat", "start_lon", "geometry_path", "geom_source",
]

_TIME_FIELDS = {f for f in CSV_FIELDS if f.endswith("_start_time") or f.endswith("_end_time")}
_JSON_FIELDS = {"tiered_rates", "rate_schedule", "geometry_path"}
_INT_FIELDS  = {"weekday_rate", "saturday_rate", "sunday_rate", "holiday_rate"}
_FLOAT_FIELDS = {"start_lat", "start_lon"}


def _serialise(field: str, value) -> str:
    if value is None:
        return ""
    if field in _TIME_FIELDS:
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    if field in _JSON_FIELDS:
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _deserialise(field: str, raw: str):
    if raw == "":
        return None
    if field in _TIME_FIELDS:
        return dtime.fromisoformat(raw)
    if field in _JSON_FIELDS:
        return json.loads(raw)
    if field in _INT_FIELDS:
        return int(raw)
    if field in _FLOAT_FIELDS:
        return float(raw)
    return raw


def _record_to_csv_row(rec: dict) -> dict:
    return {f: _serialise(f, rec.get(f)) for f in CSV_FIELDS}


def _csv_row_to_record(row: dict) -> dict:
    return {f: _deserialise(f, row.get(f, "") or "") for f in CSV_FIELDS}


def _record_key(rec: dict) -> tuple:
    """Composite key uniquely identifying a (source row): includes rate_text so that
    a road segment with two distinct rate configurations is preserved as two rows."""
    return (
        rec.get("source") or "",
        rec.get("city") or "",
        rec.get("road_segment") or "",
        rec.get("segment_endpoints") or "",
        rec.get("rate_text") or "",
        rec.get("weekday_hours_text") or "",
    )


def load_csv_records(csv_path: Path) -> list[dict]:
    if not csv_path.exists():
        return []
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return [_csv_row_to_record(row) for row in reader]


# ── Geocode batch with checkpoint + resume ────────────────────────────────────
def geocode_all(
    records: list[dict],
    csv_path: Path | str = CSV_DEFAULT_PATH,
    resume: bool = True,
) -> list[dict]:
    """Geocode all records sequentially. Append each successful record to CSV immediately.
    On resume=True (default) skips records already present in the CSV (matched by
    composite key) and reuses cached geometry within the run."""
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    done_records: list[dict] = []
    done_keys: set[tuple] = set()
    geometry_cache: dict[tuple, tuple] = {}

    if resume and csv_path.exists():
        done_records = load_csv_records(csv_path)
        done_keys = {_record_key(r) for r in done_records}
        for r in done_records:
            gkey = (r.get("city") or "", r.get("road_segment") or "", r.get("segment_endpoints") or "")
            if gkey not in geometry_cache and r.get("geometry_path"):
                geometry_cache[gkey] = (r["geometry_path"], r["geom_source"], r.get("district"))
        log.info("[Geom] resume: %d records already in %s", len(done_records), csv_path)

    file_exists = csv_path.exists()
    csv_file = open(csv_path, "a" if (resume and file_exists) else "w", encoding="utf-8", newline="")
    writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
    if not (resume and file_exists):
        writer.writeheader()
        csv_file.flush()

    forward, reverse = _make_nominatim()

    total = len(records)
    log.info("[Geom] 開始：總計 %d 筆待處理（CSV: %s）", total, csv_path)
    started = time.time()
    n_overpass = n_nominatim = n_failed = n_resumed = n_cached = 0
    out: list[dict] = list(done_records)

    try:
        for idx, rec in enumerate(records, start=1):
            key = _record_key(rec)
            if key in done_keys:
                n_resumed += 1
                # idx-aligned progress still printed below
            else:
                gkey = (rec.get("city") or "", rec.get("road_segment") or "", rec.get("segment_endpoints") or "")
                cached = geometry_cache.get(gkey)
                if cached:
                    geom, gsource, district = cached
                    rec["geometry_path"] = geom
                    rec["start_lat"]     = geom[0][0]
                    rec["start_lon"]     = geom[0][1]
                    rec["geom_source"]   = gsource
                    if rec.get("district") is None:
                        rec["district"] = district
                    n_cached += 1
                    ok = True
                else:
                    ok = geocode_one(rec, forward, reverse)
                    if ok:
                        geometry_cache[gkey] = (
                            rec["geometry_path"], rec["geom_source"], rec["district"],
                        )
                        if rec["geom_source"] == "OVERPASS":
                            n_overpass += 1
                        else:
                            n_nominatim += 1
                    else:
                        n_failed += 1
                    # Sleep only when we actually hit the network
                    time.sleep(OVERPASS_DELAY_SEC)

                if ok:
                    writer.writerow(_record_to_csv_row(rec))
                    csv_file.flush()
                    out.append(rec)
                    done_keys.add(key)

            if idx % PROGRESS_REPORT_EVERY == 0 or idx == total:
                elapsed = time.time() - started
                processed_this_run = max(1, idx - n_resumed)
                rate = processed_this_run / elapsed if elapsed > 0 else 0
                remaining_records = total - idx
                remaining_sec = remaining_records / rate if rate > 0 else 0
                log.info(
                    "[Geom] %4d/%-4d  Overpass %d  Nomi_fb %d  Cached %d  Resumed %d  Failed %d"
                    "  進度 %5.1f%%  剩餘 ~%dm%02ds",
                    idx, total, n_overpass, n_nominatim, n_cached, n_resumed, n_failed,
                    100.0 * idx / total,
                    int(remaining_sec // 60), int(remaining_sec % 60),
                )
    finally:
        csv_file.close()

    elapsed = time.time() - started
    log.info(
        "[Geom] 完成：%d 筆有效（resumed %d / cached %d / new Overpass %d / new Nomi_fb %d / failed %d），耗時 %dm%02ds",
        len(out), n_resumed, n_cached, n_overpass, n_nominatim, n_failed,
        int(elapsed // 60), int(elapsed % 60),
    )
    return out


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
    out["tiered_rates_json"]  = json.dumps(rec.get("tiered_rates"),  ensure_ascii=False) if rec.get("tiered_rates")  is not None else None
    out["rate_schedule_json"] = json.dumps(rec.get("rate_schedule"), ensure_ascii=False) if rec.get("rate_schedule") is not None else None
    out["geometry_path_json"] = json.dumps(rec.get("geometry_path"), ensure_ascii=False)
    return out


def save_to_postgres(records: list[dict]) -> None:
    """CREATE TABLE IF NOT EXISTS → TRUNCATE → batch INSERT. Drops records missing
    required NOT NULL fields (district / start_lat / start_lon / geometry_path / geom_source)."""
    valid = [
        r for r in records
        if r.get("district") and r.get("start_lat") is not None and r.get("start_lon") is not None
        and r.get("geometry_path") and r.get("geom_source")
    ]
    if not valid:
        log.error("[PostgreSQL] no valid records to write (all dropped by NOT NULL check)")
        raise SystemExit(1)
    if len(valid) < len(records):
        log.warning("[PostgreSQL] dropped %d records missing NOT NULL fields", len(records) - len(valid))

    log.info("[PostgreSQL] 連線中 ...")
    conn = psycopg2.connect(**PG_DASHBOARD)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(CREATE_TABLE_SQL)
                cur.execute("TRUNCATE TABLE public.parking_rate_tpe RESTART IDENTITY")
                payload = [_to_db_row(r) for r in valid]
                execute_batch(cur, INSERT_SQL, payload, page_size=500)
        log.info("[PostgreSQL] 寫入完成（覆蓋 %d 筆）", len(valid))
    finally:
        conn.close()


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

    # CSV round-trip on a synthetic geocoded record
    sample_rec = preprocess([ntpc_row])[0]
    sample_rec["start_lat"] = 25.0142
    sample_rec["start_lon"] = 121.4631
    sample_rec["geometry_path"] = [[25.0142, 121.4631], [25.0148, 121.4640]]
    sample_rec["geom_source"] = "OVERPASS"
    csv_row = _record_to_csv_row(sample_rec)
    round_tripped = _csv_row_to_record(csv_row)
    for f in CSV_FIELDS:
        a, b = sample_rec.get(f), round_tripped.get(f)
        assert a == b, f"CSV roundtrip mismatch for {f}: in={a!r} out={b!r}"

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

        # geocode_one — NTPC-style (district given)
        forward, reverse = _make_nominatim()
        ntpc_sample = {
            "city": "臺北市", "district": "大安區", "road_segment": "信義路四段",
        }
        ok = geocode_one(ntpc_sample, forward, reverse)
        assert ok, "geocode_one NTPC-style failed"
        assert ntpc_sample["geom_source"] in ("OVERPASS", "NOMINATIM_FALLBACK")
        assert ntpc_sample["district"] == "大安區"
        log.info("[OK] geocode_one NTPC-style passed (source=%s, points=%d).",
                 ntpc_sample["geom_source"], len(ntpc_sample["geometry_path"]))
        time.sleep(OVERPASS_DELAY_SEC)

        # geocode_one — TPE-style (district resolved by reverse geocode)
        tpe_sample = {
            "city": "臺北市", "district": None, "road_segment": "信義路四段",
        }
        ok = geocode_one(tpe_sample, forward, reverse)
        assert ok, "geocode_one TPE-style failed"
        assert tpe_sample["district"] and tpe_sample["district"].endswith("區"), \
            f"reverse geocode should fill district, got {tpe_sample['district']!r}"
        log.info("[OK] geocode_one TPE-style passed (district=%s, source=%s).",
                 tpe_sample["district"], tpe_sample["geom_source"])
        time.sleep(OVERPASS_DELAY_SEC)

        # geocode_all + CSV checkpoint + resume on a tiny sample
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            tmp_csv = Path(td) / "smoke.csv"
            sample_recs = preprocess([
                {"_source": "NTPC", "county": "新北市", "area": "板橋區",
                 "road_name": "大同街", "weekdays_time": "07:00~20:00",
                 "sat._charging_time": "無收費", "sun._charging_time": "無收費",
                 "national_holidays_charging_time": "無收費",
                 "rates": "30元/時"},
                {"_source": "TPE", "路段名稱": "信義路四段",
                 "起迄路段": "信義路-基隆路",
                 "收費時間": "8-20", "費率（元）": "60", "收費日（星期）": "1-7"},
            ])
            assert len(sample_recs) == 2

            # Round 1: fresh geocode
            r1 = geocode_all(sample_recs, csv_path=tmp_csv, resume=False)
            assert len(r1) == 2, f"first round should geocode both, got {len(r1)}"
            assert tmp_csv.exists() and tmp_csv.stat().st_size > 0
            log.info("[OK] geocode_all round 1: %d records written to %s", len(r1), tmp_csv)

            # Round 2: resume — should skip both
            sample_recs_again = preprocess([
                {"_source": "NTPC", "county": "新北市", "area": "板橋區",
                 "road_name": "大同街", "weekdays_time": "07:00~20:00",
                 "sat._charging_time": "無收費", "sun._charging_time": "無收費",
                 "national_holidays_charging_time": "無收費",
                 "rates": "30元/時"},
                {"_source": "TPE", "路段名稱": "信義路四段",
                 "起迄路段": "信義路-基隆路",
                 "收費時間": "8-20", "費率（元）": "60", "收費日（星期）": "1-7"},
            ])
            r2 = geocode_all(sample_recs_again, csv_path=tmp_csv, resume=True)
            assert len(r2) == 2, f"resume should still return 2 records, got {len(r2)}"
            log.info("[OK] geocode_all resume: skipped both, returned %d", len(r2))

    print("[OK] All helper smoke tests passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="雙北路邊停車費率資料管線")
    # Pipeline flags
    parser.add_argument("--sources", nargs="+", choices=["ntpc", "tpe"],
                        default=["ntpc", "tpe"],
                        help="哪些來源要跑（預設兩者皆跑）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只跑 crawl + preprocess + geocode，不寫入 DB")
    parser.add_argument("--limit", type=int, default=None,
                        help="處理前 N 筆（debug 用）")
    parser.add_argument("--csv-path", type=str, default=str(CSV_DEFAULT_PATH),
                        help=f"CSV checkpoint 路徑（預設 {CSV_DEFAULT_PATH}）")
    parser.add_argument("--no-resume", action="store_true",
                        help="不從 CSV 接續，重新跑全部（會覆蓋 CSV）")
    parser.add_argument("--from-csv", type=str, default=None,
                        help="跳過 crawl + preprocess + geocode，直接從指定 CSV 載入並寫入 DB")
    # Debug / smoke flags
    parser.add_argument("--smoke-test", action="store_true",
                        help="Run inline helper smoke tests and exit.")
    parser.add_argument("--with-network", action="store_true",
                        help="Also exercise live HTTP smoke checks.")
    parser.add_argument("--with-db", action="store_true",
                        help="Smoke-test save_to_postgres (writes 1 fake row + verify + exit).")
    args = parser.parse_args()

    # ── Debug shortcuts ──────────────────────────────────────────────────────
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
            "rate_schedule": {"weekday": {"charging": True, "hours_text": "07:00~20:00",
                                          "rate": 30, "tiers": None}},
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

    # ── --from-csv shortcut: skip crawl/preprocess/geocode entirely ──────────
    if args.from_csv:
        csv_in = Path(args.from_csv)
        if not csv_in.exists():
            log.error("[錯誤] --from-csv 指定的檔案不存在：%s", csv_in)
            raise SystemExit(1)
        log.info("=" * 60)
        log.info("Parking Rate Pipeline | from_csv=%s dry_run=%s", csv_in, args.dry_run)
        log.info("=" * 60)
        records = load_csv_records(csv_in)
        log.info("[CSV] loaded %d records from %s", len(records), csv_in)
        if not records:
            log.error("[錯誤] CSV 為空，未動 DB，結束。")
            raise SystemExit(1)
        if args.dry_run:
            log.info("[DRY-RUN] %d 筆已備妥（未寫入 DB）", len(records))
            raise SystemExit(0)
        save_to_postgres(records)
        log.info("=" * 60)
        log.info("完成！%d 筆寫入 parking_rate_tpe", len(records))
        log.info("=" * 60)
        raise SystemExit(0)

    # ── Full pipeline ────────────────────────────────────────────────────────
    csv_path = Path(args.csv_path)
    log.info("=" * 60)
    log.info(
        "Parking Rate Pipeline | sources=%s dry_run=%s limit=%s csv=%s resume=%s",
        args.sources, args.dry_run, args.limit, csv_path, not args.no_resume,
    )
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

    geocoded = geocode_all(records, csv_path=csv_path, resume=not args.no_resume)
    if not geocoded:
        log.error("[錯誤] geocode 後無有效資料，未動 DB，結束。")
        raise SystemExit(1)

    if args.dry_run:
        log.info("[DRY-RUN] %d 筆已備妥（未寫入 DB；CSV 已寫入 %s）", len(geocoded), csv_path)
        raise SystemExit(0)

    save_to_postgres(geocoded)
    log.info("=" * 60)
    log.info("完成！%d 筆寫入 parking_rate_tpe", len(geocoded))
    log.info("=" * 60)
