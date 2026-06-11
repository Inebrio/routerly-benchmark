#!/usr/bin/env python3
"""
Routerly Benchmark — HumanEval

Agnostic benchmark to evaluate the code generation capability
of any OpenAI-API-compatible endpoint, using the HumanEval dataset
(164 Python programming problems).

The script does not know the target: it only receives BASE_URL, API_KEY and MODEL
from the specified .env file. It works identically when pointing to Anthropic,
OpenAI, local models via Ollama or Routerly.
"""

import argparse
import json
import logging
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import warnings
from datetime import datetime
from pathlib import Path

# Silence all HuggingFace warnings (before HF imports)
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
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

console = Console()

HUMANEVAL_TOTAL = 164  # numero totale di problemi nel dataset

# List prices for known models ($/M token: input, output).
# Used as default if PRICE_INPUT/PRICE_OUTPUT are not present in the .env.
# Can always be overridden via variables in the .env file.
KNOWN_PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5":    (10.0, 50.0),
    "claude-opus-4-8":   ( 5.0, 25.0),
    "claude-opus-4-6":   (15.0, 75.0),
    "claude-sonnet-4-6": ( 3.0, 15.0),
    "gpt-4.1-nano":      ( 0.1,  0.4),
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(env_file: str) -> dict:
    cfg = dotenv_values(env_file)
    for key in ("BASE_URL", "API_KEY", "MODEL"):
        if not cfg.get(key):
            console.print(f"[red]Error: missing variable '{key}' in {env_file}[/red]")
            sys.exit(1)
    return cfg


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

def load_humaneval_problems(n: int, rng: random.Random) -> list[dict]:
    """Load N random problems from the HumanEval dataset."""
    dataset = load_dataset("openai/openai_humaneval", split="test")
    indices = list(range(len(dataset)))
    rng.shuffle(indices)
    selected = indices[:n]

    problems = []
    for i in selected:
        row = dataset[i]
        problems.append({
            "task_id": row["task_id"],
            "prompt": row["prompt"],
            "entry_point": row["entry_point"],
            "canonical_solution": row["canonical_solution"],
            "test": row["test"],
        })
    return problems


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def build_prompt(problem: dict) -> str:
    return (
        "Complete the following Python function. "
        "Return ONLY the complete, runnable Python function (including the def line and body). "
        "No markdown code fences. No explanations.\n\n"
        + problem["prompt"]
    )


# ---------------------------------------------------------------------------
# Code extraction & sandboxed execution
# ---------------------------------------------------------------------------

def extract_completion(raw: str) -> str:
    """Remove markdown fences and extra whitespace from the model response."""
    code = re.sub(r"```(?:python)?\n?", "", raw)
    code = re.sub(r"```", "", code)
    return code.strip()


def build_test_code(problem: dict, completion: str) -> str:
    """
    Build the complete code to execute.

    If the model returned the complete function (contains 'def entry_point'),
    use it directly. Otherwise, concatenate with the original prompt
    (treating the response as the function body).
    """
    if re.search(r"def\s+" + re.escape(problem["entry_point"]) + r"\s*\(", completion):
        function_code = completion
    else:
        # Il modello ha restituito solo il corpo: aggiungiamo il prompt (firma + docstring)
        function_code = problem["prompt"] + "\n" + completion

    return (
        function_code
        + "\n\n"
        + problem["test"]
        + "\n"
        + f"check({problem['entry_point']})"
    )


