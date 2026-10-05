# Performance

Docker stack (one uvicorn process, one worker, Postgres 16) after the demo: one production, 85 scenes, three scenarios, two
solved schedules. Reproduce with `make demo`, then `make load-test SCENARIO=<id> DATE=<shoot date>`.

## Reads: the stripboard and a call sheet (in rotation), closed loop, 10 s per level

| Concurrency | Requests | Throughput | p50 | p95 | p99 | Errors |
|---|---|---|---|---|---|---|
| 1 | 2558 | 255.7 req/s | 3.6 ms | 4.7 ms | 9.5 ms | 0.00% |
| 8 | 3659 | 365.3 req/s | 20.9 ms | 28.8 ms | 32.3 ms | 0.00% |
| 32 | 2725 | 270.6 req/s | 87.2 ms | 101.2 ms | 109.7 ms | 0.00% |

Target from the blueprint: p95 under 400 ms for reads: met.

## Compute

| Work | Time |
|---|---|
| Parse a 7,300-line screenplay and store 85 scenes, 12 cast, 14 locations | about 0.1 s |
| Solve a scenario: 85 scenes x 24 days (2,040 assignment variables), 8 search workers | as long as it is given: 12 s in the demo |
| First feasible schedule | under 1 s (the script-order schedule is supplied as a starting hint) |
| Evaluation: four screenplays, three solves each | about 2 minutes |

The solver is time-limited by design. Its answers improve quickly at first and slowly after; in 12 seconds the proven lower bound
is still about half the cost found, so longer runs might find cheaper schedules. Results can differ slightly between runs because
the search is parallel.

Not measured: a production with hundreds of scenes or a second unit. Model size grows with scenes x days.
