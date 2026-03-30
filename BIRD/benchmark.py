#!/usr/bin/env python3
"""
Routerly Benchmark — BIRD (Text-to-SQL)

Benchmark agnostico per valutare la capacità di generazione SQL su database
aziendali reali, usando il dataset BIRD (BIg Bench for LaRge-scale Database
Grounded Text-to-SQL).

Lo script non conosce il target: riceve solo BASE_URL, API_KEY e MODEL
dal file .env specificato. Funziona identicamente puntando a Anthropic,
OpenAI o Routerly.

Metrica principale: execution accuracy — la query è corretta se il suo
risultato su SQLite coincide con il risultato del gold SQL.
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

# Silenzia warning non pertinenti
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
# Dataset BIRD
# ---------------------------------------------------------------------------

def load_bird_questions(
    bird_dir: Path,
    n: int,
    difficulty: str,
    rng: random.Random,
) -> list[dict]:
    """Carica N domande dal dataset BIRD locale, filtrando per difficoltà."""
    dev_json = bird_dir / "dev.json"
    if not dev_json.exists():
        console.print(f"[red]File non trovato: {dev_json}[/red]")
        console.print("[dim]Scarica il dataset da https://bird-bench.github.io/ e posizionalo in --bird-dir[/dim]")
        sys.exit(1)

    with open(dev_json, encoding="utf-8") as f:
        all_questions = json.load(f)

    # Filtra per difficoltà
    if difficulty != "all":
        all_questions = [q for q in all_questions if q.get("difficulty") == difficulty]

    if not all_questions:
        console.print(f"[red]Nessuna domanda trovata con difficoltà: {difficulty}[/red]")
        sys.exit(1)

    # Campiona
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
    Estrae lo schema delle tabelle dal database SQLite come istruzioni
    CREATE TABLE testuali, usando PRAGMA table_info.
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
    """Rimuove markdown fences e testo non SQL dalla risposta del modello."""
    # Prova a estrarre da blocco ```sql ... ``` o ``` ... ```
    m = re.search(r"```(?:sql)?\s*\n?(.*?)```", raw, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Fallback: prendi tutto e pulisci
    sql = re.sub(r"```", "", raw).strip()
    return sql


def normalize_result(rows) -> list:
    """Normalizza i risultati per il confronto: liste di tuple ordinate."""
    if rows is None:
        return []
    normalized = []
    for row in rows:
        normalized.append(tuple(
            str(v).strip() if v is not None else "" for v in row
        ))
    return sorted(normalized)


def execute_sql(db_path: Path, sql: str) -> tuple[list | None, str]:
    """
    Esegue una query SQL su SQLite.
    Restituisce (rows, error). rows è None in caso di errore.
    """
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = None
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()
        conn.close()
        return rows, ""
    except Exception as e:
        return None, str(e)[:300]


def evaluate_sql(
    db_path: Path,
    sql_generated: str,
    sql_gold: str,
) -> tuple[bool, str]:
    """
    Valuta la correttezza con execution accuracy.
    Restituisce (correct, error_message).
    """
    rows_gold, err_gold = execute_sql(db_path, sql_gold)
    if rows_gold is None:
        # Gold SQL errato: skip (non dovrebbe mai accadere sul dataset ufficiale)
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

            # Routerly a volte restituisce errori come testo invece di eccezioni
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
                console.print(f"[yellow]Routerly: nessun modello disponibile, attendo {wait}s...[/yellow]")
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


# ---------------------------------------------------------------------------
# Progress bar
# ---------------------------------------------------------------------------

def make_progress(env_label: str):
    """Restituisce (bar_progress, stats_progress): barra su riga 1, stats su riga 2."""
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

    # Accuracy per difficoltà
    acc_by_diff: dict[str, tuple[int, int]] = {}
    for r in results:
        diff = r["difficulty"]
        c, tot = acc_by_diff.get(diff, (0, 0))
        acc_by_diff[diff] = (c + (1 if r["correct"] else 0), tot + 1)

    title = f"[bold]{env_label}[/bold]  [dim]({model})[/dim]"
    if interrupted:
        title += "  [bold red][INTERROTTO][/bold red]"

    table = Table(title=title, show_header=False, min_width=48)
    table.add_column(style="dim", width=30)
    table.add_column(justify="right", style="bold")

    table.add_row("Inizio", start_dt.strftime("%H:%M:%S"))
    table.add_row("Fine", end_dt.strftime("%H:%M:%S"))
    table.add_row("Durata", f"{elapsed:.1f}s")
    table.add_row("", "")
    table.add_row("Exec accuracy totale", f"{acc_total:.1f}%")
    for diff in DIFFICULTIES:
        if diff in acc_by_diff:
            c, t = acc_by_diff[diff]
            table.add_row(f"  {diff}", f"{c/t*100:.1f}%  ({c}/{t})")
    table.add_row("", "")
    table.add_row("Token totali", f"{tokens_total:,}")
    table.add_row("  input", f"{sum(r['input_tokens'] for r in results):,}")
    table.add_row("  output", f"{sum(r['output_tokens'] for r in results):,}")
    if total_reasoning > 0:
        table.add_row("    di cui reasoning", f"{total_reasoning:,}")
        table.add_row(
            "    output visibile",
            f"{sum(r['output_tokens'] - r.get('reasoning_tokens', 0) for r in results):,}",
        )
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

def save_results(
    env_label: str,
    model: str,
    results: list[dict],
    elapsed: float,
    start_dt: datetime,
    end_dt: datetime,
    cost_info: dict | None = None,
    routerly_cost_info: dict | None = None,
) -> None:
    results_dir = Path(__file__).parent / "results"
    results_dir.mkdir(exist_ok=True)
    ts = start_dt.strftime("%Y%m%d_%H%M%S")
    safe_label = re.sub(r"[^\w\-]", "_", env_label)
    path = results_dir / f"{ts}_{safe_label}.json"

    total = len(results)
    correct_total = sum(1 for r in results if r["correct"])
    tokens_total = sum(r["input_tokens"] + r["output_tokens"] for r in results)

    # Accuracy per difficoltà
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
    console.print(f"[dim]Risultati salvati in {path}[/dim]")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Routerly Benchmark — BIRD (Text-to-SQL)")
    parser.add_argument("--env", required=True, help="Percorso del file .env da usare")
    parser.add_argument("--bird-dir", required=True, help="Percorso della cartella BIRD scaricata (contiene dev.json e dev_databases/)")
    parser.add_argument("--n", type=int, default=30, help="Numero totale di domande (default: 30)")
    parser.add_argument(
        "--difficulty",
        default="all",
        choices=list(DIFFICULTIES) + ["all"],
        help="Filtra per difficoltà: simple, moderate, challenging, all (default: all)",
    )
    parser.add_argument("--seed", type=int, default=42, help="Seed per riproducibilità (default: 42)")
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Pausa in secondi tra una richiesta e la successiva (default: 0)",
    )
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

    bird_dir = Path(args.bird_dir)
    if not bird_dir.is_dir():
        console.print(f"[red]Directory BIRD non trovata: {bird_dir}[/red]")
        sys.exit(1)

    # Health-check: verifica che l'endpoint risponda correttamente prima di iniziare
    console.print(f"[dim]Verifica endpoint {cfg['BASE_URL']}...[/dim]")
    try:
        probe = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with the single word: OK"}],
            max_tokens=10,
            stream=False,
        )
        probe_text = (probe.choices[0].message.content or "").strip()
        if probe_text.lower().startswith("routing failed") or "no_models_available" in probe_text.lower():
            console.print(f"[red]Errore endpoint: {probe_text}[/red]")
            console.print("[dim]Verifica che Routerly sia avviato e il progetto abbia modelli configurati.[/dim]")
            sys.exit(1)
        console.print(f"[dim]Endpoint OK (risposta: {probe_text!r})[/dim]")
    except Exception as e:
        console.print(f"[red]Impossibile raggiungere l'endpoint: {e}[/red]")
        sys.exit(1)

    # Dataset
    console.print("[dim]Caricamento dataset BIRD...[/dim]")
    rng = random.Random(args.seed)
    questions = load_bird_questions(bird_dir, args.n, args.difficulty, rng)
    total = len(questions)
    console.print(f"[dim]{total} domande caricate (difficulty={args.difficulty})[/dim]")

    # Pre-calcola prezzi per il costo live nella progress bar
    show_cost = cfg.get("SHOW_COST", "false").lower() == "true"
    price_in = float(cfg.get("PRICE_INPUT", "0")) if show_cost else 0.0
    price_out = float(cfg.get("PRICE_OUTPUT", "0")) if show_cost else 0.0

    # Valutazione
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
                    console.print(f"[yellow]DB non trovato, skip: {db_path}[/yellow]")
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
