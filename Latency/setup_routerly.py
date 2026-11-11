#!/usr/bin/env python3
"""Configure a fresh Routerly instance for the latency benchmark.

Creates (through the documented management API) one connection to the mock
upstream, the mock models, and one router per scenario. Writes scenarios.json
(router tokens, gitignored) which benchmark.py reads.

    python setup_routerly.py --routerly http://127.0.0.1:3100 \
        --email admin@example.com --password '...' --mock http://127.0.0.1:4100/v1
"""
import argparse, json, sys
import httpx

PII_PROMPT_NOTE = "contact jane.doe@example.com or +1 202 555 0143"  # also used by benchmark.py

SCENARIOS = {
    # name: (description, router fields)
    "router-minimal": ("one model, no policies", {
        "models": ["mock-chat"], "policies": []}),
    "router-simple": ("two models, cheapest + health policies", {
        "models": ["mock-chat", "mock-chat-2"],
        "policies": [{"type": "cheapest", "enabled": True}, {"type": "health", "enabled": True}]}),
    "router-llm-policy": ("`llm` policy: one extra chat call to the routing model per request", {
        "models": ["mock-chat", "mock-chat-2"],
        "policies": [{"type": "llm", "enabled": True, "config": {"routingModelId": "mock-router"}}]}),
    "router-semantic": ("`semantic-intent` policy: one extra embedding call per request", {
        "models": ["mock-chat", "mock-chat-2"],
        "policies": [{"type": "semantic-intent", "enabled": True, "config": {
            "embedding_provider": "openai", "embedding_model": "mock-embed",
            "intents": {
                "greeting": {"examples": ["hello there", "good morning"], "candidate_models": ["mock-chat"]},
                "analysis": {"examples": ["analyse this dataset", "compare these options"], "candidate_models": ["mock-chat-2"]}}}}]}),
    "guardrails-regex": ("content guardrails: injection detection + regex rule (no extra call)", {
        "models": ["mock-chat"], "policies": [],
        "guardrails": {"detectInjection": True, "rules": [
            {"type": "regex", "enabled": True, "target": "request", "block": True, "log": False,
             "config": {"patterns": ["competitor", "rival\\s+product"]}}]}}),
    "guardrails-judge": ("content guardrails: topic rule judged by an LLM (one extra chat call)", {
        "models": ["mock-chat"], "policies": [],
        "guardrails": {"detectInjection": False, "rules": [
            {"type": "topic", "enabled": True, "target": "request",
             "config": {"modelId": "mock-judge", "allowedTopics": "General questions", "threshold": 0.5}}]}}),
    "pii": ("PII scrubbing on request and response (no extra call)", {
        "models": ["mock-chat"], "policies": [],
        "pii": {"policies": [{"enabled": True, "target": "both",
                              "entities": ["EMAIL", "PHONE", "CREDIT_CARD", "SSN", "IBAN"]}]}}),
    "optimizers": ("optimizer pipeline: session-dedup, ccr, rtk, caveman", {
        "models": ["mock-chat"], "policies": [],
        "optimizers": {"steps": [{"id": i, "enabled": True} for i in ("session-dedup", "ccr", "rtk", "caveman")]}}),
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--routerly", default="http://127.0.0.1:3100")
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--mock", default="http://127.0.0.1:4100/v1", help="mock upstream base URL as Routerly sees it")
    ap.add_argument("--out", default="scenarios.json")
    a = ap.parse_args()
    c = httpx.Client(base_url=a.routerly.rstrip("/") + "/api", timeout=30)

    def call(method, path, expect=(200, 201), **kw):
        r = c.request(method, path, **kw)
        if r.status_code not in expect:
            sys.exit(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    if c.get("/setup/status").json().get("needsSetup"):
        call("POST", "/setup/first-admin", json={"email": a.email, "password": a.password})
    tok = call("POST", "/auth/login", json={"email": a.email, "password": a.password})["token"]
    c.headers["Authorization"] = f"Bearer {tok}"

    have = {m["id"]: m for m in call("GET", "/models")}
    conn = None
    for mid, extra in (("mock-chat", {}), ("mock-chat-2", {}), ("mock-router", {}), ("mock-judge", {}),
                       ("mock-embed", {"capabilities": {"embedding": True}})):
        if mid not in have:
            body = {"id": mid, "provider": "custom", "endpoint": a.mock, "inputPerMillion": 1,
                    "outputPerMillion": 2, "contextWindow": 128000, **extra}
            body.update({"connectionId": conn} if conn else {"apiKey": "mock-key"})
            call("POST", "/models", json=body)
            have[mid] = next(m for m in call("GET", "/models") if m["id"] == mid)
        conn = have[mid].get("connectionId")

    out = {}
    for name, (desc, f) in SCENARIOS.items():
        body = {"name": f"latency-{name}", "timeoutMs": 30000, "models": [{"modelId": m} for m in f["models"]],
                "policies": f["policies"], **{k: f[k] for k in ("guardrails", "pii", "optimizers") if k in f}}
        r = call("POST", "/routers", json=body)
        out[name] = {"description": desc, "token": r["token"], "model": f["models"][0]}
        print(f"  {name:20s} {desc}")
    json.dump(out, open(a.out, "w"), indent=2)
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
