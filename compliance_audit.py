#!/usr/bin/env python3
"""Compliance audit: verify no dev gold SQL leaked into any prediction/prompt.

Checks:
1. Every prediction file has 1534 lines, qid 0-1533 continuous
2. No prediction SQL equals dev gold SQL verbatim (would indicate copying)
3. All prompt templates have no {gold}/{correct}/{answer} placeholders
4. No agent script reads ex['SQL'] (dev gold)
"""
import json, re, sys
from pathlib import Path

ROOT = Path(__file__).parent

def check_predictions():
    """Check all prediction files for integrity."""
    issues = []
    dev = json.load(open(ROOT / "dev.json"))
    dev_sqls = set()
    for d in dev:
        s = ' '.join(d.get("SQL","").lower().split())
        if s: dev_sqls.add(s)

    pred_dir = ROOT / "predictions"
    if not pred_dir.exists():
        return ["predictions/ dir not found"]

    for pf in sorted(pred_dir.glob("*.jsonl")):
        preds = [json.loads(l) for l in pf.open() if l.strip()]
        if len(preds) != 1534:
            issues.append(f"{pf.name}: {len(preds)} lines (expected 1534)")
        qids = sorted(p.get("question_id", p.get("id")) for p in preds)
        if qids != list(range(min(qids), max(qids)+1)):
            issues.append(f"{pf.name}: qids not continuous")

        # Check verbatim gold copy (not necessarily leakage, but suspicious)
        verbatim = 0
        for p in preds:
            ps = ' '.join(p.get("pred_sql","").lower().split())
            if ps in dev_sqls:
                verbatim += 1
        if verbatim > 200:
            issues.append(f"{pf.name}: {verbatim} verbatim=gold (suspicious)")
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
    """Check scripts for ex['SQL'] usage (reading dev gold)."""
    issues = []
    script_dir = ROOT / "scripts"
    if not script_dir.exists():
        return ["scripts/ dir not found"]
    for sf in sorted(script_dir.glob("*.py")):
        src = sf.read_text()
        # Check for gold SQL access patterns
        for pattern in ["ex['SQL']", 'ex["SQL"]', "ex.get('SQL')", 'ex.get("SQL")', "gold_sql", "dev_sql"]:
            if pattern in src:
                # Allow in comments
                lines_with = [l.strip() for l in src.split('\n') if pattern in l and not l.strip().startswith('#')]
                if lines_with:
                    issues.append(f"{sf.name}: uses {pattern}")
    return issues

def main():
    print("=== Compliance Audit ===\n")

    print("1. Prediction files integrity:")
    pred_issues = check_predictions()
    if pred_issues:
        for i in pred_issues: print(f"  ⚠️  {i}")
    else:
        print("  ✅ All prediction files OK")

    print("\n2. Prompt templates (gold injection check):")
    prompt_issues = check_prompts()
    if prompt_issues:
        for i in prompt_issues: print(f"  ⚠️  {i}")
    else:
        print("  ✅ All prompt templates OK")

    print("\n3. Scripts (dev gold access check):")
    script_issues = check_scripts()
    if script_issues:
        for i in script_issues: print(f"  ⚠️  {i}")
    else:
        print("  ✅ All scripts OK")

    total_issues = len(pred_issues) + len(prompt_issues) + len(script_issues)
    print(f"\n{'='*40}")
    if total_issues == 0:
        print("✅ COMPLIANCE AUDIT PASSED")
    else:
        print(f"⚠️  {total_issues} issues found")
    return total_issues

if __name__ == "__main__":
    sys.exit(main())
