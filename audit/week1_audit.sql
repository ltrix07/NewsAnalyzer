-- ============================================================
-- newsAnalyzer week-1 audit (v2 — поправлено под фактическую схему)
-- Запускать: sudo -u postgres psql -d news_analyzer -f audit/week1_audit.sql
-- ============================================================


-- ============================================================
-- БЛОК 0. Схема — для контроля
-- ============================================================

\echo '=== sources ==='
\d sources
\echo '=== event_members ==='
\d event_members


-- ============================================================
-- БЛОК 1. Воронка по стадиям за последние 8 дней
-- ============================================================

\echo '=== БЛОК 1: воронка по стадиям ==='
SELECT
    stage_name,
    stage_version,
    COUNT(*)                                                           AS decisions_total,
    COUNT(*) FILTER (WHERE decision_json->>'action' IS NOT NULL)       AS with_action,
    ROUND(AVG(cost_usd)::numeric, 6)                                   AS avg_cost_usd,
    ROUND(SUM(cost_usd)::numeric, 4)                                   AS total_cost_usd
FROM decisions
WHERE created_at >= now() - interval '8 days'
GROUP BY stage_name, stage_version
ORDER BY
    CASE stage_name
        WHEN 'ingest'        THEN 1
        WHEN 'embed'         THEN 2
        WHEN 'cluster'       THEN 3
        WHEN 'filter_rules'  THEN 4
        WHEN 'relevance'     THEN 5
        WHEN 'verify'        THEN 6
        WHEN 'summarize'     THEN 7
        ELSE 99
    END;


-- ============================================================
-- БЛОК 2. Action-распределение на каждой стадии
-- ============================================================

\echo '=== БЛОК 2: action по стадиям ==='
SELECT
    stage_name,
    decision_json->>'action'   AS action,
    COUNT(*)                   AS n,
    ROUND(100.0 * COUNT(*)
          / SUM(COUNT(*)) OVER (PARTITION BY stage_name), 1) AS pct
FROM decisions
WHERE created_at >= now() - interval '8 days'
GROUP BY stage_name, decision_json->>'action'
ORDER BY stage_name, n DESC;


-- ============================================================
-- БЛОК 3. Sample отказов на RELEVANCE — главное
-- ============================================================

\echo '=== БЛОК 3: 30 случайных relevance-отказов ==='
SELECT
    d.created_at::date                  AS day,
    d.target_id                         AS event_id,
    d.decision_json->>'why'             AS model_rationale,
    d.decision_json->>'confidence'      AS confidence,
    d.decision_json->'categories'       AS matched_categories,
    LEFT(art.title, 120)                AS article_title,
    art.source_name,
    art.url
FROM decisions d
LEFT JOIN LATERAL (
    SELECT a.title, a.url, s.name AS source_name
    FROM event_members em
    JOIN articles a ON a.id = em.article_id
    JOIN sources  s ON s.id = a.source_id
    WHERE em.event_id = d.target_id
    ORDER BY a.published_at DESC NULLS LAST
    LIMIT 1
) art ON true
WHERE d.stage_name = 'relevance'
  AND d.target_type = 'event'
  AND d.created_at >= now() - interval '8 days'
  AND (d.decision_json->>'relevant')::boolean = false
ORDER BY random()
LIMIT 30;


-- ============================================================
-- БЛОК 4. Sample отказов на VERIFY
-- ============================================================

\echo '=== БЛОК 4: 20 случайных verify-отказов ==='
SELECT
    d.created_at::date                          AS day,
    d.target_id                                 AS event_id,
    d.decision_json->>'action'                  AS action,
    d.decision_json->>'is_speculation'          AS is_speculation,
    d.decision_json->>'sources_count'           AS sources_count,
    d.decision_json->>'hype_score'              AS hype_score,
    d.decision_json->>'speaker_type'            AS speaker_type,
    LEFT(d.decision_json->>'notes', 200)        AS verifier_notes,
    LEFT(art.title, 120)                        AS article_title,
    art.source_name
