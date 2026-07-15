-- Batch delivery beta metrics.
-- Kill criterion: open rate < 30% in beta week 2 means the delivery hypothesis is not validated.

SELECT
    count(*) AS notified_batches,
    count(*) FILTER (WHERE opened_at IS NOT NULL) AS opened_batches,
    round(100.0 * count(*) FILTER (WHERE opened_at IS NOT NULL) / NULLIF(count(*), 0), 1)
        AS open_rate_percent
FROM delivery_batches
WHERE notified_at IS NOT NULL;

SELECT
    percentile_cont(0.5) WITHIN GROUP (ORDER BY opened_at - notified_at) AS median_time_to_open,
    percentile_cont(0.9) WITHIN GROUP (ORDER BY opened_at - notified_at) AS p90_time_to_open
FROM delivery_batches
WHERE notified_at IS NOT NULL AND opened_at IS NOT NULL;

SELECT
    count(*) FILTER (WHERE closed_at IS NOT NULL) AS revealed_all,
    count(*) FILTER (WHERE closed_at IS NULL) AS stopped_before_all
FROM delivery_batches
WHERE opened_at IS NOT NULL;
