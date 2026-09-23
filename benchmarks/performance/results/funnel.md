## Funnel requirements per tier (modeled)

| V5/day | attempts | enriched | enrich pages | observations | Jev calls | Jev USD | browser h | avg sessions | human h |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1,000 | 1,176 | 4,706 | 3,765 | 13,163 | 9,412 | 1.06 | 50 | 1.84 | 29.4 |
| 5,000 | 5,882 | 23,529 | 18,824 | 65,817 | 47,059 | 5.32 | 250 | 9.19 | 147.1 |
| 10,000 | 11,765 | 47,059 | 37,647 | 131,633 | 94,118 | 10.63 | 500 | 18.38 | 294.1 |

## Submission-only lanes at configured utilization (modeled; yield-adjusted; excludes resume/reconciliation)

| V5/day | window h | service s | per minute | budget s | lanes | ceil |
| --- | --- | --- | --- | --- | --- | --- |
| 1,000 | 24 | 30 | 0.694 | 86.4 | 0.58 | 1 |
| 1,000 | 24 | 60 | 0.694 | 86.4 | 1.17 | 2 |
| 1,000 | 24 | 120 | 0.694 | 86.4 | 2.33 | 3 |
| 1,000 | 8 | 30 | 2.083 | 28.8 | 1.75 | 2 |
| 1,000 | 8 | 60 | 2.083 | 28.8 | 3.5 | 4 |
| 1,000 | 8 | 120 | 2.083 | 28.8 | 7 | 8 |
| 5,000 | 24 | 30 | 3.472 | 17.28 | 2.92 | 3 |
| 5,000 | 24 | 60 | 3.472 | 17.28 | 5.84 | 6 |
| 5,000 | 24 | 120 | 3.472 | 17.28 | 11.67 | 12 |
| 5,000 | 8 | 30 | 10.417 | 5.76 | 8.75 | 9 |
| 5,000 | 8 | 60 | 10.417 | 5.76 | 17.51 | 18 |
| 5,000 | 8 | 120 | 10.417 | 5.76 | 35.01 | 36 |
| 10,000 | 24 | 30 | 6.944 | 8.64 | 5.84 | 6 |
| 10,000 | 24 | 60 | 6.944 | 8.64 | 11.67 | 12 |
| 10,000 | 24 | 120 | 6.944 | 8.64 | 23.34 | 24 |
| 10,000 | 8 | 30 | 20.833 | 2.88 | 17.51 | 18 |
| 10,000 | 8 | 60 | 20.833 | 2.88 | 35.01 | 36 |
| 10,000 | 8 | 120 | 20.833 | 2.88 | 70.03 | 71 |

## Search plan (code-derived bounds, modeled timing and detail success)

| source | search pages | detail pages | seconds | max listings | assumed FULL descriptions |
| --- | --- | --- | --- | --- | --- |
| linkedin | 8 | 10 | 128 | 50 | 0 |
| builtin | 16 | 10 | 130 | 50 | 10 |
| indeed | 16 | 10 | 130 | 50 | 10 |
| google | 6 | 10 | 76 | 50 | 0 |

Total: 86 page operations, ≈ 7.7 min, ≤ 200 observations; 20 FULL descriptions assumed, not observed.
