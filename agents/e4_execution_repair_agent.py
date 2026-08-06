"""E4 Execution Repair Agent: generate -> execute -> repair -> execute."""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from evaluation.bird_official_eval import evaluate_predictions
from tools.db_utils import BirdDatabase
from tools.llm_client import LLMClient


def load_config(config_path: Path | str) -> dict[str, Any]:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def render_prompt(template: str, **kwargs: Any) -> str:
    text = template
    for k, v in kwargs.items():
        text = text.replace(f"{{{k}}}", str(v))
    return text


def extract_sql(text: str) -> str:
    text = text.strip()
    # Remove reasoning tags that some models (e.g., GLM-5.2) emit before the SQL.
    text = re.sub(r"<reasoning>.*?</reasoning>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = text.strip()
    # Prefer the last fenced code block if present; this handles headers
    # like "## SQL" that may appear before the opening fence.
    if "```" in text:
        parts = text.split("```")
        # Pick the last non-empty fenced segment (the final empty string after
        # a closing fence is ignored; otherwise the trailing part is used).
        candidate = None
        for part in reversed(parts[1:]):  # skip text before first fence
            if part.strip():
                candidate = part.strip()
                break
        if candidate is not None:
            text = candidate
    # Remove a leading language tag line (e.g., "sql", "SQL", "sql\n").
    lines = text.splitlines()
    if lines and lines[0].strip().lower() == "sql":
        lines = lines[1:]
    text = "\n".join(lines).strip()
    # Drop an explicit "SQL:" or "SQL" prefix if still present.
    if text.lower().startswith("sql"):
        text = text[3:].lstrip(": ")
    return text.rstrip(";").strip()


def run_e4(config_path: Path | str) -> None:
    cfg = load_config(config_path)
    run_id = cfg["run_id"]
    run_dir = Path(cfg["output"]["run_dir"].format(run_id=run_id))
    pred_path = Path(cfg["output"]["predictions"].format(run_id=run_id))
    trace_path = Path(cfg["output"]["tool_traces"].format(run_id=run_id))
    error_path = Path(cfg["output"]["errors"].format(run_id=run_id))
    metrics_path = Path(cfg["output"]["metrics"].format(run_id=run_id))
    prompt_snapshot_dir = run_dir / "prompt_snapshot"

    for p in [run_dir, pred_path.parent, trace_path.parent, error_path.parent, metrics_path.parent, prompt_snapshot_dir]:
        p.mkdir(parents=True, exist_ok=True)

    shutil.copy(cfg["prompt"]["generate"], prompt_snapshot_dir / Path(cfg["prompt"]["generate"]).name)
    shutil.copy(cfg["prompt"]["repair"], prompt_snapshot_dir / Path(cfg["prompt"]["repair"]).name)
    shutil.copy(config_path, run_dir / "config.yaml")

    data_manifest = {
        "dataset": cfg["dataset"]["name"],
        "split": cfg["dataset"]["split"],
        "source": cfg["dataset"]["source"],
        "db_root": cfg["dataset"]["db_root"],
        "contains_gold": True,
        "allowed_usage": ["evaluation", "error_analysis"],
        "forbidden_usage": ["training", "sft", "retrieval_corpus"],
        "loaded_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(run_dir / "data_manifest.json", "w", encoding="utf-8") as f:
        json.dump(data_manifest, f, indent=2, ensure_ascii=False)

    client = LLMClient(
        base_url=cfg["model"]["base_url"],
        model_name=cfg["model"]["model_name"],
        api_key_env=cfg["model"]["api_key_env"],
    )

    generate_template = open(cfg["prompt"]["generate"], "r", encoding="utf-8").read()
    repair_template = open(cfg["prompt"]["repair"], "r", encoding="utf-8").read()

    with open(cfg["dataset"]["source"], "r", encoding="utf-8") as f:
        examples = json.load(f)

    max_repairs = cfg["agent"].get("max_repairs", 2)
    predictions = []
    traces = []
    errors = []
    repair_stats = {"attempted": 0, "success": 0, "damage": 0}
    total_start = time.time()

    for idx, ex in enumerate(examples):
        qid = ex.get("question_id")
        db_id = ex["db_id"]
        question = ex["question"]
        evidence = ex.get("evidence", "")
        evidence_block = f"## Evidence\n{evidence}\n" if evidence else ""
        gold_sql = ex.get("SQL", "")

        db = BirdDatabase(
            db_id=db_id,
            db_root=cfg["dataset"]["db_root"],
            timeout=cfg["execution"]["timeout_seconds"],
            max_rows=cfg["execution"]["max_rows"],
        )
        schema = db.get_schema()

        prompt = render_prompt(
            generate_template,
            db_id=db_id,
            schema=schema,
            evidence=evidence_block,
            question=question,
        )
        messages = [
            {"role": "system", "content": "You are an expert SQL assistant."},
            {"role": "user", "content": prompt},
        ]

        try:
            completion = client.chat_completion(
                messages=messages,
                temperature=cfg["model"]["temperature"],
                top_p=cfg["model"]["top_p"],
                max_tokens=cfg["model"]["max_tokens"],
            )
            raw_output, _ = client.extract_content(completion)
            pred_sql = extract_sql(raw_output)
            gen_latency = completion["latency_seconds"]
            gen_request_id = completion["response"].get("id")
        except Exception as e:
            errors.append({"question_id": qid, "stage": "generation", "error": str(e)})
            pred_sql = ""
            raw_output = ""
            gen_latency = 0.0
            gen_request_id = None

        exec_result = db.execute(pred_sql) if pred_sql else {"ok": False, "error": "empty prediction"}
        repair_history = [{"sql": pred_sql, "result": exec_result}]

        # Repair loop
        repairs_done = 0
        for r in range(max_repairs):
            if exec_result["ok"] and exec_result.get("rows"):
                break
            repair_stats["attempted"] += 1
            repairs_done += 1
            error_msg = exec_result.get("error") or "empty result set"
            repair_prompt = render_prompt(
                repair_template,
                db_id=db_id,
                schema=schema,
                evidence=evidence_block,
                question=question,
                failed_sql=pred_sql,
                error=error_msg,
            )
            try:
                repair_completion = client.chat_completion(
                    messages=[
                        {"role": "system", "content": "You are an expert SQL debugging assistant."},
                        {"role": "user", "content": repair_prompt},
                    ],
                    temperature=cfg["model"]["temperature"],
                    top_p=cfg["model"]["top_p"],
                    max_tokens=cfg["model"]["max_tokens"],
                )
                repair_raw, _ = client.extract_content(repair_completion)
                new_sql = extract_sql(repair_raw)
                if new_sql and new_sql.lower() != pred_sql.lower():
                    pred_sql = new_sql
                    new_exec = db.execute(pred_sql)
                    # Track repair success / damage
                    old_ok = exec_result["ok"]
                    new_ok = new_exec["ok"]
                    if not old_ok and new_ok:
                        repair_stats["success"] += 1
                    elif old_ok and not new_ok:
                        repair_stats["damage"] += 1
                    exec_result = new_exec
                    repair_history.append({"sql": pred_sql, "result": exec_result})
                else:
                    break
            except Exception as e:
                errors.append({"question_id": qid, "stage": f"repair_{r}", "error": str(e)})
                break

        if (idx + 1) % 5 == 0 or idx + 1 == len(examples):
            print(f"  [{idx+1}/{len(examples)}] qid={qid} valid={exec_result['ok']} repairs={repairs_done}")

        predictions.append({
            "question_id": qid,
            "db_id": db_id,
            "question": question,
            "pred_sql": pred_sql,
            "gold_sql": gold_sql,
            "valid": exec_result["ok"],
            "raw_output": raw_output,
            "repair_history": repair_history,
            "latency": gen_latency,
            "request_id": gen_request_id,
        })

        traces.append({
            "question_id": qid,
            "db_id": db_id,
            "tools": [
                {"tool": "execute_sql", "input": h["sql"], "output": h["result"]}
                for h in repair_history
            ],
        })

    total_time = time.time() - total_start

    with open(pred_path, "w", encoding="utf-8") as f:
        for p in predictions:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    with open(trace_path, "w", encoding="utf-8") as f:
        for t in traces:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")

    summary = evaluate_predictions(
        dev_path=cfg["dataset"]["source"],
        pred_path=pred_path,
        db_root=cfg["dataset"]["db_root"],
        output_path=metrics_path.parent / "bird_official_eval.json",
    )
    summary["repair_stats"] = repair_stats

    for i, pred in enumerate(predictions):
        if not summary["per_query"][i]["ex"]:
            errors.append({
                "question_id": pred["question_id"],
                "db_id": pred["db_id"],
                "stage": "eval",
                "pred_sql": pred["pred_sql"],
                "gold_sql": pred["gold_sql"],
                "valid": pred["valid"],
            })

    with open(error_path, "w", encoding="utf-8") as f:
        for e in errors:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    with open(metrics_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    run_manifest = {
        "run_id": run_id,
        "experiment": cfg["experiment"],
        "experiment_name": cfg["experiment_name"],
        "description": cfg["description"],
        "started_at": datetime.fromtimestamp(total_start, tz=timezone.utc).isoformat(),
        "duration_seconds": round(total_time, 2),
        "config": cfg,
        "metrics": summary,
    }
    with open(run_dir / "run_manifest.json", "w", encoding="utf-8") as f:
        json.dump(run_manifest, f, indent=2, ensure_ascii=False)

    print(f"Run {run_id} complete.")
    print(f"Metrics: EX={summary['ex_rate']:.2f}%, Valid={summary['valid_rate']:.2f}%")
    print(f"Repair stats: {repair_stats}")


if __name__ == "__main__":
    import sys

    config_path = sys.argv[1] if len(sys.argv) > 1 else "configs/e4_bird_dev20_glm5.2.yaml"
    run_e4(config_path)
