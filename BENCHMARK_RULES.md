# Benchmark Rules and Settings

**Project**: Routerly — AI Inference Cost Optimization
**Version**: 1.1
**Date**: April 7, 2026

> Requirement levels follow [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119): **MUST** (mandatory), **SHOULD** (recommended), **MAY** (optional).

---

## 1. Goal

Find the best Routerly configuration for each benchmark — not to cheat, but to genuinely outperform direct model calls by making smarter routing decisions. Each project configuration MUST find the right balance to perform better than individual competitor models on accuracy, cost, or both.

Three benchmarks are evaluated:

| Benchmark | Task |
|-----------|------|
| **MMLU** | Multiple-choice questions across 57 academic subjects |
| **BIRD** | SQL generation on real-world databases |
| **HumanEval** | Python function generation validated by unit tests |

If a configuration cannot fully meet the primary objective, it MUST still offer a demonstrably better trade-off than the next-best option.

---

## 2. Success Criteria

The reference model order is: **Sonnet first, Opus second**. All comparisons are made against Sonnet. Opus is used only as a fallback when the primary objective is not met.

- The primary objective **MUST** be evaluated first:
  - Routerly MUST be within ±3 percentage points of Sonnet accuracy.
  - Routerly MUST cost less than Sonnet.
  - Routerly latency MAY exceed Sonnet but MUST NOT be excessively slower. A result where latency is consistently more than 3× Sonnet's average SHOULD be flagged and investigated.
  - All three conditions MUST hold simultaneously.
- If the primary objective is not met, the secondary objective **MUST** be evaluated:
  - Routerly MUST match or exceed Opus accuracy, OR
  - Routerly MUST cost less than Opus.
  - At least one of the two conditions MUST hold.
- Results **MUST** be validated across at least 10 different random seeds before any objective is considered passed.
- A Routerly configuration that routes **all requests to a single model** (excluding the routing model itself) **MUST NOT** be considered a valid result. Routing diversity is required.
- Configurations **MUST NOT** be tuned to exploit known sample content. Optimization MUST be based on routing logic and model selection, not on memorized answers.

---

## 3. Reference Models

Every benchmark run **MUST** include exactly these three model tiers:

| Tier | Model | Notes |
|------|-------|-------|
| **High** | `claude-opus-4-6` | MUST always be run — high accuracy ceiling |
| **Mid** | `claude-sonnet-4-6` | MUST always be run — primary reference to beat |
| **Low** | Project-defined | MUST be the least powerful model configured in the project |

The "low" model varies per project. It **MUST** be explicitly declared in the project configuration.

The primary comparison reference is `claude-sonnet-4-6`. `claude-opus-4-6` is the secondary reference, used only when the primary objective is not met.

---

## 4. Benchmarks

Three benchmarks are used, each testing a different capability:

| Benchmark | Task | Dataset size |
|-----------|------|--------------|
| **HumanEval** | Write Python functions that pass unit tests | 164 problems |
| **MMLU** | Answer multiple-choice questions across 57 subjects | 14,042 questions |
| **BIRD** | Write SQL queries for real-world databases | ~1,100 questions |

Benchmark scripts **MUST NOT** be modified between model runs. This guarantees a fair, apples-to-apples comparison.

---

## 5. Test Parameters

| Parameter | Value |
|-----------|-------|
| Questions per run (`n`) | 20 |
| Number of seeds | 10 |
| Seed values | 1952, 5235, 8234, 8386, 1682, 3659, 9848, 9119, 6892, 9381 — same seed for all models |
| Total questions per benchmark per model | 200 (10 seeds × 20 questions) |

The same seed set **MUST** be used across all model runs within a benchmark.

---

## 6. Campaign Phases

| Phase | Description |
|-------|-------------|
| **Phase 0** | Setup: retrieve available models, verify all endpoints respond correctly |
| **Phase 1** | Baselines (Sonnet + Opus): run on seed 1952, n=10 — establishes the reference scores |
| **Phase 2** | Tuning: iterate Routerly configurations separately for each routing policy variant — repeat until optimized |
| **Phase 3** | Final validation: top Routerly config per policy variant + low model on all 10 seeds, n=20 |

Phases **MUST** be run in order. Phase 2 **MUST** be repeated until the Routerly configuration meets the success criteria or no further improvement is observed. Phase 3 **MUST NOT** begin before Phase 2 produces a stable configuration.

