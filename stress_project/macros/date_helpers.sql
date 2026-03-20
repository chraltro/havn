CREATE OR REPLACE MACRO quarter_start(d) AS DATE_TRUNC('quarter', d::DATE);
CREATE OR REPLACE MACRO is_weekend(d) AS EXTRACT(DOW FROM d::DATE) IN (0, 6);
CREATE OR REPLACE MACRO days_between(a, b) AS DATEDIFF('day', a::DATE, b::DATE);
