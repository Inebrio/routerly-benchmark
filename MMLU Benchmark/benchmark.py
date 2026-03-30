#!/usr/bin/env python3
"""
Routerly Benchmark — MMLU (Massive Multitask Language Understanding)

Uno script completamente agnostico: riceve solo BASE_URL, API_KEY e MODEL
dal file .env specificato. Non sa se sta parlando con Anthropic o Routerly.
"""

import argparse
import json
import logging
import os
import random
import re
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

# Silenzia tutti i warning di HuggingFace (prima degli import HF)
os.environ.setdefault("HF_HUB_VERBOSITY", "error")
os.environ.setdefault("DATASETS_VERBOSITY", "error")
logging.getLogger("datasets").setLevel(logging.ERROR)
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
warnings.filterwarnings("ignore")

import openai
from datasets import load_dataset
from dotenv import dotenv_values
from rich.console import Console, Group
from rich.live import Live
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

console = Console()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(env_file: str) -> dict:
    cfg = dotenv_values(env_file)
    for key in ("BASE_URL", "API_KEY", "MODEL"):
        if not cfg.get(key):
            console.print(f"[red]Errore: variabile '{key}' mancante in {env_file}[/red]")
            sys.exit(1)
    return cfg


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

def load_mmlu_questions(n: int, rng: random.Random) -> list[dict]:
    """Carica N domande random dal dataset MMLU."""
    dataset = load_dataset("cais/mmlu", "all", split="test")
    indices = list(range(len(dataset)))
    rng.shuffle(indices)
    selected = indices[:n]

    labels = ["A", "B", "C", "D"]
    questions = []
    for i in selected:
        row = dataset[i]
        answer_key = labels[row["answer"]]
        questions.append({
            "id": f"{row['subject']}_{i}",
            "question": row["question"],
            "choices": list(zip(labels, row["choices"])),
            "answer": answer_key,
            "subject": row["subject"],
        })
    return questions


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def build_prompt(q: dict) -> str:
    options = "\n".join(f"{label}) {text}" for label, text in q["choices"])
    return (
        f"{q['question']}\n\n"
        f"{options}\n\n"
        "Reply with only the single letter of the correct option (A, B, C, or D). No explanation."
    )


# ---------------------------------------------------------------------------
# API call
# ---------------------------------------------------------------------------

def call_api(client: openai.OpenAI, model: str, prompt: str, retries: int = 3, reasoning_effort: str | None = None) -> tuple[str, int, int, int, float, str | None]:
    """
    Chiama l'API in streaming e restituisce
    (raw_text, input_tokens, output_tokens, reasoning_tokens, ttft_s, trace_id).
    trace_id è l'header x-routerly-trace-id, presente solo con backend Routerly.
    Riprova fino a `retries` volte in caso di errore.
    """
    extra: dict = {}
    if reasoning_effort is not None:
        extra["reasoning_effort"] = reasoning_effort

    for attempt in range(retries):
        try:
            t0 = time.perf_counter()
            ttft: float | None = None
            chunks: list[str] = []
            in_tok = out_tok = reasoning_tok = 0
            trace_id: str | None = None

            with client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                stream=True,
                stream_options={"include_usage": True},
                **extra,
            ) as stream:
                trace_id = stream.response.headers.get("x-routerly-trace-id")
                for chunk in stream:
                    if ttft is None and chunk.choices and chunk.choices[0].delta.content:
                        ttft = time.perf_counter() - t0
                    if chunk.choices:
                        delta = chunk.choices[0].delta
                        content = delta.content or getattr(delta, 'reasoning', None) or ''
                        chunks.append(content)
                    if chunk.usage:
                        in_tok = chunk.usage.prompt_tokens
                        out_tok = chunk.usage.completion_tokens
                        details = getattr(chunk.usage, 'completion_tokens_details', None)
                        if details is not None:
                            reasoning_tok = getattr(details, 'reasoning_tokens', 0) or 0

            text = ''.join(chunks).strip()
            if ttft is None:
                ttft = time.perf_counter() - t0
            if text.lower().startswith("routing failed") or "no_models_available" in text.lower():
                raise openai.APIError(message=text, request=None, body=None)
            return text, in_tok, out_tok, reasoning_tok, ttft, trace_id
        except openai.RateLimitError:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise
        except openai.APIError as e:
            if "all_models_limits_exceeded" in str(e).lower() or "routing failed" in str(e).lower() or "no_models_available" in str(e).lower():
                wait = 10 * (attempt + 1)
                if attempt < retries - 1:
                    time.sleep(wait)
                else:
                    raise
            elif attempt < retries - 1:
                time.sleep(1)
            else:
                raise e


