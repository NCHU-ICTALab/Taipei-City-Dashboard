"""
periodic_rainfall.py - 中央氣象署 定期降雨機率預報資料管線
"""
import argparse
import json
import ssl
import urllib.request

import psycopg2
from psycopg2.extras import execute_batch

from config import CWA_API_KEY, PG_DASHBOARD

CWA_URL = (
    f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-C0032-001"
    f"?Authorization={CWA_API_KEY}&format=JSON"
)

TARGET_LOCATIONS = {"臺北市", "新北市"}


# ── Step 1: 爬蟲 ──────────────────────────────────────────────────────────────
def crawl() -> list[dict]:
    print("[爬蟲] 下載 CWA F-C0032-001 降雨機率預報 ...")
    ctx = ssl._create_unverified_context()
    req = urllib.request.Request(CWA_URL, headers={"User-Agent": "Mozilla/5.0"})
    res = urllib.request.urlopen(req, context=ctx, timeout=20)
    data = json.loads(res.read().decode("utf-8"))
    locations = data.get("records", {}).get("location", [])
    print(f"[爬蟲] 取得 {len(locations)} 個縣市原始資料")
    return locations


def load_sample(path: str) -> list[dict]:
    print(f"[Sample] 讀取本地 JSON：{path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── Step 2: 預處理 ─────────────────────────────────────────────────────────────
def preprocess(locations: list[dict]) -> list[dict]:
    """locations 可為 API 原始格式或已解析的 list[dict]"""
    print("[預處理] 解析大臺北降雨機率時段 ...")

    # sample 模式傳入的是已解析的 list[dict]
    if locations and isinstance(locations[0], dict) and "pop" in locations[0]:
        records = [r for r in locations if r.get("location") in TARGET_LOCATIONS]
        print(f"[預處理] 大臺北預報 {len(records)} 筆")
        return records

    # crawl 模式傳入的是 API location 陣列
    records = []
    for loc in locations:
        name = loc.get("locationName", "")
        if name not in TARGET_LOCATIONS:
            continue
        elements = {e["elementName"]: e["time"] for e in loc.get("weatherElement", [])}
        pop_times  = elements.get("PoP", [])
        wx_times   = elements.get("Wx", [])
        mint_times = elements.get("MinT", [])
        maxt_times = elements.get("MaxT", [])

        for i, slot in enumerate(pop_times):
            records.append({
                "location":   name,
                "start_time": slot["startTime"],
                "end_time":   slot["endTime"],
                "pop":        int(slot["parameter"]["parameterName"]),
                "weather":    wx_times[i]["parameter"]["parameterName"] if i < len(wx_times) else None,
                "min_temp":   int(mint_times[i]["parameter"]["parameterName"]) if i < len(mint_times) else None,
                "max_temp":   int(maxt_times[i]["parameter"]["parameterName"]) if i < len(maxt_times) else None,
            })

    print(f"[預處理] 大臺北預報 {len(records)} 筆")
    return records


# ── Step 3: 寫入 PostgreSQL ────────────────────────────────────────────────────
def save_to_postgres(records: list[dict]) -> None:
    print("[PostgreSQL] 連線中 ...")
    conn = psycopg2.connect(**PG_DASHBOARD)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS public.rainfall_forecast_tpe (
            ogc_fid    SERIAL PRIMARY KEY,
            location   VARCHAR(20)  NOT NULL,
            start_time TIMESTAMPTZ  NOT NULL,
            end_time   TIMESTAMPTZ,
            pop        SMALLINT,
            weather    TEXT,
            min_temp   REAL,
            max_temp   REAL,
            data_time  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            _ctime     TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            _mtime     TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (location, start_time)
        )
    """)

    execute_batch(cur, """
        INSERT INTO public.rainfall_forecast_tpe (
            location, start_time, end_time,
            pop, weather, min_temp, max_temp,
            data_time, _mtime
        ) VALUES (
            %(location)s, %(start_time)s, %(end_time)s,
            %(pop)s, %(weather)s, %(min_temp)s, %(max_temp)s,
            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        )
        ON CONFLICT (location, start_time) DO UPDATE SET
            end_time  = EXCLUDED.end_time,
            pop       = EXCLUDED.pop,
            weather   = EXCLUDED.weather,
            min_temp  = EXCLUDED.min_temp,
            max_temp  = EXCLUDED.max_temp,
            data_time = EXCLUDED.data_time,
            _mtime    = EXCLUDED._mtime
    """, records, page_size=200)

    conn.commit()
    cur.close()
    conn.close()
    print(f"[PostgreSQL] 寫入完成（{len(records)} 筆）")


# ── 主程式 ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="中央氣象署降雨機率預報資料管線")
    parser.add_argument(
        "--mode",
        choices=["periodic", "ondemand", "sample"],
        default="ondemand",
        help="periodic=定期排程, ondemand=AI觸發, sample=本地JSON",
    )
    parser.add_argument(
        "--sample-path",
        default="",
        help="sample 模式下的 JSON 檔案路徑",
    )
    args = parser.parse_args()

    print(f"\n{'='*50}")
    print(f"  Rainfall Pipeline  |  mode={args.mode}")
    print(f"{'='*50}\n")

    if args.mode in ("periodic", "ondemand"):
        locations = crawl()
    else:
        if not args.sample_path:
            print("[錯誤] sample 模式需提供 --sample-path")
            raise SystemExit(1)
        locations = load_sample(args.sample_path)

    records = preprocess(locations)
    if not records:
        print("[警告] 無大臺北降雨機率資料，結束。")
        raise SystemExit(0)

    save_to_postgres(records)

    print(f"\n{'='*50}")
    print("  完成！")
    print(f"{'='*50}\n")
