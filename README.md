# Routerly — Benchmark Suite

Sending every request to the most powerful model is the easiest approach — but rarely the smartest one. **[Routerly](https://blog.routerly.ai/introducing-routerly)** is a self-hosted LLM gateway that routes each request to the right model based on task complexity: routine queries go to fast, cheap models; genuinely hard ones reach premium tiers.

This repository is the open benchmark suite that measures whether routing actually delivers on that promise — across reasoning, code generation, and structured data tasks.

> **"The right metric is quality per dollar."**
> Routerly achieves near-frontier accuracy on code at one-third the cost of running Opus 4.8 directly. [See the latest numbers →](https://blog.routerly.ai/routerly-vs-fable-5-opus-4-8)

---

## Latest results

Results are published on the blog as new models and routing policies are benchmarked:

| Article | What it covers |
|---|---|
| [Routerly vs Fable 5 and Opus 4.8](https://blog.routerly.ai/routerly-vs-fable-5-opus-4-8) | How routing holds up against the two newest frontier models |
| [1,000 questions per model: BIRD caught up to Sonnet](https://blog.routerly.ai/routerly-benchmark-1000-questions) | Large-scale text-to-SQL — routing now matches Sonnet within −0.7 pp |
| [We ran 200 questions per model](https://blog.routerly.ai/we-ran-200-questions-per-model) | First large batch: routing matched top-model accuracy while cutting costs up to 69% |
| [LLM routing policies work: what three benchmarks confirm](https://blog.routerly.ai/benchmark-results-humaneval-mmlu-bird) | Validation across all three benchmarks in this suite |
| [Measuring Routerly: MMLU, HumanEval, and BIRD](https://blog.routerly.ai/routerly-benchmark-suite) | Introduction to the suite and what each benchmark measures |

---

## What this suite measures

Three benchmarks covering the workloads most relevant to real production usage:

| Benchmark | Task | Dataset | Main metric |
|---|---|---|---|
| [MMLU](./MMLU%20Benchmark/) | Multi-subject reasoning | MMLU (57 subjects, multiple choice) | Accuracy (%) |
| [HumanEval](./HumanEval/) | Python code generation | OpenAI HumanEval (164 problems) | pass@1 (%) |
| [BIRD](./BIRD/) | SQL generation on real databases | BIRD text-to-SQL (95 databases) | Execution accuracy (%) |

Every benchmark is **fully agnostic**: it reads `BASE_URL`, `API_KEY`, and `MODEL` from a `.env` file and works identically against Routerly, Anthropic, OpenAI, or any OpenAI-compatible endpoint. The central question it answers: does routing track the best model in the pool, or does it regress toward the cheapest?

---

## Repository structure

```
demo/
├── README.md                  # this file
├── BENCHMARK_RULES.md         # evaluation rules and success criteria
├── run_benchmarks.py          # batch runner (multi-seed, multi-env)
├── Benchmark_Report/          # analysis and manual reports
│   └── REPORT.md
├── results/                   # batch outputs (gitignored)
│   └── batch_YYYYMMDD_HHMMSS/
├── MMLU Benchmark/
│   ├── benchmark.py
│   ├── requirements.txt
│   └── .env_*                 # credentials per target (gitignored)
├── HumanEval/
│   ├── benchmark.py
│   ├── requirements.txt
│   └── .env_*
└── BIRD/
    ├── benchmark.py
    ├── requirements.txt
    ├── .env_*
    └── bird/                  # BIRD dataset (manual download)
```

---

## Supported targets

| Target | `.env` file |
|---|---|
| Routerly | `.env_routerly` |
| Anthropic Claude Fable 5 | `.env_anthropic_fable-5` |
| Anthropic Claude Opus 4.8 | `.env_anthropic_opus-4-8` |
| Anthropic Claude Opus 4.6 | `.env_anthropic_opus` |
| Anthropic Claude Sonnet 4.6 | `.env_anthropic_sonnet` |
| OpenAI GPT-4.1-nano | `.env_openai_41-nano` |

---

## Quick setup

Each benchmark has its own virtualenv. Example for MMLU:

```bash
cd "MMLU Benchmark"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Running a comparison

Use the **batch runner** to compare multiple models across multiple seeds in one command:

```bash
# 3 random seeds, 30 questions each, all envs
python run_benchmarks.py --seeds 3 --n 30

# Explicit seeds, only HumanEval and MMLU, two specific envs
python run_benchmarks.py --seed-values 42,1337,9999 --n 30 --benchmarks humaneval,mmlu --envs env_routerly,env_anthropic_sonnet

# Resume an interrupted batch
python run_benchmarks.py --seeds 3 --n 30 --resume results/batch_20260402_150000
```

Using the same `--seed` across runs guarantees the same question subset — essential for valid comparisons.

### CLI arguments

| Argument | Required | Default | Description |
|---|---|---|---|
| `--seeds N` | yes* | — | Number of random seeds to generate |
| `--seed-values V` | yes* | — | Comma-separated explicit seed values |
| `--n N` | yes | — | Questions per seed per benchmark |
| `--benchmarks` | no | `bird,humaneval,mmlu` | Comma-separated benchmark filter |
| `--envs` | no | all `.env_*` found | Comma-separated env filter |
| `--bird-dir` | no | `./BIRD/bird` | Path to BIRD dataset |
| `--delay` | no | `0` | Delay between requests (seconds) |
| `--resume` | no | — | Path to existing batch to resume |

\* `--seeds` and `--seed-values` are mutually exclusive; one is required.

### Output structure

```
results/
└── batch_YYYYMMDD_HHMMSS/
    ├── report.md              # batch-level summary
    ├── metadata.json          # seeds, params, timestamps
    ├── run_log.json           # per-run status log
    ├── config/                # Routerly routing config snapshot (secrets redacted)
    ├── bird/
    │   ├── report.md
    │   └── raw/
    ├── humaneval/
    │   ├── report.md
    │   └── raw/
    └── mmlu/
        ├── report.md
        └── raw/
```

Results are gitignored — only the aggregated `report.md` per batch matters for comparisons.

---

## `.env` file conventions

Each subdirectory contains a ready-to-use `.env.example`. Copy it to the target you want to run:

```bash
cp .env.example .env_routerly
cp .env.example .env_anthropic_fable-5
```

Then edit the copy with the correct `BASE_URL`, `API_KEY`, and `MODEL`:

| Target | `BASE_URL` | `MODEL` |
|---|---|---|
| Routerly | `https://api.routerly.ai/v1` | `auto` |
| Anthropic Claude Fable 5 | `https://api.anthropic.com/v1` | `claude-fable-5` |
| Anthropic Claude Opus 4.8 | `https://api.anthropic.com/v1` | `claude-opus-4-8` |
| Anthropic Claude Opus 4.6 | `https://api.anthropic.com/v1` | `claude-opus-4-6` |
| Anthropic Claude Sonnet 4.6 | `https://api.anthropic.com/v1` | `claude-sonnet-4-6` |
| OpenAI GPT-4.1-nano | `https://api.openai.com/v1` | `gpt-4.1-nano` |

Known models (`claude-fable-5`, `claude-opus-4-8`, `claude-opus-4-6`, `claude-sonnet-4-6`, `gpt-4.1-nano`) have built-in pricing and always report cost. For unknown models, add overrides:

```env
PRICE_INPUT=15.0       # $/M input tokens
PRICE_OUTPUT=75.0      # $/M output tokens
REASONING_EFFORT=low   # low / medium / high (models that support it)
```

`.env_*` files **must not be committed** — they are already listed in `.gitignore`.
