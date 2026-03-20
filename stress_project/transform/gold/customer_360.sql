-- config: materialized=table, schema=gold
-- depends_on: silver.dim_customer, silver.customer_segments, silver.fct_customer_lifetime
-- assert: row_count > 0, unique(customer_id), no_nulls(customer_id), accepted_values(activity_status, ['never_purchased', 'churned', 'at_risk', 'cooling', 'active'])

-- Full 360-degree customer profile
SELECT
    dc.customer_id,
    dc.name,
    mask_email(dc.email) AS email,
    dc.country,
    dc.created_at,
    dc.customer_tier,
    dc.total_orders,
    dc.completed_orders,
    dc.cancelled_orders,
    dc.total_spend,
    dc.avg_order_value,
    dc.first_order_date,
    dc.last_order_date,
    dc.total_sessions,
    dc.avg_session_duration,
    dc.total_page_views,
    cs.r_score,
    cs.f_score,
    cs.m_score,
    cs.rfm_total,
    cs.segment AS rfm_segment,
    cl.lifespan_days,
    cl.active_months,
    cl.monthly_value,
    cl.annualized_value,
    cl.ltv_decile,
    CASE
        WHEN dc.last_order_date IS NULL THEN 'never_purchased'
        WHEN DATEDIFF('day', dc.last_order_date, TIMESTAMP '2024-12-31') > 365 THEN 'churned'
        WHEN DATEDIFF('day', dc.last_order_date, TIMESTAMP '2024-12-31') > 180 THEN 'at_risk'
        WHEN DATEDIFF('day', dc.last_order_date, TIMESTAMP '2024-12-31') > 90 THEN 'cooling'
        ELSE 'active'
    END AS activity_status,
    DATEDIFF('day', dc.last_order_date, TIMESTAMP '2024-12-31') AS days_since_last_order
FROM silver.dim_customer dc
LEFT JOIN silver.customer_segments cs ON dc.customer_id = cs.customer_id
LEFT JOIN silver.fct_customer_lifetime cl ON dc.customer_id = cl.customer_id