**Baseline timing**:
- Sonnet and Opus **MUST** be run at the start (Phase 1) to establish reference scores before any Routerly tuning begins.
- The low model **MUST** be run at the end (Phase 3) to serve as the lower-bound comparison once the final Routerly configuration is known.

**Phase 2 — iterative tuning**:
- To save time and cost, Routerly configurations **MAY** be tested with a low `n` (e.g. n=5 or n=10) during early iterations.
- `n` **SHOULD** be gradually increased as the configuration stabilizes and results become more convincing.
- The final configuration **MUST** be validated at n=20 across all 10 seeds in Phase 3 before any conclusion is drawn.

---

## 7. Script Rules

- The benchmark script **MUST NOT** be modified except to fix a confirmed bug.
- A bug fix **MUST** be applied consistently across all benchmark scripts.
- Script changes **SHOULD** be tracked with a version note.

---

## 8. Model Discovery

Before any run, the available model list **MUST** be retrieved from the Routerly configuration files in the user's home directory:

| File | Contents |
|------|----------|
| `~/.routerly/config/models.json` | All configured models with provider, endpoint, and pricing |
| `~/.routerly/config/projects.json` | All Routerly projects and their policies |
| `~/.routerly/config/settings.json` | Global Routerly settings |

The current model list (non-Ollama) available for benchmarks:

| Model | Provider |
|-------|----------|
| `openai/gpt-5.2` | OpenAI |
| `openai/gpt-5-mini` | OpenAI |
| `openai/gpt-5-nano` | OpenAI |
| `openai/gpt-5.4-nano` | OpenAI |
| `openai/gpt-4.1` | OpenAI |
| `openai/gpt-4.1-mini` | OpenAI |
| `openai/gpt-4.1-nano` | OpenAI |
| `anthropic/claude-opus-4-6` | Anthropic |
| `anthropic/claude-sonnet-4-6` | Anthropic |
| `anthropic/claude-haiku-4-5` | Anthropic |
| `deepseek/deepseek-chat` | DeepSeek |
| `deepseek/deepseek-reasoner` | DeepSeek |

- Ollama models **MUST NOT** be used in benchmark runs.
- If additional Anthropic or OpenAI models are needed, they **MAY** be added autonomously by reusing the API key already present in `~/.routerly/config/models.json` for that provider.
- Any model added this way **MUST** follow the same format as the existing entries in `models.json`.

Model configuration files (`.env_*`) used by each benchmark script are located inside each project directory:

```
demo/BIRD/.env_*
demo/HumanEval/.env_*
demo/MMLU Benchmark/.env_*
```

When multiple Routerly policy variants are benchmarked for the same task, the environment files **MUST** be explicit and separate. Recommended naming:

```
.env_routerly_llm
.env_routerly_semantic
```

Using a single ambiguous `.env_routerly` for multiple policy variants **SHOULD** be avoided unless it is only a backward-compatible alias to one explicit variant.

---

## 9. Project Configuration

| Requirement | Level |
|-------------|-------|
| Each benchmark MUST have a dedicated Routerly project for each policy variant being evaluated | **MUST** |
| If both routing strategies are benchmarked, there MUST be two separate projects per benchmark: one `- LLM` and one `- Semantic` | **MUST** |
| Every project MUST use exactly one primary routing policy variant during a benchmark run | **MUST** |
| Every LLM project MUST use the LLM policy | **MUST** |
| Every Semantic project MUST use the semantic-intent policy | **MUST** |
| The LLM policy MAY define generic prompts | **MAY** |
| The LLM policy MAY define model-specific prompts | **MAY** |
| The semantic-intent policy MAY define curated example sets and thresholds | **MAY** |
| Every project MUST include Sonnet (MAY be inactive) | **MUST** |
| Every project MUST include Ollama (MAY be inactive) | **MUST** |
| Additional policies MAY be enabled per project | **MAY** |
| Any model MAY be used for routing decisions | **MAY** |
| A project MAY use as many models as needed | **MAY** |

Policy provenance **MUST** be unambiguous in the saved results. This can be achieved either by separate project/token pairs or by explicit environment-file naming. A report **MUST NOT** compare two Routerly result sets unless their policy provenance can be determined from project configuration or Routerly trace data.

---

## 10. Metrics

Every run **MUST** produce two outputs:

