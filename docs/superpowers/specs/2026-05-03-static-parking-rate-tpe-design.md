# Static Parking Rate (TPE) Pipeline — Design

## 背景與目標

新增一支獨立 Python 資料管線，從新北市 OpenData JSON API 與台北市 OpenData ODS 檔，抓取**雙北路邊收費停車路段**資料，預處理（含費率解析、收費時段拆解、Overpass 查路網幾何 + Nominatim fallback geocode）後寫入 dashboard PostgreSQL 的 `parking_rate_tpe` 表，供 BE / FE 顯示路段折線疊圖與費率查詢用。

本功能採地端開發 → push 到 GitHub → 由維護者 merge 進主分支的流程，因此設計上要求：
- 完全不修改現存任何檔案（含 `config.py` / `requirements.txt` / `dags/` / `docker/`）
- 僅新增單一 Python 檔案 + 單一 design doc（最小 merge 衝突面）
- 沿用 `data_preprocess/` 既有腳本的程式風格與套件白名單

## 範圍

**In scope**：
- 從新北市 OpenData JSON API 抓 1,058 筆路邊收費路段（`https://data.ntpc.gov.tw/api/datasets/d9f18db5-41c7-41d4-b7f0-82a335255b08/json`）
- 從台北市 OpenData 下載並解析 ODS 檔（`https://data.taipei/api/frontstage/tpeod/dataset/resource.download?rid=86d2a8b6-c360-4349-956d-dd7771cccf91`）
- 費率解析（含分級費率取第一級，原文保留）
- 收費時段拆解為 TIME 欄位（含跨時段原文 fallback）
- Overpass API 查 OSM way 幾何，取完整折線
- Nominatim 為 Overpass 失敗的 fallback（單點 array）
- 寫入 PostgreSQL（TRUNCATE + INSERT，保留 schema）

**Out of scope**：
- Airflow DAG / 排程（本案為 standalone script，不放 `dags/`）
- 路外停車場（兩個來源都僅取「路邊收費路段」）
- 機車 / 大型車費率（來源未提供）
- 替代 geocode 來源（內政部地名資料庫等）
- 修改 `config.py` / `requirements.txt` / `dags/` / `docker/` 任何檔案

## 交付項目

### 新增檔案（僅 1 支）

`Taipei-City-Dashboard-DE/data_preprocess/static_parking_rate_tpe.py`

### 不修改任何既有檔案

包括但不限於：`config.py`、其他 `static_*.py`、`dags/`、`docker/`、`requirements.txt`。

### Git Branch

`feature/static-parking-rate-tpe`（從 `develop` 分出）

## 架構

腳本由四段純函式串接，與 `static_street_trees.py` / `static_speeding_casualty_points_tpe.py` 風格一致：

```
crawl_ntpc()   ─┐
                ├─→ preprocess(rows) ─→ geocode_all(records) ─→ save_to_postgres(records)
crawl_tpe()    ─┘
```

`__main__` 入口處理 CLI、orchestration、空資料保護。

## 資料來源

### 新北市（NTPC）

| 項目 | 值 |
|------|----|
| 資料集 | 新北市路邊收費停車場收費路段資訊 |
| API URL | `https://data.ntpc.gov.tw/api/datasets/d9f18db5-41c7-41d4-b7f0-82a335255b08/json` |
| Method | HTTP GET |
| 回傳格式 | JSON list |
| 預估筆數 | 1,058 |
| 欄位 | `county`, `countycode`, `area`, `areacode`, `road_name`, `weekdays_time`, `sat._charging_time`, `sun._charging_time`, `national_holidays_charging_time`, `rates` |

範例回傳：
```json
{
  "county": "新北市",
  "area": "板橋區",
  "road_name": "大同街",
  "weekdays_time": "07:00~20:00",
  "sat._charging_time": "無收費",
  "sun._charging_time": "無收費",
  "national_holidays_charging_time": "無收費",
  "rates": "前2小時30元/時\n第3小時以上40元/時"
}
```

### 台北市（TPE）

| 項目 | 值 |
|------|----|
| 資料集 | 臺北市各行政區路邊收費路段資訊 |
| 下載 URL | `https://data.taipei/api/frontstage/tpeod/dataset/resource.download?rid=86d2a8b6-c360-4349-956d-dd7771cccf91` |
| Method | HTTP GET |
| 回傳格式 | ODS（OpenDocument Spreadsheet） |
| 預估筆數 | ~500 |
| 欄位 | 路段名稱、起迄路段、小型車數量（路邊計時）、大型車數量（路邊計時）、小型車數量（路邊計次）、大型車數量（路邊計次）、機車數量（路邊計次）、收費時間、費率（元）、收費日（星期） |

