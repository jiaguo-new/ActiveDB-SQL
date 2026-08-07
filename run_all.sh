#!/bin/bash
# ActiveDB-SQL: End-to-end BIRD pipeline
# Result: EX = 1230/1534 = 80.18% on BIRD dev
#
# Prerequisites:
#   export GLM_API_KEY="your-key"
#   pip install -r requirements.txt
#   # Place BIRD data: data/dev.json, data/dev_databases/
#   # Place model weights: models/ (or set MODEL_DIR)
#   # For GPU steps: install vLLM in a separate env
#
# Usage:
#   bash run_all.sh              # full pipeline (API-only, uses pre-generated pools)
#   bash run_all.sh --with-gpu   # also run GPU candidate generation steps

set -euo pipefail

# --- Portable paths ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
export PYTHONPATH="$SCRIPT_DIR"

# --- Data paths (relative to project root) ---
DEV="${DEV:-data/dev.json}"
DBROOT="${DBROOT:-data/dev_databases}"

# --- Model paths (override with MODEL_DIR env var) ---
MODEL_DIR="${MODEL_DIR:-models}"
VPY="${VPY:-python3}"  # vLLM python; defaults to system python

# --- Internal dirs ---
P=predictions
M=metrics
mkdir -p "$P" "$M"

GLM_API_KEY="${GLM_API_KEY:?ERROR: Set GLM_API_KEY env var}"

# --- Validate data exists ---
if [ ! -f "$DEV" ]; then
  echo "ERROR: $DEV not found. Place BIRD data in data/ directory."
  echo "  mkdir -p data && cp /path/to/dev.json data/ && cp -r /path/to/dev_databases data/"
  exit 1
fi
if [ ! -d "$DBROOT" ]; then
  echo "ERROR: $DBROOT not found. Place BIRD databases in data/ directory."
  exit 1
fi

WITH_GPU=false
if [ "${1:-}" = "--with-gpu" ]; then
  WITH_GPU=true
fi

echo "================================================"
echo "  ActiveDB-SQL Pipeline"
echo "  DEV=$DEV  DBROOT=$DBROOT"
echo "  GPU steps: $WITH_GPU"
echo "================================================"

# Helper: extract fail qids from eval json
get_fail_qids() {
  python3 -c "
import json
m=json.load(open('$1'))
fails=[r['idx'] for r in m['per_query'] if not r['ex']]
import json as j; j.dump(fails, open('/tmp/fail_qids.json','w'))
print(f'  failures: {len(fails)}')
"
}

# --- Step 0 (optional): GPU candidate generation ---
if [ "$WITH_GPU" = true ]; then
  echo -e "\n[Step 0] GPU candidate generation (vLLM)..."
  # Generate candidates with each model, then score with ORM v2
  # See README for detailed commands
  echo "  (GPU steps - see README for detailed instructions)"
fi

# --- Step 1: ORM band selection from pre-generated clean pool ---
echo -e "\n[Step 1] ORM band-0.05 selection from clean pool"
python3 scripts/select_compliant_merged4.py \
  --scored runs/merged4model_n4_clean_scored_20260805.jsonl \
  --dev "$DEV" --band 0.05 --output "$P/step1_merged4_orm.jsonl"
python3 evaluation/bird_official_eval_fast.py \
  --dev "$DEV" --pred "$P/step1_merged4_orm.jsonl" \
  --db-root "$DBROOT" --output "$M/step1_eval.json" --workers 8

# --- Step 2: E3v value grounding ---
echo -e "\n[Step 2] E3v value grounding"
get_fail_qids "$M/step1_eval.json"
python3 scripts/run_e3v_parallel.py \
  --config configs/e3v_on_merged4_20260729.yaml \
  --base-preds "$P/step1_merged4_orm.jsonl" \
  --dev "$DEV" --fail-qids /tmp/fail_qids.json --workers 8 --timeout 120
python3 merge_chain.py \
  --base "$P/step1_merged4_orm.jsonl" \
  --overlay "$P/e3v_on_merged4_20260729/predictions.jsonl" \
  --output "$P/step2_e3v.jsonl"
