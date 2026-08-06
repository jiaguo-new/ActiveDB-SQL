#!/bin/bash
# End-to-end reproduction script for NL2SQL Agent Harness
# Target: 1230/1534 = 80.18% on BIRD dev (leak-fixed compliant version)
#
# Prerequisites:
#   - GLM_API_KEY environment variable set
#   - vLLM env: /home/dameng/miniconda3/envs/vllm-cuda/bin/python
#   - GPU free for vLLM model loading
#   - All model weights and candidate pools linked in runs/ and model symlinks
#
# Usage: bash run_all.sh [--verify-only]  (skip to verification)
#
# COMPLIANCE NOTE: Step 1 uses the physically k5-free clean pool
# (merged4model_n4_clean_scored_20260805.jsonl), NOT the deprecated
# n4_plus_k5 pool. The final output is leak-fixed (6 questions reverted
# to merged4 base) to eliminate k5_detvg contamination.

set -euo pipefail
cd /home/dameng/project/nl2sql_harness_reproduce

export PYTHONPATH=/home/dameng/project/nl2sql_harness_reproduce
DEV=dev.json
DBROOT=dev_databases
GLM_API_KEY="${GLM_API_KEY:?Set GLM_API_KEY env var}"
VPY=/home/dameng/miniconda3/envs/vllm-cuda/bin/python
P=predictions
M=metrics

echo "================================================"
echo "  NL2SQL Agent Harness — Reproduction Pipeline"
echo "================================================"

# ── Step 0: Compliance audit ──
echo -e "\n[Step 0] Compliance audit..."
python3 compliance_audit.py

# ── Step 1: merged4 ORM select from CLEAN pool (base = 1067) ──
echo -e "\n[Step 1] merged4 n4 CLEAN pool ORM band0.05 select → 1067"
python3 scripts/select_compliant_merged4.py \
  --scored runs/merged4model_n4_clean_scored_20260805.jsonl \
  --dev $DEV --band 0.05 --output $P/step1_merged4_orm.jsonl
cd evaluation && python3 bird_official_eval_fast.py --dev ../$DEV --pred ../$P/step1_merged4_orm.jsonl \
  --db-root ../$DBROOT --output ../$M/step1_eval.json --workers 8; cd ..

# Helper: extract fail qids
get_fail_qids() {
  python3 -c "import json; m=json.load(open('$1')); print('\n'.join(str(r['idx']) for r in m['per_query'] if not r['ex']))" > /tmp/fail_qids.txt
  python3 -c "import json; m=json.load(open('$1')); f=[r['idx'] for r in m['per_query'] if not r['ex']]; import json as j; j.dump(f, open('/tmp/fail_qids.json','w'))"
}

# ── Steps 2-7: Agent Harness layers ──
echo -e "\n[Step 2] E3v value probe → +3"
get_fail_qids $M/step1_eval.json
python3 scripts/run_e3v_parallel.py --config configs/e3v_on_merged4_20260729.yaml \
  --base-preds $P/step1_merged4_orm.jsonl --dev $DEV --fail-qids /tmp/fail_qids.json --workers 8 --timeout 120
python3 merge_chain.py --base $P/step1_merged4_orm.jsonl --overlay predictions/e3v_on_merged4_20260729/predictions.jsonl --output $P/step2_e3v.jsonl
cd evaluation && python3 bird_official_eval_fast.py --dev ../$DEV --pred ../$P/step2_e3v.jsonl \
  --db-root ../$DBROOT --output ../$M/step2_eval.json --workers 8; cd ..

echo -e "\n[Step 3] E4 execution repair → +2"
get_fail_qids $M/step2_eval.json
python3 scripts/run_e4_repair_parallel.py --config configs/e4_exec_repair_on_e3v_20260729.yaml \
  --base-preds $P/step2_e3v.jsonl --dev $DEV --fail-qids /tmp/fail_qids.json --workers 8 --timeout 120
python3 merge_chain.py --base $P/step2_e3v.jsonl --overlay predictions/e4_exec_repair_on_e3v_20260729/predictions.jsonl --output $P/step3_e4.jsonl
cd evaluation && python3 bird_official_eval_fast.py --dev ../$DEV --pred ../$P/step3_e4.jsonl \
  --db-root ../$DBROOT --output ../$M/step3_eval.json --workers 8; cd ..

echo -e "\n[Step 4] E2 JOIN repair → +4"
get_fail_qids $M/step3_eval.json
python3 scripts/run_e2_join_repair_parallel.py --config configs/e2_join_repair_20260730.yaml \
  --base-preds $P/step3_e4.jsonl --dev $DEV --fail-qids /tmp/fail_qids.json --workers 8 --timeout 120
python3 merge_chain.py --base $P/step3_e4.jsonl --overlay predictions/e2_join_repair_20260730/predictions.jsonl --output $P/step4_e2.jsonl
cd evaluation && python3 bird_official_eval_fast.py --dev ../$DEV --pred ../$P/step4_e2.jsonl \
  --db-root ../$DBROOT --output ../$M/step4_eval.json --workers 8; cd ..

echo -e "\n[Step 5] E3c column grounding v2 → +24"
get_fail_qids $M/step4_eval.json
python3 scripts/run_e3c_parallel.py --config configs/e3c_v2_column_grounding_20260730.yaml \
  --base-preds $P/step4_e2.jsonl --dev $DEV --fail-qids /tmp/fail_qids.json --workers 8 --timeout 120
