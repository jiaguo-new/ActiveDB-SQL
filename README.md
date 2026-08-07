# ActiveDB-SQL

BIRD-SQL dev: EX = 1230/1534 = 80.18%

A multi-stage Text-to-SQL pipeline combining train-finetuned candidate
generators, ORM selection, 6-layer DB-active agent repair, tournament
reselection, and preference-guided regeneration.

---

## Environment

- Python 3.13
- CUDA 12.2 or 12.3
- See requirements.txt for package dependencies
- vLLM for GPU inference (separate conda env recommended)

## Setup

```bash
# API key for GLM-5.2
export GLM_API_KEY="your-key"

# Install dependencies
pip install -r requirements.txt

# Model weights (download from HuggingFace / ModelScope)
# - Qwen3-14B: modelscope/Qwen/Qwen3-14B
# - Qwen2.5-Coder-32B-Instruct: modelscope/Qwen/Qwen2.5-Coder-32B-Instruct
# - qwen3-14b-sqlplus-merged: huggingface/jiaguo/qwen3-14b-sqlplus-merged
# - omnisql-14b-bird-continue: huggingface/jiaguo/omnisql-14b-bird-continue
# - qwen3-14b-orm-v2-merged-bf16: huggingface/jiaguo/qwen3-14b-orm-v2-merged-bf16
```

## Resource Requirements

GPU: 1x A100 80G, ~3 hours (candidate generation + ORM scoring)
API: GLM-5.2, ~30M prompt tokens
If GPU is unavailable, pre-generated candidate pools in runs/ allow API-only
execution (~2-4 hours, no GPU).

## Pipeline (10 stages)

```
Stage 1: Candidate pool (4 models x 8 shots) -> ORM band selection -> 1067
Stage 2: E3v value grounding (WHERE literal repair) -> 1071
Stage 3: E4 execution repair (GLM rewrite) -> 1073
Stage 4: E2 JOIN repair (FK graph) -> 1077
Stage 5: E3c column grounding (SELECT semantic match) -> 1101
Stage 6: E3v+ enhanced probe + E5det rules -> 1106
Stage 7: Route A tournament (GLM pairwise judge) -> 1158
Stage 8: Multi-generator extension -> 1178
Stage 9: Deep regeneration (GLM from scratch + 3-round repair) -> 1202
Stage 10: Preference-guided generation + self-critique -> 1235
Leak-fix: 6 k5-sensitive questions reverted -> 1230 (80.18%)
```

## GPU Code vs API Code

GPU scripts (vLLM, need A100):
- scripts/gen_candidates_local_vllm.py
- scripts/score_candidates_with_orm_v2_vllm.py
- scripts/build_pool_from_candidates.py

API scripts (GLM-5.2, no GPU):
- scripts/select_compliant_merged4.py
- scripts/run_e3v_parallel.py
- scripts/run_e4_repair_parallel.py
- scripts/run_e2_join_repair_parallel.py
- scripts/run_e3c_parallel.py
- scripts/run_e3v_enhanced_parallel.py
- scripts/run_e5_det_repair_parallel.py
- scripts/run_route_a_reselect.py
- scripts/run_deep_regen_parallel.py
- scripts/run_e6_preference_parallel.py
- scripts/run_e7_critique_parallel.py

## Execution

```bash
# Full pipeline (GPU + API)
export GLM_API_KEY="your-key"
bash run_all.sh

# API-only (using pre-generated candidate pools in runs/)
# Skip GPU steps, start from Stage 2 in run_all.sh

# Verify dev result
cd evaluation
python bird_official_eval_fast.py \
  --dev ../dev.json \
  --pred ../predictions/final_1235.jsonl \
  --db-root ../dev_databases \
  --output /tmp/verify.json --workers 8
```

## Logging and Error Handling

Every stage runner is resume-safe (checkpointed by question_id). If
interrupted, re-running the same command resumes from where it stopped.
Each GLM call has a per-call timeout (90-180s) with retry logic.

## column_meaning.json

Not required. The system uses database DDL directly.

## Model Training Data

All LoRA adapters and ORM trained only on BIRD train split (9428 examples,
0 overlap with dev set).

## Compliance

- No dev gold SQL in any prompt / RAG / training
- All prompt templates contain no gold placeholders
- Agent scripts do not read dev gold into prompts
- Method does not rely on ground truth SQL
- Candidate pool is physically k5-free
- Prediction file verified, no manual edits

## Directory Structure

```
agents/          6 DB-active probing modules (no LLM calls)
scripts/         13 stage runners + utilities
prompts/         7 prompt templates
configs/         10 YAML configs
evaluation/      BIRD official fast evaluator
tools/           BirdDatabase + LLMClient
predictions/     Final dev predictions
runs/            Pre-generated candidate pools (ORM-scored)
run_all.sh       End-to-end pipeline script
merge_chain.py   Fail-only overlay merge tool
requirements.txt Python dependencies
```