解析方式：用 `odfpy` 載入 ODS 檔，取第一張 sheet，header 在第一列，後續為資料列。

### Overpass API（OSM 路網查詢）

| 項目 | 值 |
|------|----|
| Endpoint | `https://overpass-api.de/api/interpreter` |
| Method | HTTP POST（query 在 body） |
| 限速 | 每 req 間隔 0.5 秒 |
| Timeout | 25 秒 |

Query 範本（對 `out geom` 取得 way 上所有 node 座標）：
```
[out:json][timeout:25];
area["name"="新北市"]["admin_level"="4"]->.city;
area(area.city)["name"="板橋區"]["admin_level"="7"]->.district;
way(area.district)["highway"]["name"="大同街"];
out geom;
```

多個同名 way（如「中山路」橫跨多街口）→ concat 全部 nodes，去除連續重複點。

### Nominatim API（fallback geocode）

| 項目 | 值 |
|------|----|
| 套件 | `geopy.geocoders.Nominatim` |
| User-Agent | `taipei-dashboard-parking-rate/1.0 (charles@j-tcg.com)` |
| 限速 | `RateLimiter(min_delay_seconds=1.1)` |
| Timeout | 10 秒 |
| Query | `f"{road_segment}, {district}, {city}, Taiwan"` |

## 預處理規則

### 欄位對映表

| 目標欄位 | NTPC 來源 | TPE 來源 |
|---|---|---|
| source | `"NTPC"` (硬編) | `"TPE"` (硬編) |
| city | `county` | `"臺北市"` (硬編) |
| district | `area` | `行政區` 欄 |
| road_segment | `road_name` | `路段名稱` |
| segment_endpoints | NULL | `起迄路段` |
| weekday_rate | parse `rates` 第一級（若 `weekdays_time` ≠ "無收費"） | parse `費率（元）` 第一級（若 `收費日（星期）` 含週一-週五） |
| saturday_rate | 同上規則，依 `sat._charging_time` | 同上，依 `收費日（星期）` 是否含週六 |
| sunday_rate | 同上規則，依 `sun._charging_time` | 同上，依 `收費日（星期）` 是否含週日 |
| holiday_rate | 同上規則，依 `national_holidays_charging_time` | 同上，依 `收費日（星期）` 是否含「國定假日」/「假日」 |
| weekday_start_time / weekday_end_time | parse `weekdays_time`（"07:00~20:00" → 07:00, 20:00） | parse `收費時間` |
| saturday_*, sunday_*, holiday_* | 同上規則，分別 parse 對應 NTPC 欄位 | 同 weekday |
| weekday_hours_text | `weekdays_time` 原文 | `收費時間` 原文 |
| saturday/sunday/holiday_hours_text | 對應 NTPC 欄位原文 | `收費時間` 原文 |
| tiered_rates | parse `rates` 為 list | parse `費率（元）` 為 list |
| rate_schedule | 整合上述 → 完整 nested JSONB | 同 |
| rate_text | `rates` 原文 | `費率（元）` 原文 |
| start_lat / start_lon / geometry_path / geom_source | 來自 geocode_all() | 同 |

### Rate 解析（共用 helper）

```python
def parse_rate(text: str) -> int | None:
    """'30元/時' → 30；'前2小時30元/時\\n第3小時以上40元/時' → 30；'無收費' → None"""
    m = re.search(r"(\d+)\s*元\s*/\s*時", text or "")
    return int(m.group(1)) if m else None

def parse_tiered(text: str) -> list[dict] | None:
    """擷取所有級別。'前2小時30元/時\\n第3小時以上40元/時' →
       [{'range':'前2小時','rate':30},{'range':'第3小時以上','rate':40}]
       單一級費率 → None（沒有「分級」可言）"""
```

規則：
- 抓**所有**「N元/時」的匹配；< 2 個匹配 → `tiered_rates = None`
- `range` 取「N元/時」之前的中文片段
- 主費率欄（weekday_rate 等）一律取第一個匹配的 N

### 收費時段解析

