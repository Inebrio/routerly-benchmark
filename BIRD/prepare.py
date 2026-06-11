#!/usr/bin/env python3
"""
Routerly Benchmark — BIRD — Preparation script

Runs in sequence:
  1. Check Python dependencies
  2. Validate the .env files present
  3. Download (if missing) and validate the BIRD mini-dev dataset
  4. End-to-end smoke test on a synthetic dataset (no API calls)

Usage:
    python prepare.py                         # check (and download if needed)
    python prepare.py --bird-dir ./bird       # specify where to put the dataset
    python prepare.py --smoke-only            # smoke test only (no API, no download)
"""

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
import re
import urllib.request
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Colors / output without external dependencies
# ---------------------------------------------------------------------------

RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
CYAN   = "\033[36m"
DIM    = "\033[2m"


def ok(msg: str)   -> None: print(f"  {GREEN}✓{RESET}  {msg}")
def warn(msg: str) -> None: print(f"  {YELLOW}⚠{RESET}  {msg}")
def err(msg: str)  -> None: print(f"  {RED}✗{RESET}  {msg}")
def info(msg: str) -> None: print(f"  {CYAN}→{RESET}  {msg}")
def sep(title: str = "") -> None:
    line = "─" * 54
    if title:
        pad = (54 - len(title) - 2) // 2
        print(f"\n{DIM}{'─'*pad} {BOLD}{title}{RESET}{DIM} {'─'*(54-pad-len(title)-2)}{RESET}")
    else:
        print(f"{DIM}{line}{RESET}")


# ---------------------------------------------------------------------------
# 1. Dependencies
# ---------------------------------------------------------------------------

def check_dependencies() -> bool:
    sep("Dependencies")
    all_ok = True

    # Python version
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 10):
        ok(f"Python {major}.{minor}")
    else:
        err(f"Python {major}.{minor} — required ≥ 3.10")
        all_ok = False

    # sqlite3 (stdlib)
    try:
        import sqlite3 as _s
        ok(f"sqlite3 {_s.sqlite_version}")
    except ImportError:
        err("sqlite3 not available")
        all_ok = False

    # External packages
    packages = {"openai": "openai", "dotenv": "python-dotenv", "rich": "rich"}
    for module, pkg in packages.items():
        try:
            __import__(module)
            ok(pkg)
        except ImportError:
            err(f"{pkg} not installed — run: pip install -r requirements.txt")
            all_ok = False

    return all_ok


# ---------------------------------------------------------------------------
# 2. .env files
# ---------------------------------------------------------------------------

def check_env_files() -> bool:
    sep(".env Files")
    env_files = sorted(Path(".").glob(".env_*"))

    if not env_files:
        err("No .env file found in the current directory")
        info("Create at least one .env file with BASE_URL, API_KEY, MODEL")
        return False

    all_ok = True
    required = ("BASE_URL", "API_KEY", "MODEL")

    for ef in env_files:
        lines = ef.read_text().splitlines()
        cfg = {}
        for line in lines:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()

        missing = [k for k in required if not cfg.get(k)]
        if missing:
            err(f"{ef.name}  — missing variables: {', '.join(missing)}")
            all_ok = False
        else:
            model = cfg["MODEL"]
            url   = cfg["BASE_URL"]
            ok(f"{ef.name}  →  model={model}  url={url}")

    return all_ok


# ---------------------------------------------------------------------------
# 3. Dataset BIRD
# ---------------------------------------------------------------------------

DIFFICULTIES = ("simple", "moderate", "challenging")

# URL of the complete BIRD mini-dev package (SQLite, ~400 MB)
MINIDEV_URL = "https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip"


# ---------------------------------------------------------------------------
# Automatic download
# ---------------------------------------------------------------------------