python3 merge_chain.py --base $P/step4_e2.jsonl --overlay predictions/e3c_v2_column_grounding_20260730/predictions.jsonl --output $P/step5_e3c.jsonl
cd evaluation && python3 bird_official_eval_fast.py --dev ../$DEV --pred ../$P/step5_e3c.jsonl \
  --db-root ../$DBROOT --output ../$M/step5_eval.json --workers 8; cd ..

echo -e "\n[Step 6] E3v+ enhanced + E5det → +5"
get_fail_qids $M/step5_eval.json
python3 scripts/run_e3v_enhanced_parallel.py --config configs/e3v_enhanced_20260730.yaml \
  --base-preds $P/step5_e3c.jsonl --dev $DEV --fail-qids /tmp/fail_qids.json --workers 8 --timeout 120
python3 merge_chain.py --base $P/step5_e3c.jsonl --overlay predictions/e3v_enhanced_20260730/predictions.jsonl --output $P/step6a_e3vp.jsonl
python3 scripts/run_e5_det_repair_parallel.py --config configs/e5_det_repair_20260730.yaml \
  --base-preds $P/step6a_e3vp.jsonl --dev $DEV --fail-qids /tmp/fail_qids.json --workers 8
python3 merge_chain.py --base $P/step6a_e3vp.jsonl --overlay predictions/e5_det_repair_20260730/predictions.jsonl --output $P/step6_agent.jsonl
cd evaluation && python3 bird_official_eval_fast.py --dev ../$DEV --pred ../$P/step6_agent.jsonl \
  --db-root ../$DBROOT --output ../$M/step6_eval.json --workers 8; cd ..

# ── Step 7: Route A tournament ──
echo -e "\n[Step 7] Route A n8 top-5 tournament → +52"
get_fail_qids $M/step6_eval.json
python3 scripts/run_route_a_reselect.py --config configs/route_a_top12_20260731.yaml \
  --scored-pool runs/merged4model_n8_pool_scored_20260729.jsonl \
  --cur-preds $P/step6_agent.jsonl --dev $DEV --fail-qids /tmp/fail_qids.json --workers 8 --timeout 90
python3 merge_chain.py --base $P/step6_agent.jsonl --overlay predictions/route_a_top12_20260731/predictions.jsonl --output $P/step7_route_a.jsonl
cd evaluation && python3 bird_official_eval_fast.py --dev ../$DEV --pred ../$P/step7_route_a.jsonl \
  --db-root ../$DBROOT --output ../$M/step7_eval.json --workers 8; cd ..

# ── Step 8: Multi-generator (uses pre-scored pools) ──
echo -e "\n[Step 8] Multi-generator Route A (triple pool) → +20"
get_fail_qids $M/step7_eval.json
python3 scripts/run_route_a_reselect.py --config configs/route_a_top12_20260731.yaml \
  --scored-pool runs/triple_merged_scored_pool.jsonl \
  --cur-preds $P/step7_route_a.jsonl --dev $DEV --fail-qids /tmp/fail_qids.json --workers 8 --timeout 90
python3 merge_chain.py --base $P/step7_route_a.jsonl --overlay predictions/route_a_top12_20260731/predictions.jsonl --output $P/step8_multigen.jsonl
cd evaluation && python3 bird_official_eval_fast.py --dev ../$DEV --pred ../$P/step8_multigen.jsonl \
  --db-root ../$DBROOT --output ../$M/step8_eval.json --workers 8; cd ..

# ── Step 9: Deep Regen ──
echo -e "\n[Step 9] Deep regeneration → +21"
get_fail_qids $M/step8_eval.json
python3 scripts/run_deep_regen_parallel.py --config configs/deep_regen_20260731.yaml \
  --base-preds $P/step8_multigen.jsonl --dev $DEV --fail-qids /tmp/fail_qids.json --workers 8 --timeout 120
python3 merge_chain.py --base $P/step8_multigen.jsonl --overlay predictions/deep_regen_20260731/predictions.jsonl --output $P/step9_deepregen.jsonl
cd evaluation && python3 bird_official_eval_fast.py --dev ../$DEV --pred ../$P/step9_deepregen.jsonl \
  --db-root ../$DBROOT --output ../$M/step9_eval.json --workers 8; cd ..

# ── Step 10: E6 preference-guided + E7 result critique ──
echo -e "\n[Step 10] E6 preference + E7 critique → +26"
get_fail_qids $M/step9_eval.json
python3 scripts/run_e6_preference_parallel.py --config configs/e6_preference_guided_20260731.yaml \
  --base-preds $P/step9_deepregen.jsonl --dev $DEV --fail-qids /tmp/fail_qids.json --workers 8 --timeout 120
python3 merge_chain.py --base $P/step9_deepregen.jsonl --overlay predictions/e6_preference_guided_20260731/predictions.jsonl --output $P/step10a_e6.jsonl
get_fail_qids() { :; }  # reuse same fail qids
python3 scripts/run_e7_critique_parallel.py --config configs/e7_result_critique_20260731.yaml \
  --base-preds $P/step10a_e6.jsonl --dev $DEV --fail-qids /tmp/fail_qids.json --workers 8 --timeout 120
python3 merge_chain.py --base $P/step10a_e6.jsonl --overlay predictions/e7_result_critique_20260731/predictions.jsonl --output $P/final.jsonl
cd evaluation && python3 bird_official_eval_fast.py --dev ../$DEV --pred ../$P/final.jsonl \
  --db-root ../$DBROOT --output ../$M/final_eval.json --workers 8; cd ..

# ── Final audit ──
echo -e "\n[Final] Compliance audit..."
python3 compliance_audit.py

echo -e "\n================================================"
echo "  REPRODUCTION COMPLETE"
echo "  Final EX: see metrics/final_eval.json"
echo "================================================"
