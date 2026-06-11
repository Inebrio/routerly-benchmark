#!/usr/bin/env python3
"""
Routerly Benchmark — BIRD (Text-to-SQL)

Agnostic benchmark to evaluate SQL generation capability on real-world
business databases, using the BIRD dataset (BIg Bench for LaRge-scale Database
Grounded Text-to-SQL).

The script does not know the target: it only receives BASE_URL, API_KEY and MODEL
from the specified .env file. It works identically when pointing to Anthropic,
OpenAI or Routerly.

Main metric: execution accuracy — a query is correct if its result
on SQLite matches the result of the gold SQL.
"""

import argparse
import json
import os
import random
import re
import sqlite3
import sys
import time
import warnings
import logging
from datetime import datetime
from pathlib import Path

# Silence irrelevant warnings
warnings.filterwarnings("ignore")
logging.getLogger("openai").setLevel(logging.ERROR)

import openai
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

DIFFICULTIES = ("simple", "moderate", "challenging")

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
# Dataset BIRD
# ---------------------------------------------------------------------------

def load_bird_questions(
    bird_dir: Path,
    n: int,
    difficulty: str,
    rng: random.Random,
) -> list[dict]:
    """Load N questions from the local BIRD dataset, filtered by difficulty."""
    dev_json = bird_dir / "dev.json"
    if not dev_json.exists():
        console.print(f"[red]File not found: {dev_json}[/red]")
        console.print("[dim]Download the dataset from https://bird-bench.github.io/ and place it in --bird-dir[/dim]")
        sys.exit(1)

    with open(dev_json, encoding="utf-8") as f:
        all_questions = json.load(f)

    # Filter by difficulty
    if difficulty != "all":
        all_questions = [q for q in all_questions if q.get("difficulty") == difficulty]

    if not all_questions:
        console.print(f"[red]No questions found with difficulty: {difficulty}[/red]")
        sys.exit(1)

    # Sample
    rng.shuffle(all_questions)
    selected = all_questions[:n]

    questions = []
    for q in selected:
        questions.append({
            "question_id": q.get("question_id", ""),
            "db_id": q["db_id"],
            "question": q["question"],
            "evidence": q.get("evidence", ""),
            "sql_gold": q["SQL"],
            "difficulty": q.get("difficulty", "unknown"),
        })
    return questions


# ---------------------------------------------------------------------------
# Schema extraction
# ---------------------------------------------------------------------------

def get_db_path(bird_dir: Path, db_id: str) -> Path:
    return bird_dir / "dev_databases" / db_id / f"{db_id}.sqlite"