```python
def parse_time_range(text: str) -> tuple[time | None, time | None]:
    """'07:00~20:00' → (07:00, 20:00)
       '07:00-12:00, 14:00-20:00' → (07:00, 20:00)  # 取最早起到最晚迄
       '無收費' → (None, None)"""
```

跨時段（如 `07-12, 14-20`）→ `*_start_time = 07:00`、`*_end_time = 20:00`，原文完整保留在 `*_hours_text`。

### `rate_schedule` JSONB 結構

```json
{
  "weekday":  {"charging": true,  "hours_text": "07:00-20:00", "rate": 30, "tiers": [{"range":"前2小時","rate":30},{"range":"第3小時以上","rate":40}]},
  "saturday": {"charging": false, "hours_text": "無收費",       "rate": null, "tiers": null},
  "sunday":   {"charging": false, "hours_text": "無收費",       "rate": null, "tiers": null},
  "holiday":  {"charging": false, "hours_text": "無收費",       "rate": null, "tiers": null}
}
```

## Geocode 流程

```
for each record:
    1. 查 Overpass API：way["highway"]["name"=road_segment](area=district inside city)
       └─ 命中 → geometry_path = [[lat,lon],...] 完整折線
                start_lat/lon = geometry_path[0]
                geom_source = 'OVERPASS'
    2. 找不到 / timeout / 無 highway match → fallback：Nominatim geocode
       └─ 命中 → geometry_path = [[lat, lon]] (單點 array)
                start_lat/lon = lat, lon
                geom_source = 'NOMINATIM_FALLBACK'
    3. 兩者都失敗 → drop 該筆並 log warning
```

### 進度回報（每 50 筆）

```
[Geom] 開始：總計 1558 筆待處理
[Geom]    50/1558  Overpass 38  Nominatim_fb 9   Failed 3   進度  3.2%  剩餘 ~22 分鐘
[Geom]   100/1558  Overpass 78  Nominatim_fb 16  Failed 6   進度  6.4%  剩餘 ~21 分鐘
...
[Geom] 完成：Overpass 1212 / Nominatim_fb 220 / Failed 126（保留率 91.9%），耗時 22m 14s
```

實作：用 `time.time()` 記起點，每 50 筆計算 ETA。

### 預估耗時
- Overpass：≈ 1500 × 0.6s = **15 分鐘**
- Nominatim fallback：≈ 25% × 1500 × 1.1s = **7 分鐘**
- 總計 ≈ **22 分鐘**

## 表結構（CREATE TABLE IF NOT EXISTS）

```sql
CREATE TABLE IF NOT EXISTS public.parking_rate_tpe (
    ogc_fid              SERIAL           PRIMARY KEY,
    -- 來源 / 識別
    source               VARCHAR(10)      NOT NULL,   -- 'NTPC' | 'TPE'
    city                 VARCHAR(20)      NOT NULL,
    district             VARCHAR(20)      NOT NULL,
    road_segment         TEXT             NOT NULL,
    segment_endpoints    TEXT,                        -- TPE 的「起迄路段」欄；NTPC 為 NULL

    -- 主費率（每日別獨立，元/時，第一級費率）
    weekday_rate         INTEGER,
    saturday_rate        INTEGER,
    sunday_rate          INTEGER,
    holiday_rate         INTEGER,

    -- 收費時段（TIME 型別 + 原文 fallback）
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

    -- 分級費率
    tiered_rates         JSONB,                      -- [{"range":"前2小時","rate":30},...] | null

    -- 完整結構化費率（無損保留）
    rate_schedule        JSONB,                      -- nested 完整結構

    -- 來源原文
    rate_text            TEXT,

    -- Geometry
    start_lat            DOUBLE PRECISION NOT NULL,
    start_lon            DOUBLE PRECISION NOT NULL,
    geometry_path        JSONB            NOT NULL,  -- [[lat,lon],...]
    geom_source          VARCHAR(25)      NOT NULL,  -- 'OVERPASS' | 'NOMINATIM_FALLBACK'

    -- 元資料
    data_time            TIMESTAMPTZ      DEFAULT CURRENT_TIMESTAMP,
    _ctime               TIMESTAMPTZ      DEFAULT CURRENT_TIMESTAMP,
    _mtime               TIMESTAMPTZ      DEFAULT CURRENT_TIMESTAMP
);
```

