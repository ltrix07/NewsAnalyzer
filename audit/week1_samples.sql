-- ============================================================
-- newsAnalyzer week-1: семплы отказов (v3 — путь decision_json фиксирован)
-- Запуск: sudo -u postgres psql -d news_analyzer -f audit/week1_samples.sql
-- ============================================================


-- ============================================================
-- БЛОК A. 40 случайных relevance-отказов
-- ============================================================

\echo '=== A: 40 случайных relevance-отказов ==='
SELECT
    d.created_at::date                                       AS day,
    d.target_id                                              AS event_id,
    LEFT(d.decision_json->'verdict'->>'why', 240)            AS model_rationale,
    (d.decision_json->'verdict'->>'confidence')::float       AS confidence,
    LEFT(art.title, 100)                                     AS article_title,
    art.source_name,
    art.lang
FROM decisions d
LEFT JOIN LATERAL (
    SELECT a.title, a.lang, s.name AS source_name
    FROM event_members em
    JOIN articles a ON a.id = em.article_id
    JOIN sources  s ON s.id = a.source_id
    WHERE em.event_id = d.target_id
    ORDER BY a.published_at DESC NULLS LAST
    LIMIT 1
) art ON true
WHERE d.stage_name = 'relevance'
  AND d.decision_json->>'action' = 'irrelevant'
  AND d.created_at >= now() - interval '8 days'
ORDER BY random()
LIMIT 40;


-- ============================================================
-- БЛОК B. ОТКАЗЫ, ГДЕ В ЗАГОЛОВКЕ УПОМЯНУТА УКРАИНА
-- (главный тест на false negatives — события про UA должны
-- проходить почти все)
-- ============================================================

\echo '=== B: relevance-отказы со словом Украина/Ukraine в заголовке ==='
SELECT
    d.created_at::date                                       AS day,
    d.target_id                                              AS event_id,
    LEFT(d.decision_json->'verdict'->>'why', 240)            AS model_rationale,
    (d.decision_json->'verdict'->>'confidence')::float       AS confidence,
    LEFT(art.title, 120)                                     AS article_title,
    art.source_name
FROM decisions d
JOIN LATERAL (
    SELECT a.title, s.name AS source_name
    FROM event_members em
    JOIN articles a ON a.id = em.article_id
    JOIN sources  s ON s.id = a.source_id
    WHERE em.event_id = d.target_id
    ORDER BY a.published_at DESC NULLS LAST
    LIMIT 1
) art ON true
WHERE d.stage_name = 'relevance'
  AND d.decision_json->>'action' = 'irrelevant'
  AND d.created_at >= now() - interval '8 days'
  AND (
        art.title ILIKE '%Україн%'
     OR art.title ILIKE '%Украин%'
     OR art.title ILIKE '%Ukrain%'
     OR art.title ILIKE '%Ukrain%'
  )
ORDER BY random()
LIMIT 30;


-- ============================================================
-- БЛОК C. ОТКАЗЫ, ГДЕ ЗАГОЛОВОК УПОМИНАЕТ ПОЛЬСКУЮ МИГРАЦИЮ
-- ============================================================

\echo '=== C: relevance-отказы со словами миграция/виза/permit в заголовке ==='
SELECT
    d.created_at::date                                       AS day,
    d.target_id                                              AS event_id,
    LEFT(d.decision_json->'verdict'->>'why', 240)            AS model_rationale,
    LEFT(art.title, 120)                                     AS article_title,
    art.source_name
FROM decisions d
JOIN LATERAL (
    SELECT a.title, s.name AS source_name
    FROM event_members em
    JOIN articles a ON a.id = em.article_id
    JOIN sources  s ON s.id = a.source_id
    WHERE em.event_id = d.target_id
    ORDER BY a.published_at DESC NULLS LAST
    LIMIT 1
) art ON true
WHERE d.stage_name = 'relevance'
  AND d.decision_json->>'action' = 'irrelevant'
  AND d.created_at >= now() - interval '8 days'
  AND (
        art.title ILIKE '%cudzoziem%'        -- иностранец (PL)
     OR art.title ILIKE '%migrac%'
     OR art.title ILIKE '%migrant%'
     OR art.title ILIKE '%uchodź%'           -- беженец (PL)
     OR art.title ILIKE '%виз%'
     OR art.title ILIKE '%pobyt%'            -- residence (PL)
     OR art.title ILIKE '%residence%'
     OR art.title ILIKE '%permit%'
     OR art.title ILIKE '%карт%полякa%'
     OR art.title ILIKE '%refugee%'
  )
ORDER BY random()
LIMIT 30;


-- ============================================================
-- БЛОК D. Уверенные «короткие» отказы (confident shallow)
-- ============================================================

\echo '=== D: confident shallow rejections ==='
SELECT
    d.created_at::date                                       AS day,
    (d.decision_json->'verdict'->>'confidence')::float       AS confidence,
    LENGTH(d.decision_json->'verdict'->>'why')               AS why_len,
    LEFT(d.decision_json->'verdict'->>'why', 200)            AS why,
    LEFT(art.title, 100)                                     AS title,
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
  AND d.decision_json->>'action' = 'irrelevant'
  AND d.created_at >= now() - interval '8 days'
  AND (d.decision_json->'verdict'->>'confidence')::float >= 0.9
ORDER BY random()
LIMIT 20;


-- ============================================================
-- БЛОК E. Что ВСЁ-ТАКИ прошло (5 digestов) — для контраста
-- ============================================================

\echo '=== E: 5 digestов недели ==='
SELECT
    dg.created_at::date            AS day,
    dg.event_id,
    dg.confidence_level,
    LEFT(dg.headline, 100)         AS headline,
    LEFT(dg.summary, 200)          AS summary_start
FROM digests dg
WHERE dg.created_at >= now() - interval '8 days'
ORDER BY dg.created_at DESC;


-- ============================================================
-- БЛОК F. Cluster join rate ПО ИСТОЧНИКАМ
-- Покажет, какие источники сливаются с существующими events,
-- а какие всегда создают новые (плохая кластеризация по языку).
-- ============================================================

\echo '=== F: cluster join rate по источникам ==='
SELECT
    s.name                                                                AS source_name,
    COUNT(*)                                                              AS clustered,
    COUNT(*) FILTER (WHERE d.decision_json->>'action' = 'joined_event')   AS joined,
    COUNT(*) FILTER (WHERE d.decision_json->>'action' = 'created_event')  AS created_new,
    ROUND(100.0 * COUNT(*) FILTER (WHERE d.decision_json->>'action' = 'joined_event')
                / NULLIF(COUNT(*), 0), 1)                                  AS join_pct
FROM decisions d
JOIN articles a ON a.id = d.target_id AND d.target_type = 'article'
JOIN sources  s ON s.id = a.source_id
WHERE d.stage_name = 'cluster'
  AND d.created_at >= now() - interval '8 days'
GROUP BY s.name
ORDER BY clustered DESC;
