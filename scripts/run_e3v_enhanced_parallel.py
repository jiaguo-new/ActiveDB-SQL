#!/usr/bin/env python3
"""Runner for enhanced value probe (LIKE + date format) on current-chain failures."""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.db_utils import BirdDatabase
from tools.llm_client import LLMClient
sys.path.insert(0, str(ROOT / "agents"))
from e3v_enhanced_probe import probe_like_patterns, probe_date_formats
from e3v_value_probe import probe_values
from e4_execution_repair_agent import extract_sql


def _fmt_result(res):
    if not res.get("ok"):
        return f"Error: {res.get('error','unknown')}"
    rows = res.get("rows") or []
    if not rows: return "Empty result (0 rows)"
    return f"{len(rows)} rows: " + " | ".join(str(v) for v in rows[0][:5])

def _score(res):
    if not res.get("ok"): return (0,0)
    return (1, 1 if res.get("rows") else 0)

def process_one(args):
    ex, cur_pred, cfg, client = args
    qid=ex.get("question_id"); db_id=ex["db_id"]; question=ex["question"]
    evidence=ex.get("evidence",""); sql=cur_pred.get("pred_sql","")
    db=BirdDatabase(db_id=db_id,db_root=cfg["dataset"]["db_root"],
                    timeout=cfg["execution"]["timeout_seconds"],max_rows=cfg["execution"]["max_rows"])
    rec={"question_id":qid,"db_id":db_id,"question":question}
    if not sql.strip(): rec["pred_sql"]=""; return rec

    # enhanced probes
    like_info=probe_like_patterns(sql, question, db)
    date_info=probe_date_formats(sql, question, db)
    val_info=probe_values(sql, db, sample_limit=200)

    has_issues = like_info["has_issues"] or date_info["has_issues"] or len(val_info["repairs"])>0
    candidates=[("base",sql)]

    # det repair from value probe
    if val_info["repaired_sql"]!=sql:
        candidates.append(("det_val",val_info["repaired_sql"]))

    if has_issues:
        schema=db.get_schema()
        draft_res=db.execute(sql)
        # build combined report
        report_parts=[val_info["cell_values_text"]]
        if like_info["has_issues"]: report_parts.append("\nLIKE pattern suggestions:\n"+like_info["report_text"])
        if date_info["has_issues"]: report_parts.append("\nDate format suggestions:\n"+date_info["report_text"])
        combined_report="\n".join(report_parts)
        prompt_tmpl=open(cfg["prompt"]["template"]).read()
        prompt=(prompt_tmpl
            .replace("{db_id}",db_id).replace("{schema}",schema)
            .replace("{evidence}",f"## Evidence\n{evidence}" if evidence else "")
            .replace("{question}",question).replace("{draft_sql}",sql)
            .replace("{draft_result}",_fmt_result(draft_res))
            .replace("{cell_values}",combined_report))
        try:
            comp=client.chat_completion(
                messages=[{"role":"system","content":"You are an expert SQL assistant."},
                          {"role":"user","content":prompt}],
                temperature=0.0,top_p=1.0,max_tokens=cfg["model"]["max_tokens"])
            raw,_=client.extract_content(comp)
            new_sql=extract_sql(raw)
            if new_sql and new_sql.strip().lower()!=sql.strip().lower():
                candidates.append(("llm_repair",new_sql))
        except Exception as e: rec["llm_error"]=str(e)[:200]

    # pick best
    best_sql=sql; best_key=(-1,-1,0)
    prio={"llm_repair":3,"det_val":2,"base":1}
    for tag,cs in candidates:
        res=db.execute(cs); sc=_score(res)
        key=(sc[0],sc[1],prio.get(tag,0))
        if key>best_key: best_key=key; best_sql=cs; rec["chosen"]=tag
    rec["pred_sql"]=best_sql
    rec["has_like"]=like_info["has_issues"]; rec["has_date"]=date_info["has_issues"]
    return rec

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--config",required=True)
    ap.add_argument("--base-preds",required=True,type=Path)
    ap.add_argument("--dev",required=True,type=Path)
    ap.add_argument("--fail-qids",required=True,type=Path)
    ap.add_argument("--workers",type=int,default=8)
    ap.add_argument("--timeout",type=float,default=120.0)
    args=ap.parse_args()
    cfg=yaml.safe_load(open(args.config)); run_id=cfg["run_id"]
    out_dir=ROOT/"predictions"/run_id; out_dir.mkdir(parents=True,exist_ok=True)
    out_path=out_dir/"predictions.jsonl"
    dev=json.load(open(args.dev)); dev_by={d.get("question_id"):d for d in dev}
    base={}
    for l in open(args.base_preds):
        if l.strip(): d=json.loads(l); base[d["question_id"]]=d
    fail_ids=set(json.load(open(args.fail_qids)))
    done=set()
    if out_path.exists():
        for l in out_path.open():
            if l.strip(): done.add(json.loads(l)["question_id"])
        print(f"resume: {len(done)}",flush=True)
    todo=[(dev_by[q],base[q]) for q in sorted(fail_ids) if q not in done and q in base]
    print(f"todo: {len(todo)}",flush=True)
    if not todo: return
    client=LLMClient(base_url=cfg["model"]["base_url"],model_name=cfg["model"]["model_name"],
                     api_key_env=cfg["model"]["api_key_env"],timeout=args.timeout)
    t0=time.time(); written=0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs={pool.submit(process_one,(ex,bp,cfg,client)):ex.get("question_id") for ex,bp in todo}
        with out_path.open("a") as f:
            for fut in as_completed(futs):
                try: rec=fut.result(timeout=args.timeout+60)
                except Exception as e:
                    qid=futs[fut]; rec={"question_id":qid,"pred_sql":base[qid]["pred_sql"],"error":str(e)[:150]}
                f.write(json.dumps(rec,ensure_ascii=False)+"\n"); f.flush()
                written+=1
                if written%25==0: print(f"  [{written}/{len(todo)}] {time.time()-t0:.0f}s",flush=True)
    print(f"done: {written} -> {out_path}",flush=True)

if __name__=="__main__": main()