# ---------------------------------------------------------------------------
# Routerly tracelog
# ---------------------------------------------------------------------------

def fetch_routerly_trace(trace_id: str, data_file: Path, max_retries: int = 3) -> dict | None:
    """
    Aggrega tutti i record da usage.json con lo stesso traceId:
    completion + routing (LLM policy) + eventuali cascade falliti.
    Il totale corrisponde al costo riportato dal backend.
    """
    for attempt in range(max_retries):
        try:
            records = json.loads(data_file.read_text(encoding="utf-8"))
            matches = [r for r in records if r.get("traceId") == trace_id]
            if not matches:
                if attempt < max_retries - 1:
                    time.sleep(0.5)
                continue
            # Il record completion (callType != routing) porta modelId, outcome e trace
            completion = next(
                (r for r in reversed(matches) if r.get("callType") != "routing" and r.get("outcome") == "success"),
                matches[-1],
            )
            return {
                "cost": sum(r.get("cost") or 0.0 for r in matches),
                "costInput": sum(r.get("costInput") or 0.0 for r in matches),
                "costOutput": sum(r.get("costOutput") or 0.0 for r in matches),
                "modelId": completion.get("modelId"),
                "outcome": completion.get("outcome"),
                "inputTokens": sum(r.get("inputTokens") or 0 for r in matches),
                "outputTokens": sum(r.get("outputTokens") or 0 for r in matches),
                "callCount": len(matches),
                "trace": completion.get("trace"),
            }
        except (json.JSONDecodeError, OSError, TypeError):
            pass
        if attempt < max_retries - 1:
            time.sleep(0.5)
    return None


def extract_letter(text: str) -> str | None:
    """Estrae la prima lettera A-E dalla risposta del modello."""
    m = re.search(r"\b([A-E])\b", text.upper())
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Progress bar
# ---------------------------------------------------------------------------

def make_progress(env_label: str):
    """Restituisce (bar_progress, stats_progress): barra su riga 1, stat su riga 2."""
    bar = Progress(
        SpinnerColumn(),
        TextColumn(f"[bold cyan]{env_label}[/bold cyan]"),
        BarColumn(bar_width=28),
        TaskProgressColumn(),
        TextColumn("│"),
        TimeElapsedColumn(),
        TextColumn("│ ETA"),
        TimeRemainingColumn(),
    )
    stats = Progress(
        TextColumn("  "),
        TextColumn("[green]{task.fields[correct]}✓[/green] [red]{task.fields[wrong]}✗[/red]"),
        TextColumn("│ acc [bold]{task.fields[acc]}[/bold]"),
        TextColumn("│ [yellow]{task.fields[tokens]}[/yellow] tok"),
        TextColumn("│ [magenta]{task.fields[tps]}[/magenta] tok/s"),
        TextColumn("│ ttft [cyan]{task.fields[ttft]}[/cyan]"),
        TextColumn("│ lat [cyan]{task.fields[lat]}[/cyan]"),
        TextColumn("│ [green]{task.fields[cost]}[/green]"),
    )
    return bar, stats


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

