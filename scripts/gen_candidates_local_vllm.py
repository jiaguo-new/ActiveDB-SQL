#!/usr/bin/env python3
"""Generate diverse candidate SQLs for specific questions using a local
vLLM model with temperature sampling.

Uses Qwen3-14B + sqlplus LoRA (train-split fine-tuned, compliant).
Generates N candidates per question at temperature>0 for diversity.
Read-only DB execution to attach results.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vllm import LLM, SamplingParams
from transformers import AutoTokenizer


def build_prompt(question: str, schema: str, evidence: str, db_id: str) -> str:
    """Build Qwen3-14B sqlplus prompt (matching training format)."""
    evidence_block = f"\n\nEvidence: {evidence}" if evidence else ""
    return (
        f"Generate a valid SQLite SELECT query to answer the following question.\n\n"
        f"Question: {question}{evidence_block}\n\n"
        f"Database: {db_id}\n{schema}\n\n"
        f"Output only the SQL query:"
    )


def extract_sql(text: str) -> str:
    text = text.strip()
    # strip markdown fences
    m = re.search(r"```sql\n(.*?)```", text, re.S)
    if m:
        return m.group(1).strip().rstrip(";")
    m = re.search(r"```\n(.*?)```", text, re.S)
    if m:
        return m.group(1).strip().rstrip(";")
    # take first SELECT
    m = re.search(r"(SELECT .*?)(?:;|$)", text, re.S | re.I)
    if m:
        return m.group(1).strip()
    return text.split("\n")[0].strip().rstrip(";")


def exec_sql(db_path: str, sql: str, limit: int = 5):
    try:
        uri = f"file:{db_path}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as conn:
            return [list(r) for r in conn.execute(sql).fetchmany(limit)]
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--lora-path", default=None)
    ap.add_argument("--dev", required=True)
    ap.add_argument("--db-root", required=True)
    ap.add_argument("--qids", default=None, help="JSON list of question ids (default: all)")
    ap.add_argument("--output", required=True)
    ap.add_argument("--n", type=int, default=8, help="candidates per question")
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--top-p", type=float, default=0.95)
    ap.add_argument("--max-tokens", type=int, default=1024)
    ap.add_argument("--gpu-mem", type=float, default=0.5)
    ap.add_argument("--max-model-len", type=int, default=4096)
    args = ap.parse_args()

    dev = json.load(open(args.dev))
    if args.qids:
        target_qids = set(json.load(open(args.qids)))
    else:
        target_qids = None  # process all questions

    # build prompts
    tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
    db_paths = {}
    for item in dev:
        db_paths[item["db_id"]] = str(Path(args.db_root) / item["db_id"] / f"{item['db_id']}.sqlite")

    prompts = []
    meta = []  # (qid, db_id, question, db_path)
    for item in dev:
        qid = item.get("question_id")
        if target_qids is not None and qid not in target_qids:
            continue
        db_id = item["db_id"]
        # get schema from DB
        db_path = db_paths[db_id]
        try:
            uri = f"file:{db_path}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=5) as conn:
                tables = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
                schema_parts = []
                for t in tables:
                    ddl = conn.execute(
                        f"SELECT sql FROM sqlite_master WHERE name='{t}'").fetchone()
                    if ddl:
                        schema_parts.append(ddl[0])
                schema = "\n".join(schema_parts[:20])  # limit schema length
        except Exception:
            schema = f"(tables: {tables})"

        evidence = item.get("evidence", "")
        prompt_text = build_prompt(item["question"], schema, evidence, db_id)
        chat_prompt = tokenizer.apply_chat_template(
            [{"role": "user", "content": prompt_text}],
            tokenize=False, add_generation_prompt=True,
        )
        # truncate
        ids = tokenizer.encode(chat_prompt, add_special_tokens=False)
        if len(ids) > args.max_model_len - args.max_tokens - 16:
            ids = ids[-(args.max_model_len - args.max_tokens - 16):]
            chat_prompt = tokenizer.decode(ids, skip_special_tokens=False)
        prompts.append(chat_prompt)
        meta.append((qid, db_id, item["question"], db_path, evidence))

    print(f"Generating for {len(prompts)} questions, n={args.n}", flush=True)

    # load model
    llm_kwargs = dict(
        model=args.model, trust_remote_code=True, dtype="bfloat16",
        tensor_parallel_size=1, max_model_len=args.max_model_len,
        gpu_memory_utilization=args.gpu_mem, swap_space=8,
        disable_custom_all_reduce=True, enforce_eager=True,
        enable_chunked_prefill=False, enable_prefix_caching=True,
    )
    if args.lora_path:
        llm_kwargs["enable_lora"] = True
        llm_kwargs["max_loras"] = 1
        llm_kwargs["max_lora_rank"] = 64
    llm = LLM(**llm_kwargs)

    sampling = SamplingParams(
        temperature=args.temperature, top_p=args.top_p,
        max_tokens=args.max_tokens, n=args.n,
    )

    # generate
    if args.lora_path:
        outputs = llm.generate(prompts, sampling, use_tqdm=True)
    else:
        outputs = llm.generate(prompts, sampling, use_tqdm=True)

    # collect candidates
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        for out, (qid, db_id, question, db_path, evidence) in zip(outputs, meta):
            cands = []
            seen = set()
            for choice in out.outputs:
                sql = extract_sql(choice.text)
                sql_clean = sql.strip().lower()
                if not sql_clean or sql_clean in seen:
                    continue
                seen.add(sql_clean)
                result = exec_sql(db_path, sql)
                cands.append({"sql": sql, "model": "qwen3-sqlplus", "result": result})
            f.write(json.dumps({
                "id": qid, "db_id": db_id, "question": question,
                "evidence": evidence,
                "candidates": cands, "n_candidates": len(cands),
            }, ensure_ascii=False, default=str) + "\n")
    print(f"Done -> {args.output}", flush=True)


if __name__ == "__main__":
    main()
