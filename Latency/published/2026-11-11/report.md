# Routerly latency overhead

Run by the Routerly team on 2026-11-11T14:24:31+00:00 — Routerly 0.6.1 (df1e0bbb).

Machine: Intel(R) Core(TM) i3-4150T CPU @ 3.00GHz, 3 logical cores, 8.0Gi RAM, Linux-7.0.14-25-pve-x86_64-with-glibc2.36.
Simulated upstream: {"host": "127.0.0.1", "port": 4100, "ttfb_ms": 100, "chunk_ms": 5, "reply_tokens": 20, "aux_ms": 150, "embed_ms": 40}.
Topology: client, Routerly and the simulated upstream run on the same host over loopback.

Overhead = latency through Routerly − latency direct to the upstream, ms. `spread` = max−min of the per-round p50 overhead (the reproducibility margin). `extra` = upstream calls per request besides the answer (aux = routing/judge chat call, emb = embedding).

## Non-streaming — concurrency 1, small request

| Scenario | direct p50 | gw p50 | +p50 | +p95 | +p99 | +mean | spread | extra | err |
|---|---|---|---|---|---|---|---|---|---|
| router-minimal | 202.5 | 212.0 | +9.5 | +22.3 | +33.3 | +11.7 | 1.1 | — | 0 |
| router-simple | 202.4 | 224.7 | +22.3 | +47.7 | +51.4 | +25.0 | 5.6 | — | 0 |
| router-llm-policy | 202.4 | 366.0 | +163.6 | +171.2 | +176.8 | +164.5 | 0.7 | aux 1.0, emb 0.0 | 0 |
| router-semantic | 202.4 | 253.3 | +51.0 | +60.9 | +63.5 | +52.1 | 0.9 | aux 0.0, emb 1.0 | 0 |
| guardrails-regex | 202.7 | 211.9 | +9.2 | +10.6 | +27.9 | +9.9 | 8.7 | — | 0 |
| guardrails-judge | 202.4 | 367.6 | +165.2 | +187.2 | +215.8 | +169.6 | 7.0 | aux 1.0, emb 0.0 | 0 |
| pii | 202.3 | 212.4 | +10.1 | +23.9 | +33.8 | +11.6 | 1.8 | — | 0 |
| optimizers | 202.3 | 211.7 | +9.4 | +15.6 | +15.7 | +10.3 | 2.2 | — | 0 |

(total shown)

## Non-streaming — concurrency 1, large request

| Scenario | direct p50 | gw p50 | +p50 | +p95 | +p99 | +mean | spread | extra | err |
|---|---|---|---|---|---|---|---|---|---|
| router-minimal | 202.5 | 211.3 | +8.8 | +29.6 | +36.3 | +11.1 | 4.7 | — | 0 |
| router-simple | 202.6 | 269.0 | +66.3 | +115.1 | +206.9 | +75.7 | 9.3 | — | 0 |
| router-llm-policy | 202.5 | 367.1 | +164.6 | +175.9 | +183.8 | +165.7 | 2.3 | aux 1.0, emb 0.0 | 0 |
| router-semantic | 202.5 | 256.2 | +53.7 | +69.4 | +75.6 | +56.3 | 0.6 | aux 0.0, emb 1.0 | 0 |
| guardrails-regex | 202.8 | 211.8 | +9.0 | +19.0 | +21.3 | +11.0 | 5.6 | — | 0 |
| guardrails-judge | 202.6 | 370.4 | +167.9 | +196.7 | +216.7 | +172.7 | 2.2 | aux 1.0, emb 0.0 | 0 |
| pii | 202.5 | 213.4 | +10.9 | +22.6 | +30.3 | +12.1 | 2.5 | — | 0 |
| optimizers | 202.6 | 219.5 | +16.9 | +35.5 | +44.1 | +19.5 | 7.2 | — | 0 |

(total shown)

## Non-streaming — concurrency 4, small request