def run_tests(code: str, timeout: int) -> tuple[bool, str]:
    """
    Execute the generated code in an isolated subprocess with timeout.
    Returns (passed, error_message).
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(code)
        tmp_path = f.name

    try:
        result = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return True, ""
        err = (result.stderr or result.stdout or "unknown error").strip()
        return False, err[:500]
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout}s"
    except Exception as e:
        return False, str(e)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def evaluate_solution(problem: dict, completion: str, timeout: int) -> tuple[bool, str]:
    code = build_test_code(problem, completion)
    return run_tests(code, timeout)


# ---------------------------------------------------------------------------
# API call
# ---------------------------------------------------------------------------

def call_api(
    client: openai.OpenAI,
    model: str,
    prompt: str,
    retries: int = 3,
    reasoning_effort: str | None = None,
) -> tuple[str, int, int, int, float, str | None]:
    """
    Call the API in streaming mode and return
    (raw_text, input_tokens, output_tokens, reasoning_tokens, ttft_s, trace_id).
    trace_id is the x-routerly-trace-id header, present only with the Routerly backend.
    Retries up to `retries` times on error.
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
                        content = delta.content or getattr(delta, "reasoning", None) or ""
                        chunks.append(content)
                    if chunk.usage:
                        in_tok = chunk.usage.prompt_tokens
                        out_tok = chunk.usage.completion_tokens
                        details = getattr(chunk.usage, "completion_tokens_details", None)
                        if details is not None:
                            reasoning_tok = getattr(details, "reasoning_tokens", 0) or 0

            text = "".join(chunks).strip()
            if ttft is None:
                ttft = time.perf_counter() - t0
            if text.lower().startswith("routing failed") or "no_models_available" in text.lower():
                raise openai.APIError(message=text, request=None, body=None)
            return text, in_tok, out_tok, reasoning_tok, ttft, trace_id

        except openai.RateLimitError:
            if attempt < retries - 1:
                time.sleep(2**attempt)
            else:
                raise
        except openai.APIError as e:
            # Routerly: all models have reached their limits → long backoff
            if "all_models_limits_exceeded" in str(e).lower() or "routing failed" in str(e).lower() or "no_models_available" in str(e).lower():
                wait = 10 * (attempt + 1)
                console.print(f"[yellow]Routing limits exceeded, waiting {wait}s...[/yellow]")
                if attempt < retries - 1:
                    time.sleep(wait)
                else:
                    raise e
            elif attempt < retries - 1:
                time.sleep(1)
            else:
                raise e



# ---------------------------------------------------------------------------
# Routerly tracelog
# ---------------------------------------------------------------------------

def fetch_routerly_trace(trace_id: str, data_file: Path, max_retries: int = 3) -> dict | None:
    """
    Aggregate all records from usage.json with the same traceId:
    completion + routing (LLM policy) + any failed cascades.
    The total matches the cost reported by the backend.
    """
    for attempt in range(max_retries):
        try:
            records = json.loads(data_file.read_text(encoding="utf-8"))
            matches = [r for r in records if r.get("traceId") == trace_id]
            if not matches:
                if attempt < max_retries - 1:
                    time.sleep(0.5)
                continue
            # The completion record (callType != routing) carries modelId, outcome and trace
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


# ---------------------------------------------------------------------------
# Progress bar
# ---------------------------------------------------------------------------

def make_progress(env_label: str):
    """Return (bar_progress, stats_progress): progress bar on line 1, stats on line 2."""
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
        TextColumn("[green]{task.fields[passed]}✓[/green] [red]{task.fields[failed]}✗[/red]"),
        TextColumn("│ pass@1 [bold]{task.fields[pass1]}[/bold]"),
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

def print_summary(
    env_label: str,
    model: str,
    results: list[dict],
    elapsed: float,
    start_dt: datetime,
    end_dt: datetime,
    cost_info: dict | None = None,
    interrupted: bool = False,
    routerly_cost_info: dict | None = None,
) -> None:
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    pass1 = passed / total * 100 if total else 0
    tokens_total = sum(r["input_tokens"] + r["output_tokens"] for r in results)
    total_reasoning = sum(r.get("reasoning_tokens", 0) for r in results)
    tps = tokens_total / elapsed if elapsed > 0 else 0

    title = f"[bold]{env_label}[/bold]  [dim]({model})[/dim]"
    if interrupted:
        title += "  [bold red][INTERRUPTED][/bold red]"

    table = Table(title=title, show_header=False, min_width=46)
    table.add_column(style="dim", width=28)
    table.add_column(justify="right", style="bold")

    table.add_row("Start", start_dt.strftime("%H:%M:%S"))
    table.add_row("End", end_dt.strftime("%H:%M:%S"))
    table.add_row("Duration", f"{elapsed:.1f}s")
    table.add_row("", "")
    table.add_row("pass@1", f"{pass1:.1f}%")
    table.add_row("  passed", str(passed))
    table.add_row("  failed", str(total - passed))
    table.add_row("", "")
    table.add_row("Total tokens", f"{tokens_total:,}")
    table.add_row("  input", f"{sum(r['input_tokens'] for r in results):,}")
    table.add_row("  output", f"{sum(r['output_tokens'] for r in results):,}")
    if total_reasoning > 0:
        table.add_row("    of which reasoning", f"{total_reasoning:,}")
        table.add_row(
            "    visible output",
            f"{sum(r['output_tokens'] - r.get('reasoning_tokens', 0) for r in results):,}",
        )
    table.add_row("Avg tok/s", f"{tps:.0f}")
    table.add_row("", "")
    ttfts = [r["ttft_s"] for r in results if r.get("ttft_s") is not None]
    if ttfts:
        table.add_row("TTFT min", f"{min(ttfts)*1000:.0f} ms")
        table.add_row("TTFT max", f"{max(ttfts)*1000:.0f} ms")
        table.add_row("TTFT avg", f"{sum(ttfts)/len(ttfts)*1000:.0f} ms")
    if cost_info:
        table.add_row("", "")
        table.add_row("Total cost", f"${cost_info['total_cost']:.6f}")
        table.add_row(f"  input  (${cost_info['price_input']}/M tok)", f"${cost_info['cost_input']:.6f}")
        table.add_row(f"  output (${cost_info['price_output']}/M tok)", f"${cost_info['cost_output']:.6f}")
    if routerly_cost_info:
        table.add_row("", "")
        table.add_row("Routerly cost (tracelog)", f"${routerly_cost_info['total_cost']:.6f}")
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

