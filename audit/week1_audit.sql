-- ============================================================
-- newsAnalyzer week-1 audit
-- Цель: оценить, не зарезали ли фильтры что-то важное.
-- Запускать через: psql $DATABASE_URL -f week1_audit.sql
-- Или блоками вручную через psql и копировать output сюда.
-- ============================================================


-- ============================================================
-- БЛОК 0. Проверка схемы (выполнить ОДИН РАЗ перед остальными)
-- Цель: убедиться, что имена колонок совпадают с тем, что я
-- использую ниже. Если расходятся — править запросы под фактические.
-- ============================================================

\echo '=== decisions ==='
\d decisions
\echo '=== digests ==='
\d digests
\echo '=== events ==='
\d events
\echo '=== articles ==='
\d articles


-- ============================================================
-- БЛОК 1. Воронка по стадиям за последние 8 дней
-- Что искать: на какой стадии происходит самый большой
-- "обрыв" — где из X сущностей дальше идёт <1%.
-- ============================================================

SELECT
    stage,
    stage_version,
    COUNT(*)                                                           AS decisions_total,
    COUNT(*) FILTER (WHERE decision_json->>'action' IS NOT NULL)       AS with_action,
    ROUND(AVG(cost_usd)::numeric, 6)                                   AS avg_cost_usd,
    ROUND(SUM(cost_usd)::numeric, 4)                                   AS total_cost_usd
FROM decisions
WHERE created_at >= now() - interval '8 days'
GROUP BY stage, stage_version
ORDER BY
    CASE stage
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
-- Что искать: процент "rejected" / "skipped" / "passed".
-- Если на relevance 99% отвергнуто — фильтр слишком жёсткий.
-- ============================================================

SELECT
    stage,
    decision_json->>'action'   AS action,
    COUNT(*)                   AS n,
    ROUND(100.0 * COUNT(*)
          / SUM(COUNT(*)) OVER (PARTITION BY stage), 1) AS pct
FROM decisions
WHERE created_at >= now() - interval '8 days'
GROUP BY stage, decision_json->>'action'
ORDER BY stage, n DESC;


-- ============================================================
-- БЛОК 3. Sample отказов на RELEVANCE — главное!
-- 30 случайных отвергнутых событий с рационалом модели и
-- одним примером статьи. Прочитать руками — есть ли среди
-- них что-то, что должно было пройти.
-- ============================================================

SELECT
    d.created_at::date                      AS day,
    d.target_id                             AS event_id,
    d.decision_json->>'why'                 AS model_rationale,
    d.decision_json->>'confidence'          AS confidence,
    d.decision_json->'categories'           AS matched_categories,
    LEFT(a.title, 120)                      AS article_title,
    a.source_name,
    a.url
FROM decisions d
JOIN events e ON e.id = d.target_id
LEFT JOIN LATERAL (
    SELECT a.title, a.source_name, a.url
    FROM articles a
    WHERE a.event_id = e.id
    ORDER BY a.published_at DESC NULLS LAST
    LIMIT 1
) a ON true
WHERE d.stage = 'relevance'
  AND d.target_type = 'event'
  AND d.created_at >= now() - interval '8 days'
  AND (d.decision_json->>'relevant')::boolean = false
ORDER BY random()
LIMIT 30;
-- ^ Примечание: если у articles нет колонки event_id (связь many-to-many через
-- отдельную таблицу), замени JOIN на правильную форму.
-- Признак: ошибка "column a.event_id does not exist".


-- ============================================================
-- БЛОК 4. Sample отказов на VERIFY
-- События, прошедшие relevance, но НЕ дошедшие до digest.
-- ============================================================

SELECT
    d.created_at::date                          AS day,
    d.target_id                                 AS event_id,
    d.decision_json->>'action'                  AS action,
    d.decision_json->>'is_speculation'          AS is_speculation,
    d.decision_json->>'sources_count'           AS sources_count,
    d.decision_json->>'hype_score'              AS hype_score,
    d.decision_json->>'speaker_type'            AS speaker_type,
    d.decision_json->>'notes'                   AS verifier_notes,
    LEFT(a.title, 120)                          AS article_title,
    a.url
FROM decisions d
LEFT JOIN LATERAL (
    SELECT a.title, a.url
    FROM articles a
    WHERE a.event_id = d.target_id
    ORDER BY a.published_at DESC NULLS LAST
    LIMIT 1
) a ON true
WHERE d.stage = 'verify'
  AND d.created_at >= now() - interval '8 days'
  AND COALESCE(d.decision_json->>'action', '') NOT IN ('passed', 'verified')
ORDER BY random()
LIMIT 20;


-- ============================================================
-- БЛОК 5. Распределение digest'ов по категориям интересов
-- Какие interest-категории за неделю реально дошли до доставки.
-- Категории, у которых 0 — потенциальный пробел в покрытии.
-- ============================================================

WITH recent_digests AS (
    SELECT
        dg.id,
        dg.created_at::date AS day,
        dg.headline,
        jsonb_array_elements_text(
            COALESCE(
                (SELECT d.decision_json->'categories'
                 FROM decisions d
                 WHERE d.stage = 'relevance'
                   AND d.target_id = dg.event_id
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
-- БЛОК 6. Баланс языков/источников в дошедших digest'ах
-- Перекос покажет, какие источники / языки систематически
-- проигрывают на фильтрации.
-- ============================================================

-- 6a. Источники В digest'ах
SELECT
    a.source_name,
    COUNT(DISTINCT dg.id) AS digests_with_this_source
FROM digests dg
JOIN articles a ON a.event_id = dg.event_id
WHERE dg.created_at >= now() - interval '8 days'
GROUP BY a.source_name
ORDER BY digests_with_this_source DESC;

-- 6b. Источники, ИНГЕСТНУТЫЕ за неделю (всё, что пришло в БД)
SELECT
    source_name,
    COUNT(*) AS articles_ingested
FROM articles
WHERE created_at >= now() - interval '8 days'
GROUP BY source_name
ORDER BY articles_ingested DESC;
-- Сравнить 6a и 6b: источники, которые много ингестятся, но
-- редко дают digest, — кандидаты на проблему с фильтрацией.


-- ============================================================
-- БЛОК 7. Контроль качества: высоко-confidence отказы
-- Relevance отверг с confidence >= 0.85, но рационал короткий
-- ("not relevant", "off-topic") — часто там модель ошиблась уверенно.
-- ============================================================

SELECT
    d.created_at::date                                  AS day,
    LEFT(d.decision_json->>'why', 200)                  AS why,
    LEFT(a.title, 120)                                  AS title,
    a.source_name
FROM decisions d
LEFT JOIN LATERAL (
    SELECT a.title, a.source_name
    FROM articles a
    WHERE a.event_id = d.target_id
    ORDER BY a.published_at DESC NULLS LAST
    LIMIT 1
) a ON true
WHERE d.stage = 'relevance'
  AND d.created_at >= now() - interval '8 days'
  AND (d.decision_json->>'relevant')::boolean = false
  AND (d.decision_json->>'confidence')::float >= 0.85
  AND LENGTH(d.decision_json->>'why') < 150
ORDER BY random()
LIMIT 20;
