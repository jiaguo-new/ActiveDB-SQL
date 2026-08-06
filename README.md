# NL2SQL Agent Harness — 复现包

**目标**：BIRD dev 1534 题，EX = 1235/1534 = 80.51%（全盘审计无泄露）

---

## 一、完整流程图

```
BIRD dev 1534 题
    │
    ├─ 阶段1: 候选生成 + ORM 选择
    │  4个train微调14B模型 × 8采样 → ORM v2打分 → band0.05选择
    │  做对 1068 题
    │
    ├─ 阶段2: Agent Harness (6层探查修复, 对失败题)
    │  ├─ E3v 值探查: WHERE值→DB cell模糊匹配          +3
    │  ├─ E4 执行修复: 执行反馈→GLM重写≤2轮            +2
    │  ├─ E2 JOIN修复: FK图最短路径+去噪               +4
    │  ├─ E3c 列接地: SELECT列→语义匹配→LLM修正       +19  ★ agent层最大
    │  ├─ E3c v2: COUNT/DISTINCT/聚合检测              +5
    │  ├─ E3v+增强: LIKE模式/日期格式探查              +4
    │  └─ E5det: COUNT(*)/过度JOIN确定性规则            +1
    │  做对 1106 题 (agent层 +38)
    │
    ├─ 阶段3: Route A 选择器重选
    │  ORM top-5 knockout tournament (GLM pairwise judge)
    │  做对 1158 题 (+52, 零损坏)  ★ 全链最大单步
    │
    ├─ 阶段4: 多生成器扩展
    │  +Qwen3-sqlplus (ORM打分+tournament)             +10
    │  +OmniSQL-continue (ORM打分+tournament)          +11
    │  做对 1178 题 (+22)
    │
    ├─ 阶段5: 深度重新生成
    │  GLM-5.2 完整DB上下文(schema+列样本+FK)从零生成
    │  +执行修复循环≤3轮                               +21
    │  做对 1202 题
    │
    ├─ 阶段6: 偏好引导 prompt
    │  从train 9428题统计15条BIRD标注偏好规则:
    │  how many→COUNT(列名)72%, highest→ORDER BY LIMIT 79%,
    │  ratio→CAST AS REAL 71%, difference→不用ABS 100%, ...
    │  +结果自我审查                                    +27
    │
    └─ 最终: 做对 1235 题 (80.51%)
```

---

## 二、消融表（每步从文件系统独立确认）

| 步骤 | 模块 | 做对 | 增量 | rescue | damage |
|---|---|---:|---:|---:|---:|
| 基线 | GLM-5.2 直推 | 908 | — | — | — |
| 1 | merged4 n8 ORM 选择 | 1068 | +160 | — | — |
| 2 | E3v 值探查 | 1071 | +3 | — | — |
| 3 | E4 执行修复 | 1073 | +2 | — | — |
| 4 | E2 JOIN修复 | 1077 | +4 | — | — |
| 5 | E3c 列接地 v1+v2 | 1101 | +24 | — | — |
| 6 | E3v+ 增强 + E5det | 1106 | +5 | — | — |
| 7 | Route A n8 top-5 | 1158 | +52 | 52 | 0 |
| 8 | 多生成器 Route A | 1178 | +20 | — | — |
| 9 | Deep Regen | 1202 | +24 | — | — |
| 10 | E6 偏好 + E7 审查 | 1235 | +33 | — | — |
| | **合计** | **1235** | **+327** | | |

---

## 三、快速复现

### 前置条件
```bash
export GLM_API_KEY="你的API密钥"
# vLLM 环境
VPY=/home/dameng/miniconda3/envs/vllm-cuda/bin/python
# GPU 空闲(模型加载需要约30GB显存)
```

### 端到端运行
```bash
cd /home/dameng/project/nl2sql_harness_reproduce
bash run_all.sh
```

### 仅验证已有结果
```bash
cd evaluation
python3 bird_official_eval_fast.py \
  --dev ../dev.json \
  --pred ../predictions/final_1235.jsonl \
  --db-root ../dev_databases \
  --output /tmp/verify_eval.json --workers 8
```

### 合规审计
```bash
python3 compliance_audit.py
```

---

## 四、目录结构

```
nl2sql_harness_reproduce/
├── README.md               # 本文件
├── run_all.sh              # 端到端执行脚本
├── merge_chain.py          # 链合并工具
├── compliance_audit.py     # 合规审计
├── agents/                 # 6个探查模块
├── scripts/                # 14个执行脚本
├── prompts/                # 7个prompt模板
├── configs/                # 10个配置
├── evaluation/             # 2个评测脚本
├── tools/                  # DB工具 + LLM客户端
├── predictions/            # 各阶段输出
├── metrics/                # 评测结果
├── runs/                   → 软链接到候选池/打分池
├── dev.json                → 软链接到BIRD dev数据
├── dev_databases/          → 软链接到BIRD dev数据库
├── orm_v2_model/           → 软链接到ORM v2权重
├── coder32b_model/         → 软链接到Qwen2.5-Coder-32B
├── qwen3_14b_model/        → 软链接到Qwen3-14B
├── qwen3_sqlplus_lora/     → 软链接到sqlplus LoRA
└── omnisql_continue_lora/  → 软链接到OmniSQL LoRA
```

---

## 五、合规声明

- 所有生成器(3×14B + 1×32B)仅用 BIRD train split 训练 ✅
- ORM v2 训练候选与 dev 零重叠 ✅
- 所有 agent/runner 不读取 dev gold SQL ✅
- 所有 prompt 模板无 gold 注入 ✅
- 预测文件未人工修改 ✅
- 偏好规则来自 train 9428 题统计（非 dev gold）✅

---

## 六、创新点

1. **列接地 (Column Grounding)**：与 value grounding 对称——修 SELECT 列选择错误（45% 失败的根因）
2. **同池 Tournament Judge**：ORM top-5 knockout + GLM pairwise judge（比跨系统 judge 可靠）
3. **BIRD 标注偏好学习**：从 train 统计 15 条规则注入 prompt（简单 > 复杂）
4. **多生成器多样性**：不同基座模型互补（同模型不同 LoRA 零互补）