def _progress_hook(block_num: int, block_size: int, total_size: int) -> None:
    downloaded = block_num * block_size
    if total_size > 0:
        pct = min(downloaded / total_size * 100, 100)
        mb_done = downloaded / 1_048_576
        mb_total = total_size / 1_048_576
        bar = "█" * int(pct / 2) + "░" * (50 - int(pct / 2))
        print(f"\r  {CYAN}↓{RESET}  [{bar}] {pct:5.1f}%  {mb_done:.0f}/{mb_total:.0f} MB", end="", flush=True)
    else:
        mb_done = downloaded / 1_048_576
        print(f"\r  {CYAN}↓{RESET}  {mb_done:.0f} MB downloaded...", end="", flush=True)


def download_bird_dataset(bird_dir: Path) -> bool:
    """Download the BIRD mini-dev dataset and install it in bird_dir."""
    sep("Download BIRD mini-dev dataset")
    info(f"URL: {MINIDEV_URL}")
    info(f"Destinazione: {bird_dir.resolve()}")
    print()

    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = Path(tmpdir) / "minidev.zip"

        # Download
        try:
            urllib.request.urlretrieve(MINIDEV_URL, zip_path, reporthook=_progress_hook)
            print()  # newline dopo la progress bar
        except Exception as e:
            print()
            err(f"Download failed: {e}")
            return False

        ok(f"Download complete ({zip_path.stat().st_size / 1_048_576:.0f} MB)")

        # Extraction
        info("Extracting archive...")
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(tmpdir)
        except Exception as e:
            err(f"Extraction failed: {e}")
            return False

        ok("Archive extracted")

        # Find mini_dev_sqlite.json in the extracted content
        extracted = Path(tmpdir)
        json_candidates = list(extracted.rglob("mini_dev_sqlite.json"))
        if not json_candidates:
            err("mini_dev_sqlite.json not found in the archive")
            return False

        json_src = json_candidates[0]
        data_root = json_src.parent  # folder containing json + dev_databases
        info(f"Data found in: {data_root}")

        # Find dev_databases/
        db_root_candidates = list(data_root.glob("dev_databases"))
        if not db_root_candidates:
            # try one level up
            db_root_candidates = list(data_root.parent.glob("dev_databases"))
        if not db_root_candidates:
            err("Folder dev_databases/ not found in the archive")
            return False
        db_src = db_root_candidates[0]

        # Create bird_dir and copy
        bird_dir.mkdir(parents=True, exist_ok=True)

        dest_json = bird_dir / "dev.json"
        shutil.copy2(json_src, dest_json)
        ok(f"dev.json → {dest_json}")

        dest_db = bird_dir / "dev_databases"
        if dest_db.exists():
            shutil.rmtree(dest_db)
        shutil.copytree(db_src, dest_db)
        db_count = sum(1 for d in dest_db.iterdir() if d.is_dir())
        ok(f"dev_databases/ → {dest_db}  ({db_count} database)")

    return True


