-- Показать фактическую структуру decision_json для каждой стадии.
-- 3 случайных записи на стадию — этого хватит чтобы перестроить семпл-запросы.

\echo '=== relevance: 3 случайных decision_json ==='
SELECT id, decision_json
FROM decisions
WHERE stage_name = 'relevance'
ORDER BY random()
LIMIT 3;

\echo '=== verify: 3 случайных decision_json ==='
SELECT id, decision_json
FROM decisions
WHERE stage_name = 'verify'
ORDER BY random()
LIMIT 3;

\echo '=== summarize: 3 случайных decision_json ==='
SELECT id, decision_json
FROM decisions
WHERE stage_name = 'summarize'
ORDER BY random()
LIMIT 3;

\echo '=== cluster: 3 случайных decision_json ==='
SELECT id, decision_json
FROM decisions
WHERE stage_name = 'cluster'
ORDER BY random()
LIMIT 3;