**沒有 UNIQUE 約束** —— 因為更新模式為 TRUNCATE + INSERT，不需要 ON CONFLICT。

## 寫入策略

| 階段 | 動作 |
|------|------|
| 表存在性 | `CREATE TABLE IF NOT EXISTS`（不會清掉現存資料） |
| 既有資料 | `TRUNCATE TABLE public.parking_rate_tpe RESTART IDENTITY` |
| 寫入 | `psycopg2.extras.execute_batch(...)`，page_size=500 |
| 交易 | 整段在同一 connection 內 commit；失敗 rollback |
| JSONB | Python dict → `json.dumps(...)` → 透過 psycopg2 寫入 |

**關鍵保護**：TRUNCATE 在 crawl + preprocess + geocode **都成功且非空**之後才執行，確保不會「先清空再因抓取/geocode 失敗留下空表」。

## 容錯策略

| 情境 | 行為 |
|------|------|
| NTPC API 失敗（HTTP / JSON parse） | log error；若 TPE 還有資料 → 繼續 |
| TPE ODS 下載失敗 / odfpy 解析失敗 | log error；若 NTPC 還有資料 → 繼續 |
| 兩個來源都失敗 → crawl 結果空 | `SystemExit(1)`，不 TRUNCATE |
| 單筆 rate 解析失敗 | rate 欄位填 NULL，`rate_text` 仍保留原文 |
| 單筆 Overpass timeout / 無 match | fallback 到 Nominatim |
| Overpass + Nominatim 都失敗 | drop 該筆，log warning |
| Overpass / Nominatim 整體服務 down | 全筆 drop → 預處理結果空 → `SystemExit(1)` 不 TRUNCATE |
| 預處理 + geocode 後總筆數為 0 | `SystemExit(1)`，不 TRUNCATE |

## 套件白名單

僅使用：

| 套件 | 用途 | 來源 |
|------|------|------|
| `psycopg2` | DB 連線、`execute_batch` | requirements.txt ✅ |
| `requests` | NTPC API、TPE ODS、Overpass HTTP | 已被其他 static_*.py 使用（geopy 的 transitive dep）✅ |
| `odfpy` | TPE ODS 解析 | requirements.txt ✅ |
| `geopy` | Nominatim fallback geocode + RateLimiter | requirements.txt ✅ |
| `argparse` / `json` / `re` / `time` / `logging` / `datetime` | CLI、解析、時間 | stdlib ✅ |

**不引入 `pandas` / `beautifulsoup4` / `lxml`** —— 與 `data_preprocess/` 內所有現存腳本一致。

**完全不需要修改 `requirements.txt` 與 `config.py`**。

## CLI 介面

```bash
# 全跑（預設）
python static_parking_rate_tpe.py

# 只跑特定來源（debug）
python static_parking_rate_tpe.py --sources ntpc
python static_parking_rate_tpe.py --sources tpe
python static_parking_rate_tpe.py --sources ntpc tpe

# 跳過寫入 DB（debug 預處理用）
python static_parking_rate_tpe.py --dry-run

# 限制處理筆數（debug 用）
python static_parking_rate_tpe.py --limit 10
```

| flag | 預設 | 說明 |
|------|------|------|
| `--sources` | `ntpc tpe` | 哪些來源要跑 |
| `--dry-run` | False | 只跑 crawl + preprocess + geocode，印出但不寫入 DB |
| `--limit` | None | 處理前 N 筆（含兩來源加總，主要 debug 用） |

## DB 連線設定

從 `data_preprocess/config.py` 既有的 `PG_DASHBOARD` 直接 import：

```python
from config import PG_DASHBOARD
conn = psycopg2.connect(**PG_DASHBOARD)
```

不修改 `config.py`。

## 程式骨架（示意）

