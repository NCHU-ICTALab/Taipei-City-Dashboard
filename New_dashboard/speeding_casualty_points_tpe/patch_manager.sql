-- =============================================================================
-- 組件：speeding_casualty_points_tpe — dashboardmanager 差異 patch
--
-- 適用資料庫 : dashboardmanager（postgres-manager, port 5432）
-- 可在 pgAdmin (http://localhost:8889) 開啟對應資料庫後直接貼上執行，
-- 或由 setup.py / 根目錄 setup.py 自動呼叫。
-- 此版本新增地圖點位組件，資料來源為臺北市超速傷亡事故點位。
-- =============================================================================


ALTER TABLE public.component_maps
ADD COLUMN IF NOT EXISTS city varchar;


-- ── 1. component_maps ─────────────────────────────────────────────────────────
INSERT INTO public.component_maps (id, index, title, type, source, size, icon, paint, property, city)
VALUES (
    5031,
    'speeding_casualty_points_tpe',
    '超速傷亡事故點位',
    'circle',
    'geojson',
    'big',
    NULL,
    '{"circle-color":"#DC2626","circle-opacity":0.95,"circle-stroke-color":"#DC2626","circle-stroke-width":0,"cluster-enabled":true,"cluster-radius":42,"cluster-max-zoom":14,"cluster-color":"#B91C1C","cluster-text-color":"#FFFFFF","cluster-stroke-color":"#7F1D1D","cluster-stroke-width":1.5}'::json,
    '[
        {"key":"district","name":"行政區"},
        {"key":"location_text","name":"地點"},
        {"key":"accident_time","name":"事故時間"},
        {"key":"death_count","name":"死亡人數"},
        {"key":"injury_count","name":"受傷人數"}
    ]'::json,
    'taipei'
)
ON CONFLICT (id) DO UPDATE SET
    index = EXCLUDED.index,
    title = EXCLUDED.title,
    type = EXCLUDED.type,
    source = EXCLUDED.source,
    size = EXCLUDED.size,
    icon = EXCLUDED.icon,
    paint = EXCLUDED.paint,
    property = EXCLUDED.property,
    city = EXCLUDED.city;


-- ── 2. 將點位圖層掛到既有行政區計數組件 ───────────────────────────────────────
UPDATE public.query_charts
SET map_config_ids = CASE
        WHEN map_config_ids @> ARRAY[5031]::integer[] THEN map_config_ids
        WHEN map_config_ids IS NULL THEN ARRAY[5031]::integer[]
        ELSE array_append(map_config_ids, 5031)
END
WHERE index = 'speeding_casualty_district'
    AND city = 'taipei';

-- 同時保證 metrotaipei query_chart 的 map_config_ids 也含 5031（修復雙北 toggle）
UPDATE public.query_charts
SET map_config_ids = CASE
        WHEN map_config_ids @> ARRAY[5031]::integer[] THEN map_config_ids
        WHEN map_config_ids IS NULL THEN ARRAY[5031]::integer[]
        ELSE array_append(map_config_ids, 5031)
END
WHERE index = 'speeding_casualty_district'
    AND city = 'metrotaipei';

-- ── 3. 移除獨立點位圖表 metadata ──────────────────────────────────────────────
UPDATE public.dashboards
SET components = array_remove(components, 503)
WHERE id = 501
    AND components @> ARRAY[503]::integer[];

DELETE FROM public.query_charts
WHERE index = 'speeding_casualty_points_tpe';

DELETE FROM public.component_charts
WHERE index = 'speeding_casualty_points_tpe';

DELETE FROM public.components
WHERE id = 503
     OR index = 'speeding_casualty_points_tpe';
