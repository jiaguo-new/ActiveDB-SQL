To: bird.bench23@gmail.com
Subject: BIRD-SQL Test Submission — DAMENG-SUZHOU (Type 4, dev EX 78.88%, DeepSeek API)

Dear BIRD organizers,

Thank you for your feedback regarding API providers. We have switched from GLM to DeepSeek API (an accepted provider). We would like to submit our system ActiveDB-SQL for official test-set evaluation.

Team: DAMENG-SUZHOU

System Summary:
- Type: Type 4 (Combined Models — open-source LLMs + DeepSeek API)
- Dev result (official fast evaluator): EX = 1210/1534 = 78.88%
- Pipeline: 4 train-finetuned models (14B/32B) for candidate generation, ORM v2 scoring and band selection, 6-layer DB-active agent harness (value grounding, execution repair, JOIN repair, column grounding), Route A tournament (DeepSeek pairwise judge), multi-generator extension, deep regeneration, preference-guided generation and self-critique

Resource Requirements:
- GPU: 1x A100 80G, ~3 hours (candidate generation + ORM scoring). Pre-generated candidate pools are included in the code zip (runs/ directory) for API-only execution.
- API: DeepSeek-V4-Flash
  - Key: [REDACTED]
  - Base URL: https://api.deepseek.com
  - Model: deepseek-v4-flash
  - Prompt tokens on dev: ~30M
  - We will reset the key after evaluation completes
- CUDA: 12.2 or 12.3
- Python: 3.13

Model Weights:
- qwen3-14b-sqlplus-merged (LoRA): https://huggingface.co/jiaguo/qwen3-14b-sqlplus-merged
- omnisql-14b-bird-continue (LoRA): https://huggingface.co/jiaguo/omnisql-14b-bird-continue
- qwen3-14b-orm-v2-merged-bf16 (ORM scorer): https://huggingface.co/jiaguo/qwen3-14b-orm-v2-merged-bf16
- Qwen3-14B (base, public): https://modelscope.cn/models/Qwen/Qwen3-14B
- Qwen2.5-Coder-32B-Instruct (public): https://modelscope.cn/models/Qwen/Qwen2.5-Coder-32B-Instruct

All models trained only on BIRD train split (9428 examples, 0 overlap with dev).

column_meaning.json: Not required. Our system uses database DDL directly.

How to Run:
1. Unzip ActiveDB-SQL.zip
2. Place test data: data/test.json, data/test_databases/
3. Set API key: export DEEPSEEK_API_KEY="[REDACTED]"
4. Run: DEV=data/test.json DBROOT=data/test_databases bash run_all.sh

GPU candidate generation is optional — pre-generated pools are included in runs/.

Compliance:
- No ground truth SQL used in any prompt, RAG, or training
- All models trained only on BIRD train split
- Method does not rely on ground truth SQL
- Code includes logging + resume-safe error handling

Attachments:
1. ActiveDB-SQL.zip — code (scripts, configs, prompts, agents, tools, evaluation, pre-generated candidate pools)
2. final_1235.jsonl — dev predictions (EX = 1210/1534 = 78.88%)

We are happy to assist with any setup questions.

Best regards,
DAMENG-SUZHOU
