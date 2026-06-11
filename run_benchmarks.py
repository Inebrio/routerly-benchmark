#!/usr/bin/env python3
"""
Unified Benchmark Runner — runs BIRD, HumanEval, and MMLU benchmarks
in batch, collecting results into a single timestamped folder under results/.
"""

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
RESULTS_ROOT = ROOT / "results"

BENCHMARKS = {
    "bird": {
        "dir": ROOT / "BIRD",
        "script": ROOT / "BIRD" / "benchmark.py",
        "extra_args": lambda args: ["--bird-dir", str(args.bird_dir)],
        "python": ROOT / "BIRD" / ".venv" / "bin" / "python3",
    },
    "humaneval": {
        "dir": ROOT / "HumanEval",
        "script": ROOT / "HumanEval" / "benchmark.py",
        "extra_args": lambda _: [],
        "python": ROOT / "HumanEval" / ".venv" / "bin" / "python3",
    },
    "mmlu": {
        "dir": ROOT / "MMLU Benchmark",
        "script": ROOT / "MMLU Benchmark" / "benchmark.py",
        "extra_args": lambda _: [],
        "python": ROOT / "MMLU Benchmark" / ".venv" / "bin" / "python3",
    },
}


# ── helpers ──────────────────────────────────────────────────────────────────


def discover_envs(bench_dir: Path) -> list[Path]:
    """Return sorted list of .env_* files in a benchmark directory."""
    return sorted(bench_dir.glob(".env_*"))


_REDACTED = "***REDACTED***"
_SECRET_KEYS = {"apiKey", "token", "api_key", "secret", "password", "access_token", "refresh_token"}


