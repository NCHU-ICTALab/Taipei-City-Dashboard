-- =============================================================================
-- 組件：bus_route — 公車路線站點圖  (BusRouteChart)
--
-- 適用資料庫 : dashboardmanager（postgres-manager, port 5432）
-- 所有 INSERT 均為冪等操作，重複執行不會報錯。
--
-- 資料表依賴（須先跑 static_bus_stops.py pipeline）:
--   bus_route_tpe, bus_route_stops_tpe, bus_stop_tpe
--
-- series 格式（two_d）:
--   [{ "x": "299", "y": 0 }, { "x": "2", "y": 0 }, ...]
--   BusRouteChart.vue 以 series[i].x 作為路線名稱清單。
--   站點資料由 GET /api/v1/bus/stops?route=&city= 動態取得。
-- =============================================================================


-- ── 1. component_charts ───────────────────────────────────────────────────────
INSERT INTO public.component_charts (index, color, types, unit)
VALUES (
    'bus_route',
    '{#2979FF,#00BFA5}',
    '{BusRouteChart}',
    '站'
)
ON CONFLICT (index) DO NOTHING;


-- ── 2. components ─────────────────────────────────────────────────────────────
INSERT INTO public.components (id, index, name)
VALUES (1302, 'bus_route', '公車路線站點圖')
ON CONFLICT (id) DO NOTHING;


-- ── 3. query_charts ───────────────────────────────────────────────────────────
-- taipei：僅臺北市路線
INSERT INTO public.query_charts (
    index, history_config, map_config_ids, map_filter,
    time_from, time_to, update_freq, update_freq_unit,
    source, short_desc, long_desc, use_case,
    links, contributors, created_at, updated_at,
    query_type, query_chart, query_history, city
)
SELECT
    'bus_route', NULL, NULL, NULL,
    'static', NULL, NULL, NULL,
    'tcgbusfs', '公車路線站點（臺北市）',
    '以互動式蛇形路線圖顯示臺北市公車各路線去程站序，可搜尋路線編號。',
    '可用於查詢特定路線的停靠站位置與順序。',
    '{}', '{}',
    '2025-01-01 00:00:00+00', '2025-01-01 00:00:00+00',
    'two_d',
    'SELECT route_name AS x_axis, 0 AS data
FROM public.bus_route_tpe
WHERE city = ''臺北市''
GROUP BY route_name
ORDER BY
    CASE WHEN route_name ~ E''^[0-9]+$'' THEN route_name::integer ELSE 99999 END,
    route_name',
    NULL, 'taipei'
WHERE NOT EXISTS (
    SELECT 1 FROM public.query_charts
    WHERE index = 'bus_route' AND city = 'taipei'
);

-- metrotaipei：雙北路線（去重）
INSERT INTO public.query_charts (
    index, history_config, map_config_ids, map_filter,
    time_from, time_to, update_freq, update_freq_unit,
    source, short_desc, long_desc, use_case,
    links, contributors, created_at, updated_at,
    query_type, query_chart, query_history, city
)
SELECT
    'bus_route', NULL, NULL, NULL,
    'static', NULL, NULL, NULL,
    'tcgbusfs/ntpcbus', '公車路線站點（雙北）',
    '以互動式蛇形路線圖顯示雙北公車各路線去程站序，可搜尋路線編號。',
    '可用於查詢特定路線的停靠站位置與順序。',
    '{}', '{}',
    '2025-01-01 00:00:00+00', '2025-01-01 00:00:00+00',
    'two_d',
    'SELECT route_name AS x_axis, 0 AS data
FROM public.bus_route_tpe
GROUP BY route_name
ORDER BY
    CASE WHEN route_name ~ E''^[0-9]+$'' THEN route_name::integer ELSE 99999 END,
    route_name',
    NULL, 'metrotaipei'
WHERE NOT EXISTS (
    SELECT 1 FROM public.query_charts
    WHERE index = 'bus_route' AND city = 'metrotaipei'
);


-- ── 4. dashboards ─────────────────────────────────────────────────────────────
INSERT INTO public.dashboards (id, index, name, components, icon, updated_at, created_at)
VALUES
    (1372, 'bus_route_tpe',    '公車路線查詢', '{1302}', 'directions_bus',
     '2025-01-01 00:00:00+00', '2025-01-01 00:00:00+00'),
    (1373, 'bus_route_newtpe', '公車路線查詢', '{1302}', 'directions_bus',
     '2025-01-01 00:00:00+00', '2025-01-01 00:00:00+00')
ON CONFLICT (id) DO NOTHING;


-- ── 5. dashboard_groups ───────────────────────────────────────────────────────
-- group 2 = taipei（臺北市）、group 3 = metrotaipei（雙北）
INSERT INTO public.dashboard_groups (dashboard_id, group_id)
VALUES (1372, 2), (1373, 3)
ON CONFLICT DO NOTHING;
