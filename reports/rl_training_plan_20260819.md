# RL 训练路线规划 — 突破 84.7% Oracle 上限

> 目标：90% EX (1381/1534)。当前 1188 (77.4%)，完美选择上限 1300 (84.7%)。
> RL 是唯一能突破模型能力上限的路径（已有数据证明：同模型多采样仅 +2 题）。

---

## 一、现状与依据

| 事实 | 数据来源 |
|------|----------|
| 推理时优化饱和 | 9 轮自动发现全部拒绝（trials.jsonl）|
| 同模型扩采样无效 | GPU 扩池 2768 新候选仅 +2 正解 |
| Oracle 上限 | 1300/1534 = 84.7%（旧池 110 + 新候选 2）|
| 盲区题 | 234 题所有现有模型都做错 |
| 上游已有训练基础 | 8 个 LoRA 模型有 train_results（SFT+RFT 流程可复用）|

## 二、技术路线（三阶段递进）

### Phase 1：RFT 迭代（Rejection-sampling Fine-Tuning）— 风险最低，先跑

**原理**：用当前模型在 train 上生成 N 候选 → 保留执行正确的 → 微调。这是"简化版 RL"（相当于 positive-only policy gradient），上游已跑通过。

**步骤**：
1. 用 OmniSQL-14B（最强单模型 greedy 64.6%）在 train 9428 题上 N=8 采样
2. 执行验证：候选结果 == train gold 结果 → 保留（约 60-70% 题有正解）
3. 对有正解的题，用正解候选做 SFT（LoRA r=16）
4. 对无正解的题（~30%），用 train gold SQL 直接做 SFT（teacher forcing）
5. 训练 1-2 epoch → 新模型 → 在 dev 上合规评测

**预期**：+3~8 题（RFT 论文报告 BIRD 上约 +1~2pp）
**成本**：GPU 生成 2h + LoRA 训练 6h = 1 天
**合规**：✅ 只用 train gold 做执行比对和 SFT，不碰 dev

### Phase 2：GRPO 在线强化（Group Relative Policy Optimization）

**原理**：RFT 是离线的（一次采样一次训练）。GRPO 在线采样：每步生成一组候选 → 组内相对奖励 → 策略梯度更新。对"模型总差一点"的题最有效（负样本也有梯度信号）。

**步骤**：
1. **奖励函数**（分层）：
   - 执行匹配 train gold：+1.0（主奖励）
   - SQL 可执行但结果错：+0.1（鼓励语法正确）
   - 执行报错：-0.2（惩罚）
   - 空结果：-0.1
2. **训练配置**：
   - 基座：Phase 1 的 RFT 模型（热启动）
   - 框架：TRL GRPOTrainer 或 verl（llm-ft env）
   - group_size=8（每题采 8 个），batch=4 题，grad_accum=4
   - LoRA r=16（显存友好）
   - KL 惩罚 β=0.05（防漂移）
3. **数据**：train 9428 题 × 2 epoch
4. **早停**：每 500 步在 train heldout（切 500 题做验证）评测

**预期**：+8~15 题（Reward-SQL 报告 7B 模型 +16pp；我们是 14B 已有 64.6% 基础，增幅会小）
**成本**：GRPO 训练 14B LoRA ≈ 20-30 GPU 时（GB10 上 1-2 天）
**合规**：✅ 奖励信号来自 train gold 执行比对；dev 从不进入训练循环

### Phase 3：PRM 过程奖励（如果 Phase 2 增益不足）

**原理**：结果奖励（0/1）太稀疏。PRM 给每个 SQL 子步骤（表选择/JOIN/WHERE/聚合）打分，提供密集奖励。

**步骤**：
1. 用 train gold SQL 的结构分解（表集合、JOIN 数、聚合类型）作为"过程标签"
2. 训练 PRM：输入 (question, schema, candidate_sql) → 输出各子步骤正确概率
   - 可复用 ORM v2 基座（已有结果级判断能力，扩展到结构级）
3. GRPO 奖励 = 0.6×结果奖励 + 0.4×PRM 分数

**预期**：在 Phase 2 基础上再 +3~5 题
**成本**：PRM 训练 4h + GRPO 重训 1-2 天
**合规**：✅ PRM 训练标签来自 train gold 结构分解

## 三、合规门禁（每一阶段强制）

```yaml
training_data:
  source: bird_train_official.json  # 9428 题，与 dev 零重叠（已验证）
  gold_usage: execution_reward_only  # gold 只用于执行比对产生奖励
  forbidden:
    - dev.json 任何字段进训练
    - dev 问题/模式进 prompt
    - 用 dev EX 做训练早停（早停只用 train heldout）
evaluation:
  dev_usage: final_evaluation_only  # 只在训练完成后做一次 dev 全量评测
  script: evaluation/bird_official_eval_fast.py
  audit: compliance_audit.py
```

## 四、集成到插件框架

训练完成后，新模型作为一个插件接入：

```yaml
# pipeline_config_rl.yaml
plugins:
  - stage: select
    name: orm_band
    module: plugins.select.orm_band
    config:
      pool: runs/rl_expanded_pool.jsonl  # RL 模型生成的新候选池
      band: 0.05
  # ... 其余插件不变
```

自动发现引擎可继续在扩展后的池上搜索。

## 五、时间线

| 周 | 任务 | 产出 |
|----|------|------|
| 1 | Phase 1 RFT（数据构造+训练+评测）| RFT 模型 + dev 分数 |
| 2 | Phase 2 GRPO（训练+调参）| RL 模型 + dev 分数 |
| 3 | Phase 3 PRM（如需）+ 插件集成 + 自动发现 | 最终配置 |

## 六、风险与备选

| 风险 | 概率 | 缓解 |
|------|------|------|
| GRPO 训练不稳定（14B 大模型）| 中 | 从小 LoRA rank 开始，强 KL 约束 |
| GB10 显存不足（GRPO 需 policy+ref 双模型）| 中 | 用 4-bit ref model 或减小 group_size |
| RL 增益 < 5 题 | 中 | Phase 3 PRM 或换 32B 基座 |
| 训练后 dev 反降（过拟合 train 分布）| 低 | 早停 + LoRA 弱更新 + ensemble（新旧模型候选都入池）|

## 七、立即行动项

1. [ ] 构造 RFT 数据：train 上 OmniSQL N=8 采样 + 执行验证（GPU 2h）
2. [ ] 跑 Phase 1 LoRA SFT（llamafactory env，复用上游配置模板）
3. [ ] dev 合规评测 → 决定是否进 Phase 2
