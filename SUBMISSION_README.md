# ActiveDB-SQL — BIRD-SQL Official Test Submission

**System**: ActiveDB-SQL (Combined Models: open-source LLMs + GLM-5.2 API)
**BIRD dev result**: EX = 1230/1534 = **80.18%** (official fast evaluator, leak-fixed, k5 audit PASS)
**Submission type**: Type 4 (Combined Models)
**Contact**: [your email]

---

## 1. System Overview

ActiveDB-SQL is a multi-stage Text-to-SQL pipeline combining:
- **4 train-finetuned models** (14B/32B) for candidate generation
- **ORM v2** (14B) for candidate scoring
- **GLM-5.2 API** for agent repair, tournament judging, and deep regeneration
- **6-layer DB-active agent harness** (read-only DB probing)

All models trained **only on BIRD train split** (9428 examples, 0 overlap with dev).

---

## 2. Pipeline (10 stages)

```
Stage 1: Candidate pool (4 models × 8 shots) → ORM v2 band selection → 1067
Stage 2: E3v value grounding (WHERE literal repair) → 1071
Stage 3: E4 execution repair (GLM rewrite) → 1073
Stage 4: E2 JOIN repair (FK graph) → 1077
Stage 5: E3c column grounding (SELECT semantic match) → 1101
Stage 6: E3v+ enhanced probe + E5det rules → 1106
Stage 7: Route A tournament (GLM pairwise judge) → 1158
Stage 8: Multi-generator extension (+Qwen3-sqlplus +OmniSQL) → 1178
Stage 9: Deep regeneration (GLM from scratch + 3-round repair) → 1202
Stage 10: Preference-guided generation + self-critique → 1235
Leak-fix: 6 k5-sensitive questions reverted → 1230 (80.18%)
```

---

## 3. Resource Requirements (for test inference)

### GPU (for candidate generation + ORM scoring)

| Resource | Requirement |
|----------|------------|
| GPU | 1× A100 80G (or equivalent ~80GB VRAM) |
| Time | ~3 hours (candidate generation: 4 models × 8 shots + ORM scoring) |
| Models | Qwen3-14B (28G) + LoRAs (1-8G each) + Qwen2.5-Coder-32B (62G) |

> **Note**: Pre-generated candidate pools are included in the code zip
> (`runs/*.jsonl`). If the Exp Team prefers to skip GPU steps, they can use
> these pre-generated pools and run only the API-based stages (Stages 2-10).

### API (GLM-5.2)

| Resource | Requirement |
|----------|------------|
| API | GLM-5.2 (智谱 ZhipuAI, OpenAI-compatible) |
| Key | Provided separately (can be reset after evaluation) |
| Base URL | `https://open.bigmodel.cn/api/paas/v4/` |
| Prompt tokens (dev) | ~30M tokens total across all stages |
| Estimated test tokens | ~30M (similar to dev) |
| Temperature | 0 (deterministic, except self-consistency sampling at 0.7) |
| Max tokens | 4096-8192 per call |

### Environment

```
CUDA: 12.2 or 12.3 (compatible)
Python: 3.13
OS: Linux
```

---

## 4. Code Structure (GPU vs API separated)

The codebase is organized to **separate GPU-based code from API-based code**
as requested:

### GPU Code (candidate generation + ORM scoring)
```
scripts/
  gen_candidates_local_vllm.py     # vLLM candidate generation (N samples)
  score_candidates_with_orm_v2_vllm.py  # ORM v2 scoring via vLLM
  build_pool_from_candidates.py    # Build scored pool from raw candidates
```

### API Code (agent harness — no GPU needed)
```
scripts/
  select_compliant_merged4.py      # ORM band selection (CPU)
  run_e3v_parallel.py              # Value grounding (GLM)
  run_e4_repair_parallel.py        # Execution repair (GLM)
  run_e2_join_repair_parallel.py   # JOIN repair (GLM)
  run_e3c_parallel.py              # Column grounding (GLM)
  run_e3v_enhanced_parallel.py     # Enhanced value probe (GLM)
  run_e5_det_repair_parallel.py    # Deterministic rules (no LLM)
  run_route_a_reselect.py          # Tournament judge (GLM)
  run_deep_regen_parallel.py       # Deep regeneration (GLM)
  run_e6_preference_parallel.py    # Preference-guided (GLM)
  run_e7_critique_parallel.py      # Self-critique (GLM)
```

### Execution Order

```bash
# ── Phase 1: GPU (run once, ~3 hours on 1× A100) ──
# Step 0: Generate candidates with 4 models (vLLM)
python scripts/gen_candidates_local_vllm.py --model qwen3_14b_model --lora-path qwen3_sqlplus_lora --dev test.json --db-root test_databases --output runs/cands_qwen3.jsonl --n 8 --temperature 0.7

# Step 1: Score candidates with ORM v2 (vLLM)
python scripts/score_candidates_with_orm_v2_vllm.py --model orm_v2_model --input runs/cands_qwen3.jsonl --output runs/cands_qwen3_scored.jsonl

# Step 2: Build pool + ORM band selection (CPU)
python scripts/build_pool_from_candidates.py --prompts test_prompts.jsonl --candidates ... --db-root test_databases --output runs/test_pool.jsonl
python scripts/select_compliant_merged4.py --scored runs/test_pool.jsonl --dev test.json --band 0.05 --output predictions/step1.jsonl

# ── Phase 2: API (GLM-5.2, ~2-4 hours) ──
# Steps 3-10: Agent harness (each runs on failures only)
bash run_all.sh  # runs Steps 2-10 sequentially
```

