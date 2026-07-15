-- Key number: completion rate over all operator-invited users.
WITH invited AS (
    SELECT count(*)::numeric AS total FROM users
), funnel AS (
    SELECT action, context->>'step' AS step, count(DISTINCT chat_id) AS users
    FROM ui_events
    WHERE action IN ('onboarding_started', 'onboarding_step', 'onboarding_completed')
    GROUP BY action, context->>'step'
)
SELECT action, step, users,
       round(100 * users / NULLIF((SELECT total FROM invited), 0), 1) AS pct_of_invited
FROM funnel
ORDER BY CASE action WHEN 'onboarding_started' THEN 1 WHEN 'onboarding_step' THEN 2 ELSE 3 END, step;