| Scenario | direct p50 | gw p50 | +p50 | +p95 | +p99 | +mean | spread | extra | err |
|---|---|---|---|---|---|---|---|---|---|
| router-minimal | 204.2 | 222.9 | +18.7 | +41.8 | +59.3 | +20.5 | 16.7 | — | 0 |
| router-simple | 203.3 | 569.6 | +366.3 | +615.9 | +842.6 | +390.1 | 75.2 | — | 0 |
| router-llm-policy | 203.5 | 384.6 | +181.1 | +266.7 | +266.6 | +190.3 | 18.5 | aux 1.0, emb 0.0 | 0 |
| router-semantic | 203.9 | 264.6 | +60.7 | +88.8 | +109.1 | +64.3 | 9.6 | aux 0.0, emb 1.0 | 0 |
| guardrails-regex | 204.0 | 224.3 | +20.2 | +47.0 | +91.5 | +25.4 | 6.6 | — | 0 |
| guardrails-judge | 204.0 | 378.5 | +174.6 | +284.1 | +352.2 | +192.0 | 15.5 | aux 1.0, emb 0.0 | 0 |
| pii | 203.8 | 219.4 | +15.5 | +42.7 | +56.2 | +18.4 | 10.5 | — | 0 |
| optimizers | 204.0 | 223.4 | +19.4 | +60.8 | +104.3 | +24.4 | 16.0 | — | 0 |

(total shown)

## Non-streaming — concurrency 4, large request

| Scenario | direct p50 | gw p50 | +p50 | +p95 | +p99 | +mean | spread | extra | err |
|---|---|---|---|---|---|---|---|---|---|
| router-minimal | 204.0 | 215.5 | +11.4 | +21.0 | +42.8 | +12.5 | 6.4 | — | 0 |
| router-simple | 206.9 | 1489.6 | +1282.7 | +2170.2 | +3265.9 | +1393.0 | 326.4 | — | 0 |
| router-llm-policy | 203.6 | 378.3 | +174.7 | +196.7 | +246.7 | +178.3 | 4.3 | aux 1.0, emb 0.0 | 0 |
| router-semantic | 203.4 | 259.0 | +55.6 | +76.8 | +74.4 | +58.6 | 5.6 | aux 0.0, emb 1.0 | 0 |
| guardrails-regex | 203.4 | 217.8 | +14.4 | +36.0 | +48.9 | +16.7 | 4.8 | — | 0 |
| guardrails-judge | 204.0 | 367.7 | +163.7 | +214.2 | +312.9 | +171.9 | 2.8 | aux 1.0, emb 0.0 | 0 |
| pii | 204.4 | 221.6 | +17.1 | +74.6 | +106.6 | +25.8 | 23.5 | — | 0 |
| optimizers | 203.6 | 235.1 | +31.6 | +72.5 | +282.2 | +37.3 | 15.6 | — | 0 |

(total shown)

## Streaming — concurrency 1, small request

| Scenario | direct p50 | gw p50 | +p50 | +p95 | +p99 | +mean | spread | extra | err |
|---|---|---|---|---|---|---|---|---|---|
| router-minimal | 101.9 | 109.8 | +7.9 | +13.6 | +17.4 | +8.7 | 2.6 | — | 0 |
| router-simple | 101.8 | 151.1 | +49.3 | +88.9 | +125.3 | +55.2 | 7.6 | — | 0 |
| router-llm-policy | 101.8 | 266.6 | +164.8 | +175.9 | +189.7 | +166.2 | 3.0 | aux 1.0, emb 0.0 | 0 |
| router-semantic | 101.9 | 152.8 | +51.0 | +64.3 | +63.4 | +52.2 | 3.9 | aux 0.0, emb 1.0 | 0 |
| guardrails-regex | 101.8 | 111.5 | +9.7 | +19.5 | +22.4 | +11.0 | 3.1 | — | 0 |
| guardrails-judge | 101.9 | 267.0 | +165.1 | +179.2 | +193.4 | +168.0 | 3.3 | aux 1.0, emb 0.0 | 0 |
| pii | 101.8 | 110.7 | +8.9 | +23.4 | +39.6 | +12.1 | 5.8 | — | 0 |
| optimizers | 101.8 | 110.2 | +8.3 | +18.5 | +24.8 | +9.6 | 2.5 | — | 0 |

(TTFB shown; total time is in results.json)

## Streaming — concurrency 1, large request

