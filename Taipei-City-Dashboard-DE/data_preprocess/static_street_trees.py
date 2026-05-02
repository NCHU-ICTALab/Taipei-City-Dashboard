"""
static_street_trees.py - 大臺北行道樹路網資料管線
資料來源: street_trees_merged_with_coords.json（Overpass API 預處理產出）
"""
import argparse
import json
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_batch

from config import PG_DASHBOARD

DEFAULT_DATA_PATH = Path(__file__).parent.parent.parent / "DataPreprocess" / "street_trees_merged_with_coords.json"


# ── Step 1: 載入 JSON ──────────────────────────────────────────────────────────
def load(path: Path) -> list[dict]:
    print(f"[載入] 讀取 {path} ...")
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    print(f"[載入] 共 {len(rows)} 筆")
    return rows


# ── Step 2: 預處理 ─────────────────────────────────────────────────────────────
def preprocess(rows: list[dict]) -> list[dict]:
    print("[預處理] 正規化欄位 ...")
    records, skipped = [], 0

    for r in rows:
        city    = (r.get("市") or "").strip()
        dist    = (r.get("區") or "").strip()
        road    = (r.get("路段") or "").strip()
        remark  = (r.get("位置備註") or "").strip()
        count   = r.get("樹總數量")

        if not road:
            skipped += 1
            continue

        start = r.get("起始座標") or {}
        end   = r.get("終止座標") or {}

        records.append({
            "city":           city,
            "district":       dist,
            "road_segment":   road,
            "location_remark": remark,
            "tree_count":     int(count) if count is not None else None,
            "start_lat":      start.get("lat"),
            "start_lon":      start.get("lon"),
            "end_lat":        end.get("lat"),
            "end_lon":        end.get("lon"),
        })

    print(f"[預處理] 有效 {len(records)} 筆，過濾 {skipped} 筆")
    return records


# ── Step 3: 寫入 PostgreSQL ────────────────────────────────────────────────────
def save_to_postgres(records: list[dict]) -> None:
    print("[PostgreSQL] 連線中 ...")
    conn = psycopg2.connect(**PG_DASHBOARD)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS public.street_trees_tpe (
            ogc_fid          SERIAL PRIMARY KEY,
            city             VARCHAR(20),
            district         VARCHAR(20),
            road_segment     TEXT        NOT NULL,
            location_remark  TEXT,
            tree_count       INTEGER,
            start_lat        DOUBLE PRECISION,
            start_lon        DOUBLE PRECISION,
            end_lat          DOUBLE PRECISION,
            end_lon          DOUBLE PRECISION,
            data_time        TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            _ctime           TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            _mtime           TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (city, district, road_segment, location_remark)
        )
    """)

    execute_batch(cur, """
        INSERT INTO public.street_trees_tpe (
            city, district, road_segment, location_remark,
            tree_count, start_lat, start_lon, end_lat, end_lon,
            data_time, _mtime
        ) VALUES (
            %(city)s, %(district)s, %(road_segment)s, %(location_remark)s,
            %(tree_count)s, %(start_lat)s, %(start_lon)s, %(end_lat)s, %(end_lon)s,
            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        )
        ON CONFLICT (city, district, road_segment, location_remark) DO UPDATE SET
            tree_count  = EXCLUDED.tree_count,
            start_lat   = EXCLUDED.start_lat,
            start_lon   = EXCLUDED.start_lon,
            end_lat     = EXCLUDED.end_lat,
            end_lon     = EXCLUDED.end_lon,
            data_time   = EXCLUDED.data_time,
            _mtime      = EXCLUDED._mtime
    """, records, page_size=500)

    conn.commit()
    cur.close()
    conn.close()
    print(f"[PostgreSQL] 寫入完成（{len(records)} 筆）")


# ── 主程式 ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="行道樹路網資料管線")
    parser.add_argument(
        "--data-path",
        default=str(DEFAULT_DATA_PATH),
        help="street_trees_merged_with_coords.json 的路徑",
    )
    args = parser.parse_args()

    print(f"\n{'='*50}")
    print(f"  Street Trees Pipeline  |  path={args.data_path}")
    print(f"{'='*50}\n")

    rows    = load(Path(args.data_path))
    records = preprocess(rows)
    if not records:
        print("[警告] 無有效資料，結束。")
        raise SystemExit(0)

    save_to_postgres(records)

    print(f"\n{'='*50}")
    print("  完成！")
    print(f"{'='*50}\n")
