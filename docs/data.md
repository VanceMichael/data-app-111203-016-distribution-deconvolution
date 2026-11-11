# 数据契约

渠道分析保存分群、转化延迟与上报日，按渠道汇总已观测转化和暴露数量。

主测量 `conversions` 的单位为次，主体字段是 `cohort`，核算维度是 `channel`。`observation_id` 是文件内唯一观测编号，主体允许重复观测。数值只接受有限值；空单元格解析为 None，不能直接当成零。额外列不进入当前核算。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `observation_id` | str | 必填 |
| `cohort` | str | 必填 |
| `channel` | str | 必填 |
| `start_day` | int | 允许空值，空值不是零 |
| `lag` | int | 允许空值，空值不是零 |
| `conversions` | float | 必填 |
| `exposures` | float | 允许空值，空值不是零 |
| `reported_day` | int | 允许空值，空值不是零 |
| `revision` | int | 允许空值，空值不是零 |

时间字段 day、week、tick、hour、minute、cycle、start_day、reported_day、known_at、visible_at、opened、observed 均以各字段名所述的相对时间单位表示整数；不同字段并不必然代表同一种时间。缺少的登记码与浓度等信息保持为空。示例只表达产品数据结构，不能代替实际调查资料。