| Output | Description |
|--------|-------------|
| **Stdout** | Live progress and summary printed during execution |
| **JSON report** | Full result saved to `./results/<timestamp>_<env_label>.json` |

Both outputs **MUST** include the following metrics per round:

| Metric | Description |
|--------|-------------|
| `accuracy` | Pass rate (%) |
| `passed` / `failed` | Absolute counts |
| `duration_s` | Total run duration in seconds |
| `tokens_total` | Total tokens consumed |
| `tokens_input` | Input tokens |
| `tokens_output` | Output tokens (visible) |
| `tokens_reasoning` | Reasoning tokens (subset of output, if applicable) |
| `tok_per_s` | Average throughput (tokens/s) |
| `ttft_min_ms` | Minimum time to first token |
| `ttft_max_ms` | Maximum time to first token |
| `ttft_avg_ms` | Average time to first token |
| `cost_total` | Total estimated cost — **MUST include routing model cost** |
| `cost_input` | Cost attributed to input tokens |
| `cost_output` | Cost attributed to output tokens |

For Routerly runs, `cost_total` **MUST** account for all models involved in a request: the routing model and the final model. Reporting only the final model cost is not valid.

Cost fields **SHOULD** be omitted when pricing data is not available for the model.

---

## 11. Output Structure

At the end of a full benchmark campaign, a `Benchmark_Report/` folder **MUST** be produced with the following structure:

```
Benchmark_Report/
├── REPORT.md           # Detailed analysis of all results (see below)
├── raw_results/        # All JSON files produced by benchmark runs, including direct-model and Routerly runs
└── logs/               # Any execution logs
```

`raw_results/` **MUST** contain the complete raw archive used to support the report:

- all direct-model baseline runs (for example Sonnet, Opus, low model)
- all final Routerly LLM-policy runs
- all final Routerly semantic-intent runs
- any additional raw files explicitly cited in the report

Direct-model baseline runs **MUST NOT** be omitted from `raw_results/`.

### REPORT.md

`REPORT.md` **MUST** include:

- Executive summary with a comparative table (accuracy, cost, delta vs. Sonnet) across all benchmarks
- Methodology section: setup, success criteria, campaign structure
- Phase 1 baseline results for all models
- Phase 2 configuration iterations with rationale
- Phase 3 final results per seed and averaged, for all models and for each Routerly policy variant
- Comparative analysis against Sonnet and Opus
- Final configurations used
- Recommendations

If more than one Routerly policy variant is evaluated, `REPORT.md` **MUST** clearly distinguish them and **MUST** identify which raw result files belong to each variant.

`REPORT.md` **MUST** be written in English.

---

## 12. Documentation Reference

When in doubt about Routerly behavior, configuration, or concepts, refer to the official documentation:

```
/Users/carlosatta/Documents/lavoro/inebrio/routerly.ai/code/docs
```

- The documentation **SHOULD** be consulted before making assumptions about Routerly's routing logic, policy system, or project structure.
- The documentation **MAY** be outdated. If a discrepancy is found between the docs and observed behavior, observed behavior takes precedence and the discrepancy **SHOULD** be noted.

---

## 13. Batch Runner

The unified batch runner (`run_benchmarks.py`) automates full benchmark campaigns across multiple seeds and environments.

### Rules

1. All batch outputs **MUST** be stored under `results/batch_<timestamp>/`. This directory is gitignored; only `results/.gitkeep` is tracked.
2. Each batch **MUST** produce:
   - `metadata.json` — seeds, parameters, timestamps.
   - `config/` — snapshot of the Routerly routing configuration at the time of the run.
   - Per-benchmark subdirectory (`bird/`, `humaneval/`, `mmlu/`) with `raw/` JSON files and a `report.md`.
   - A top-level `report.md` summarizing all benchmarks.
3. The runner uses `--output-dir` to direct each benchmark's output into the batch folder. Individual benchmark scripts remain backward-compatible (default behavior unchanged when `--output-dir` is omitted).
4. Results **MUST** include the seed in the filename (prefix `seed<N>_`) to enable resume and traceability.
5. Resume support: the runner **MUST** skip any (benchmark, env, seed) combination whose output file already exists.
6. Sequential execution is **REQUIRED** to avoid rate-limit interference between concurrent runs.
7. The `--envs` filter applies per benchmark: only `.env_*` files matching the filter in each benchmark directory are used.
8. On subprocess failure the runner **MUST** log the error and continue with remaining runs.
