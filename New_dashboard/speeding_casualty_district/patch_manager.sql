-- =============================================================================
-- 組件：speeding_casualty_district — dashboardmanager 差異 patch
--
-- 適用資料庫 : dashboardmanager（postgres-manager, port 5432）
-- 可在 pgAdmin (http://localhost:8889) 開啟對應資料庫後直接貼上執行，
-- 或由 setup.py / 根目錄 setup.py 自動呼叫。
-- 此版本只包含必要 table：components / component_charts / query_charts / dashboards / dashboard_groups。
-- =============================================================================


-- ── 1. component_charts ───────────────────────────────────────────────────────
INSERT INTO public.component_charts (index, color, types, unit)
VALUES (
    'speeding_casualty_district',
    '{#FECACA,#FCA5A5,#F87171,#EF4444,#DC2626,#B91C1C,#991B1B,#7F1D1D}',
    '{DistrictChart}',
    '件'
)
ON CONFLICT (index) DO NOTHING;


-- ── 2. components ─────────────────────────────────────────────────────────────
INSERT INTO public.components (id, index, name)
VALUES (301, 'speeding_casualty_district', '超速死亡事故行政區計數')
ON CONFLICT (id) DO UPDATE SET
    index = EXCLUDED.index,
    name = EXCLUDED.name;


-- ── 3. query_charts ───────────────────────────────────────────────────────────
DELETE FROM public.query_charts
WHERE index = 'speeding_casualty_district'
    AND city = 'metrotaipei';

INSERT INTO public.query_charts (
    index, history_config, map_config_ids, map_filter,
    time_from, time_to, update_freq, update_freq_unit,
    source, short_desc, long_desc, use_case,
    links, contributors, created_at, updated_at,
    query_type, query_chart, query_history, city
)
SELECT
    'speeding_casualty_district', NULL, '{5031}', NULL,
    'static', NULL, NULL, NULL,
    '臺北市政府警察局 / 新北市政府警察局', '雙北超速傷亡事故行政區統計',
    '顯示雙北各行政區超速死亡事故件數，並以行政區圖呈現死亡分布。',
    '可用於觀察雙北各行政區超速事故熱點，協助交通安全改善與執法資源配置。',
    '{https://data.taipei/dataset/detail?id=2f238b4f-1b27-4085-93e9-d684ef0e2735,https://data.gov.tw/dataset/125357}', '{doit,ntpc}',
    '2026-05-03 00:00:00+00', '2026-05-03 00:00:00+00',
    'two_d',
    'SELECT district AS x_axis, a1_count AS data
FROM public.speeding_casualty_district_tpe
WHERE a1_count IS NOT NULL
ORDER BY 1',
    NULL, 'metrotaipei';

DELETE FROM public.query_charts
WHERE index = 'speeding_casualty_district'
  AND city = 'taipei';

INSERT INTO public.query_charts (
    index, history_config, map_config_ids, map_filter,
    time_from, time_to, update_freq, update_freq_unit,
    source, short_desc, long_desc, use_case,
    links, contributors, created_at, updated_at,
    query_type, query_chart, query_history, city
)
SELECT
    'speeding_casualty_district', NULL, NULL, NULL,
    'static', NULL, NULL, NULL,
    '臺北市政府警察局', '臺北市超速傷亡事故行政區統計',
    '顯示臺北市各行政區超速傷亡事故件數，並以行政區圖呈現分布。',
    '可用於觀察臺北市各行政區超速事故熱點，協助交通安全改善與執法資源配置。',
    '{https://data.taipei/dataset/detail?id=2f238b4f-1b27-4085-93e9-d684ef0e2735}', '{doit}',
    '2026-05-03 00:00:00+00', '2026-05-03 00:00:00+00',
    'two_d',
    'SELECT district AS x_axis,
            COALESCE(a1_count, 0) + COALESCE(a2_count, 0) AS data
     FROM public.speeding_casualty_district_tpe
     WHERE city = ''臺北市''
     ORDER BY 1',
    NULL, 'taipei';

-- ── 4. dashboards ─────────────────────────────────────────────────────────────
INSERT INTO public.dashboards (id, index, name, components, icon, updated_at, created_at)
VALUES (
    501,
    'speeding_casualty_metrotaipei',
    '超速傷亡事故',
    ARRAY[301]::integer[],
    'warning',
    '2026-05-03 00:00:00+00',
    '2026-05-03 00:00:00+00'
)
ON CONFLICT (id) DO UPDATE SET
    index = EXCLUDED.index,
    name = EXCLUDED.name,
    icon = EXCLUDED.icon,
    updated_at = EXCLUDED.updated_at,
    components = array_append(
        array_remove(public.dashboards.components, 301),
        301
    );

-- ── 5. dashboard_groups ───────────────────────────────────────────────────────
INSERT INTO public.dashboard_groups (dashboard_id, group_id)
VALUES (501, 3)
ON CONFLICT DO NOTHING;
