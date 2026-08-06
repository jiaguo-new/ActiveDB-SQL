#!/usr/bin/env python3
"""Compliance audit: verify no dev gold SQL leaked into any prediction/prompt.

Checks:
1. Every prediction file has 1534 lines, qid 0-1533 continuous
2. No prediction SQL equals dev gold SQL verbatim above expected threshold
3. All prompt templates have no {gold}/{correct}/{answer} placeholders
4. No agent script reads ex['SQL'] into a prompt (dev gold access)
5. **k5 lineage check**: no final prediction equals a k5_detvg candidate SQL
   that is NOT also present in the clean merged4 candidate pool — these are
   "true leak hits" where the prediction could only have come from the
   dev-gold-fed retrieval baseline (dev_train1234.json). This catches the
   indirect leakage that the original audit missed.
6. **pool cleanliness**: verify the selection pool used by run_all.sh is the
   physically k5-free clean pool, not the deprecated n4_plus_k5 pool.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent

LEAK_SENSITIVE_QIDS = [133, 264, 636, 883, 1128, 1512]


def _norm_sql(s: str) -> str:
    if not s:
        return ""
    s = s.strip().rstrip(";")
    s = re.sub(r"\s+", " ", s.lower())
    s = re.sub(r'["`]', "", s)
    return s


def check_predictions():
    """Check all prediction files for integrity."""
    issues = []
    dev = json.load(open(ROOT / "dev.json"))
    dev_sqls = {_norm_sql(d.get("SQL", "")) for d in dev if d.get("SQL")}

    pred_dir = ROOT / "predictions"
    if not pred_dir.exists():
        return ["predictions/ dir not found"]

    for pf in sorted(pred_dir.glob("*.jsonl")):
        preds = [json.loads(l) for l in pf.open() if l.strip()]
        if len(preds) != 1534:
            issues.append(f"{pf.name}: {len(preds)} lines (expected 1534)")
            continue
        qids = sorted(p.get("question_id", p.get("id")) for p in preds)
        if qids != list(range(min(qids), max(qids) + 1)):
            issues.append(f"{pf.name}: qids not continuous")

        verbatim = sum(1 for p in preds if _norm_sql(p.get("pred_sql", "")) in dev_sqls)
        # 12-13% exact match is expected; >15% is suspicious
        if verbatim > 230:
            issues.append(f"{pf.name}: {verbatim} verbatim=gold (>230 suspicious)")
    return issues


def check_k5_lineage():
    """Check final prediction for k5_detvg true-leak hits.

    A 'true leak hit' is a prediction whose SQL equals the k5_detvg candidate
    AND is NOT found in any clean merged4 candidate for that question. This
    means the SQL could only have originated from the dev-gold-fed retrieval
    baseline (dev_train1234.json = dev subset).
    """
    issues = []

    # Load k5 pool and merged4 candidates
    k5_pool_path = ROOT / "runs" / "merged4model_n4_plus_k5_all_scored_vllm.jsonl"
    if not k5_pool_path.exists():
        return ["k5 pool not found (symlink missing?) — cannot check lineage"]

    k5_sql = {}
    m4_sqls = {}
    with k5_pool_path.open() as f:
        for line in f:
            if not line.strip():
                continue
            d = json.loads(line)
            qid = d["id"]
            cands = d.get("candidates", [])
            if cands and cands[0].get("model") == "k5_detvg":
                k5_sql[qid] = _norm_sql(cands[0].get("sql", ""))
            m4_sqls[qid] = {_norm_sql(c.get("sql", "")) for c in cands[1:] if c.get("sql")}

    # Check each prediction file (skip pre-leakfix audit-trail files)
    pred_dir = ROOT / "predictions"
    for pf in sorted(pred_dir.glob("final*.jsonl")):
        if "pre_leakfix" in pf.name:
            continue  # audit trail, not for submission
        preds = {}
        for l in pf.open():
            if l.strip():
                d = json.loads(l)
                preds[d["question_id"]] = _norm_sql(d.get("pred_sql", ""))

        true_leaks = [
            q for q, s in preds.items()
            if s and s == k5_sql.get(q, "\x00") and s not in m4_sqls.get(q, set())
        ]
        if true_leaks:
            issues.append(
                f"{pf.name}: {len(true_leaks)} k5 true-leak hits -> {true_leaks[:10]}. "
                f"Known sensitive qids: {LEAK_SENSITIVE_QIDS}. "
                f"Revert these to merged4 base predictions."
            )
    return issues


def check_pool_cleanliness():
    """Verify run_all.sh uses the clean pool, not the deprecated k5 pool."""
    issues = []
    run_all = ROOT / "run_all.sh"
    if not run_all.exists():
        return ["run_all.sh not found"]
    content = run_all.read_text()
    if "merged4model_n4_plus_k5_all_scored_vllm" in content and "--scored" in content:
        # Check if it's only referenced in comments
        for i, line in enumerate(content.split("\n"), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "merged4model_n4_plus_k5_all_scored_vllm" in stripped and "--scored" in stripped:
                issues.append(
                    f"run_all.sh:{i}: uses deprecated k5 pool for selection. "
                    f"Use merged4model_n4_clean_scored_20260805.jsonl instead."
                )
    return issues


def check_prompts():
    """Check prompt templates for gold injection."""
    issues = []
    prompt_dir = ROOT / "prompts"
    if not prompt_dir.exists():
        return ["prompts/ dir not found"]
    for pf in sorted(prompt_dir.glob("*.md")):
        content = pf.read_text()
        for bad in ["{gold", "{correct_sql", "{answer_sql", "{reference_sql", "{gold_result"]:
            if bad in content:
                issues.append(f"{pf.name}: contains {bad}")
    return issues


def check_scripts():
    """Check scripts for ex['SQL'] usage that feeds into prompts (reading dev gold)."""
    issues = []
    script_dir = ROOT / "scripts"
    if not script_dir.exists():
        return ["scripts/ dir not found"]
    for sf in sorted(script_dir.glob("*.py")):
        src = sf.read_text()
        for pattern in ["ex['SQL']", 'ex["SQL"]', "ex.get('SQL')", 'ex.get("SQL")']:
            if pattern in src:
                lines_with = [l.strip() for l in src.split('\n')
                              if pattern in l and not l.strip().startswith('#')]
                if lines_with:
                    issues.append(f"{sf.name}: uses {pattern} (may read dev gold)")
    return issues


def main():
    print("=== Compliance Audit (v2 — with k5 lineage check) ===\n")

    all_issues = []

    print("1. Prediction files integrity:")
    pred_issues = check_predictions()
    all_issues.extend(pred_issues)
    for i in pred_issues:
        print(f"  ⚠️  {i}")
    if not pred_issues:
        print("  ✅ All prediction files OK")

    print("\n2. k5 lineage check (TRUE LEAK DETECTION):")
    k5_issues = check_k5_lineage()
    all_issues.extend(k5_issues)
    for i in k5_issues:
        print(f"  ❌ {i}")
    if not k5_issues:
        print("  ✅ No k5 true-leak hits in any final prediction file")

    print("\n3. Pool cleanliness (run_all.sh source check):")
    pool_issues = check_pool_cleanliness()
    all_issues.extend(pool_issues)
    for i in pool_issues:
        print(f"  ⚠️  {i}")
    if not pool_issues:
        print("  ✅ run_all.sh uses clean pool")

    print("\n4. Prompt templates (gold injection check):")
    prompt_issues = check_prompts()
    all_issues.extend(prompt_issues)
    for i in prompt_issues:
        print(f"  ⚠️  {i}")
    if not prompt_issues:
        print("  ✅ All prompt templates OK")

    print("\n5. Scripts (dev gold access check):")
    script_issues = check_scripts()
    all_issues.extend(script_issues)
    for i in script_issues:
        print(f"  ⚠️  {i}")
    if not script_issues:
        print("  ✅ All scripts OK")

    print(f"\n{'='*50}")
    if not all_issues:
        print("✅ COMPLIANCE AUDIT PASSED")
    else:
        print(f"❌ {len(all_issues)} issues found — FIX BEFORE SUBMITTING")
    return len(all_issues)


if __name__ == "__main__":
    sys.exit(main())