def extract_schema(db_path: Path) -> str:
    """
    Extract the table schema from the SQLite database as textual
    CREATE TABLE statements, using PRAGMA table_info.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row[0] for row in cursor.fetchall()]

        statements = []
        for table in tables:
            cursor.execute(f"PRAGMA table_info('{table}')")
            cols = cursor.fetchall()
            # cid, name, type, notnull, dflt_value, pk
            col_defs = []
            for col in cols:
                col_name = col[1]
                col_type = col[2] or "TEXT"
                pk = " PRIMARY KEY" if col[5] else ""
                notnull = " NOT NULL" if col[3] else ""
                col_defs.append(f"  {col_name} {col_type}{pk}{notnull}")
            ddl = f"CREATE TABLE {table} (\n" + ",\n".join(col_defs) + "\n);"
            statements.append(ddl)
        return "\n\n".join(statements)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def build_prompt(q: dict, schema: str) -> str:
    parts = [f"Database schema:\n{schema}"]
    if q["evidence"].strip():
        parts.append(f"Additional context: {q['evidence'].strip()}")
    parts.append(f"Question: {q['question']}")
    parts.append("Write a SQLite SQL query to answer the question. Return only the SQL query, no explanation.")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# SQL extraction & evaluation
# ---------------------------------------------------------------------------

def extract_sql(raw: str) -> str:
    """Remove markdown fences and non-SQL text from the model response."""
    # Try to extract from a ```sql ... ``` or ``` ... ``` block
    m = re.search(r"```(?:sql)?\s*\n?(.*?)```", raw, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Fallback: take everything and clean up
    sql = re.sub(r"```", "", raw).strip()
    return sql


def normalize_result(rows) -> list:
    """Normalize results for comparison: sorted lists of tuples."""
    if rows is None:
        return []
    normalized = []
    for row in rows:
        normalized.append(tuple(
            str(v).strip() if v is not None else "" for v in row
        ))
    return sorted(normalized)


def execute_sql(db_path: Path, sql: str, timeout_s: float = 30.0) -> tuple[list | None, str]:
    """
    Execute a SQL query on SQLite with a timeout.
    Returns (rows, error). rows is None on error or timeout.
    """
    import threading

    result: list[object] = [None, ""]  # [rows, error]
    done = threading.Event()

    def _run():
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = None
            cursor = conn.cursor()
            cursor.execute(sql)
            rows = cursor.fetchall()
            conn.close()
            result[0] = rows
        except Exception as e:
            result[1] = str(e)[:300]
        finally:
            done.set()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    if not done.wait(timeout_s):
        return None, f"query timeout after {timeout_s}s"
    return result[0], result[1]


def evaluate_sql(
    db_path: Path,
    sql_generated: str,
    sql_gold: str,
) -> tuple[bool, str]:
    """
    Evaluate correctness using execution accuracy.
    Returns (correct, error_message).
    """
    rows_gold, err_gold = execute_sql(db_path, sql_gold)
    if rows_gold is None:
        # Gold SQL error: skip (should never happen on the official dataset)
        return False, f"gold SQL error: {err_gold}"

    rows_pred, err_pred = execute_sql(db_path, sql_generated)
    if rows_pred is None:
        return False, err_pred

    return normalize_result(rows_gold) == normalize_result(rows_pred), ""


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

            # Routerly sometimes returns errors as text instead of exceptions
            if text.lower().startswith("routing failed") or "no_models_available" in text.lower():
                raise openai.APIError(
                    message=text,
                    request=None,  # type: ignore[arg-type]
                    body=None,
                )

            return text, in_tok, out_tok, reasoning_tok, ttft, trace_id

        except openai.RateLimitError:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise
        except openai.APIError as e:
            msg = str(e).lower()
            if "all_models_limits_exceeded" in msg or "routing failed" in msg or "no_models_available" in msg:
                wait = 10 * (attempt + 1)
                console.print(f"[yellow]Routerly: no models available, waiting {wait}s...[/yellow]")
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
    correct_total = sum(1 for r in results if r["correct"])
    acc_total = correct_total / total * 100 if total else 0
    tokens_total = sum(r["input_tokens"] + r["output_tokens"] for r in results)
    total_reasoning = sum(r.get("reasoning_tokens", 0) for r in results)
    tps = tokens_total / elapsed if elapsed > 0 else 0

    # Accuracy by difficulty
    acc_by_diff: dict[str, tuple[int, int]] = {}
    for r in results:
        diff = r["difficulty"]
        c, tot = acc_by_diff.get(diff, (0, 0))
        acc_by_diff[diff] = (c + (1 if r["correct"] else 0), tot + 1)

    title = f"[bold]{env_label}[/bold]  [dim]({model})[/dim]"
    if interrupted:
        title += "  [bold red][INTERRUPTED][/bold red]"

    table = Table(title=title, show_header=False, min_width=48)
    table.add_column(style="dim", width=30)
    table.add_column(justify="right", style="bold")

    table.add_row("Start", start_dt.strftime("%H:%M:%S"))
    table.add_row("End", end_dt.strftime("%H:%M:%S"))
    table.add_row("Duration", f"{elapsed:.1f}s")
    table.add_row("", "")
    table.add_row("Total exec accuracy", f"{acc_total:.1f}%")
    for diff in DIFFICULTIES:
        if diff in acc_by_diff:
            c, t = acc_by_diff[diff]
            table.add_row(f"  {diff}", f"{c/t*100:.1f}%  ({c}/{t})")
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
    correct_total = sum(1 for r in results if r["correct"])
    tokens_total = sum(r["input_tokens"] + r["output_tokens"] for r in results)

    # Accuracy by difficulty
    acc_by_diff: dict[str, dict] = {}
    for diff in DIFFICULTIES:
        subset = [r for r in results if r["difficulty"] == diff]
        if subset:
            c = sum(1 for r in subset if r["correct"])
            acc_by_diff[diff] = round(c / len(subset) * 100, 2)

    payload = {
        "env": env_label,
        "model": model,
        "started_at": start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "ended_at": end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_s": round(elapsed, 2),
        "summary": {
            "accuracy_total": round(correct_total / total * 100, 2) if total else 0,
            "accuracy_by_difficulty": acc_by_diff,
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
    parser = argparse.ArgumentParser(description="Routerly Benchmark — BIRD (Text-to-SQL)")
    parser.add_argument("--env", required=True, help="Path to the .env file to use")
    parser.add_argument("--bird-dir", required=True, help="Path to the downloaded BIRD folder (contains dev.json and dev_databases/)")
    parser.add_argument("--n", type=int, default=30, help="Total number of questions (default: 30)")
    parser.add_argument(
        "--difficulty",
        default="all",
        choices=list(DIFFICULTIES) + ["all"],
        help="Filter by difficulty: simple, moderate, challenging, all (default: all)",
    )
    parser.add_argument("--seed", type=int, default=42, help="Seed for reproducibility (default: 42)")
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Delay in seconds between requests (default: 0)",
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

    bird_dir = Path(args.bird_dir)
    if not bird_dir.is_dir():
        console.print(f"[red]BIRD directory not found: {bird_dir}[/red]")
        sys.exit(1)

    # Health-check: verify the endpoint responds correctly before starting
    console.print(f"[dim]Checking endpoint {cfg['BASE_URL']}...[/dim]")
    try:
        probe = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with the single word: OK"}],
            max_tokens=10,
            stream=False,
        )
        probe_text = (probe.choices[0].message.content or "").strip()
        if probe_text.lower().startswith("routing failed") or "no_models_available" in probe_text.lower():
            console.print(f"[red]Endpoint error: {probe_text}[/red]")
            console.print("[dim]Make sure Routerly is running and the project has models configured.[/dim]")
            sys.exit(1)
        console.print(f"[dim]Endpoint OK (response: {probe_text!r})[/dim]")
    except Exception as e:
        console.print(f"[red]Cannot reach endpoint: {e}[/red]")
        sys.exit(1)

    # Dataset
    console.print("[dim]Loading BIRD dataset...[/dim]")
    rng = random.Random(args.seed)
    questions = load_bird_questions(bird_dir, args.n, args.difficulty, rng)
    total = len(questions)
    console.print(f"[dim]{total} questions loaded (difficulty={args.difficulty})[/dim]")

    # Prices: explicit override from .env, otherwise fallback to KNOWN_PRICES.
    # If the model is unknown and prices are not in the .env, cost is not calculated.
    _known = KNOWN_PRICES.get(model, (None, None))
    price_in: float | None = float(cfg["PRICE_INPUT"]) if cfg.get("PRICE_INPUT") else _known[0]
    price_out: float | None = float(cfg["PRICE_OUTPUT"]) if cfg.get("PRICE_OUTPUT") else _known[1]
    show_cost = price_in is not None and price_out is not None

    # Evaluation
    results: list[dict] = []
    correct_count = 0
    wrong_count = 0
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
                correct=0,
                wrong=0,
                acc="—",
                tokens=0,
                tps="—",
                ttft="—",
                lat="—",
                cost="—",
            )

            for q in questions:
                db_path = get_db_path(bird_dir, q["db_id"])
                if not db_path.exists():
                    console.print(f"[yellow]DB not found, skipping: {db_path}[/yellow]")
                    bar_progress.update(bar_task, advance=1)
                    continue

                schema = extract_schema(db_path)
                prompt = build_prompt(q, schema)
                q_start = time.perf_counter()

                try:
                    raw, in_tok, out_tok, reasoning_tok, ttft, trace_id = call_api(
                        client, model, prompt, reasoning_effort=reasoning_effort
                    )
                except Exception as api_err:
                    console.print(f"\n[red]API error ({q['db_id']}): {api_err}[/red]")
                    wrong_count += 1
                    bar_progress.update(bar_task, advance=1)
                    results.append({
                        "question_id": q["question_id"],
                        "db_id": q["db_id"],
                        "difficulty": q["difficulty"],
                        "question": q["question"],
                        "sql_gold": q["sql_gold"],
                        "sql_generated": "",
                        "raw_response": str(api_err),
                        "correct": False,
                        "error": f"api_error: {api_err}",
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "reasoning_tokens": 0,
                        "latency_s": round(time.perf_counter() - q_start, 3),
                        "ttft_s": 0.0,
                    })
                    stats_progress.update(
                        stats_task,
                        correct=correct_count,
                        wrong=wrong_count,
                        acc=f"{correct_count/(correct_count+wrong_count)*100:.1f}%",
                        tokens=f"{tokens_total:,}",
                        tps="—",
                        ttft="—",
                        lat="—",
                        cost=f"${total_routerly_cost:.4f}" if routerly_cost_count > 0 else (f"${total_live_cost:.4f}" if show_cost else "—"),
                    )
                    continue

                routerly_trace = None
                if trace_id is not None:
                    routerly_trace = fetch_routerly_trace(trace_id, data_file)
                    if routerly_trace and routerly_trace.get("cost") is not None:
                        total_routerly_cost += routerly_trace["cost"]
                        total_routerly_cost_input += routerly_trace.get("costInput") or 0.0
                        total_routerly_cost_output += routerly_trace.get("costOutput") or 0.0
                        routerly_cost_count += 1

                sql_generated = extract_sql(raw)
                is_correct, error_msg = evaluate_sql(db_path, sql_generated, q["sql_gold"])

                if is_correct:
                    correct_count += 1
                else:
                    wrong_count += 1

                tokens_this = in_tok + out_tok
                tokens_total += tokens_this
                if show_cost:
                    total_live_cost += (in_tok * price_in + out_tok * price_out) / 1_000_000
                elapsed_so_far = time.perf_counter() - start
                tps = tokens_total / elapsed_so_far if elapsed_so_far > 0 else 0
                acc_pct = correct_count / (correct_count + wrong_count) * 100
                avg_ttft_ms = sum(r["ttft_s"] for r in results) / len(results) * 1000 if results else 0
                avg_lat_ms = sum(r["latency_s"] for r in results) / len(results) * 1000 if results else 0

                results.append({
                    "question_id": q["question_id"],
                    "db_id": q["db_id"],
                    "difficulty": q["difficulty"],
                    "question": q["question"],
                    "sql_gold": q["sql_gold"],
                    "sql_generated": sql_generated,
                    "raw_response": raw,
                    "correct": is_correct,
                    "error": error_msg,
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
                    correct=correct_count,
                    wrong=wrong_count,
                    acc=f"{acc_pct:.1f}%",
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
    print_summary(env_label, model, results, elapsed, start_dt, end_dt,
                  cost_info=cost_info, interrupted=interrupted, routerly_cost_info=routerly_cost_info)
    save_results(env_label, model, results, elapsed, start_dt, end_dt,
                 cost_info=cost_info, routerly_cost_info=routerly_cost_info,
                 output_dir=args.output_dir)


if __name__ == "__main__":
    main()
