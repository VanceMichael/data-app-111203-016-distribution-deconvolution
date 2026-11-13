# 数据契约

渠道分析保存分群、转化延迟与上报日，按渠道汇总已观测转化和暴露数量。

主测量 `conversions` 的单位为次，主体字段是 `cohort`，核算维度是 `channel`。`observation_id` 是文件内唯一观测编号，主体允许重复观测。数值只接受有限值；空单元格解析为 None，不能直接当成零。额外列不进入当前核算。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `observation_id` | str | 必填 |
| `cohort` | str | 必填 |
| `channel` | str | 必填 |
| `start_day` | int | 必填 |
| `lag` | int | 必填 |
| `conversions` | float | 必填 |
| `exposures` | float | 必填 |
| `reported_day` | int | 必填 |
| `revision` | int | 必填 |

时间字段 day、week、tick、hour、minute、cycle、start_day、reported_day、known_at、visible_at、opened、observed 均以各字段名所述的相对时间单位表示整数；不同字段并不必然代表同一种时间。缺少的登记码与浓度等信息保持为空。示例只表达产品数据结构，不能代替实际调查资料。

## 业务字段语义

conversions 是 cohort 在 lag 天内的累计转化，不是当日增量。exposures 是整个 cohort 的暴露总数，各 lag 重复携带不能求和。reported_day 是上报日，revision 为同一 cohort、lag 的修订号。summary 只核对观测行中已上报数量。

## Nowcast 口径

nowcast 在某个上报截点 `as_of_day` 把分群分成成熟与未成熟两组，只对未成熟分群给最终量区间。处理顺序固定为：

1. **截点选择**：丢弃 `reported_day > as_of_day` 的行；同一 `(cohort, lag)` 只保留 `revision` 最大的一行。同一键出现相同修订号直接报错，不静默选行。
2. **累计曲线合法化**：按 lag 排序后，负值钳到当前累计水平并记 `negative_value`；任何累计下降拉回上一累计水平并记 `decreasing_cumulative`，原值与修复值都留在修复台账里。超过 `max_lag` 的 lag 不计入曲线但计数。
3. **成熟判定**：`age = as_of_day - start_day`，`age >= maturity_window` 为成熟。默认 `maturity_window=6`、`max_lag=5`，窗口不小于最大延迟，保证末端修订有结算时间。
4. **延迟模型训练**：只用成熟、曲线完整到 `max_lag` 且最终转化为正的分群。末端截断的成熟分群以 `tail_truncated` 排除并在结果中声明；零最终转化分群以 `zero_final_conversions` 排除。
5. **池化层次**：同渠道合格分群数达到 `min_donor_cohorts`（默认 2）时用渠道池；否则回退到全局池，并在每个估计结果里写明 `source_channels` 与 `donor_cohorts`；全局池也不足时返回 `status=refused`，拒绝外推。
6. **现报估计**：已观测为正时用比率法 `C_obs / q(a)`，其中 `q(a)` 是延迟分布到观测 lag 的累计质量；区间来自 Dirichlet 后验抽样与借样分群完成率异质性的 lognormal 乘子。已观测为零但有暴露时，改用借样分群单位暴露转化率的显式先验（`rate_prior_zero_observed`）。零暴露分群的合法最终量只能为零，直接钉到零。
7. **物理边界**：每次蒙特卡洛抽样都裁剪到 `[C_obs, exposures]`，输出区间因此天然非负、累计不下降且不超过分群规模。当 `q(a) < min_model_mass`（默认 0.05）时以 `model_mass_below_floor` 拒绝，避免在模型支持之外放大估计。

### 拒绝原因码

| 原因码 | 含义 |
| --- | --- |
| `no_observations_visible` | 截点处该分群没有任何可见行 |
| `observed_exceeds_exposures` | 已观测累计已经超过分群暴露规模，数据本身不合法 |
| `model_mass_below_floor` | 观测 lag 处模型累计质量过低，估计需要超支持外推 |
| `insufficient_training_history` | 渠道池与全局池都没有足够的成熟分群 |
| `no_rate_information_for_zero_observed` | 零观测分群缺少可用的单位暴露转化率先验 |

### 诊断标志

`delay_tvd` 是本群已观测增量的条件分布与模型条件延迟分布之间的总变差距离，超过 `drift_threshold`（默认 0.25）置 `delay_drift`。`direction` 比较两边的条件平均 lag：观测均值显著偏大是 `slower_reporting`（转化被压后、慢上报渠道的典型特征），显著偏小是 `faster_reporting`。曲线被修复时带 `curve_repaired`，可见 lag 未覆盖到 `min(age, max_lag)` 时带 `truncated_observed_curve`。

## 回测纪律

backtest 在每个历史截点只用当时可见的行重新执行 1–7 步，训练池逐截点重拟合，全期分布从不回填。真值取全量数据中 `max_lag` 的最终累计值；曲线仍被截断的分群不进入分母。覆盖率、MAPE 按整体、渠道与截点汇总；借样分群若在该截点尚未成熟会直接触发断言失败，作为未来信息泄漏的硬防线。