FROM decisions d
LEFT JOIN LATERAL (
    SELECT a.title, s.name AS source_name
    FROM event_members em
    JOIN articles a ON a.id = em.article_id
    JOIN sources  s ON s.id = a.source_id
    WHERE em.event_id = d.target_id
    ORDER BY a.published_at DESC NULLS LAST
    LIMIT 1
) art ON true
WHERE d.stage_name = 'verify'
  AND d.created_at >= now() - interval '8 days'
  AND COALESCE(d.decision_json->>'action', '') NOT IN ('passed', 'verified', 'summarized')
ORDER BY random()
LIMIT 20;


-- ============================================================
-- БЛОК 5. Категории интересов в дошедших digest'ах
-- ============================================================

\echo '=== БЛОК 5: категории в digestах ==='
WITH recent_digests AS (
    SELECT
        dg.id,
        jsonb_array_elements_text(
            COALESCE(
                (SELECT d.decision_json->'categories'
                 FROM decisions d
                 WHERE d.stage_name = 'relevance'
                   AND d.target_id  = dg.event_id
                 ORDER BY d.created_at DESC
                 LIMIT 1),
                '[]'::jsonb
            )
        ) AS category
    FROM digests dg
    WHERE dg.created_at >= now() - interval '8 days'
)
SELECT category, COUNT(*) AS digests
FROM recent_digests
GROUP BY category
ORDER BY digests DESC;


-- ============================================================
-- БЛОК 6. Баланс источников
-- ============================================================

\echo '=== БЛОК 6a: источники В digestах ==='
SELECT
    s.name                       AS source_name,
    COUNT(DISTINCT dg.id)        AS digests_with_this_source
FROM digests dg
JOIN event_members em ON em.event_id = dg.event_id
JOIN articles a      ON a.id = em.article_id
JOIN sources  s      ON s.id = a.source_id
WHERE dg.created_at >= now() - interval '8 days'
GROUP BY s.name
ORDER BY digests_with_this_source DESC;

\echo '=== БЛОК 6b: источники ИНГЕСТНУТЫЕ ==='
SELECT
    s.name        AS source_name,
    COUNT(*)      AS articles_ingested
FROM articles a
JOIN sources  s ON s.id = a.source_id
WHERE a.fetched_at >= now() - interval '8 days'
GROUP BY s.name
ORDER BY articles_ingested DESC;


-- ============================================================
-- БЛОК 7. Уверенные короткие отказы relevance
-- ============================================================

\echo '=== БЛОК 7: confident shallow rejections ==='
SELECT
    d.created_at::date                          AS day,
    LEFT(d.decision_json->>'why', 200)          AS why,
    LEFT(art.title, 120)                        AS title,
    art.source_name
FROM decisions d
LEFT JOIN LATERAL (
    SELECT a.title, s.name AS source_name
    FROM event_members em
    JOIN articles a ON a.id = em.article_id
    JOIN sources  s ON s.id = a.source_id
    WHERE em.event_id = d.target_id
    ORDER BY a.published_at DESC NULLS LAST
    LIMIT 1
) art ON true
WHERE d.stage_name = 'relevance'
  AND d.created_at >= now() - interval '8 days'
  AND (d.decision_json->>'relevant')::boolean = false
  AND COALESCE((d.decision_json->>'confidence')::float, 0) >= 0.85
  AND LENGTH(d.decision_json->>'why') < 150
ORDER BY random()
LIMIT 20;


-- ============================================================
-- БЛОК 8. Бонус: распределение языков статей за неделю
-- ============================================================

\echo '=== БЛОК 8: язык ингестнутых статей ==='
SELECT
    COALESCE(lang, '(null)') AS lang,
    COUNT(*) AS articles
FROM articles
WHERE fetched_at >= now() - interval '8 days'
GROUP BY lang
ORDER BY articles DESC;
