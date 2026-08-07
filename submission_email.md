To: bird.bench23@gmail.com
Subject: BIRD-SQL Test Submission — ActiveDB-SQL (Type 4, dev EX 80.18%)

Dear BIRD organizers,

We would like to submit our system **ActiveDB-SQL** for official test-set evaluation.

## System Summary

- **Type**: Type 4 (Combined Models — open-source LLMs + GLM-5.2 API)
- **Dev result** (official fast evaluator): EX = 1230/1534 = 80.18%
- **Pipeline**: 4 train-finetuned models → ORM v2 selection → 6-layer DB-active
  agent harness → Route A tournament → deep regeneration → preference-guided generation

## Resource Requirements

- **GPU**: 1× A100 80G, ~3 hours (for candidate generation + ORM scoring)
  - If GPU is unavailable, we provide **pre-generated candidate pools** so only
    the API-based stages need to run (~2-4 hours, no GPU)
- **API**: GLM-5.2 (ZhipuAI, OpenAI-compatible)
  - Key: [provided below]
  - Prompt tokens on dev: ~30M
- **CUDA**: 12.2 or 12.3
- **Python**: 3.13

## Model Weights

| Model | Location |
|-------|----------|
| qwen3-14b-sqlplus-merged (LoRA) | https://huggingface.co/jiaguo/qwen3-14b-sqlplus-merged |
| omnisql-14b-bird-continue (LoRA) | https://huggingface.co/jiaguo/omnisql-14b-bird-continue |
| qwen3-14b-orm-v2-merged-bf16 | https://huggingface.co/jiaguo/qwen3-14b-orm-v2-merged-bf16 |
| Qwen3-14B (base, public) | https://modelscope.cn/models/Qwen/Qwen3-14B |
| Qwen2.5-Coder-32B-Instruct (public) | https://modelscope.cn/models/Qwen/Qwen2.5-Coder-32B-Instruct |

All models trained **only on BIRD train split** (9428 examples, 0 dev overlap).

## column_meaning.json

We do **NOT** require column_meaning.json. Our system uses database DDL directly.

## API Key

GLM-5.2 API key: [your key here]
Base URL: https://open.bigmodel.cn/api/paas/v4/
Model: glm-5.2
We will reset the key after evaluation completes.

## Attachments

1. **SUBMISSION_README.md** — detailed instructions with commands
2. **ActiveDB-SQL.zip** — code (scripts, configs, prompts, agents, tools, evaluation, pre-generated candidate pools)
3. **final_1235.jsonl** — dev predictions (EX = 1230/1534 = 80.18%)

## Compliance

- No dev gold SQL in any prompt/RAG/training
- k5 lineage audit PASS (0 true-leak hits)
- Method does not rely on ground truth SQL
- Code includes logging + resume-safe error handling

Detailed instructions are in SUBMISSION_README.md. We are happy to assist with
any setup questions.

Best regards,
[Your name / team]
[Contact email]
