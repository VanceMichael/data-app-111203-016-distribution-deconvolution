"""滚动回测：在每个历史截点重新拟合延迟模型，事后用成熟真值检验区间。

回测纪律
--------
* 每个 cut 只喂入 ``reported_day <= cut`` 的记录，延迟模型在该截点重新拟合，
  训练池只包含当时已成熟且曲线完整的分群；全期分布永远不回填到过去截点。
* 真值取全量数据中各 cohort 在 max_lag 的最终累计值；在评估时点尚未成熟的
  cohort 不进入分母。
* 覆盖率等指标按截点与渠道分别汇总，便于发现区间系统性偏窄或偏宽。
"""

from .nowcast import NowcastConfig, build_cohorts, run_nowcast

def build_truth(records, config):
    """全量数据下每个 cohort 的最终累计转化。

    必须从合法化后的完整曲线取 max_lag，而不是直接拿原始修订值，否则负向修订
    污染过的非法终值会被当成回测真值。
    """
    if not records:
        return {}
    full_day = max(row['reported_day'] for row in records)
    cohorts = build_cohorts(records, full_day, config)
    truth = {}
    for cohort in cohorts:
        if config.max_lag not in cohort.curve:
            continue
        truth[cohort.cohort] = {
            'final_conversions': cohort.curve[config.max_lag],
            'channel': cohort.channel,
            'start_day': cohort.start_day,
        }
    return truth


def backtest(records, cutoff_days, config=None):
    config = config or NowcastConfig()
    config.validate()
    if not records:
        raise ValueError('回测输入为空，无法确定时间范围')
    cutoff_days = sorted(cutoff_days)
    if not cutoff_days:
        raise ValueError('至少需要一个回测截点')
    truth = build_truth(records, config)
    full_cohorts = build_cohorts(records, max(row['reported_day'] for row in records),
                                 config)
    full_by_cohort = {cohort.cohort: cohort for cohort in full_cohorts}

    evaluations = []
    for cut in sorted(cutoff_days):
        visible = [row for row in records if row['reported_day'] <= cut]
        if not visible:
            continue
        result = run_nowcast(visible, cut, config)
        for item in result['immature_cohorts']:
            cohort = item['cohort']
            target = truth.get(cohort)
            if target is None:
                continue
            state = full_by_cohort[cohort]
            # 全量数据里仍未成熟或曲线被截断的 cohort 不能充当回测真值。
            if config.max_lag not in state.curve:
                continue
            entry = {
                'cutoff_day': cut,
                'cohort': cohort,
                'channel': item['channel'],
                'age': item['age'],
                'status': item['status'],
                'refusal_reason': item['refusal_reason'],
                'observed_conversions': item['observed_conversions'],
                'truth_final': target['final_conversions'],
                'point': None,
                'interval': None,
                'covered': None,
                'absolute_percentage_error': None,
                'donor_cohorts': None,
                'pooling_level': None,
            }
            if item['status'] == 'estimated':
                estimate = item['estimate']
                lower, upper = item['interval']
                point = estimate['point']
                entry.update(
                    point=point,
                    interval=[lower, upper],
                    covered=bool(lower - 1e-12 <= target['final_conversions']
                                 <= upper + 1e-12),
                    absolute_percentage_error=(
                        abs(point - target['final_conversions'])
                        / target['final_conversions']
                        if target['final_conversions'] > 0 else
                        abs(point - target['final_conversions'])),
                    donor_cohorts=item['pooling']['donor_cohorts'],
                    pooling_level=item['pooling']['level'])
                # 泄漏防线：训练分群在该截点必须已经成熟。
                for donor in item['pooling']['donor_cohorts']:
                    if full_by_cohort[donor].start_day + config.maturity_window > cut:
                        raise AssertionError(
                            f'cut={cut} 估计 {cohort} 时借样 {donor} 尚未成熟，'
                            '存在未来信息泄漏')
            evaluations.append(entry)

    return {
        'report': 'nowcast_backtest',
        'schema_version': 1,
        'config': config.as_dict(),
        'cutoff_days': sorted(cutoff_days),
        'evaluations': evaluations,
        'metrics': _aggregate(evaluations, config.confidence),
    }


def _aggregate(evaluations, confidence):
    estimated = [entry for entry in evaluations if entry['status'] == 'estimated']
    refused = [entry for entry in evaluations if entry['status'] == 'refused']

    def summarize(rows):
        if not rows:
            return None
        covered = [row for row in rows if row['covered'] is not None]
        errors = [row['absolute_percentage_error'] for row in rows
                  if row['absolute_percentage_error'] is not None]
        return {
            'cases': len(rows),
            'coverage': (sum(row['covered'] for row in covered) / len(covered)
                         if covered else None),
            'nominal_confidence': confidence,
            'mean_absolute_percentage_error': (
                sum(errors) / len(errors) if errors else None),
            'median_absolute_percentage_error': (
                sorted(errors)[len(errors) // 2] if errors else None),
        }

    by_channel = {}
    for entry in estimated:
        by_channel.setdefault(entry['channel'], []).append(entry)

    return {
        'overall': summarize(estimated),
        'refusals': len(refused),
        'refusal_reasons': sorted({row['refusal_reason'] for row in refused}),
        'by_channel': {
            channel: summarize(rows) for channel, rows in sorted(by_channel.items())},
        'by_cutoff': {
            str(cut): summarize([row for row in estimated
                                 if row['cutoff_day'] == cut])
            for cut in sorted({row['cutoff_day'] for row in estimated})},
    }