def check_bird_dataset(bird_dir: Path) -> bool:
    sep("BIRD Dataset")

    if not bird_dir.is_dir():
        warn(f"Directory not found: {bird_dir}")
        info("Downloading BIRD mini-dev dataset automatically...")
        if not download_bird_dataset(bird_dir):
            err("Download failed — install the dataset manually:")
            info("  https://bird-bench.github.io/")
            info(f"  Expected structure: {bird_dir}/dev.json + {bird_dir}/dev_databases/")
            return False
        sep("Verifying downloaded dataset")

    ok(f"Directory found: {bird_dir.resolve()}")

    # dev.json
    dev_json = bird_dir / "dev.json"
    if not dev_json.exists():
        err("dev.json not found")
        return False

    try:
        with open(dev_json, encoding="utf-8") as f:
            questions = json.load(f)
    except Exception as e:
        err(f"dev.json not readable: {e}")
        return False

    ok(f"dev.json  →  {len(questions):,} total questions")

    # Count by difficulty
    diff_counts: dict[str, int] = {}
    for q in questions:
        d = q.get("difficulty", "unknown")
        diff_counts[d] = diff_counts.get(d, 0) + 1
    for d in DIFFICULTIES:
        if d in diff_counts:
            info(f"  {d}: {diff_counts[d]}")

    # dev_databases/
    db_root = bird_dir / "dev_databases"
    if not db_root.is_dir():
        err("dev_databases/ not found")
        return False

    db_ids_in_json = set(q["db_id"] for q in questions)
    db_dirs = [d for d in db_root.iterdir() if d.is_dir()]
    db_ids_on_disk = set(d.name for d in db_dirs)

    missing_dbs = db_ids_in_json - db_ids_on_disk
    sqlite_missing = []
    for db_dir in db_dirs:
        sqlite_file = db_dir / f"{db_dir.name}.sqlite"
        if not sqlite_file.exists():
            sqlite_missing.append(db_dir.name)

    ok(f"dev_databases/  →  {len(db_dirs)} databases found")

    if missing_dbs:
        warn(f"{len(missing_dbs)} databases referenced in dev.json but missing on disk")
        for db in sorted(missing_dbs)[:5]:
            info(f"  missing: {db}")
        if len(missing_dbs) > 5:
            info(f"  ... and {len(missing_dbs)-5} more")
    else:
        ok("All referenced databases are present")

    if sqlite_missing:
        warn(f"{len(sqlite_missing)} folders without .sqlite file")
    else:
        ok("All .sqlite files are present")

    # Prova ad aprire un database a campione
    sample_db = next(
        (db_root / db_id / f"{db_id}.sqlite"
         for db_id in sorted(db_ids_on_disk)[:1]),
        None
    )
    if sample_db and sample_db.exists():
        try:
            conn = sqlite3.connect(str(sample_db))
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in cur.fetchall()]
            conn.close()
            ok(f"SQLite OK ({sample_db.parent.name}: {len(tables)} tabelle)")
        except Exception as e:
            err(f"SQLite open error: {e}")
            return False

    return True


# ---------------------------------------------------------------------------
# 4. End-to-end smoke test (synthetic dataset, no API)
# ---------------------------------------------------------------------------

