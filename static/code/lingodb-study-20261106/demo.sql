SELECT a.k, SUM(a.v * b.w) AS total, COUNT(*) AS n
FROM (VALUES (1, 10), (1, 20), (2, 7), (3, 9)) AS a(k, v)
JOIN (VALUES (1, 2), (1, 3), (2, 4)) AS b(k, w) ON a.k = b.k
GROUP BY a.k
ORDER BY a.k;
