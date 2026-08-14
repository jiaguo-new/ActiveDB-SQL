# Plugin Combination Experiments — 2026-08-14

## Results Summary

| Experiment | Config | EX | Rate | Δ from prev |
|------------|--------|-----|------|-------------|
| A | select only (ORM band) | 1068 | 69.6% | — |
| B | + repair (4 plugins) | 1085 | 70.7% | +17 |
| C | + judge (Route A tournament) | 1135 | 74.0% | +47 |
| D | + regen (Deep Regen) | 1156 | 75.4% | +21 |

## Per-Stage Contribution (cumulative from A=1068)

| Stage | Plugins | Δ | Share |
|-------|---------|---|-------|
| repair | value_grounding + execution_repair + join_repair + column_grounding | +17 | 19% |
| judge | route_a tournament | +47 | 53% |
| regen | deep_regen | +21 | 24% |
| missing | e3v_enhanced + e5det + multigen + preference + critique | (est +50) | — |

## Key Findings

1. **Judge is the biggest contributor** (+47, 53%) — Route A tournament rescue is the most valuable single stage
2. **Repair layer contributes modestly** (+17, 19%) — but it's cheap (some plugins are deterministic, no API call)
3. **Regen adds solid recovery** (+21, 24%) — DeepSeek's from-scratch generation on blind-spot questions
4. **Plugin framework works correctly** — all 4 experiments ran from YAML config, zero code changes between runs

## Comparison with Old Fixed Pipeline

| Version | EX | Notes |
|---------|-----|-------|
| Plugin framework (exp D, 7 plugins) | 1156 (75.4%) | Missing 5 stages from old pipeline |
| Old fixed pipeline (DeepSeek Flash tuned) | 1210 (78.88%) | Full 10-stage pipeline |
| Gap | 54 questions | Due to missing: E3v+ enhanced, E5det, MultiGen, E6 preference, E7 critique |

## Next Steps

1. Port remaining 5 plugins (e3v_enhanced, e5det, multigen, preference, critique) to close the 54-question gap
2. Experiment with new plugin combinations (e.g., regen before judge, or judge on regen output)
3. Try DeepSeek-V4-Pro selectively (e.g., only for judge or only for regen)

## Experiment F: Full 12-Plugin Pipeline (Added 2026-08-14)

| Stage | Plugins | EX | Delta |
|-------|---------|-----|-------|
| select | orm_band | 1068 (69.6%) | — |
| repair | value_grounding, execution_repair, join_repair, column_grounding, e3v_enhanced, e5_det_rules | 1090 (71.1%) | +22 |
| judge | route_a_tournament, multigen | 1134 (73.9%) | +44 |
| regen | deep_regen, preference_guided, result_critique | 1188 (77.4%) | +54 |

FINAL: EX = 1188/1534 = 77.4%

### Comparison

| Version | EX | Notes |
|---------|-----|-------|
| Plugin framework 12 plugins (exp F) | 1188 (77.4%) | Single YAML config |
| Old fixed pipeline v1 | 1189 (77.51%) | Hardcoded 10 steps |
| Old fixed pipeline tuned | 1210 (78.88%) | +3 rounds of manual tuning |
| Gap to tuned | 22 questions | Tuning portable to plugin config |

The plugin framework matches the old pipeline v1 with a single YAML config.
The remaining 22-question gap is from manual tuning (judge prompt optimization,
3-way judge, extra regen round) that can be ported as config changes.