def _redact(obj: object) -> object:
    """Recursively redact known secret keys from a JSON-like object."""
    if isinstance(obj, dict):
        return {k: (_REDACTED if k in _SECRET_KEYS else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(item) for item in obj]
    return obj


def snapshot_config(dest: Path) -> None:
    """Copy Routerly routing config files into dest/config/ with secrets redacted."""
    config_src = Path.home() / ".routerly" / "config"
    if not config_src.exists():
        print(f"  ⚠ Routerly config dir not found ({config_src}), skipping snapshot")
        return
    config_dest = dest / "config"
    config_dest.mkdir(parents=True, exist_ok=True)
    for name in ("projects.json", "models.json"):
        src = config_src / name
        if src.exists():
            try:
                data = json.loads(src.read_text())
                redacted = _redact(data)
                (config_dest / name).write_text(json.dumps(redacted, indent=2) + "\n")
            except (json.JSONDecodeError, OSError):
                # Fallback: copy as-is if JSON parsing fails
                shutil.copy2(src, config_dest / name)


def result_file_for(raw_dir: Path, env_label: str, seed: int) -> Path | None:
    """Return the result file if it already exists (for resume support)."""
    # Files follow pattern: YYYYMMDD_HHMMSS__<safe_env_label>.json
    # We match by env label suffix and check metadata inside.
    for f in raw_dir.glob(f"*__{env_label}.json"):
        try:
            data = json.loads(f.read_text())
            # The benchmark scripts don't store seed in output, so we rely on
            # file naming convention added by this runner (see run_single).
            if f.stem.startswith(f"seed{seed}_"):
                return f
        except (json.JSONDecodeError, KeyError):
            continue
    return None


def run_single(
    bench_key: str,
    env_path: Path,
    seed: int,
    n: int,
    raw_dir: Path,
    extra_args: list[str],
    delay: float,
) -> dict:
    """Run a single benchmark subprocess. Returns a status dict."""
    env_label = env_path.stem  # e.g. ".env_routerly" → ".env_routerly" — we need name part
    safe_label = env_path.name.replace(".env_", "env_")

    # Check if already done (resume)
    existing = list(raw_dir.glob(f"seed{seed}_*__{safe_label}.json"))
    if existing:
        print(f"    ↩ skip (already exists: {existing[0].name})")
        return {"status": "skipped", "file": str(existing[0])}

    # We'll run the benchmark and then rename the output to include the seed prefix
    raw_dir.mkdir(parents=True, exist_ok=True)

    bench_info = BENCHMARKS[bench_key]
    python_bin = bench_info.get("python", Path(sys.executable))
    if not python_bin.exists():
        python_bin = Path(sys.executable)

    cmd = [
        str(python_bin),
        str(BENCHMARKS[bench_key]["script"]),
        "--env", str(env_path),
        "--n", str(n),
        "--seed", str(seed),
        "--output-dir", str(raw_dir),
        *extra_args,
    ]
    if delay > 0 and bench_key in ("bird", "humaneval"):
        cmd.extend(["--delay", str(delay)])

    print(f"    → {' '.join(cmd[-8:])}")  # show last 8 args for readability
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode != 0:
            print(f"    ✗ exit code {result.returncode}")
            if result.stderr:
                for line in result.stderr.strip().splitlines()[-5:]:
                    print(f"      {line}")
            return {"status": "error", "returncode": result.returncode, "stderr": result.stderr[-500:]}
    except subprocess.TimeoutExpired:
        print("    ✗ timeout (3600s)")
        return {"status": "timeout"}

    # Rename the produced file to include seed prefix
    # The benchmark script creates: YYYYMMDD_HHMMSS__<safe_label>.json
    produced = sorted(raw_dir.glob(f"*__{safe_label}.json"), key=lambda p: p.stat().st_mtime)
    if produced:
        newest = produced[-1]
        # Only rename if not already prefixed
        if not newest.name.startswith("seed"):
            new_name = f"seed{seed}_{newest.name}"
            renamed = newest.rename(raw_dir / new_name)
            return {"status": "ok", "file": str(renamed)}
        return {"status": "ok", "file": str(newest)}
    else:
        print("    ⚠ no output file found after run")
        return {"status": "no_output"}


# ── report generation ────────────────────────────────────────────────────────


def load_raw_results(raw_dir: Path) -> list[dict]:
    """Load all JSON result files from a raw directory."""
    results = []
    for f in sorted(raw_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            data["_file"] = f.name
            # Extract seed from filename
            if f.name.startswith("seed"):
                data["_seed"] = int(f.name.split("_")[0].replace("seed", ""))
            results.append(data)
        except (json.JSONDecodeError, ValueError):
            continue
    return results


def _extract_cost(summary: dict) -> float | None:
    """Try to extract cost in USD from a summary dict."""
    if "routerly_cost_usd" in summary:
        return summary["routerly_cost_usd"]
    if "routerly_cost_breakdown" in summary and "total_cost" in summary["routerly_cost_breakdown"]:
        return summary["routerly_cost_breakdown"]["total_cost"]
    if "cost_usd" in summary and isinstance(summary["cost_usd"], dict):
        return summary["cost_usd"].get("total_cost")
    return None


def _fmt(val, fmt=".2f") -> str:
    return f"{val:{fmt}}" if val is not None else "—"


def generate_bird_report(raw_dir: Path, dest: Path) -> None:
    """Generate per-benchmark report for BIRD."""
    data = load_raw_results(raw_dir)
    if not data:
        dest.write_text("# BIRD Report\n\nNo results found.\n")
        return

    lines = ["# BIRD Benchmark Report\n"]
    lines.append("| Seed | Env | Accuracy (%) | Simple (%) | Moderate (%) | Challenging (%) | Tokens | Cost ($) | Duration (s) |")
    lines.append("|------|-----|-------------|-----------|-------------|----------------|--------|----------|-------------|")

    # Group by env for averages
    env_stats: dict[str, list] = {}

    for r in data:
        s = r.get("summary", {})
        env = r.get("env", "?")
        seed = r.get("_seed", "?")
        acc = s.get("accuracy_total")
        by_diff = s.get("accuracy_by_difficulty", {})
        tokens = s.get("tokens_total")
        cost = _extract_cost(s)
        elapsed = r.get("elapsed_s")

        lines.append(
            f"| {seed} | {env} | {_fmt(acc)} | {_fmt(by_diff.get('simple'))} | "
            f"{_fmt(by_diff.get('moderate'))} | {_fmt(by_diff.get('challenging'))} | "
            f"{tokens or '—'} | {_fmt(cost, '.4f')} | {_fmt(elapsed, '.1f')} |"
        )

        env_stats.setdefault(env, []).append({
            "acc": acc, "tokens": tokens, "cost": cost, "elapsed": elapsed,
        })

    # Averages
    lines.append("")
    lines.append("## Averages by Environment\n")
    lines.append("| Env | Avg Accuracy (%) | Avg Tokens | Avg Cost ($) | Avg Duration (s) | Runs |")
    lines.append("|-----|-----------------|-----------|-------------|-----------------|------|")
    for env, stats in sorted(env_stats.items()):
        n = len(stats)
        avg_acc = sum(x["acc"] for x in stats if x["acc"] is not None) / max(1, sum(1 for x in stats if x["acc"] is not None))
        avg_tok = sum(x["tokens"] for x in stats if x["tokens"] is not None) / max(1, sum(1 for x in stats if x["tokens"] is not None))
        costs = [x["cost"] for x in stats if x["cost"] is not None]
        avg_cost = sum(costs) / len(costs) if costs else None
        elaps = [x["elapsed"] for x in stats if x["elapsed"] is not None]
        avg_el = sum(elaps) / len(elaps) if elaps else None
        lines.append(f"| {env} | {_fmt(avg_acc)} | {_fmt(avg_tok, '.0f')} | {_fmt(avg_cost, '.4f')} | {_fmt(avg_el, '.1f')} | {n} |")

    dest.write_text("\n".join(lines) + "\n")


def generate_humaneval_report(raw_dir: Path, dest: Path) -> None:
    """Generate per-benchmark report for HumanEval."""
    data = load_raw_results(raw_dir)
    if not data:
        dest.write_text("# HumanEval Report\n\nNo results found.\n")
        return

    lines = ["# HumanEval Benchmark Report\n"]
    lines.append("| Seed | Env | pass@1 (%) | Passed | Failed | Tokens | Cost ($) | Duration (s) |")
    lines.append("|------|-----|-----------|--------|--------|--------|----------|-------------|")

    env_stats: dict[str, list] = {}

    for r in data:
        s = r.get("summary", {})
        env = r.get("env", "?")
        seed = r.get("_seed", "?")
        p1 = s.get("pass_at_1")
        passed = s.get("passed")
        failed = s.get("failed")
        tokens = s.get("tokens_total")
        cost = _extract_cost(s)
        elapsed = r.get("elapsed_s")

        lines.append(
            f"| {seed} | {env} | {_fmt(p1)} | {passed or '—'} | {failed or '—'} | "
            f"{tokens or '—'} | {_fmt(cost, '.4f')} | {_fmt(elapsed, '.1f')} |"
        )

        env_stats.setdefault(env, []).append({
            "p1": p1, "tokens": tokens, "cost": cost, "elapsed": elapsed,
        })

    lines.append("")
    lines.append("## Averages by Environment\n")
    lines.append("| Env | Avg pass@1 (%) | Avg Tokens | Avg Cost ($) | Avg Duration (s) | Runs |")
    lines.append("|-----|---------------|-----------|-------------|-----------------|------|")
    for env, stats in sorted(env_stats.items()):
        n = len(stats)
        avg_p1 = sum(x["p1"] for x in stats if x["p1"] is not None) / max(1, sum(1 for x in stats if x["p1"] is not None))
        avg_tok = sum(x["tokens"] for x in stats if x["tokens"] is not None) / max(1, sum(1 for x in stats if x["tokens"] is not None))
        costs = [x["cost"] for x in stats if x["cost"] is not None]
        avg_cost = sum(costs) / len(costs) if costs else None
        elaps = [x["elapsed"] for x in stats if x["elapsed"] is not None]
        avg_el = sum(elaps) / len(elaps) if elaps else None
        lines.append(f"| {env} | {_fmt(avg_p1)} | {_fmt(avg_tok, '.0f')} | {_fmt(avg_cost, '.4f')} | {_fmt(avg_el, '.1f')} | {n} |")

    dest.write_text("\n".join(lines) + "\n")


def generate_mmlu_report(raw_dir: Path, dest: Path) -> None:
    """Generate per-benchmark report for MMLU."""
    data = load_raw_results(raw_dir)
    if not data:
        dest.write_text("# MMLU Report\n\nNo results found.\n")
        return

    lines = ["# MMLU Benchmark Report\n"]
    lines.append("| Seed | Env | Accuracy (%) | Tokens | Cost ($) | Duration (s) |")
    lines.append("|------|-----|-------------|--------|----------|-------------|")

    env_stats: dict[str, list] = {}

    for r in data:
        s = r.get("summary", {})
        env = r.get("env", "?")
        seed = r.get("_seed", "?")
        acc = s.get("accuracy_total")
        tokens = s.get("tokens_total")
        cost = _extract_cost(s)
        elapsed = r.get("elapsed_s")

        lines.append(
            f"| {seed} | {env} | {_fmt(acc)} | {tokens or '—'} | {_fmt(cost, '.4f')} | {_fmt(elapsed, '.1f')} |"
        )

        env_stats.setdefault(env, []).append({
            "acc": acc, "tokens": tokens, "cost": cost, "elapsed": elapsed,
        })

    lines.append("")
    lines.append("## Averages by Environment\n")
    lines.append("| Env | Avg Accuracy (%) | Avg Tokens | Avg Cost ($) | Avg Duration (s) | Runs |")
    lines.append("|-----|-----------------|-----------|-------------|-----------------|------|")
    for env, stats in sorted(env_stats.items()):
        n = len(stats)
        avg_acc = sum(x["acc"] for x in stats if x["acc"] is not None) / max(1, sum(1 for x in stats if x["acc"] is not None))
        avg_tok = sum(x["tokens"] for x in stats if x["tokens"] is not None) / max(1, sum(1 for x in stats if x["tokens"] is not None))
        costs = [x["cost"] for x in stats if x["cost"] is not None]
        avg_cost = sum(costs) / len(costs) if costs else None
        elaps = [x["elapsed"] for x in stats if x["elapsed"] is not None]
        avg_el = sum(elaps) / len(elaps) if elaps else None
        lines.append(f"| {env} | {_fmt(avg_acc)} | {_fmt(avg_tok, '.0f')} | {_fmt(avg_cost, '.4f')} | {_fmt(avg_el, '.1f')} | {n} |")

    dest.write_text("\n".join(lines) + "\n")


REPORT_GENERATORS = {
    "bird": generate_bird_report,
    "humaneval": generate_humaneval_report,
    "mmlu": generate_mmlu_report,
}

ACCURACY_KEY = {
    "bird": "accuracy_total",
    "humaneval": "pass_at_1",
    "mmlu": "accuracy_total",
}


def generate_batch_report(batch_dir: Path, bench_keys: list[str]) -> None:
    """Generate the top-level batch report linking per-benchmark reports."""
    lines = [f"# Batch Benchmark Report\n"]
    lines.append(f"**Batch**: `{batch_dir.name}`\n")

    meta_path = batch_dir / "metadata.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        lines.append(f"- **Seeds**: {meta.get('seeds', '?')}")
        lines.append(f"- **Questions per seed**: {meta.get('n', '?')}")
        lines.append(f"- **Benchmarks**: {', '.join(meta.get('benchmarks', []))}")
        lines.append(f"- **Started**: {meta.get('started_at', '?')}")
        lines.append(f"- **Finished**: {meta.get('ended_at', '?')}")
        lines.append("")

    lines.append("## Summary\n")
    lines.append("| Benchmark | Env | Avg Accuracy/pass@1 (%) | Avg Cost ($) | Runs |")
    lines.append("|-----------|-----|------------------------|-------------|------|")

    for bk in bench_keys:
        raw_dir = batch_dir / bk / "raw"
        data = load_raw_results(raw_dir)
        acc_key = ACCURACY_KEY.get(bk, "accuracy_total")

        env_groups: dict[str, list] = {}
        for r in data:
            env = r.get("env", "?")
            s = r.get("summary", {})
            env_groups.setdefault(env, []).append(s)

        for env, summaries in sorted(env_groups.items()):
            accs = [s.get(acc_key) for s in summaries if s.get(acc_key) is not None]
            avg_acc = sum(accs) / len(accs) if accs else None
            costs = [_extract_cost(s) for s in summaries]
            costs = [c for c in costs if c is not None]
            avg_cost = sum(costs) / len(costs) if costs else None
            lines.append(
                f"| {bk} | {env} | {_fmt(avg_acc)} | {_fmt(avg_cost, '.4f')} | {len(summaries)} |"
            )

    lines.append("")
    lines.append("## Per-Benchmark Reports\n")
    for bk in bench_keys:
        lines.append(f"- [{bk}]({bk}/report.md)")
    lines.append("")
    lines.append("## Routing Configuration Snapshot\n")
    lines.append("- [config/](config/)")

    (batch_dir / "report.md").write_text("\n".join(lines) + "\n")


# ── main ─────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run BIRD/HumanEval/MMLU benchmarks in batch",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  python run_benchmarks.py --seeds 3 --n 30
  python run_benchmarks.py --seed-values 42,1337,9999 --n 50 --benchmarks bird,mmlu
  python run_benchmarks.py --seeds 2 --n 20 --envs env_routerly,env_anthropic_sonnet
""",
    )

    seed_group = parser.add_mutually_exclusive_group(required=True)
    seed_group.add_argument("--seeds", type=int, help="Number of random seeds to generate")
    seed_group.add_argument(
        "--seed-values",
        type=str,
        help="Comma-separated list of explicit seed values",
    )

    parser.add_argument("--n", type=int, required=True, help="Number of questions per seed per benchmark")
    parser.add_argument(
        "--benchmarks",
        type=str,
        default="bird,humaneval,mmlu",
        help="Comma-separated benchmark list (default: bird,humaneval,mmlu)",
    )
    parser.add_argument(
        "--envs",
        type=str,
        default=None,
        help="Comma-separated env filter, e.g. env_routerly,env_anthropic_sonnet (default: all .env_* found)",
    )
    parser.add_argument(
        "--bird-dir",
        type=str,
        default=str(ROOT / "BIRD" / "bird"),
        help="Path to BIRD dataset directory (default: ./BIRD/bird)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Delay between requests passed to benchmarks (default: 0)",
    )
    parser.add_argument(
        "--resume",
        type=str,
        default=None,
        help="Path to an existing batch folder to resume (skips completed runs)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # ── resolve seeds ────────────────────────────────────────────────────
    if args.seed_values:
        seeds = [int(s.strip()) for s in args.seed_values.split(",")]
    else:
        seeds = [random.randint(0, 2**31 - 1) for _ in range(args.seeds)]

    bench_keys = [b.strip().lower() for b in args.benchmarks.split(",")]
    for bk in bench_keys:
        if bk not in BENCHMARKS:
            print(f"✗ Unknown benchmark: {bk}. Available: {', '.join(BENCHMARKS)}")
            sys.exit(1)

    # ── env filter ───────────────────────────────────────────────────────
    env_filter = None
    if args.envs:
        env_filter = set()
        for e in args.envs.split(","):
            e = e.strip()
            if not e.startswith(".env_"):
                e = ".env_" + e.removeprefix("env_")
            env_filter.add(e)

    # ── batch folder ─────────────────────────────────────────────────────
    start_dt = datetime.now()
    if args.resume:
        batch_dir = Path(args.resume).resolve()
        if not batch_dir.exists():
            print(f"✗ Resume path does not exist: {batch_dir}")
            sys.exit(1)
        print(f"▶ Resuming batch: {batch_dir.name}")
        # Load existing metadata to recover seeds
        meta_path = batch_dir / "metadata.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            seeds = meta.get("seeds", seeds)
            print(f"  Recovered seeds from metadata: {seeds}")
    else:
        ts = start_dt.strftime("%Y%m%d_%H%M%S")
        batch_dir = RESULTS_ROOT / f"batch_{ts}"
        batch_dir.mkdir(parents=True, exist_ok=True)

    print(f"▶ Batch dir: {batch_dir}")
    print(f"  Seeds ({len(seeds)}): {seeds}")
    print(f"  Questions per seed: {args.n}")
    print(f"  Benchmarks: {', '.join(bench_keys)}")

    # ── snapshot config ──────────────────────────────────────────────────
    snapshot_config(batch_dir)

    # ── save metadata ────────────────────────────────────────────────────
    metadata = {
        "seeds": seeds,
        "n": args.n,
        "benchmarks": bench_keys,
        "envs_filter": list(env_filter) if env_filter else None,
        "bird_dir": args.bird_dir,
        "delay": args.delay,
        "started_at": start_dt.isoformat(),
        "ended_at": None,
    }
    meta_path = batch_dir / "metadata.json"
    meta_path.write_text(json.dumps(metadata, indent=2) + "\n")

    # ── run benchmarks ───────────────────────────────────────────────────
    run_log: list[dict] = []

    for bk in bench_keys:
        bench_info = BENCHMARKS[bk]
        bench_dir = bench_info["dir"]
        raw_dir = batch_dir / bk / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)

        # Discover envs for this benchmark
        all_envs = discover_envs(bench_dir)
        if env_filter:
            all_envs = [e for e in all_envs if e.name in env_filter]
        if not all_envs:
            print(f"\n⚠ No env files found for {bk} (filter: {env_filter})")
            continue

        extra_fn = bench_info["extra_args"]
        extra = extra_fn(args)

        print(f"\n{'='*60}")
        print(f"  BENCHMARK: {bk.upper()} — {len(all_envs)} envs × {len(seeds)} seeds")
        print(f"{'='*60}")

        for env_path in all_envs:
            for seed in seeds:
                print(f"\n  [{bk}] env={env_path.name} seed={seed}")
                status = run_single(bk, env_path, seed, args.n, raw_dir, extra, args.delay)
                run_log.append({
                    "benchmark": bk,
                    "env": env_path.name,
                    "seed": seed,
                    **status,
                })

    # ── generate reports ─────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("  GENERATING REPORTS")
    print(f"{'='*60}")

    for bk in bench_keys:
        raw_dir = batch_dir / bk / "raw"
        report_path = batch_dir / bk / "report.md"
        gen = REPORT_GENERATORS.get(bk)
        if gen:
            gen(raw_dir, report_path)
            print(f"  ✓ {bk}/report.md")

    # ── batch report ─────────────────────────────────────────────────────
    end_dt = datetime.now()
    metadata["ended_at"] = end_dt.isoformat()
    meta_path.write_text(json.dumps(metadata, indent=2) + "\n")

    generate_batch_report(batch_dir, bench_keys)
    print(f"  ✓ report.md")

    # ── save run log ─────────────────────────────────────────────────────
    (batch_dir / "run_log.json").write_text(json.dumps(run_log, indent=2) + "\n")

    # ── summary ──────────────────────────────────────────────────────────
    ok = sum(1 for r in run_log if r["status"] == "ok")
    skipped = sum(1 for r in run_log if r["status"] == "skipped")
    errors = sum(1 for r in run_log if r["status"] not in ("ok", "skipped"))
    elapsed = (end_dt - start_dt).total_seconds()

    print(f"\n{'='*60}")
    print(f"  DONE in {elapsed:.0f}s — {ok} ok, {skipped} skipped, {errors} errors")
    print(f"  Results: {batch_dir}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
