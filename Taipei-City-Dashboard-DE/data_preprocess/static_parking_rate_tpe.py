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

    if with_network:
        rows = crawl_ntpc()
        assert len(rows) > 100, f"NTPC crawl returned too few rows: {len(rows)}"
        assert {"county", "area", "road_name", "rates", "_source"}.issubset(rows[0].keys()), \
            f"NTPC row missing expected keys: {rows[0].keys()}"
        assert rows[0]["_source"] == "NTPC"
        log.info("[OK] NTPC crawl smoke test passed (%d rows).", len(rows))

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
