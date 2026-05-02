"""
periodic_pm25.py - 環境部 定期 PM2.5 空品監測資料管線
"""
import argparse
import json
import warnings

import psycopg2
import requests
from psycopg2.extras import execute_batch

from config import EPA_API_KEY, PG_DASHBOARD

warnings.filterwarnings("ignore")  # suppress InsecureRequestWarning

TARGET_COUNTIES = {"臺北市", "新北市"}

MOENV_BASE = "https://data.moenv.gov.tw/api/v2"
_PARAMS    = {"format": "JSON", "api_key": EPA_API_KEY, "limit": 1000}


# ── Step 1: 爬蟲 ──────────────────────────────────────────────────────────────
def _fetch(dataset: str) -> list[dict]:
    res = requests.get(f"{MOENV_BASE}/{dataset}", params=_PARAMS, timeout=20, verify=False)
    res.encoding = "utf-8"
    return res.json()


def crawl() -> list[dict]:
    print("[爬蟲] 下載 MOENV aqx_p_07 測站座標 ...")
    stations_raw = _fetch("aqx_p_07")
    station_coords = {
        s["sitename"]: {
            "site_id":   s["siteid"],
            "site_eng":  s.get("siteengname", ""),
            "township":  s.get("township", ""),
            "site_type": s.get("sitetype", ""),
            "lon":       float(s["twd97lon"]) if s.get("twd97lon") else None,
            "lat":       float(s["twd97lat"]) if s.get("twd97lat") else None,
        }
        for s in stations_raw
        if s.get("twd97lon") and s.get("twd97lat")
    }
    print(f"[爬蟲] 有效測站座標 {len(station_coords)} 個")

    print("[爬蟲] 下載 MOENV aqx_p_02 PM2.5 即時資料 ...")
    pm25_raw = _fetch("aqx_p_02")
    print(f"[爬蟲] 取得 {len(pm25_raw)} 筆 PM2.5 原始資料")

    merged = []
    for r in pm25_raw:
        coords = station_coords.get(r.get("site", ""))
        if not coords:
            continue
        pm25_val = r.get("pm25", "")
        merged.append({
            "site":      r.get("site", ""),
            "county":    r.get("county", ""),
            "pm25":      float(pm25_val) if pm25_val not in ("", "--") else None,
            "data_time": r.get("datacreationdate", ""),
            **coords,
        })
    return merged


def load_sample(path: str) -> list[dict]:
    print(f"[Sample] 讀取本地 JSON：{path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── Step 2: 預處理 ─────────────────────────────────────────────────────────────
def preprocess(rows: list[dict]) -> list[dict]:
    print("[預處理] 過濾大臺北地區 ...")
    records, skipped = [], 0
    for r in rows:
        if r.get("county", "") not in TARGET_COUNTIES:
            skipped += 1
            continue
        if not r.get("site_id") or not r.get("data_time"):
            skipped += 1
            continue
        records.append(r)
    print(f"[預處理] 大臺北 PM2.5 {len(records)} 筆，過濾 {skipped} 筆")
    return records


# ── Step 3: 寫入 PostgreSQL ────────────────────────────────────────────────────
def save_to_postgres(records: list[dict]) -> None:
    print("[PostgreSQL] 連線中 ...")
    conn = psycopg2.connect(**PG_DASHBOARD)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS public.air_quality_pm25_tpe (
            ogc_fid    SERIAL PRIMARY KEY,
            site_id    VARCHAR(10)      NOT NULL,
            site       VARCHAR(50),
            site_eng   VARCHAR(100),
            county     VARCHAR(20),
            township   VARCHAR(30),
            site_type  VARCHAR(50),
            latitude   DOUBLE PRECISION,
            longitude  DOUBLE PRECISION,
            pm25       REAL,
            data_time  TIMESTAMPTZ,
            _ctime     TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            _mtime     TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (site_id, data_time)
        )
    """)

    execute_batch(cur, """
        INSERT INTO public.air_quality_pm25_tpe (
            site_id, site, site_eng, county, township,
            site_type, latitude, longitude, pm25, data_time, _mtime
        ) VALUES (
            %(site_id)s, %(site)s, %(site_eng)s, %(county)s, %(township)s,
            %(site_type)s, %(lat)s, %(lon)s, %(pm25)s, %(data_time)s,
            CURRENT_TIMESTAMP
        )
        ON CONFLICT (site_id, data_time) DO UPDATE SET
            pm25   = EXCLUDED.pm25,
            _mtime = EXCLUDED._mtime
    """, records, page_size=200)

    conn.commit()
    cur.close()
    conn.close()
    print(f"[PostgreSQL] 寫入完成（{len(records)} 筆）")


# ── 主程式 ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="環境部 PM2.5 空品監測資料管線")
    parser.add_argument(
        "--mode",
        choices=["periodic", "ondemand", "sample"],
        default="ondemand",
        help="periodic=定期排程, ondemand=AI觸發, sample=本地JSON",
    )
    parser.add_argument("--sample-path", default="")
    args = parser.parse_args()

    print(f"\n{'='*50}")
    print(f"  PM2.5 Pipeline  |  mode={args.mode}")
    print(f"{'='*50}\n")

    if args.mode in ("periodic", "ondemand"):
        rows = crawl()
    else:
        if not args.sample_path:
            print("[錯誤] sample 模式需提供 --sample-path")
            raise SystemExit(1)
        rows = load_sample(args.sample_path)

    records = preprocess(rows)
    if not records:
        print("[警告] 無大臺北地區 PM2.5 資料，結束。")
        raise SystemExit(0)

    save_to_postgres(records)

    print(f"\n{'='*50}")
    print("  完成！")
    print(f"{'='*50}\n")
