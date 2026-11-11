#!/usr/bin/env python3
"""Measure the latency a Routerly router adds to a provider call.

For every scenario the same request is sent (a) straight to the simulated
upstream and (b) through a Routerly router that forwards to that upstream.
Direct and gateway batches alternate inside every round so slow drift of the
machine hits both sides equally. Overhead = gateway latency - direct latency,
reported at p50 / p95 / p99 and mean. The spread of the per-round p50 overhead
is reported as the reproducibility margin.

    python benchmark.py --scenarios scenarios.json --routerly http://127.0.0.1:3100 \
        --mock http://127.0.0.1:4100 --routerly-version 0.6.1
"""
import argparse, asyncio, json, os, platform, statistics, subprocess, sys, time
from datetime import datetime, timezone
import httpx

PII = "Please contact jane.doe@example.com or +1 202 555 0143 about this."
FILLER = "The quick brown fox jumps over the lazy dog while the gateway keeps measuring. "
SIZES = {"small": 0, "large": 24_000}  # extra characters of filler in the user message


def body(model, size, stream):
    text = PII + " " + (FILLER * (SIZES[size] // len(FILLER) + 1))[:SIZES[size]]
    b = {"model": model, "messages": [{"role": "user", "content": text.strip()}], "max_tokens": 64}
    if stream:
        b["stream"] = True
        b["stream_options"] = {"include_usage": True}
    return b


def pct(xs, p):
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def stats(xs):
    return {"n": len(xs), "mean": statistics.fmean(xs), "p50": pct(xs, 50), "p95": pct(xs, 95), "p99": pct(xs, 99)}


async def one(client, url, token, payload, stream):
    """Return (ttfb_ms, total_ms) or None on error."""
    h = {"Authorization": f"Bearer {token}"}
    t0 = time.perf_counter()
    try:
        if not stream:
            r = await client.post(url, json=payload, headers=h)
            if r.status_code != 200:
                return None
            t = (time.perf_counter() - t0) * 1000
            return t, t
        ttfb = None
        async with client.stream("POST", url, json=payload, headers=h) as r:
            if r.status_code != 200:
                return None
            async for chunk in r.aiter_bytes():
                if ttfb is None and chunk:
                    ttfb = (time.perf_counter() - t0) * 1000
        return ttfb, (time.perf_counter() - t0) * 1000
    except httpx.HTTPError:
        return None


async def batch(client, url, token, payload, stream, n, conc):
    sem, out = asyncio.Semaphore(conc), []

    async def run():
        async with sem:
            out.append(await one(client, url, token, payload, stream))

    await asyncio.gather(*(run() for _ in range(n)))
    ok = [x for x in out if x]
    return ok, len(out) - len(ok)


def overhead(d, g):
    return {k: g[k] - d[k] for k in ("mean", "p50", "p95", "p99")}


async def cell(client, a, name, sc, stream, size, conc, n):
    payload = body(sc["model"], size, stream)
    direct_url = a.mock.rstrip("/") + "/v1/chat/completions"
    gw_url = a.routerly.rstrip("/") + "/v1/chat/completions"
    D, G, derr, gerr, rounds, extra = [], [], 0, 0, [], {"aux": 0, "embed": 0, "chat": 0}
    for r in range(a.rounds):
        order = ("d", "g") if r % 2 == 0 else ("g", "d")
        rd = rg = None
        for side in order:
            if side == "d":
                rd, e = await batch(client, direct_url, "mock-key", {**payload, "model": "mock-chat"}, stream, n, conc)
                derr += e
            else:
                before = (await client.get(a.mock.rstrip("/") + "/stats")).json()
                rg, e = await batch(client, gw_url, sc["token"], payload, stream, n, conc)
                after = (await client.get(a.mock.rstrip("/") + "/stats")).json()
                for k in extra:
                    extra[k] += after[k] - before[k]
                gerr += e
        D += rd; G += rg
        if rd and rg:
            rounds.append(statistics.median(x[1] for x in rg) - statistics.median(x[1] for x in rd))
    res = {"scenario": name, "stream": stream, "size": size, "concurrency": conc, "requests_per_round": n,
           "rounds": a.rounds, "errors": {"direct": derr, "gateway": gerr},
           "upstream_calls_per_gateway_request": {k: round(v / max(len(G), 1), 2) for k, v in extra.items()},
           "round_p50_overhead_ms": rounds,
           "round_p50_spread_ms": (max(rounds) - min(rounds)) if rounds else None}
    if not G or not D:                          # gateway down / all requests failed: keep the row, say so
        res["failed"] = True
        return res
    for idx, label in ((1, "total"), (0, "ttfb")):
        if idx == 0 and not stream:
            continue
        d, g = stats([x[idx] for x in D]), stats([x[idx] for x in G])
        res[label] = {"direct": d, "gateway": g, "overhead": overhead(d, g)}
    return res


def environment(a):
    def sh(c):
        try:
            return subprocess.check_output(c, shell=True, text=True, stderr=subprocess.DEVNULL).strip()
        except Exception:
            return "unknown"
    cpu = sh("grep -m1 'model name' /proc/cpuinfo | cut -d: -f2").strip()
    return {"date_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "run_by": a.run_by, "routerly_version": a.routerly_version, "routerly_commit": a.routerly_commit,
            "machine": {"cpu": cpu, "logical_cores": os.cpu_count(), "memory": sh("free -h | awk '/Mem/{print $2}'"),
                        "os": platform.platform(), "python": platform.python_version(),
                        "node": sh("node --version"), "load_avg_start": os.getloadavg()},
            "topology": "client, Routerly and the simulated upstream run on the same host over loopback",
            "mock_upstream": httpx.get(a.mock.rstrip("/") + "/config").json(),
            "parameters": {"concurrency": a.concurrency, "sizes": {k: f"+{SIZES[k]} chars" for k in a.sizes},
                           "requests_per_round": {"1": a.n1, "other": a.n16}, "rounds": a.rounds, "warmup": a.warmup}}


def report(env, rows):
    L = ["# Routerly latency overhead", "",
         f"Run by {env['run_by']} on {env['date_utc']} — Routerly {env['routerly_version']} ({env['routerly_commit']}).", "",
         f"Machine: {env['machine']['cpu']}, {env['machine']['logical_cores']} logical cores, {env['machine']['memory']} RAM, {env['machine']['os']}.",
         f"Simulated upstream: {json.dumps(env['mock_upstream'])}.", f"Topology: {env['topology']}.", "",
         "Overhead = latency through Routerly − latency direct to the upstream, ms. "
         "`spread` = max−min of the per-round p50 overhead (the reproducibility margin). "
         "`extra` = upstream calls per request besides the answer (aux = routing/judge chat call, emb = embedding).", ""]
    for stream in (False, True):
        for conc in sorted({r["concurrency"] for r in rows}):
            for size in dict.fromkeys(r["size"] for r in rows):
                sub = [r for r in rows if r["stream"] == stream and r["concurrency"] == conc and r["size"] == size]
                if not sub:
                    continue
                metric = "TTFB" if stream else "total"
                L += [f"## {'Streaming' if stream else 'Non-streaming'} — concurrency {conc}, {size} request", "",
                      f"| Scenario | direct p50 | gw p50 | +p50 | +p95 | +p99 | +mean | spread | extra | err |",
                      "|---|---|---|---|---|---|---|---|---|---|"]
                for r in sub:
                    if r.get("failed"):
                        L.append(f"| {r['scenario']} | FAILED: {r['errors']['gateway']} gateway / {r['errors']['direct']} direct errors | | | | | | | | |")
                        continue
                    t = r["ttfb"] if stream else r["total"]
                    o, e = t["overhead"], r["upstream_calls_per_gateway_request"]
                    x = f"aux {e['aux']}, emb {e['embed']}" if (e["aux"] or e["embed"]) else "—"
                    L.append(f"| {r['scenario']} | {t['direct']['p50']:.1f} | {t['gateway']['p50']:.1f} | {o['p50']:+.1f} | {o['p95']:+.1f} | "
                             f"{o['p99']:+.1f} | {o['mean']:+.1f} | {r['round_p50_spread_ms']:.1f} | {x} | {sum(r['errors'].values())} |")
                L += ["", f"({metric} shown{'; total time is in results.json' if stream else ''})", ""]
    return "\n".join(L) + "\n"


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenarios", default="scenarios.json")
    ap.add_argument("--routerly", default="http://127.0.0.1:3100")
    ap.add_argument("--mock", default="http://127.0.0.1:4100")
    ap.add_argument("--only", nargs="*", help="scenario names (default: all)")
    ap.add_argument("--concurrency", type=int, nargs="+", default=[1, 4])
    ap.add_argument("--sizes", nargs="+", default=["small", "large"], choices=list(SIZES))
    ap.add_argument("--n1", type=int, default=20, help="requests per round at concurrency 1")
    ap.add_argument("--n16", type=int, default=64, help="requests per round at concurrency > 1")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--warmup", type=int, default=10)
    ap.add_argument("--routerly-version", default="unknown")
    ap.add_argument("--routerly-commit", default="unknown")
    ap.add_argument("--run-by", default="the Routerly team")
    ap.add_argument("--out", default="results")
    a = ap.parse_args()
    sc = json.load(open(a.scenarios))
    names = a.only or list(sc)
    out = os.path.join(a.out, "latency_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out)
    env = environment(a)
    json.dump(env, open(os.path.join(out, "environment.json"), "w"), indent=2)
    lim = httpx.Limits(max_connections=64, max_keepalive_connections=64)
    rows = []
    async with httpx.AsyncClient(timeout=60, limits=lim) as client:
        for name in names:                       # warm every path (connections, JIT, caches)
            for stream in (False, True):
                await batch(client, a.routerly.rstrip("/") + "/v1/chat/completions", sc[name]["token"],
                            body(sc[name]["model"], "small", stream), stream, a.warmup, 4)
        await batch(client, a.mock.rstrip("/") + "/v1/chat/completions", "mock-key",
                    body("mock-chat", "small", False), False, a.warmup, 4)
        for conc in a.concurrency:
            for size in a.sizes:
                for stream in (False, True):
                    for name in names:
                        n = a.n1 if conc == 1 else a.n16
                        r = await cell(client, a, name, sc[name], stream, size, conc, n)
                        rows.append(r)
                        if r.get("failed"):
                            print(f"c={conc:<3} {size:5s} {'stream' if stream else 'plain '} {name:18s} FAILED errors {r['errors']}", flush=True)
                            continue
                        t = r["ttfb"] if stream else r["total"]
                        print(f"c={conc:<3} {size:5s} {'stream' if stream else 'plain '} {name:18s} "
                              f"+p50 {t['overhead']['p50']:+7.1f}  +p95 {t['overhead']['p95']:+7.1f}  err {sum(r['errors'].values())}", flush=True)
                        json.dump({"environment": env, "rows": rows}, open(os.path.join(out, "results.json"), "w"), indent=2)
    open(os.path.join(out, "report.md"), "w").write(report(env, rows))
    print("results in", out)


if __name__ == "__main__":
    asyncio.run(main())
