# Latency overhead benchmark

How many milliseconds does Routerly add to a provider call? This directory measures it: the same request is sent (a) straight to an upstream and (b) through a Routerly router that forwards to that upstream. **Overhead = gateway latency − direct latency**, reported at p50 / p95 / p99, for streaming (time to first byte) and non-streaming (total time), at concurrency 1 and 4, with a small and a large request.

Run by the Routerly team (the authors of both the gateway and this benchmark). Numbers are measured by the vendor on one machine and are not an external audit; rerun them on your own hardware (below).

## Method

- **Simulated upstream** (`mock_upstream.py`, OpenAI-compatible chat completions + embeddings, pure stdlib). Fixed, known latency: first byte after 100 ms, one chunk every 5 ms, 20 reply tokens (non-stream total = 200 ms), 150 ms for the calls Routerly itself makes (routing/judge), 40 ms for embeddings. Using a simulated upstream removes provider noise so the only variable is the gateway. Wire format follows the [OpenAI API reference](https://github.com/openai/openai-openapi) (consulted 2026-11-11).
- **Interleaving.** Direct and gateway batches alternate inside each of 3 rounds (order alternates every round) so slow machine drift hits both sides. 10 warm-up requests per cell are discarded. 20 requests per round at concurrency 1, 64 at concurrency 4.
- **Reproducibility margin.** `spread` in the report = max − min of the per-round p50 overhead.
- **`selfcheck.py`** must pass before a run is trusted: the direct baseline reproduces the mock's configured latency (±3 ms; measured floor is ≈ +2.4 ms non-stream, +1.8 ms stream TTFB from timers/loopback) and every scenario triggers exactly the extra upstream calls it is named for.

## Scenarios (one router each, `setup_routerly.py`)

| Scenario | What it is |
|---|---|
| `router-minimal` | one model, no policy: the floor of the gateway |
| `router-simple` | `cheapest` + `health` policies, 2 models |
| `router-llm-policy` | routing decided by an LLM call (`llm` policy) |
| `router-semantic` | `semantic-intent` policy (one embedding call) |
| `guardrails-regex` | prompt-injection detection + regex rule |
| `guardrails-judge` | topic guardrail judged by an LLM (one extra call) |
| `pii` | PII detection/masking on request and response |
| `optimizers` | session-dedup, ccr, rtk, caveman optimizers |

The request body carries PII-like text so the PII scenario has work to do; the large request adds ≈ 24,000 characters of filler.

## Published results — Routerly 0.6.1 (df1e0bbb), 2026-11-11

Full tables: [`published/2026-11-11/report.md`](published/2026-11-11/report.md); raw data and environment: `results.json`, `environment.json`; harness check: `selfcheck.txt`; second run for the reproducibility comparison: `repeat/results.json`.

Non-streaming, concurrency 1, small request (overhead in ms):

| Scenario | p50 | p95 | p99 |
|---|---|---|---|
| router-minimal | +9.5 | +22.3 | +33.3 |
| router-simple | +22.3 | +47.7 | +51.4 |
| router-llm-policy | +163.6 | +171.2 | +176.8 |
| router-semantic | +51.0 | +60.9 | +63.5 |
| guardrails-regex | +9.2 | +10.6 | +27.9 |
| guardrails-judge | +165.2 | +187.2 | +215.8 |
| pii | +10.1 | +23.9 | +33.8 |
| optimizers | +9.4 | +15.6 | +15.7 |

Reading: a gateway with no policy adds ≈ 9 ms at p50; regex guardrails, PII and optimizers stay within a few ms of that. `llm` routing and judge guardrails add the latency of the extra model call they make (≈ 150 ms in this setup) plus the gateway floor. Semantic routing adds ≈ 40 ms embedding + floor.

### Unfavourable results (shown on purpose)

- **`router-simple` grows with usage history and with concurrency**: +22 ms at concurrency 1 on an empty history, +366 ms (small) and +1,283 ms (large) at concurrency 4, +991 ms streaming TTFB at concurrency 4. Policies `health`/`performance`/`fairness`/`rate-limit` re-read the whole usage log on every request (tracked as ROUT-740). These rows are not reproducible between runs for that reason.
- **Concurrency 4 raises the tail everywhere**: p95/p99 of the cheap scenarios go from ~20–30 ms to 40–125 ms.
- **High concurrency crashes the service**: at concurrency ≥ 8 the 0.6.1 service can die with an `ELOCKED` error (ROUT-737); one repeat run hit it at concurrency 4 too. That is why published concurrency stops at 4.

## Limits

- One machine (Intel i3-4150T, 3 cores, 8 GB) that also runs client, gateway and upstream over loopback: no network, no TLS, CPU shared between the three. Absolute values will differ on other hardware; the ranking of scenarios should not.
- Simulated upstream, not real providers: real providers add their own variable latency; what is measured is only the gateway's share.
- One Routerly version, one date. Single run is published; the second run (`repeat/`) did not complete (see above), so the comparison covers the 34 cells it did produce: median difference of the per-cell p50 overhead 1.3 ms, 90th percentile 6 ms, max 8.3 ms for all scenarios except `router-simple` (median 8.8 ms, max 73 ms). Treat differences below ≈ 8 ms as noise.
- A few p99 values are dominated by outliers (one streaming PII cell shows a negative p99 because of a slow direct-side outlier); p99 over 60–192 samples is indicative only.
- Concurrency above 4 and sustained load are not covered.

## Reproduce

```bash
cd Latency
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt

# 1. simulated upstream
python mock_upstream.py --port 4100 &

# 2. a Routerly service (0.6.1 or other) with its first admin created, e.g. on :3100

# 3. create the models and routers (idempotent), writes scenarios.json
python setup_routerly.py --routerly http://127.0.0.1:3100 --email <admin email> --password <admin password> --mock http://127.0.0.1:4100

# 4. validate the harness, then measure
python selfcheck.py --scenarios scenarios.json
python benchmark.py --scenarios scenarios.json --routerly-version <version> --routerly-commit <sha>
```

Output: `results/latency_<timestamp>/{results.json,report.md,environment.json}`. Start every run from an empty usage history (`usage.ndjson`) to compare runs; `--only`, `--concurrency`, `--sizes`, `--rounds` select a subset.