def print_summary(env_label: str, model: str, results: list[dict], elapsed: float,
                  start_dt: datetime, end_dt: datetime, cost_info: dict | None = None,
                  interrupted: bool = False, routerly_cost_info: dict | None = None) -> None:
    total = len(results)
    correct_total = sum(1 for r in results if r["correct"])
    acc_total = correct_total / total * 100 if total else 0
    subjects = sorted(set(r["subject"] for r in results))
    tokens_total = sum(r["input_tokens"] + r["output_tokens"] for r in results)
    tps = tokens_total / elapsed if elapsed > 0 else 0

    title = f"[bold]{env_label}[/bold]  [dim]({model})[/dim]"
    if interrupted:
        title += "  [bold red][INTERROTTO][/bold red]"
    table = Table(title=title, show_header=False, min_width=46)
    table.add_column(style="dim", width=28)
    table.add_column(justify="right", style="bold")

    table.add_row("Inizio", start_dt.strftime("%H:%M:%S"))
    table.add_row("Fine", end_dt.strftime("%H:%M:%S"))
    table.add_row("Durata", f"{elapsed:.1f}s")
    table.add_row("", "")
    table.add_row("Accuracy totale", f"{acc_total:.1f}%")
    table.add_row("  soggetti coperti", str(len(subjects)))
    table.add_row("", "")
    total_reasoning = sum(r.get('reasoning_tokens', 0) for r in results)
    table.add_row("Token totali", f"{tokens_total:,}")
    table.add_row("  input", f"{sum(r['input_tokens'] for r in results):,}")
    table.add_row("  output", f"{sum(r['output_tokens'] for r in results):,}")
    if total_reasoning > 0:
        table.add_row("    di cui reasoning", f"{total_reasoning:,}")
        table.add_row("    output visibile", f"{sum(r['output_tokens'] - r.get('reasoning_tokens', 0) for r in results):,}")
    table.add_row("Tok/s medi", f"{tps:.0f}")
    table.add_row("", "")
    ttfts = [r["ttft_s"] for r in results if r.get("ttft_s") is not None]
    if ttfts:
        table.add_row("TTFT min", f"{min(ttfts)*1000:.0f} ms")
        table.add_row("TTFT max", f"{max(ttfts)*1000:.0f} ms")
        table.add_row("TTFT avg", f"{sum(ttfts)/len(ttfts)*1000:.0f} ms")
    if cost_info:
        table.add_row("", "")
        table.add_row("Costo totale", f"${cost_info['total_cost']:.6f}")
        table.add_row(f"  input  (${cost_info['price_input']}/M tok)", f"${cost_info['cost_input']:.6f}")
        table.add_row(f"  output (${cost_info['price_output']}/M tok)", f"${cost_info['cost_output']:.6f}")
    if routerly_cost_info:
        table.add_row("", "")
        table.add_row("Costo Routerly (tracelog)", f"${routerly_cost_info['total_cost']:.6f}")
        if routerly_cost_info.get('price_input') is not None:
            table.add_row(f"  input  (${routerly_cost_info['price_input']}/M tok)", f"${routerly_cost_info['cost_input']:.6f}")
            table.add_row(f"  output (${routerly_cost_info['price_output']}/M tok)", f"${routerly_cost_info['cost_output']:.6f}")
        else:
            table.add_row("  input", f"${routerly_cost_info['cost_input']:.6f}")
            table.add_row("  output", f"${routerly_cost_info['cost_output']:.6f}")

    console.print()
    console.print(table)


# ---------------------------------------------------------------------------
# Save results
# ---------------------------------------------------------------------------