def run_smoke_test() -> bool:
    sep("Smoke test (synthetic pipeline, no API)")

    # Importa le funzioni del benchmark
    try:
        import importlib.util, sys as _sys
        spec = importlib.util.spec_from_file_location(
            "benchmark_bird", Path(__file__).parent / "benchmark.py"
        )
        bm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bm)
    except Exception as e:
        err(f"Cannot import benchmark.py: {e}")
        return False

    ok("benchmark.py importato")

    # Crea un database SQLite temporaneo
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db_path = tmp / "test.sqlite"

        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE employees (
                id   INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                dept TEXT,
                salary REAL
            )
        """)
        conn.executemany(
            "INSERT INTO employees VALUES (?,?,?,?)",
            [
                (1, "Alice",   "Engineering", 90000),
                (2, "Bob",     "Marketing",   70000),
                (3, "Carol",   "Engineering", 95000),
                (4, "Dave",    "HR",          65000),
                (5, "Eve",     "Engineering", 88000),
            ],
        )
        conn.commit()
        conn.close()
        ok("Synthetic database created (5 rows)")

        # Test extract_schema
        schema = bm.extract_schema(db_path)
        assert "employees" in schema, "Schema non contiene la tabella"
        assert "salary" in schema.lower(), "Schema non contiene la colonna salary"
        ok("extract_schema  ✓")

        # Test extract_sql
        cases = [
            ("SELECT * FROM employees", "SELECT * FROM employees"),
            ("```sql\nSELECT id FROM employees\n```", "SELECT id FROM employees"),
            ("```\nSELECT id FROM employees\n```", "SELECT id FROM employees"),
            ("Here is the query:\n\nSELECT id FROM employees", "SELECT id FROM employees"),
        ]
        for raw, expected in cases:
            result = bm.extract_sql(raw)
            assert expected in result or result == expected, \
                f"extract_sql({raw!r}) = {result!r}, expected {expected!r}"
        ok("extract_sql  ✓")

        # Test execute_sql + normalize_result
        gold_sql = "SELECT name FROM employees WHERE dept = 'Engineering' ORDER BY salary DESC"
        pred_sql = "SELECT name FROM employees WHERE dept = 'Engineering'"

        rows_gold, err_gold = bm.execute_sql(db_path, gold_sql)
        assert rows_gold is not None, f"Gold SQL failed: {err_gold}"
        assert len(rows_gold) == 3
        ok("execute_sql (gold)  ✓")

        rows_pred, err_pred = bm.execute_sql(db_path, pred_sql)
        assert rows_pred is not None, f"Pred SQL failed: {err_pred}"
        ok("execute_sql (pred)  ✓")

        # evaluate_sql — identical query → correct
        correct, error = bm.evaluate_sql(db_path, gold_sql, gold_sql)
        assert correct, f"Identical query not evaluated as correct: {error}"
        ok("evaluate_sql (identical → correct)  ✓")

        # evaluate_sql — query diversa ma stesso risultato → correct
        pred_same = "SELECT name FROM employees WHERE dept = 'Engineering' ORDER BY name"
        # Results are the same after normalize (sort), so it depends on the data.
        # Use a certain case: COUNT
        gold_count = "SELECT COUNT(*) FROM employees WHERE dept = 'Engineering'"
        pred_count = "SELECT COUNT(*) FROM employees WHERE dept = 'Engineering'"
        correct2, _ = bm.evaluate_sql(db_path, pred_count, gold_count)
        assert correct2
        ok("evaluate_sql (count equal → correct)  ✓")

        # evaluate_sql — wrong query → not correct
        wrong_sql = "SELECT name FROM employees WHERE dept = 'HR'"
        correct3, _ = bm.evaluate_sql(db_path, wrong_sql, gold_sql)
        assert not correct3
        ok("evaluate_sql (wrong → not correct)  ✓")

        # evaluate_sql — invalid SQL → error, not correct
        correct4, err4 = bm.evaluate_sql(db_path, "NOT VALID SQL !!!!", gold_sql)
        assert not correct4
        assert err4 != ""
        ok(f"evaluate_sql (invalid SQL → error caught)  ✓")

        # build_prompt
        q = {
            "question": "How many employees work in Engineering?",
            "evidence": "dept is the department column",
            "sql_gold": gold_sql,
            "difficulty": "simple",
        }
        prompt = bm.build_prompt(q, schema)
        assert "employees" in prompt
        assert "How many" in prompt
        assert "dept is the department column" in prompt
        ok("build_prompt  ✓")

    sep()
    ok("Smoke test complete — pipeline working")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Routerly BIRD — environment preparation")
    parser.add_argument(
        "--bird-dir",
        default="./bird",
        help="Path to the BIRD folder (default: ./bird). Downloaded automatically if missing.",
    )
    parser.add_argument(
        "--smoke-only",
        action="store_true",
        help="Run only the synthetic smoke test, without downloading or verifying the real dataset",
    )
    args = parser.parse_args()

    print(f"\n{BOLD}Routerly Benchmark — BIRD — Setup{RESET}\n")

    results = []

    if not args.smoke_only:
        results.append(("Dependencies",  check_dependencies()))
        results.append((".env Files",    check_env_files()))
        results.append(("BIRD Dataset",  check_bird_dataset(Path(args.bird_dir))))

    results.append(("Smoke test",    run_smoke_test()))

    # Final summary
    sep("Summary")
    all_passed = True
    for name, passed in results:
        if passed:
            ok(name)
        else:
            err(name)
            all_passed = False

    print()
    if all_passed:
        print(f"  {GREEN}{BOLD}All done.{RESET}  You can run the benchmark:\n")
        bird_dir = args.bird_dir
        print(f"    python benchmark.py --env .env_routerly --bird-dir {bird_dir} --n 30 --seed 42")
        print(f"    python benchmark.py --env .env_anthropic_opus --bird-dir {bird_dir} --n 30 --seed 42")
        print()
        sys.exit(0)
    else:
        print(f"  {RED}{BOLD}Incomplete setup.{RESET}  Fix the errors reported above.\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