| Scenario | direct p50 | gw p50 | +p50 | +p95 | +p99 | +mean | spread | extra | err |
|---|---|---|---|---|---|---|---|---|---|
| router-minimal | 102.2 | 111.0 | +8.8 | +33.3 | +36.5 | +11.7 | 1.2 | — | 0 |
| router-simple | 102.2 | 189.1 | +87.0 | +158.8 | +297.6 | +101.4 | 14.1 | — | 0 |
| router-llm-policy | 102.1 | 266.3 | +164.1 | +174.2 | +200.3 | +166.6 | 4.3 | aux 1.0, emb 0.0 | 0 |
| router-semantic | 102.1 | 153.6 | +51.4 | +72.7 | +73.7 | +54.9 | 3.2 | aux 0.0, emb 1.0 | 0 |
| guardrails-regex | 102.1 | 111.3 | +9.2 | +23.1 | +31.2 | +11.7 | 6.6 | — | 0 |
| guardrails-judge | 102.1 | 266.0 | +163.9 | +186.8 | +190.8 | +167.4 | 5.4 | aux 1.0, emb 0.0 | 0 |
| pii | 102.1 | 111.2 | +9.1 | +17.9 | -263.4 | -1.6 | 4.3 | — | 0 |
| optimizers | 102.1 | 116.5 | +14.4 | +32.2 | +60.8 | +17.4 | 4.4 | — | 0 |

(TTFB shown; total time is in results.json)

## Streaming — concurrency 4, small request

| Scenario | direct p50 | gw p50 | +p50 | +p95 | +p99 | +mean | spread | extra | err |
|---|---|---|---|---|---|---|---|---|---|
| router-minimal | 103.1 | 118.5 | +15.4 | +40.7 | +125.3 | +20.0 | 10.0 | — | 0 |
| router-simple | 103.1 | 1094.2 | +991.1 | +1560.0 | +1800.8 | +993.3 | 127.0 | — | 0 |
| router-llm-policy | 103.8 | 287.6 | +183.9 | +227.1 | +268.9 | +189.4 | 9.6 | aux 1.0, emb 0.0 | 0 |
| router-semantic | 103.2 | 160.4 | +57.2 | +92.5 | +128.1 | +62.3 | 7.2 | aux 0.0, emb 1.0 | 0 |
| guardrails-regex | 103.4 | 123.0 | +19.7 | +40.4 | +122.4 | +24.2 | 24.3 | — | 0 |
| guardrails-judge | 103.1 | 279.5 | +176.5 | +254.2 | +331.6 | +188.1 | 19.6 | aux 1.0, emb 0.0 | 0 |
| pii | 104.5 | 138.6 | +34.1 | +84.5 | +196.6 | +39.6 | 60.2 | — | 0 |
| optimizers | 103.2 | 117.3 | +14.1 | +47.9 | +68.7 | +18.1 | 17.7 | — | 0 |

(TTFB shown; total time is in results.json)

## Streaming — concurrency 4, large request

| Scenario | direct p50 | gw p50 | +p50 | +p95 | +p99 | +mean | spread | extra | err |
|---|---|---|---|---|---|---|---|---|---|
| router-minimal | 103.6 | 114.1 | +10.5 | +60.1 | +467.6 | +23.9 | 6.5 | — | 0 |
| router-simple | 103.4 | 1761.4 | +1658.0 | +2341.1 | +3209.9 | +1616.4 | 212.1 | — | 0 |
| router-llm-policy | 103.3 | 275.6 | +172.3 | +223.3 | +287.0 | +179.4 | 23.6 | aux 1.0, emb 0.0 | 0 |
| router-semantic | 103.2 | 161.5 | +58.3 | +86.2 | +120.4 | +63.2 | 8.2 | aux 0.0, emb 1.0 | 0 |
| guardrails-regex | 103.0 | 119.1 | +16.1 | +34.6 | +101.1 | +19.1 | 7.5 | — | 0 |
| guardrails-judge | 103.1 | 272.1 | +169.0 | +230.8 | +274.1 | +178.1 | 10.3 | aux 1.0, emb 0.0 | 0 |
| pii | 103.2 | 120.1 | +17.0 | +37.9 | +50.9 | +19.8 | 13.9 | — | 0 |
| optimizers | 103.2 | 130.7 | +27.5 | +43.6 | +62.2 | +27.5 | 8.0 | — | 0 |

(TTFB shown; total time is in results.json)

