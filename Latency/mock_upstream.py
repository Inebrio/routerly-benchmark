#!/usr/bin/env python3
"""Simulated OpenAI-compatible upstream with fixed, known latency.

Used as the "provider" behind Routerly in the latency benchmark so that the only
variable between the direct path and the gateway path is the gateway itself.
Pure standard library (asyncio, HTTP/1.1 keep-alive, chunked SSE).

Endpoints (OpenAI wire format, https://platform.openai.com/docs/api-reference/chat):
  POST /v1/chat/completions   JSON, or SSE `chat.completion.chunk` stream + `data: [DONE]`
  POST /v1/embeddings         deterministic vectors
  GET  /health
  GET  /stats                 call counters {chat, aux, embed} (benchmark sanity check)

Latency model (all configurable):
  chat, stream      first byte after --ttfb-ms, then one content chunk every --chunk-ms
  chat, non-stream  --ttfb-ms + reply_tokens * --chunk-ms, then the whole JSON
  routing / judge   --aux-ms (replies to Routerly's `llm` routing policy and guardrail judges)
  embeddings        --embed-ms
"""
import argparse, asyncio, hashlib, json, math, re, time

OPTS = argparse.Namespace()
WORDS = ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron "
         "pi rho sigma tau upsilon phi chi psi omega").split()


def reply_text(n):
    return " ".join(WORDS[i % len(WORDS)] for i in range(n))


def text_of(content):
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def aux_reply(body):
    """Reply for Routerly's internal calls, or None for a normal chat call."""
    msgs = body.get("messages") or []
    system = next((text_of(m.get("content")) for m in msgs if m.get("role") == "system"), "")
    if system.startswith("You are a routing engine"):
        ids = re.findall(r'^- id: "([^"]+)"', system, re.M)
        entries = [{"model": m, "point": round(1.0 - i * 0.1, 2)} for i, m in enumerate(ids)]
        return json.dumps({"routing": entries})
    if "topic classifier" in system:
        return json.dumps({"reason": "On topic.", "score": 10.0})
    if "Respond ONLY with a JSON object" in system or "content safety" in system:
        return json.dumps({"reason": "Safe.", "score": 0.0})
    return None


def embed(text):
    h = hashlib.sha256(text.encode()).digest()
    v = [(h[i % 32] - 127.5) / 127.5 for i in range(64)]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def chunk_json(cid, model, delta, finish=None, usage=None):
    d = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage is not None:
        d["usage"] = usage
    return json.dumps(d, separators=(",", ":"))


async def send(w, status, headers, body):
    headers = {**headers, "Content-Length": str(len(body))}
    head = f"HTTP/1.1 {status}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
    w.write(head.encode() + body)
    await w.drain()


COUNTS = {"chat": 0, "aux": 0, "embed": 0}


async def chat(body, w):
    model = body.get("model", "mock-chat")
    cid = "chatcmpl-mock"
    aux = aux_reply(body)
    COUNTS["aux" if aux is not None else "chat"] += 1
    if aux is not None:
        await asyncio.sleep(OPTS.aux_ms / 1000)
        out = {"id": cid, "object": "chat.completion", "created": int(time.time()), "model": model,
               "choices": [{"index": 0, "message": {"role": "assistant", "content": aux}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 200, "completion_tokens": 20, "total_tokens": 220}}
        return await send(w, "200 OK", {"Content-Type": "application/json"}, json.dumps(out).encode())
    n = OPTS.reply_tokens
    usage = {"prompt_tokens": 100, "completion_tokens": n, "total_tokens": 100 + n}
    if not body.get("stream"):
        await asyncio.sleep((OPTS.ttfb_ms + n * OPTS.chunk_ms) / 1000)
        out = {"id": cid, "object": "chat.completion", "created": int(time.time()), "model": model,
               "choices": [{"index": 0, "message": {"role": "assistant", "content": reply_text(n)}, "finish_reason": "stop"}],
               "usage": usage}
        return await send(w, "200 OK", {"Content-Type": "application/json"}, json.dumps(out).encode())
    await asyncio.sleep(OPTS.ttfb_ms / 1000)
    head = "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nTransfer-Encoding: chunked\r\n\r\n"
    w.write(head.encode())

    async def event(data):
        payload = f"data: {data}\n\n".encode()
        w.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
        await w.drain()

    await event(chunk_json(cid, model, {"role": "assistant", "content": ""}))
    for i in range(n):
        await event(chunk_json(cid, model, {"content": WORDS[i % len(WORDS)] + " "}))
        await asyncio.sleep(OPTS.chunk_ms / 1000)
    await event(chunk_json(cid, model, {}, finish="stop"))
    if (body.get("stream_options") or {}).get("include_usage"):
        d = json.loads(chunk_json(cid, model, {}, usage=usage)); d["choices"] = []
        await event(json.dumps(d, separators=(",", ":")))
    await event("[DONE]")
    w.write(b"0\r\n\r\n")
    await w.drain()


async def embeddings(body, w):
    inp = body.get("input", "")
    items = inp if isinstance(inp, list) else [inp]
    COUNTS["embed"] += 1
    await asyncio.sleep(OPTS.embed_ms / 1000)
    out = {"object": "list", "model": body.get("model", "mock-embed"),
           "data": [{"object": "embedding", "index": i, "embedding": embed(str(t))} for i, t in enumerate(items)],
           "usage": {"prompt_tokens": 10, "total_tokens": 10}}
    await send(w, "200 OK", {"Content-Type": "application/json"}, json.dumps(out).encode())


async def handle(r, w):
    try:
        while True:
            line = await r.readline()
            if not line:
                break
            method, path, _ = line.decode().split(" ", 2)
            headers = {}
            while (h := await r.readline()) not in (b"\r\n", b""):
                k, _, v = h.decode().partition(":")
                headers[k.strip().lower()] = v.strip()
            raw = await r.readexactly(int(headers.get("content-length", 0)))
            path = path.split("?")[0]
            if method == "GET" and path == "/health":
                await send(w, "200 OK", {"Content-Type": "text/plain"}, b"ok")
                continue
            if method == "GET" and path == "/config":
                await send(w, "200 OK", {"Content-Type": "application/json"}, json.dumps(vars(OPTS)).encode())
                continue
            if method == "GET" and path == "/stats":
                await send(w, "200 OK", {"Content-Type": "application/json"}, json.dumps(COUNTS).encode())
                continue
            body = json.loads(raw or b"{}")
            if path.endswith("/chat/completions"):
                await chat(body, w)
                if body.get("stream"):
                    continue
            elif path.endswith("/embeddings"):
                await embeddings(body, w)
            else:
                msg = b'{"error":{"message":"not found"}}'
                await send(w, "404 Not Found", {"Content-Type": "application/json"}, msg)
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        w.close()


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4100)
    ap.add_argument("--ttfb-ms", type=float, default=100)
    ap.add_argument("--chunk-ms", type=float, default=5)
    ap.add_argument("--reply-tokens", type=int, default=20)
    ap.add_argument("--aux-ms", type=float, default=150, help="latency of routing/judge calls")
    ap.add_argument("--embed-ms", type=float, default=40)
    ap.parse_args(namespace=OPTS)
    srv = await asyncio.start_server(handle, OPTS.host, OPTS.port)
    print(f"mock upstream on http://{OPTS.host}:{OPTS.port}  "
          f"ttfb={OPTS.ttfb_ms}ms chunk={OPTS.chunk_ms}ms tokens={OPTS.reply_tokens} "
          f"aux={OPTS.aux_ms}ms embed={OPTS.embed_ms}ms", flush=True)
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
