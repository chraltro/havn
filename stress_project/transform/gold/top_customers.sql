-- config: materialized=table, schema=gold
-- depends_on: silver.dim_customer, silver.customer_segments, silver.fct_customer_lifetime

-- Top 100 customers by lifetime spend
SELECT
    dc.customer_id,
    dc.customer_name,
    dc.email,
    dc.created_at,
    dc.customer_tier,
    dc.total_orders,
    dc.completed_orders,
    dc.total_spend,
    dc.avg_order_value,
    dc.first_order_date,
    dc.last_order_date,
    dc.total_sessions,
    dc.total_page_views,
    cs.segment AS rfm_segment,
    cs.rfm_total,
    cl.annualized_value,
    cl.ltv_decile,
    cl.active_months,
    normalize_country(dc.country) AS country,
    revenue_tier(dc.total_spend) AS revenue_tier,
    rank() OVER (ORDER BY dc.total_spend DESC) AS spend_rank,
    round(dc.total_spend / nullif(dc.total_orders, 0), 2) AS spend_per_order,
    datediff('day', dc.first_order_date, dc.last_order_date)
        AS customer_tenure_days
FROM silver.dim_customer AS dc
LEFT JOIN silver.customer_segments AS cs ON dc.customer_id = cs.customer_id
LEFT JOIN silver.fct_customer_lifetime AS cl ON dc.customer_id = cl.customer_id
WHERE dc.total_spend > 0
ORDER BY dc.total_spend DESC
LIMIT 100
