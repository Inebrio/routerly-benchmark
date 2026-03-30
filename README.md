# Routerly Demo — Benchmark Suite

A set of benchmarks to measure the impact of **Routerly**'s intelligent routing on quality, cost, and latency compared to calling LLM models directly.

---

## What this suite demonstrates

An AI router intercepts every request and assigns it to the most suitable model based on the estimated complexity of the task. The expected outcome is accuracy close to premium models at a significantly lower average cost, because simple requests are served by cheaper models.

The three benchmarks cover different dimensions:

| Benchmark | Task | Dataset | Main metric |
|---|---|---|---|
| [MMLU](./MMLU%20Benchmark/) | Multi-subject reasoning | MMLU (57 subjects, multiple choice) | Accuracy (%) |
| [HumanEval](./HumanEval/) | Python code generation | OpenAI HumanEval (164 problems) | pass@1 (%) |
| [BIRD](./BIRD/) | SQL generation on real databases | BIRD text-to-SQL (95 databases) | Execution accuracy (%) |

---

## Reference baselines (seed 42, n=30)

Latest comparative run results on reference models:

### MMLU — Accuracy

| Model | Accuracy |
|---|---|
| claude-sonnet-4-6 | 96.7% |
| claude-opus-4-6 | 93.3% |
| gpt-4.1-nano | 70.0% |

### HumanEval — pass@1

| Model | pass@1 |
|---|---|
| claude-sonnet-4-6 | 96.7% |
| claude-opus-4-6 | 86.7% |
| gpt-4.1-nano | 76.7% |

### BIRD — Execution Accuracy

| Model | Accuracy |
|---|---|
| claude-opus-4-6 | 66.7% |
| claude-sonnet-4-6 | 56.7% |
| gpt-4.1-nano | 36.7% |

---

## Repository structure

```
demo/
├── README.md                  # this file
├── MMLU Benchmark/
│   ├── benchmark.py
│   ├── README
│   ├── requirements.txt
│   ├── .env_*                 # credentials per target
│   └── results/               # JSON run outputs
├── HumanEval/
│   ├── benchmark.py
│   ├── README
│   ├── requirements.txt
│   ├── .env_*
│   └── results/
└── BIRD/
    ├── benchmark.py
    ├── README
    ├── requirements.txt
    ├── .env_*
    ├── bird/                  # BIRD dataset (manual download)
    └── results/
```

---

## Supported targets

Every benchmark is fully agnostic: it reads `BASE_URL`, `API_KEY`, and `MODEL` from a `.env` file and works identically against any OpenAI-compatible endpoint.

| Target | `.env` file |
|---|---|
| Routerly | `.env_routerly` |
| Anthropic Claude Opus | `.env_anthropic_opus` |
| Anthropic Claude Sonnet | `.env_anthropic_sonnet` |
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

Using the same `--seed` across all runs guarantees the same question subset — essential for valid comparisons.

```bash
# Terminal 1
python benchmark.py --env .env_routerly --seed 42

# Terminal 2
python benchmark.py --env .env_anthropic_opus --seed 42

# Terminal 3
python benchmark.py --env .env_anthropic_sonnet --seed 42

# Terminal 4
python benchmark.py --env .env_openai_41-nano --seed 42
```

Results are saved to `results/<timestamp>_<env_label>.json`.

---

## `.env` file conventions

Each subdirectory contains a ready-to-use `.env.example`. Copy it to the target you want to run:

```bash
cp .env.example .env_routerly          # Routerly router
cp .env.example .env_anthropic_opus    # Claude Opus direct
cp .env.example .env_anthropic_sonnet  # Claude Sonnet direct
cp .env.example .env_openai_41-nano    # GPT-4.1-nano direct
```

Then edit the copy with the correct `BASE_URL`, `API_KEY`, and `MODEL`:

| Target | `BASE_URL` | `MODEL` |
|---|---|---|
| Routerly | `https://api.routerly.ai/v1` | `auto` |
| Anthropic Claude Opus | `https://api.anthropic.com/v1` | `claude-opus-4-6` |
| Anthropic Claude Sonnet | `https://api.anthropic.com/v1` | `claude-sonnet-4-6` |
| OpenAI GPT-4.1-nano | `https://api.openai.com/v1` | `gpt-4.1-nano` |

Optional variables:

```env
SHOW_COST=true
PRICE_INPUT=15.0       # $/M input tokens
PRICE_OUTPUT=75.0      # $/M output tokens
REASONING_EFFORT=low   # low / medium / high (models that support it)
```

`.env_*` files **must not be committed** — they are already listed in `.gitignore`.