def save_results(env_label: str, model: str, results: list[dict], elapsed: float,
                 start_dt: datetime, end_dt: datetime, cost_info: dict | None = None,
                 routerly_cost_info: dict | None = None) -> None:
    results_dir = Path(__file__).parent / "results"
    results_dir.mkdir(exist_ok=True)
    ts = start_dt.strftime("%Y%m%d_%H%M%S")
    safe_label = re.sub(r"[^\w\-]", "_", env_label)
    path = results_dir / f"{ts}_{safe_label}.json"

    total = len(results)
    correct_total = sum(1 for r in results if r["correct"])
    tokens_total = sum(r["input_tokens"] + r["output_tokens"] for r in results)

    payload = {
        "env": env_label,
        "model": model,
        "started_at": start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "ended_at": end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_s": round(elapsed, 2),
        "summary": {
            "accuracy_total": round(correct_total / total * 100, 2) if total else 0,
            "subjects": sorted(set(r["subject"] for r in results)),
            "tokens_total": tokens_total,
            "tokens_input": sum(r["input_tokens"] for r in results),
            "tokens_output": sum(r["output_tokens"] for r in results),
            "tokens_reasoning": sum(r.get("reasoning_tokens", 0) for r in results),
            **({} if routerly_cost_info is None else {"routerly_cost_usd": routerly_cost_info.get("total_cost"), "routerly_cost_breakdown": routerly_cost_info}),
            "tokens_output_visible": sum(r["output_tokens"] - r.get("reasoning_tokens", 0) for r in results),
            "tps": round(tokens_total / elapsed, 1) if elapsed > 0 else 0,            "ttft_min_ms": round(min(r["ttft_s"] for r in results) * 1000, 1),
            "ttft_max_ms": round(max(r["ttft_s"] for r in results) * 1000, 1),
            "ttft_avg_ms": round(sum(r["ttft_s"] for r in results) / len(results) * 1000, 1),            **(({"cost_usd": cost_info}) if cost_info else {}),
        },
        "results": results,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    console.print(f"[dim]Risultati salvati in {path}[/dim]")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Routerly Benchmark — MMLU")
    parser.add_argument("--env", required=True, help="Percorso del file .env da usare")
    parser.add_argument("--n", type=int, default=30, help="Numero domande MMLU (default: 30)")
    parser.add_argument("--seed", type=int, default=42, help="Seed per riproducibilità (default: 42)")
    args = parser.parse_args()

    # Carica configurazione
    cfg = load_config(args.env)
    env_label = Path(args.env).name
    model = cfg["MODEL"]
    reasoning_effort: str | None = cfg.get("REASONING_EFFORT") or None
    _data_file_str = cfg.get("ROUTERLY_DATA_FILE", "").strip()
    data_file = Path(_data_file_str) if _data_file_str else Path.home() / ".routerly" / "data" / "usage.json"
    client = openai.OpenAI(
        base_url=cfg["BASE_URL"],
        api_key=cfg["API_KEY"],
    )

    # Dataset
    console.print(f"[dim]Caricamento dataset MMLU...[/dim]")
    rng = random.Random(args.seed)
    questions = load_mmlu_questions(args.n, rng)
    total = len(questions)
    console.print(f"[dim]{total} domande caricate[/dim]")

    # Pre-calcola prezzi per il costo live nella progress bar
    show_cost = cfg.get("SHOW_COST", "false").lower() == "true"
    price_in = float(cfg.get("PRICE_INPUT", "0")) if show_cost else 0.0
    price_out = float(cfg.get("PRICE_OUTPUT", "0")) if show_cost else 0.0

    # Valutazione
    results = []
    correct = 0
    wrong = 0
    tokens_total = 0
    total_live_cost: float = 0.0
    total_routerly_cost: float = 0.0
    total_routerly_cost_input: float = 0.0
    total_routerly_cost_output: float = 0.0
    routerly_cost_count: int = 0
    start_dt = datetime.now()
    start = time.perf_counter()

    interrupted = False
    bar_progress, stats_progress = make_progress(env_label)
    try:
        with Live(Group(bar_progress, stats_progress), console=console, refresh_per_second=10):
            bar_task = bar_progress.add_task("benchmark", total=total)
            stats_task = stats_progress.add_task(
                "stats", total=None, correct=0, wrong=0, acc="—", tokens=0, tps="—", ttft="—", lat="—", cost="—"
            )

            for q in questions:
                prompt = build_prompt(q)
                q_start = time.perf_counter()

                raw, in_tok, out_tok, reasoning_tok, ttft, trace_id = call_api(client, model, prompt, reasoning_effort=reasoning_effort)
                predicted = extract_letter(raw)
                is_correct = predicted == q["answer"]

                routerly_trace = None
                if trace_id is not None:
                    routerly_trace = fetch_routerly_trace(trace_id, data_file)
                    if routerly_trace and routerly_trace.get("cost") is not None:
                        total_routerly_cost += routerly_trace["cost"]
                        total_routerly_cost_input += routerly_trace.get("costInput") or 0.0
                        total_routerly_cost_output += routerly_trace.get("costOutput") or 0.0
                        routerly_cost_count += 1

                if is_correct:
                    correct += 1
                else:
                    wrong += 1

                tokens_this = in_tok + out_tok
                tokens_total += tokens_this
                if show_cost:
                    total_live_cost += (in_tok * price_in + out_tok * price_out) / 1_000_000
                elapsed_so_far = time.perf_counter() - start
                tps = tokens_total / elapsed_so_far if elapsed_so_far > 0 else 0
                acc_pct = correct / (correct + wrong) * 100
                avg_ttft_ms = sum(r["ttft_s"] for r in results) / len(results) * 1000 if results else 0
                avg_lat_ms = sum(r["latency_s"] for r in results) / len(results) * 1000 if results else 0

                results.append({
                    "id": q["id"],
                    "subject": q["subject"],
                    "question": q["question"],
                    "answer": q["answer"],
                    "predicted": predicted,
                    "raw_response": raw,
                    "debug_prompt": prompt,
                    "correct": is_correct,
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "reasoning_tokens": reasoning_tok,
                    "latency_s": round(time.perf_counter() - q_start, 3),
                    "ttft_s": round(ttft, 3),
                    **({"routerly_trace": routerly_trace} if routerly_trace is not None else {}),
                })

                bar_progress.update(bar_task, advance=1)
                stats_progress.update(
                    stats_task,
                    correct=correct,
                    wrong=wrong,
                    acc=f"{acc_pct:.1f}%",
                    tokens=f"{tokens_total:,}",
                    tps=f"{tps:.0f}",
                    ttft=f"{avg_ttft_ms:.0f}ms",
                    lat=f"{avg_lat_ms:.0f}ms",
                    cost=f"${total_routerly_cost:.4f}" if routerly_cost_count > 0 else (f"${total_live_cost:.4f}" if show_cost else "—"),
                )
    except KeyboardInterrupt:
        interrupted = True
        console.print("\n[bold red]Test interrotto dall'utente.[/bold red]")

    elapsed = time.perf_counter() - start
    end_dt = datetime.now()

    if not results:
        console.print("[dim]Nessun risultato da mostrare.[/dim]")
        return

    # Calcola costo se abilitato nel .env (price_in/price_out già calcolati sopra)
    cost_info = None
    if show_cost:
        tok_in = sum(r["input_tokens"] for r in results)
        tok_out = sum(r["output_tokens"] for r in results)
        cost_info = {
            "total_cost": (tok_in * price_in + tok_out * price_out) / 1_000_000,
            "cost_input": tok_in * price_in / 1_000_000,
            "cost_output": tok_out * price_out / 1_000_000,
            "price_input": price_in,
            "price_output": price_out,
        }

    routerly_cost_info = None
    if routerly_cost_count > 0:
        routerly_cost_info = {
            "total_cost": total_routerly_cost,
            "cost_input": total_routerly_cost_input,
            "cost_output": total_routerly_cost_output,
        }
    print_summary(env_label, model, results, elapsed, start_dt, end_dt,
                  cost_info=cost_info, interrupted=interrupted, routerly_cost_info=routerly_cost_info)
    save_results(env_label, model, results, elapsed, start_dt, end_dt,
                 cost_info=cost_info, routerly_cost_info=routerly_cost_info)


if __name__ == "__main__":
    main()