python3 evaluation/bird_official_eval_fast.py \
  --dev "$DEV" --pred "$P/step2_e3v.jsonl" \
  --db-root "$DBROOT" --output "$M/step2_eval.json" --workers 8

# --- Step 3: E4 execution repair ---
echo -e "\n[Step 3] E4 execution repair"
get_fail_qids "$M/step2_eval.json"
python3 scripts/run_e4_repair_parallel.py \
  --config configs/e4_exec_repair_on_e3v_20260729.yaml \
  --baseline-pred "$P/step2_e3v.jsonl" --workers 8
python3 merge_chain.py \
  --base "$P/step2_e3v.jsonl" \
  --overlay "$P/e4_exec_repair_on_e3v_20260729/predictions.jsonl" \
  --output "$P/step3_e4.jsonl"
python3 evaluation/bird_official_eval_fast.py \
  --dev "$DEV" --pred "$P/step3_e4.jsonl" \
  --db-root "$DBROOT" --output "$M/step3_eval.json" --workers 8

# --- Step 4: E2 JOIN repair ---
echo -e "\n[Step 4] E2 JOIN repair"
get_fail_qids "$M/step3_eval.json"
python3 scripts/run_e2_join_repair_parallel.py \
  --config configs/e2_join_repair_20260730.yaml \
  --base-preds "$P/step3_e4.jsonl" \
  --dev "$DEV" --fail-qids /tmp/fail_qids.json --workers 8 --timeout 120
python3 merge_chain.py \
  --base "$P/step3_e4.jsonl" \
  --overlay "$P/e2_join_repair_20260730/predictions.jsonl" \
  --output "$P/step4_e2.jsonl"
python3 evaluation/bird_official_eval_fast.py \
  --dev "$DEV" --pred "$P/step4_e2.jsonl" \
  --db-root "$DBROOT" --output "$M/step4_eval.json" --workers 8

# --- Step 5: E3c column grounding ---
echo -e "\n[Step 5] E3c column grounding"
get_fail_qids "$M/step4_eval.json"
python3 scripts/run_e3c_parallel.py \
  --config configs/e3c_v2_column_grounding_20260730.yaml \
  --base-preds "$P/step4_e2.jsonl" \
  --dev "$DEV" --fail-qids /tmp/fail_qids.json --workers 8 --timeout 120
python3 merge_chain.py \
  --base "$P/step4_e2.jsonl" \
  --overlay "$P/e3c_v2_column_grounding_20260730/predictions.jsonl" \
  --output "$P/step5_e3c.jsonl"
python3 evaluation/bird_official_eval_fast.py \
  --dev "$DEV" --pred "$P/step5_e3c.jsonl" \
  --db-root "$DBROOT" --output "$M/step5_eval.json" --workers 8

# --- Step 6: E3v+ enhanced + E5det deterministic ---
echo -e "\n[Step 6] E3v+ enhanced + E5det"
get_fail_qids "$M/step5_eval.json"
python3 scripts/run_e3v_enhanced_parallel.py \
  --config configs/e3v_enhanced_20260730.yaml \
  --base-preds "$P/step5_e3c.jsonl" \
  --dev "$DEV" --fail-qids /tmp/fail_qids.json --workers 8 --timeout 120
python3 merge_chain.py \
  --base "$P/step5_e3c.jsonl" \
  --overlay "$P/e3v_enhanced_20260730/predictions.jsonl" \
  --output "$P/step6a_e3vp.jsonl"
python3 scripts/run_e5_det_repair_parallel.py \
  --config configs/e5_det_repair_20260730.yaml \
  --base-preds "$P/step6a_e3vp.jsonl" \
  --dev "$DEV" --fail-qids /tmp/fail_qids.json
python3 merge_chain.py \
  --base "$P/step6a_e3vp.jsonl" \
  --overlay "$P/e5_det_repair_20260730/predictions.jsonl" \
  --output "$P/step6_agent.jsonl"
python3 evaluation/bird_official_eval_fast.py \
  --dev "$DEV" --pred "$P/step6_agent.jsonl" \
  --db-root "$DBROOT" --output "$M/step6_eval.json" --workers 8