def save_results(
    env_label: str,
    model: str,
    results: list[dict],
    elapsed: float,
    start_dt: datetime,
    end_dt: datetime,
    cost_info: dict | None = None,
    routerly_cost_info: dict | None = None,
    output_dir: str | None = None,
) -> None:
    results_dir = Path(output_dir) if output_dir else Path(__file__).parent / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    ts = start_dt.strftime("%Y%m%d_%H%M%S")
    safe_label = re.sub(r"[^\w\-]", "_", env_label)
    path = results_dir / f"{ts}_{safe_label}.json"

    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    tokens_total = sum(r["input_tokens"] + r["output_tokens"] for r in results)

    payload = {
        "env": env_label,
        "model": model,
        "started_at": start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "ended_at": end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_s": round(elapsed, 2),
        "summary": {
            "pass_at_1": round(passed / total * 100, 2) if total else 0,
            "passed": passed,
            "failed": total - passed,
            "tokens_total": tokens_total,
            "tokens_input": sum(r["input_tokens"] for r in results),
            "tokens_output": sum(r["output_tokens"] for r in results),
            "tokens_reasoning": sum(r.get("reasoning_tokens", 0) for r in results),
            **({} if routerly_cost_info is None else {"routerly_cost_usd": routerly_cost_info.get("total_cost"), "routerly_cost_breakdown": routerly_cost_info}),
            "tokens_output_visible": sum(
                r["output_tokens"] - r.get("reasoning_tokens", 0) for r in results
            ),
            "tps": round(tokens_total / elapsed, 1) if elapsed > 0 else 0,
            "ttft_min_ms": round(min(r["ttft_s"] for r in results) * 1000, 1),
            "ttft_max_ms": round(max(r["ttft_s"] for r in results) * 1000, 1),
            "ttft_avg_ms": round(
                sum(r["ttft_s"] for r in results) / len(results) * 1000, 1
            ),
            **(({"cost_usd": cost_info}) if cost_info else {}),
        },
        "results": results,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    console.print(f"[dim]Results saved to {path}[/dim]")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Routerly Benchmark — HumanEval")
    parser.add_argument("--env", required=True, help="Path to the .env file to use")
    parser.add_argument(
        "--n",
        type=int,
        default=30,
        help=f"Number of HumanEval problems (default: 30, max: {HUMANEVAL_TOTAL})",
    )
    parser.add_argument("--seed", type=int, default=42, help="Seed for reproducibility (default: 42)")
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="Test execution timeout per problem in seconds (default: 10)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Delay in seconds between requests (default: 0). Useful with Routerly to avoid all_models_limits_exceeded.",
    )
    parser.add_argument("--output-dir", default=None, help="Directory for result JSON (default: ./results/)")
    args = parser.parse_args()

    # Load configuration
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
    console.print("[dim]Loading HumanEval dataset...[/dim]")
    rng = random.Random(args.seed)
    n = min(args.n, HUMANEVAL_TOTAL)
    problems = load_humaneval_problems(n, rng)
    total = len(problems)
    console.print(f"[dim]{total} problems loaded[/dim]")

    # Prices: explicit override from .env, otherwise fallback to KNOWN_PRICES.
    # If the model is unknown and prices are not in the .env, cost is not calculated.
    _known = KNOWN_PRICES.get(model, (None, None))
    price_in: float | None = float(cfg["PRICE_INPUT"]) if cfg.get("PRICE_INPUT") else _known[0]
    price_out: float | None = float(cfg["PRICE_OUTPUT"]) if cfg.get("PRICE_OUTPUT") else _known[1]
    show_cost = price_in is not None and price_out is not None

    # Evaluation
    results: list[dict] = []
    passed_count = 0
    failed_count = 0
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
                "stats",
                total=None,
                passed=0,
                failed=0,
                pass1="—",
                tokens=0,
                tps="—",
                ttft="—",
                lat="—",
                cost="—",
            )

            for problem in problems:
                prompt = build_prompt(problem)
                q_start = time.perf_counter()

                raw, in_tok, out_tok, reasoning_tok, ttft, trace_id = call_api(
                    client, model, prompt, reasoning_effort=reasoning_effort
                )

                completion = extract_completion(raw)

                routerly_trace = None
                if trace_id is not None:
                    routerly_trace = fetch_routerly_trace(trace_id, data_file)
                    if routerly_trace and routerly_trace.get("cost") is not None:
                        total_routerly_cost += routerly_trace["cost"]
                        total_routerly_cost_input += routerly_trace.get("costInput") or 0.0
                        total_routerly_cost_output += routerly_trace.get("costOutput") or 0.0
                        routerly_cost_count += 1

                passed, error_msg = evaluate_solution(problem, completion, args.timeout)

                if passed:
                    passed_count += 1
                else:
                    failed_count += 1

                tokens_this = in_tok + out_tok
                tokens_total += tokens_this
                if show_cost:
                    total_live_cost += (in_tok * price_in + out_tok * price_out) / 1_000_000
                elapsed_so_far = time.perf_counter() - start
                tps = tokens_total / elapsed_so_far if elapsed_so_far > 0 else 0
                pass1_pct = passed_count / (passed_count + failed_count) * 100
                avg_ttft_ms = sum(r["ttft_s"] for r in results) / len(results) * 1000 if results else 0
                avg_lat_ms = sum(r["latency_s"] for r in results) / len(results) * 1000 if results else 0

                results.append({
                    "task_id": problem["task_id"],
                    "entry_point": problem["entry_point"],
                    "passed": passed,
                    "error": error_msg,
                    "completion": completion,
                    "raw_response": raw,
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "reasoning_tokens": reasoning_tok,
                    "latency_s": round(time.perf_counter() - q_start, 3),
                    "ttft_s": round(ttft, 3),
                    **({**{"routerly_trace": routerly_trace}} if routerly_trace is not None else {}),
                })

                bar_progress.update(bar_task, advance=1)
                stats_progress.update(
                    stats_task,
                    passed=passed_count,
                    failed=failed_count,
                    pass1=f"{pass1_pct:.1f}%",
                    tokens=f"{tokens_total:,}",
                    tps=f"{tps:.0f}",
                    ttft=f"{avg_ttft_ms:.0f}ms",
                    lat=f"{avg_lat_ms:.0f}ms",
                    cost=f"${total_routerly_cost:.4f}" if routerly_cost_count > 0 else (f"${total_live_cost:.4f}" if show_cost else "—"),
                )

                if args.delay > 0:
                    time.sleep(args.delay)

    except KeyboardInterrupt:
        interrupted = True
        console.print("\n[bold red]Test interrupted by user.[/bold red]")

    elapsed = time.perf_counter() - start
    end_dt = datetime.now()

    if not results:
        console.print("[dim]No results to display.[/dim]")
        return

    # Calculate cost if enabled in the .env (price_in/price_out already computed above)
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
    print_summary(
        env_label, model, results, elapsed, start_dt, end_dt,
        cost_info=cost_info, interrupted=interrupted, routerly_cost_info=routerly_cost_info,
    )
    save_results(env_label, model, results, elapsed, start_dt, end_dt,
                 cost_info=cost_info, routerly_cost_info=routerly_cost_info,
                 output_dir=args.output_dir)


if __name__ == "__main__":
    main()
