-- =============================================================================
-- 組件：bus_transfer — 公車轉乘圖  (BusTransferChart)
--
-- 適用資料庫 : dashboardmanager（postgres-manager, port 5432）
-- 所有 INSERT 均為冪等操作，重複執行不會報錯。
--
-- 資料表依賴（須先跑 static_bus_stops.py pipeline）:
--   bus_route_tpe, bus_route_stops_tpe, bus_stop_tpe
-- =============================================================================

-- ── 1. component_charts ───────────────────────────────────────────────────────
INSERT INTO public.component_charts (index, color, types, unit)
VALUES (
    'bus_transfer',
    '{#2979FF,#00BFA5}',
    '{BusTransferChart}',
    '次'
)
ON CONFLICT (index) DO NOTHING;


-- ── 2. components ─────────────────────────────────────────────────────────────
INSERT INTO public.components (id, index, name)
VALUES (303, 'bus_transfer', '公車轉乘查詢')
ON CONFLICT (id) DO NOTHING;


-- ── 3. query_charts ───────────────────────────────────────────────────────────
-- taipei
INSERT INTO public.query_charts (
    index, history_config, map_config_ids, map_filter,
    time_from, time_to, update_freq, update_freq_unit,
    source, short_desc, long_desc, use_case,
    links, contributors, created_at, updated_at,
    query_type, query_chart, query_history, city
)
SELECT
    'bus_transfer', NULL, NULL, NULL,
    'static', NULL, NULL, NULL,
    'tcgbusfs', '公車轉乘查詢（臺北市）',
    '以互動介面提供臺北市公車直達與一次轉乘路線查詢。',
    '可用於查詢跨站點之轉乘建議方案。',
    '{}', '{}',
    '2025-01-01 00:00:00+00', '2025-01-01 00:00:00+00',
    'two_d',
    'SELECT ''轉乘'' AS x_axis, 0 AS data',
    NULL, 'taipei'
WHERE NOT EXISTS (
    SELECT 1 FROM public.query_charts
    WHERE index = 'bus_transfer' AND city = 'taipei'
);

-- metrotaipei
INSERT INTO public.query_charts (
    index, history_config, map_config_ids, map_filter,
    time_from, time_to, update_freq, update_freq_unit,
    source, short_desc, long_desc, use_case,
    links, contributors, created_at, updated_at,
    query_type, query_chart, query_history, city
)
SELECT
    'bus_transfer', NULL, NULL, NULL,
    'static', NULL, NULL, NULL,
    'tcgbusfs/ntpcbus', '公車轉乘查詢（雙北）',
    '以互動介面提供雙北公車直達與一次轉乘路線查詢。',
    '可用於查詢跨站點之轉乘建議方案。',
    '{}', '{}',
    '2025-01-01 00:00:00+00', '2025-01-01 00:00:00+00',
    'two_d',
    'SELECT ''轉乘'' AS x_axis, 0 AS data',
    NULL, 'metrotaipei'
WHERE NOT EXISTS (
    SELECT 1 FROM public.query_charts
    WHERE index = 'bus_transfer' AND city = 'metrotaipei'
);


-- ── 4. dashboards ─────────────────────────────────────────────────────────────
INSERT INTO public.dashboards (id, index, name, components, icon, updated_at, created_at)
VALUES
    (374, 'bus_transfer_tpe',    '公車轉乘規劃', '{303}', 'directions_transit', '2025-01-01 00:00:00+00', '2025-01-01 00:00:00+00'),
    (375, 'bus_transfer_newtpe', '公車轉乘規劃', '{303}', 'directions_transit', '2025-01-01 00:00:00+00', '2025-01-01 00:00:00+00')
ON CONFLICT (id) DO NOTHING;


-- ── 5. dashboard_groups ───────────────────────────────────────────────────────
INSERT INTO public.dashboard_groups (dashboard_id, group_id)
VALUES (374, 2), (375, 3)
ON CONFLICT DO NOTHING;
