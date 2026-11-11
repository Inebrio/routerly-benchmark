#!/usr/bin/env python3
"""Validate the harness before trusting a run (exit code 1 on failure).

1. The direct baseline reproduces the mock's configured latency (+-3 ms at p50),
   so the instrument measures what it claims to.
2. Every scenario triggers exactly the extra upstream calls it is named for
   (llm / judge: 1 chat-type call, semantic: 1 embedding call, others: none).

    python selfcheck.py --scenarios scenarios.json
"""
import argparse, asyncio, json, sys
import httpx
from benchmark import batch, body, pct

EXPECT = {"router-llm-policy": {"aux": 1, "embed": 0}, "router-semantic": {"aux": 0, "embed": 1},
          "guardrails-judge": {"aux": 1, "embed": 0}}
TOL_MS = 3.0  # asyncio timer slop + loopback + JSON encode put the measured floor ~2 ms above the configured delay


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", default="scenarios.json")
    ap.add_argument("--routerly", default="http://127.0.0.1:3100")
    ap.add_argument("--mock", default="http://127.0.0.1:4100")
    a = ap.parse_args()
    sc, mock, ok = json.load(open(a.scenarios)), a.mock.rstrip("/"), True
    async with httpx.AsyncClient(timeout=60) as c:
        cfg = (await c.get(mock + "/config")).json()
        want_plain = cfg["ttfb_ms"] + cfg["reply_tokens"] * cfg["chunk_ms"]
        for stream, want in ((False, want_plain), (True, cfg["ttfb_ms"])):
            await batch(c, mock + "/v1/chat/completions", "k", body("mock-chat", "small", stream), stream, 5, 1)
            res, _ = await batch(c, mock + "/v1/chat/completions", "k", body("mock-chat", "small", stream), stream, 30, 1)
            got = pct([x[0 if stream else 1] for x in res], 50)
            good = abs(got - want) <= TOL_MS
            ok &= good
            print(f"{'PASS' if good else 'FAIL'}  direct {'stream TTFB' if stream else 'non-stream total'} p50 {got:.1f} ms, configured {want} ms (+-{TOL_MS})")
        for name, s in sc.items():
            await batch(c, a.routerly.rstrip("/") + "/v1/chat/completions", s["token"], body(s["model"], "small", False), False, 2, 1)  # warm-up: caches (e.g. intent embeddings) fill once
            before = (await c.get(mock + "/stats")).json()
            res, err = await batch(c, a.routerly.rstrip("/") + "/v1/chat/completions", s["token"], body(s["model"], "small", False), False, 4, 1)
            after = (await c.get(mock + "/stats")).json()
            got = {k: (after[k] - before[k]) / 4 for k in ("aux", "embed")}
            want = EXPECT.get(name, {"aux": 0, "embed": 0})
            good = not err and got == want
            ok &= good
            print(f"{'PASS' if good else 'FAIL'}  {name}: extra upstream calls per request {got}, expected {want}")
    sys.exit(0 if ok else 1)


asyncio.run(main())
