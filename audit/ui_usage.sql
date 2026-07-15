-- Feature usage per ISO week. Missing actions are the features nobody uses.
SELECT
    date_trunc('week', created_at)::date AS iso_week,
    action,
    COUNT(*) AS presses
FROM ui_events
GROUP BY iso_week, action
ORDER BY iso_week DESC, action;

-- Share of dislike presses followed by a reason from the same chat within 10 minutes.
SELECT
    COUNT(*) AS dislike_presses,
    COUNT(*) FILTER (WHERE completed) AS completed_drill_downs,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE completed) / NULLIF(COUNT(*), 0),
        1
    ) AS completion_rate_pct
FROM (
    SELECT EXISTS (
        SELECT 1
        FROM ui_events reason
        WHERE reason.action = 'dislike_reason'
          AND reason.chat_id = dislike.chat_id
          AND reason.created_at >= dislike.created_at
          AND reason.created_at <= dislike.created_at + interval '10 minutes'
    ) AS completed
    FROM ui_events dislike
    WHERE dislike.action = 'dislike'
) drill_downs;
