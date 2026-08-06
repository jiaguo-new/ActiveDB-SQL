#!/usr/bin/env python3
"""Score candidate SQLs with the ORM v2 merged BF16 model using vLLM.

Uses prompt_logprobs to read P(True) and P(False) at the last prompt position.
Input JSONL must contain candidates with `result` (rows or None).  No LoRA is
used; the model is expected to be a merged (base+adapter) checkpoint.
"""
from __future__ import annotations

import json
import math
import argparse
import sys
from pathlib import Path

from vllm import LLM, SamplingParams
from transformers import AutoTokenizer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def result_preview(rows, k=5):
    if rows is None:
        return "<execution failed / error>"
    if len(rows) == 0:
        return "<empty result set (0 rows)>"
    lines = [" | ".join(str(v) for v in row) for row in rows[:k]]
    return f"{len(rows)} rows (first {min(k, len(rows))}):\n" + "\n".join(lines)


def make_judge_prompt(base_prompt: str, candidate_sql: str, rows) -> str:
    return (
        base_prompt.rstrip()
        + "\n\n---\nCandidate SQL:\n```sql\n"
        + candidate_sql.strip()
        + "\n```\n\nExecution result of the candidate SQL:\n"
        + result_preview(rows)
        + "\n\nDoes the candidate SQL correctly answer the question? "
        + "Answer only True or False."
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-samples", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--gpu-mem", type=float, default=0.92)
    parser.add_argument("--swap-space", type=int, default=8)
    parser.add_argument("--prompt-logprobs", type=int, default=10)
    parser.add_argument("--max-model-len", type=int, default=8192)
    parser.add_argument("--enable-chunked-prefill", action="store_true", default=True)
    parser.add_argument("--disable-chunked-prefill", dest="enable_chunked_prefill", action="store_false")
    parser.add_argument("--enable-prefix-caching", action="store_true", default=True)
    parser.add_argument("--disable-prefix-caching", dest="enable_prefix_caching", action="store_false")
    args = parser.parse_args()

    tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
    true_token = tokenizer.encode("True", add_special_tokens=False)[-1]
    false_token = tokenizer.encode("False", add_special_tokens=False)[-1]

    print(f"Loading vLLM model from {args.model} ...")
    llm = LLM(
        model=str(args.model),
        trust_remote_code=True,
        dtype="bfloat16",
        tensor_parallel_size=1,
        max_model_len=args.max_model_len,
        gpu_memory_utilization=args.gpu_mem,
        swap_space=args.swap_space,
        disable_custom_all_reduce=True,
        enforce_eager=True,
        enable_chunked_prefill=args.enable_chunked_prefill,
        enable_prefix_caching=args.enable_prefix_caching,
    )
    print("Model loaded.")

    sampling = SamplingParams(
        max_tokens=1,
        temperature=0.0,
        logprobs=args.prompt_logprobs,
    )

    with open(args.input) as f:
        data = [json.loads(line) for line in f if line.strip()]
    if args.max_samples > 0:
        data = data[: args.max_samples]

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    total_batches = (len(data) - 1) // args.batch_size + 1
    written = 0

    # Resume: if the output file already has N lines, skip the first N samples.
    skip = 0
    if out_path.exists():
        with out_path.open() as f:
            skip = sum(1 for line in f if line.strip())
        if skip:
            print(f"Resuming: skipping {skip} already-scored samples.")
            data = data[skip:]
            written = skip

    with open(out_path, "a") as f:
        for b in range(total_batches):
            start = b * args.batch_size
            end = min(start + args.batch_size, len(data))
            batch = data[start:end]

            flat_prompts = []
            back_pointers = []
            for sample_idx, sample in enumerate(batch):
                # Truncate the shared base prompt ONCE per question (keep head
                # and tail of the schema/question text) so that every candidate
                # of the same question shares an identical token prefix and
                # vLLM prefix caching can reuse it.  The budget accounts for
                # the longest candidate suffix in this question plus chat
                # template overhead.
                cands = sample.get("candidates", [])
                suffix_lens = []
                for cand in cands:
                    sql = cand["sql"] if isinstance(cand, dict) else cand
                    rows = cand.get("result")
                    suffix = make_judge_prompt("", sql, rows)
                    suffix_lens.append(len(tokenizer.encode(suffix, add_special_tokens=False)))
                max_suffix = max(suffix_lens) if suffix_lens else 0
                base_budget = args.max_model_len - 48 - max_suffix
                base_ids = tokenizer.encode(sample["prompt"].rstrip(), add_special_tokens=False)
                if len(base_ids) > base_budget:
                    half = max(base_budget // 2, 1)
                    base_ids = base_ids[:half] + base_ids[-half:]
                    base_text = tokenizer.decode(base_ids, skip_special_tokens=False)
                else:
                    base_text = sample["prompt"].rstrip()
                for cand_idx, cand in enumerate(cands):
                    sql = cand["sql"] if isinstance(cand, dict) else cand
                    rows = cand.get("result")
                    prompt = make_judge_prompt(base_text, sql, rows)
                    chat_prompt = tokenizer.apply_chat_template(
                        [{"role": "user", "content": prompt}],
                        tokenize=False,
                        add_generation_prompt=True,
                    )
                    # Final safety: decode/encode round-trips can inflate the
                    # token count.  If the full prompt still exceeds the model
                    # context, hard-truncate from the left (prefix sharing is
                    # lost only for this candidate).
                    ids = tokenizer.encode(chat_prompt, add_special_tokens=False)
                    limit = args.max_model_len - 8
                    if len(ids) > limit:
                        ids = ids[-limit:]
                        chat_prompt = tokenizer.decode(ids, skip_special_tokens=False)
                    flat_prompts.append(chat_prompt)
                    back_pointers.append((sample_idx, cand_idx))

            outputs = llm.generate(flat_prompts, sampling)

            for out, (sample_idx, cand_idx) in zip(outputs, back_pointers):
                sample = batch[sample_idx]
                cand = sample["candidates"][cand_idx]
                logprobs = None
                if out.outputs and out.outputs[0].logprobs:
                    logprobs = out.outputs[0].logprobs[0]
                if logprobs:
                    lp_true = logprobs.get(true_token)
                    lp_false = logprobs.get(false_token)
                    if lp_true is None and lp_false is None:
                        score = 0.5
                    else:
                        l_true = lp_true.logprob if lp_true is not None else -100.0
                        l_false = lp_false.logprob if lp_false is not None else -100.0
                        score = math.exp(l_true) / (math.exp(l_true) + math.exp(l_false))
                else:
                    score = 0.5
                cand["orm_score"] = score

            for sample in batch:
                f.write(json.dumps(sample, ensure_ascii=False, default=str) + "\n")
                written += 1

            print(f"  Batch {b+1}/{total_batches}: scored {len(flat_prompts)} candidates ({start+1}-{end} queries)")

    print(f"\nSaved {written} scored samples -> {out_path}")


if __name__ == "__main__":
    main()
