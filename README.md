# distribution-deconvolution

渠道分析保存分群、转化延迟与上报日，按渠道汇总已观测转化和暴露数量。nowcast 在上报截点把近期未成熟分群与成熟分群分开，给预算会提供带不确定性的当前最终量估计，避免慢上报渠道被提前降预算。

## 使用

运行需要 Python 3.11 或更新版本，没有第三方运行依赖。数据在本地处理，不请求外部接口。

```bash
python3 -m analysis_app summary
python3 -m analysis_app summary --input data/observations.csv --output output/summary.json
python3 -m analysis_app nowcast --input data/observations.csv --as-of 27 --output output/nowcast.json
python3 -m analysis_app backtest --input data/observations.csv --cutoffs 11,14,17,20,23,26 --output output/backtest.json
```

标准输出是完整 JSON，输出文件使用临时文件原子替换；失败退出码为 2，不覆盖既有结果。空数据集保留零计数与空分组，缺失数值不进入数值分母。数据指纹由按 observation_id 排序后的解析记录计算。

### nowcast 子命令

`--as-of` 是上报截点，只使用 `reported_day` 不晚于该日的行，同一 `(cohort, lag)` 取最新 revision。输出包含：

- `mature_cohorts`：`age >= maturity_window` 的分群，标记曲线是否完整到 `max_lag`；
- `immature_cohorts`：每个未成熟分群的点估计、置信区间（默认 90%）、当前已观测占比、池化借样来源与延迟漂移诊断；不满足支持条件时 `status=refused` 并给出原因码，而不是强行外推；
- `training`：渠道池/全局池的延迟 PMF、实际借样分群清单，以及被排除分群（末端截断、零最终转化）的原因。

关键配置：`--max-lag`（默认 5）、`--maturity-window`（默认 6，必须不小于 max_lag）、`--min-donor-cohorts`（默认 2）、`--simulations`（默认 2000）、`--confidence`（默认 0.90）、`--min-model-mass`（默认 0.05）。蒙特卡洛由固定种子驱动，同输入同截点结果完全可复现。

### backtest 子命令

`--cutoffs` 是逗号分隔的历史截点。每个截点重新拟合延迟模型，只用当时已成熟的同渠道（或全局池）分群，再用全量数据成熟后的真实最终量核对区间覆盖率与 MAPE。指标按整体、渠道、截点汇总；任何借样分群在当时尚未成熟都会直接报错，防止全期分布回填。

## 验证与容器

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q analysis_app
docker build -t distribution-deconvolution .
docker run --rm distribution-deconvolution
```

容器内也可执行 `python -m unittest discover -s tests -v`。镜像构建阶段取得 Python 镜像，产品启动、测试和演示均无需下载。小规模样例的完整单元测试和 summary 可以连续执行，不需要等待定时任务。

## 代码与数据

- `analysis_app/io.py`：有类型的 CSV 读取、完整性校验及内容指纹。
- `analysis_app/schema.py`：字段类型、主体与聚合维度。
- `analysis_app/domain.py`：当前业务核算函数。
- `analysis_app/summary.py`：确定性的分组与数值口径。
- `analysis_app/nowcast.py`：截点视图、累计曲线合法化、延迟池化与带不确定性的现报。
- `analysis_app/backtest.py`：逐截点重拟合的滚动回测与覆盖率核对。
- `analysis_app/cli.py`：本地命令及原子输出。
- `docs/data.md`：数据契约、nowcast 口径、拒绝原因码与回测纪律。

演示数据全部为本地合成，不对应现实机构、设备或人员；不能用来推断真实业务效果。合成数据包含负向修订（C25）、零暴露（C26）、突发上报延迟（C27）与末端截断（C28）等边界场景。
