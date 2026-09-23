## Pipeline simulation, per simulated day (modeled; no site acceptance measured)

| scenario | V1 obs | V2 unique | enrich pages | Jev calls | Jev USD | V3 APPLY | REVIEW | simulated accepted | unknown | input stops | human h | binding | util |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| today: supply 50, 1 browser, sequential Jev, no enrich lane | 10,208 | 50 | 0 | 66 | 0.005 | 0 | 32 | 0 | 0 | 0 | 0 | chrome | 0.224 |
| phase1-3: supply 50, 1 browser, enrich lane, gate before Jev | 8,880 | 50 | 52 | 42 | 0.005 | 1 | 35 | 1 | 0 | 1 | 0.12 | chrome | 0.121 |
| EXPERIMENTAL simulated phase4: supply 50, 4 browser slots, jev 4, chrome 2 | 8,976 | 50 | 43 | 36 | 0.004 | 0 | 32 | 0 | 0 | 0 | 0 | chrome | 0.061 |
| today: supply 200, 1 browser, sequential Jev, no enrich lane | 10,324 | 200 | 0 | 272 | 0.02 | 0 | 120 | 0 | 0 | 0 | 0 | chrome | 0.227 |
| phase1-3: supply 200, 1 browser, enrich lane, gate before Jev | 8,901 | 200 | 182 | 168 | 0.018 | 4 | 123 | 4 | 0 | 4 | 0.42 | chrome | 0.129 |
| EXPERIMENTAL simulated phase4: supply 200, 4 browser slots, jev 4, chrome 2 | 8,976 | 200 | 170 | 168 | 0.018 | 5 | 117 | 5 | 0 | 5 | 0.79 | human_window | 0.099 |
| today: supply 1000, 1 browser, sequential Jev, no enrich lane | 10,324 | 1,000 | 0 | 1,448 | 0.108 | 5 | 628 | 5 | 0 | 5 | 0.67 | chrome | 0.226 |
| phase1-3: supply 1000, 1 browser, enrich lane, gate before Jev | 8,832 | 1,000 | 877 | 962 | 0.104 | 39 | 573 | 34 | 5 | 40 | 6 | human_window | 0.75 |
| EXPERIMENTAL simulated phase4: supply 1000, 4 browser slots, jev 4, chrome 2 | 8,976 | 1,000 | 859 | 966 | 0.104 | 38 | 565 | 37 | 1 | 38 | 6.21 | human_window | 0.776 |
| today: supply 5000, 1 browser, sequential Jev, no enrich lane | 10,324 | 5,000 | 0 | 7,026 | 0.528 | 14 | 3,043 | 14 | 0 | 13 | 1.54 | chrome | 0.229 |
| phase1-3: supply 5000, 1 browser, enrich lane, gate before Jev | 8,400 | 5,000 | 4,246 | 4,604 | 0.498 | 169 | 2,773 | 118 | 2 | 102 | 8 | human_window | 1 |
| EXPERIMENTAL simulated phase4: supply 5000, 4 browser slots, jev 4, chrome 2 | 8,826 | 5,000 | 4,265 | 4,828 | 0.522 | 160 | 2,768 | 106 | 8 | 94 | 8 | human_window | 1 |
