## Local queue abandonment exercise (observed; simulated effects, no OS process kills)

| variant | items | workers | kills | takeovers | reclaims | routed to reconcile | no effect after intent | effects | double effects | lane violations | live leases | wall s | result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| default_heartbeat | 40 | 4 | 11 | 0 | 7 | 4 | 3 | 37 | 0 | 0 | 0 | 2.628 | PASS |
| long_steps_no_heartbeat | 40 | 4 | 5 | 55 | 20 | 40 | 39 | 1 | 0 | 0 | 0 | 9.155 | PASS |
| long_steps_heartbeat | 40 | 4 | 6 | 0 | 3 | 3 | 3 | 37 | 0 | 0 | 0 | 12.038 | PASS |