> **Important**: If GPU is unavailable, use the **pre-generated candidate pools**
> in `runs/` (already included in the code zip). In that case, only Phase 2
> (API calls) is needed, which takes ~2-4 hours with no GPU.

---

## 5. Model Weights

| Model | Size | Location | Purpose |
|-------|------|----------|---------|
| Qwen3-14B | 28G | [ModelScope: Qwen/Qwen3-14B](https://modelscope.cn/models/Qwen/Qwen3-14B) | Base model (public) |
| Qwen2.5-Coder-32B-Instruct | 62G | [ModelScope: Qwen/Qwen2.5-Coder-32B-Instruct](https://modelscope.cn/models/Qwen/Qwen2.5-Coder-32B-Instruct) | Candidate generation (public) |
| qwen3-14b-sqlplus-merged | 1.3G | [HuggingFace: jiaguo/qwen3-14b-sqlplus-merged](https://huggingface.co/jiaguo/qwen3-14b-sqlplus-merged) | SQL+ LoRA adapter |
| omnisql-14b-bird-continue | 8.3G | [HuggingFace: jiaguo/omnisql-14b-bird-continue](https://huggingface.co/jiaguo/omnisql-14b-bird-continue) | OmniSQL LoRA adapter |
| qwen3-14b-orm-v2-merged-bf16 | 28G | [HuggingFace: jiaguo/qwen3-14b-orm-v2-merged-bf16](https://huggingface.co/jiaguo/qwen3-14b-orm-v2-merged-bf16) | ORM v2 scorer (merged bf16) |

**Training data**: All LoRA adapters and ORM trained **only on BIRD train split**
(9428 examples). ORM training labels derived from train candidate execution
results. Zero overlap with dev set (verified by SHA256 + question overlap check).

---

## 6. column_meaning.json Usage

**We do NOT require `column_meaning.json`** for testing.

Our system uses the database schema directly (`CREATE TABLE` DDL from
`sqlite_master`) and does not rely on external column descriptions. The
column-meaning information is not used in any prompt or agent module.

---

## 7. Dev SQL File

Predicted SQL on BIRD dev set: `predictions/final_1235.jsonl`

Format: one JSON per line:
```json
{"question_id": 0, "db_id": "california_schools", "question": "...", "pred_sql": "SELECT ..."}
```

Verification:
```bash
cd evaluation
python bird_official_eval_fast.py \
  --dev ../dev.json \
  --pred ../predictions/final_1235.jsonl \
  --db-root ../dev_databases \
  --output /tmp/verify.json --workers 8
# Expected: EX = 1230/1534 = 80.18%
```

---

## 8. Compliance Statement

- ✅ All generators trained only on BIRD train split (0 dev overlap)
- ✅ ORM v2 trained on train-split candidate execution labels
- ✅ No dev gold SQL in any prompt / RAG / few-shot / template
- ✅ All prompt templates contain no `{gold}` placeholders
- ✅ Agent scripts do NOT read `ex['SQL']` into prompts
- ✅ Candidate pool is physically k5-free (dev-gold retrieval removed)
- ✅ Prediction file SHA256-verified, no manual edits
- ✅ Preference rules mined from train 9428 examples only
- ✅ Method does NOT rely on ground truth SQL (test.json SQL field is empty)
- ✅ Code includes logging + error handling + resume-safe checkpointing

### k5 Lineage Audit

6 questions (qid 133/264/636/883/1128/1512) have k5_detvg candidate SQL that
could be independently regenerated by GLM in agent layers. These are reverted
to merged4 base predictions. `compliance_audit.py` verifies 0 true-leak hits.

---

## 9. Logging & Error Handling

- Every stage runner is **resume-safe**: checkpointed by question_id, can
  restart from where it stopped without re-running completed questions.
- Each GLM call has a **per-call timeout** (90-180s) and retry logic.
- Execution results (success/error/empty) are logged per question.
- If >5% of SQL outputs are abnormal, the system logs warnings.

```bash
# Resume from interruption — just re-run the same command:
python scripts/run_e3c_parallel.py --config configs/e3c_v2_column_grounding_20260730.yaml \
  --base-preds predictions/step4_e2.jsonl --dev test.json \
  --fail-qids /tmp/fail_qids.json --workers 8
# Already-processed questions are skipped automatically.
```

---

## 10. Quick Reproduction

```bash
# Set API key
export GLM_API_KEY="[provided separately]"

# Full pipeline (GPU + API, ~5 hours)
bash run_all.sh

# Or API-only (using pre-generated candidate pools, ~2-4 hours, no GPU):
# Skip to Step 2 in run_all.sh after ensuring runs/ pools are present

# Verify result
cd evaluation && python bird_official_eval_fast.py \
  --dev ../dev.json --pred ../predictions/final_1235.jsonl \
  --db-root ../dev_databases --output /tmp/check.json --workers 8
```

---

## 11. API Key

The GLM-5.2 API key is provided separately in the submission email.
After the evaluation terminates, we will reset the key.

Base URL: `https://open.bigmodel.cn/api/paas/v4/`
Model name: `glm-5.2`