# --- Step 7: Route A tournament ---
echo -e "\n[Step 7] Route A tournament"
get_fail_qids "$M/step6_eval.json"
python3 scripts/run_route_a_reselect.py \
  --config configs/route_a_top12_20260731.yaml \
  --scored-pool runs/merged4model_n8_pool_scored_20260729.jsonl \
  --cur-preds "$P/step6_agent.jsonl" \
  --dev "$DEV" --fail-qids /tmp/fail_qids.json --workers 8 --timeout 90
python3 merge_chain.py \
  --base "$P/step6_agent.jsonl" \
  --overlay "$P/route_a_top12_20260731/predictions.jsonl" \
  --output "$P/step7_route_a.jsonl"
python3 evaluation/bird_official_eval_fast.py \
  --dev "$DEV" --pred "$P/step7_route_a.jsonl" \
  --db-root "$DBROOT" --output "$M/step7_eval.json" --workers 8

# --- Step 8: Multi-generator Route A ---
echo -e "\n[Step 8] Multi-generator Route A"
get_fail_qids "$M/step7_eval.json"
python3 scripts/run_route_a_reselect.py \
  --config configs/route_a_multigen_20260731.yaml \
  --scored-pool runs/triple_merged_scored_pool.jsonl \
  --cur-preds "$P/step7_route_a.jsonl" \
  --dev "$DEV" --fail-qids /tmp/fail_qids.json --workers 8 --timeout 90
python3 merge_chain.py \
  --base "$P/step7_route_a.jsonl" \
  --overlay "$P/route_a_multigen_20260731/predictions.jsonl" \
  --output "$P/step8_multigen.jsonl"
python3 evaluation/bird_official_eval_fast.py \
  --dev "$DEV" --pred "$P/step8_multigen.jsonl" \
  --db-root "$DBROOT" --output "$M/step8_eval.json" --workers 8

# --- Step 9: Deep regeneration ---
echo -e "\n[Step 9] Deep regeneration"
get_fail_qids "$M/step8_eval.json"
python3 scripts/run_deep_regen_parallel.py \
  --config configs/deep_regen_20260731.yaml \
  --base-preds "$P/step8_multigen.jsonl" \
  --dev "$DEV" --fail-qids /tmp/fail_qids.json --workers 8 --timeout 120
python3 merge_chain.py \
  --base "$P/step8_multigen.jsonl" \
  --overlay "$P/deep_regen_20260731/predictions.jsonl" \
  --output "$P/step9_deepregen.jsonl"
python3 evaluation/bird_official_eval_fast.py \
  --dev "$DEV" --pred "$P/step9_deepregen.jsonl" \
  --db-root "$DBROOT" --output "$M/step9_eval.json" --workers 8

# --- Step 10: E6 preference + E7 critique ---
echo -e "\n[Step 10] E6 preference + E7 critique"
get_fail_qids "$M/step9_eval.json"
python3 scripts/run_e6_preference_parallel.py \
  --config configs/e6_preference_guided_20260731.yaml \
  --base-preds "$P/step9_deepregen.jsonl" \
  --dev "$DEV" --fail-qids /tmp/fail_qids.json --workers 8 --timeout 120
python3 merge_chain.py \
  --base "$P/step9_deepregen.jsonl" \
  --overlay "$P/e6_preference_guided_20260731/predictions.jsonl" \
  --output "$P/step10a_e6.jsonl"
python3 scripts/run_e7_critique_parallel.py \
  --config configs/e7_result_critique_20260731.yaml \
  --base-preds "$P/step10a_e6.jsonl" \
  --dev "$DEV" --fail-qids /tmp/fail_qids.json --workers 8 --timeout 120
python3 merge_chain.py \
  --base "$P/step10a_e6.jsonl" \
  --overlay "$P/e7_result_critique_20260731/predictions.jsonl" \
  --output "$P/final.jsonl"
python3 evaluation/bird_official_eval_fast.py \
  --dev "$DEV" --pred "$P/final.jsonl" \
  --db-root "$DBROOT" --output "$M/final_eval.json" --workers 8

echo -e "\n================================================"
echo "  PIPELINE COMPLETE"
echo "  Output: predictions/final.jsonl"
echo "  Eval:   metrics/final_eval.json"
echo "================================================"