```python
"""
static_parking_rate_tpe.py - 雙北路邊停車費率資料管線
資料來源：
  - 新北市 OpenData API: https://data.ntpc.gov.tw/api/datasets/d9f18db5-.../json
  - 台北市 OpenData ODS: https://data.taipei/api/frontstage/tpeod/dataset/resource.download?rid=...
"""
import argparse
import json
import logging
import re
import time
from datetime import time as dtime

import psycopg2
import requests
from psycopg2.extras import execute_batch
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
from odf.opendocument import load as load_ods
from odf.table import Table, TableRow, TableCell
from odf.text import P

from config import PG_DASHBOARD

NTPC_API     = "https://data.ntpc.gov.tw/api/datasets/d9f18db5-41c7-41d4-b7f0-82a335255b08/json"
TPE_ODS_URL  = "https://data.taipei/api/frontstage/tpeod/dataset/resource.download?rid=86d2a8b6-c360-4349-956d-dd7771cccf91"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT   = "taipei-dashboard-parking-rate/1.0 (charles@j-tcg.com)"

# ── Helpers ─────────────────────────────────────────────────────────────────────
def parse_rate(text: str) -> int | None: ...
def parse_tiered(text: str) -> list[dict] | None: ...
def parse_time_range(text: str) -> tuple[dtime | None, dtime | None]: ...
def is_no_charge(text: str) -> bool: ...

# ── Step 1: 爬取 ────────────────────────────────────────────────────────────────
def crawl_ntpc() -> list[dict]: ...
def crawl_tpe()  -> list[dict]: ...

# ── Step 2: 預處理 ─────────────────────────────────────────────────────────────
def preprocess(rows: list[dict]) -> list[dict]: ...

# ── Step 3: Geocode ────────────────────────────────────────────────────────────
def query_overpass(road: str, city: str, district: str) -> list[list[float]] | None: ...
def geocode_one(rec: dict, nominatim: RateLimiter) -> dict | None: ...
def geocode_all(records: list[dict]) -> list[dict]: ...

# ── Step 4: 寫入 PostgreSQL ────────────────────────────────────────────────────
def save_to_postgres(records: list[dict]) -> None: ...

# ── 主程式 ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="雙北路邊停車費率資料管線")
    parser.add_argument("--sources", nargs="+", choices=["ntpc", "tpe"], default=["ntpc", "tpe"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    rows = []
    if "ntpc" in args.sources:
        rows.extend(crawl_ntpc())
    if "tpe" in args.sources:
        rows.extend(crawl_tpe())
    if not rows:
        raise SystemExit(1)

    records = preprocess(rows)
    if args.limit:
        records = records[:args.limit]

    geocoded = geocode_all(records)
    if not geocoded:
        raise SystemExit(1)

    if args.dry_run:
        print(f"[DRY-RUN] 共 {len(geocoded)} 筆，跳過寫入")
        raise SystemExit(0)

    save_to_postgres(geocoded)
```

## 驗收標準

1. 在 lab DB 執行 `python static_parking_rate_tpe.py` 能成功建表並寫入資料（≥ 1 筆）
2. 重複執行 → 表內容刷新（TRUNCATE 生效），無新增 schema、無資料殘留累積
3. NTPC 失敗單一來源 → TPE 仍正常寫入；反之亦然
4. 兩源都失敗 → 程式 exit 1，**且原有表資料未被清空**
5. 進度 log 每 50 筆有報告（geocode 階段），含 Overpass / Nominatim_fb / Failed 計數與 ETA
6. NTPC 板橋區「大同街」（分級費率）寫入後 `tiered_rates` 應 ≠ NULL
7. 至少一筆 `geom_source = 'OVERPASS'` 且 `geometry_path` 長度 > 1
8. 對 git diff：除新增的單檔 + 新增 design doc 外無其他變動
9. `--dry-run` 模式不會碰 DB（無 TRUNCATE、無 INSERT）
10. `--limit 10` 能在數秒內完成（需含 geocode）

## 範例資料樣貌

執行完後 `SELECT * FROM parking_rate_tpe LIMIT 3;` 大致會像：

| ogc_fid | source | city | district | road_segment | weekday_rate | saturday_rate | charging hours (各日) | tiered_rates | start_lat | start_lon | geometry_path 摘要 | geom_source |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | NTPC | 新北市 | 板橋區 | 大同街 | 30 | NULL | 平日 07:00-20:00 | `[{"range":"前2小時","rate":30},{"range":"第3小時以上","rate":40}]` | 25.0142 | 121.4631 | 32 點 | OVERPASS |
| 2 | NTPC | 新北市 | 板橋區 | 田單北街 | 30 | NULL | 平日 07:00-20:00 | NULL | 25.0118 | 121.4598 | 1 點 | NOMINATIM_FALLBACK |
| 3 | TPE | 臺北市 | 大安區 | 信義路四段 | 60 | 60 | 週一至週日 08:00-20:00 | NULL | 25.0339 | 121.5520 | 87 點 | OVERPASS |
