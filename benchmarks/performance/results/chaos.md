## Local queue abandonment exercise (observed; simulated effects, no OS process kills)

| variant | items | workers | kills | takeovers | reclaims | routed to reconcile | no effect after intent | effects | double effects | lane violations | live leases | wall s | result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| default_heartbeat | 40 | 4 | 11 | 0 | 5 | 6 | 4 | 36 | 0 | 0 | 0 | 2.596 | PASS |
| long_steps_no_heartbeat | 40 | 4 | 8 | 57 | 25 | 40 | 39 | 1 | 0 | 0 | 0 | 9.843 | PASS |
| long_steps_heartbeat | 40 | 4 | 8 | 0 | 4 | 4 | 4 | 36 | 0 | 0 | 0 | 11.918 | PASS |
