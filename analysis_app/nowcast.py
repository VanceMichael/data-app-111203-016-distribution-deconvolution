"""Nowcast 引擎：在某个上报截点把成熟与未成熟分群分开估计。

口径约定
--------
* 每行是 cohort 在 lag 天内的*累计*转化；exposures 是分群规模，每个 lag 重复携带。
* 截点 as_of_day 处只保留 ``reported_day <= as_of_day`` 的行，同一 (cohort, lag)
  取 revision 最大的一行。
* 延迟分布只能从截点处已经成熟（age >= maturity_window）且曲线完整到 max_lag、
  最终转化为正的同渠道分群学习；渠道历史不足时回退到全局池化，并在结果中声明
  借样来源；连全局样本都不足时拒绝估计而不是外推。
* 估计区间通过蒙特卡洛得到，每次抽样都裁剪到 [已观测累计, exposures]，因此输出
  天然满足非负、累计不下降且不超过 exposures。
"""

import math
import random
import zlib
from collections import defaultdict
from dataclasses import dataclass, field

from .io import fingerprint
from .schema import PRODUCT, UNIT


# 拒绝估计的原因码，调用方可以据此决定是否在预算会上人工复核。
REFUSAL_NO_VIEW = 'no_observations_visible'
REFUSAL_EXPOSURE = 'observed_exceeds_exposures'
REFUSAL_SUPPORT = 'model_mass_below_floor'
REFUSAL_NO_DONORS = 'insufficient_training_history'
REFUSAL_NO_RATE = 'no_rate_information_for_zero_observed'

DIRECTION_SLOWER = 'slower_reporting'
DIRECTION_FASTER = 'faster_reporting'
DIRECTION_ALIGNED = 'aligned'


@dataclass(frozen=True)
class NowcastConfig:
    max_lag: int = 5
    maturity_window: int = 6
    min_donor_cohorts: int = 2
    simulations: int = 2000
    confidence: float = 0.90
    drift_threshold: float = 0.25
    min_model_mass: float = 0.05
    direction_threshold: float = 0.25
    base_seed: int = 20261113

    def validate(self):
        if self.max_lag < 0:
            raise ValueError('max_lag 不能为负')
        if self.maturity_window < self.max_lag:
            raise ValueError('maturity_window 必须不小于 max_lag，修订才能结算')
        if self.min_donor_cohorts < 1:
            raise ValueError('min_donor_cohorts 至少为 1')
        if self.simulations < 100:
            raise ValueError('simulations 至少为 100')
        if not 0.0 < self.confidence < 1.0:
            raise ValueError('confidence 必须落在 (0, 1)')
        if not 0.0 <= self.drift_threshold <= 1.0:
            raise ValueError('drift_threshold 必须落在 [0, 1]')
        if not 0.0 < self.min_model_mass < 1.0:
            raise ValueError('min_model_mass 必须落在 (0, 1)')
        if not 0.0 <= self.direction_threshold:
            raise ValueError('direction_threshold 不能为负')
        if not isinstance(self.base_seed, int):
            raise ValueError('base_seed 必须是整数')

    def as_dict(self):
        return {
            'max_lag': self.max_lag,
            'maturity_window': self.maturity_window,
            'min_donor_cohorts': self.min_donor_cohorts,
            'simulations': self.simulations,
            'confidence': self.confidence,
            'drift_threshold': self.drift_threshold,
            'min_model_mass': self.min_model_mass,
            'direction_threshold': self.direction_threshold,
            'base_seed': self.base_seed,
        }


@dataclass
class CohortState:
    cohort: str
    channel: str
    start_day: int
    exposures: float
    age: int
    curve: dict = field(default_factory=dict)  # lag -> 合法化后的累计转化
    repairs: list = field(default_factory=list)
    ignored_lags: int = 0

    @property
    def observed_lag(self):
        return max(self.curve) if self.curve else None

    @property
    def observed_conversions(self):
        lag = self.observed_lag
        return None if lag is None else self.curve[lag]

    def is_complete(self, max_lag):
        return max_lag in self.curve


def latest_revisions(records, as_of_day):
    """按 reported_day 截止，每个 (cohort, lag) 保留 revision 最大的行。"""
    if as_of_day is None or as_of_day < 0:
        raise ValueError('as_of_day 必须是非负整数')
    view = {}
    for row in records:
        if row['reported_day'] > as_of_day:
            continue
        key = (row['cohort'], row['lag'])
        current = view.get(key)
        if current is not None and row['revision'] == current['revision']:
            if row['conversions'] != current['conversions']:
                raise ValueError(
                    f"同一 cohort/lag 的修订号重复且数值冲突：{row['cohort']} "
                    f"lag={row['lag']} revision={row['revision']}")
            continue
        if current is None or row['revision'] > current['revision']:
            view[key] = row
    return view

def legalize_curve(pairs):
    """把 (lag, 累计值) 序列转成非负、累计不下降的合法曲线，返回 (curve, repairs)。

    负向修订先钳到当前累计水平（负值本身不合法），随后任何下降都被拉回上一个
    累计水平；每次干预都记录原值、修复值与原因，供数据质量诊断。
    """
    curve = {}
    repairs = []
    running = 0.0
    for lag, value in sorted(pairs):
        if lag < 0:
            raise ValueError(f'lag 不能为负：{lag}')
        fixed = value
        if fixed < 0.0:
            repairs.append({'lag': lag, 'raw': value, 'fixed': running,
                            'reason': 'negative_value'})
            fixed = running
        elif fixed < running:
            repairs.append({'lag': lag, 'raw': value, 'fixed': running,
                            'reason': 'decreasing_cumulative'})
            fixed = running
        curve[lag] = fixed
        running = fixed
    return curve, repairs


def build_cohorts(records, as_of_day, config):
    """从截点视图构建每个 cohort 的合法累计曲线与暴露规模。"""
    view = latest_revisions(records, as_of_day)
    grouped = defaultdict(list)
    for (cohort, lag), row in view.items():
        grouped[cohort].append(row)

    cohorts = []
    for cohort in sorted(grouped):
        rows = grouped[cohort]
        channels = {row['channel'] for row in rows}
        starts = {row['start_day'] for row in rows}
        exposures = {row['exposures'] for row in rows}
        if len(channels) != 1 or len(starts) != 1:
            raise ValueError(f'cohort {cohort} 的 channel/start_day 登记不一致')
        if len(exposures) != 1:
            raise ValueError(f'cohort {cohort} 的 exposures 在不同行中不一致')
        exposure = exposures.pop()
        if exposure < 0:
            raise ValueError(f'cohort {cohort} 的 exposures 为负')
        start_day = starts.pop()
        age = as_of_day - start_day

        pairs = []
        ignored = 0
        for row in rows:
            if row['lag'] < 0:
                raise ValueError(f"cohort {cohort} 出现负 lag：{row['lag']}")
            if row['lag'] > config.max_lag:
                ignored += 1
                continue
            pairs.append((row['lag'], row['conversions']))
        curve, repairs = legalize_curve(pairs)
        cohorts.append(CohortState(
            cohort=cohort, channel=channels.pop(), start_day=start_day,
            exposures=exposure, age=age, curve=curve, repairs=repairs,
            ignored_lags=ignored))
    return cohorts


def _increments(curve, max_lag):
    values = [curve.get(k) for k in range(max_lag + 1)]
    if any(value is None for value in values):
        return None
    increments = []
    previous = 0.0
    for value in values:
        increments.append(value - previous)
        previous = value
    return increments


def _weighted_log_std(samples, weights):
    positive = [(math.log(value), weight)
                for value, weight in zip(samples, weights) if value > 0 and weight > 0]
    if len(positive) < 2:
        return 0.0
    weight_sum = math.fsum(weight for _, weight in positive)
    mean = math.fsum(log_value * weight for log_value, weight in positive) / weight_sum
    variance = math.fsum(weight * (log_value - mean) ** 2
                         for log_value, weight in positive) / weight_sum
    return math.sqrt(max(variance, 0.0))


def _pool(pool, max_lag):
    """汇总一组成熟分群的增量计数，返回池化延迟模型。"""
    increments = [0.0] * (max_lag + 1)
    total = 0.0
    exposure_sum = 0.0
    for cohort in pool:
        cohort_increments = _increments(cohort.curve, max_lag)
        if cohort_increments is None:
            continue
        for lag, amount in enumerate(cohort_increments):
            increments[lag] += amount
        total += math.fsum(cohort_increments)
        exposure_sum += cohort.exposures
    if total <= 0:
        raise ValueError('训练分群的最终转化总量为零，无法构造延迟分布')
    pmf = [amount / total for amount in increments]
    alpha = [1.0 + amount for amount in increments]
    rate = (total / exposure_sum) if exposure_sum > 0 else None
    return {
        'pmf': pmf,
        'alpha': alpha,
        'pooled_total': total,
        'pooled_exposures': exposure_sum,
        'conversion_rate': rate,
        'donors': sorted(cohort.cohort for cohort in pool),
    }


def fit_delay_models(cohorts, config):
    """只从截点处成熟、完整、最终转化为正的分群学习延迟分布。

    池化层次：channel（同渠道成熟分群数 >= min_donor_cohorts）-> global（全部
    渠道合格分群）。被排除的分群逐条登记原因，绝不静默使用。
    """
    by_channel = defaultdict(list)
    global_pool = []
    excluded = []
    for cohort in cohorts:
        if cohort.age < config.maturity_window:
            continue
        if config.max_lag not in cohort.curve:
            excluded.append({'cohort': cohort.cohort, 'reason': 'tail_truncated'})
            continue
        final = cohort.curve[config.max_lag]
        if final <= 0:
            excluded.append({'cohort': cohort.cohort,
                             'reason': 'zero_final_conversions'})
            continue
        by_channel[cohort.channel].append(cohort)
        global_pool.append(cohort)

    channel_models = {
        channel: _pool(pool, config.max_lag)
        for channel, pool in sorted(by_channel.items())
    }
    global_model = _pool(global_pool, config.max_lag) if global_pool else None
    global_available = global_model is not None and len(global_pool) >= config.min_donor_cohorts

    resolved = {}
    for channel in sorted(by_channel):
        pool = by_channel[channel]
        if len(pool) >= config.min_donor_cohorts:
            model = dict(channel_models[channel])
            model['level'] = 'channel'
            model['source_channels'] = [channel]
        elif global_available:
            model = dict(global_model)
            model['level'] = 'global'
            model['source_channels'] = sorted(by_channel)
        else:
            model = None
        resolved[channel] = model

    # 数据里出现但没有任何同渠道成熟分群的渠道，也尝试挂全局池。
    for cohort in cohorts:
        if cohort.channel not in resolved and global_available:
            model = dict(global_model)
            model['level'] = 'global'
            model['source_channels'] = sorted(by_channel)
            resolved[cohort.channel] = model

    return {
        'channels': resolved,
        'channel_fits': channel_models,
        'global_fit': global_model,
        'global_donor_count': len(global_pool),
        'excluded': excluded,
        'donor_lookup': {cohort.cohort: cohort for cohort in global_pool},
    }


def _dirichlet(rng, alpha):
    gammas = [rng.gammavariate(value, 1.0) for value in alpha]
    total = math.fsum(gammas)
    return [value / total for value in gammas]


def _quantile(sorted_values, probability):
    position = probability * (len(sorted_values) - 1)
    lower = int(math.floor(position))
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = position - lower
    return sorted_values[lower] * (1 - fraction) + sorted_values[upper] * fraction


def _clip(value, low, high):
    return max(low, min(high, value))


def compute_diagnostics(cohort, pmf, config):
    """对比本群已观测增量的条件分布与模型条件延迟分布。"""
    lag = cohort.observed_lag
    flags = []
    if cohort.repairs:
        flags.append('curve_repaired')
    if lag is not None and lag < min(cohort.age, config.max_lag):
        flags.append('truncated_observed_curve')

    model_conditional = [pmf[k] for k in range(lag + 1)] if lag is not None else []
    model_mass = math.fsum(model_conditional)
    model_mean = None
    observed_mean = None
    tvd = None
    direction = None
    if model_mass > 0:
        model_conditional = [value / model_mass for value in model_conditional]
        model_mean = math.fsum(k * value for k, value in enumerate(model_conditional))

    observed = cohort.observed_conversions
    if lag is not None and observed is not None and observed > 0:
        # 只在实际可见 lag 上取增量，缺失 lag 视为零增量。
        raw_increments = []
        previous = 0.0
        for k in range(lag + 1):
            value = cohort.curve.get(k, previous)
            raw_increments.append(max(value - previous, 0.0))
            previous = value
        empirical = [value / observed for value in raw_increments]
        observed_mean = math.fsum(k * value for k, value in enumerate(empirical))
        if model_mass > 0:
            tvd = 0.5 * math.fsum(
                abs(empirical[k] - model_conditional[k]) for k in range(lag + 1))
            gap = observed_mean - model_mean
            # 条件均值比较：已观测增量的平均 lag 大于模型 -> 转化被压到后面才上报
            # （慢上报）；小于模型 -> 已发生的转化到得比预期早（快上报），但也可能
            # 意味着后续 lag 的量还在路上，需结合 drift 标志一起看。
            if gap > config.direction_threshold:
                direction = DIRECTION_SLOWER
            elif gap < -config.direction_threshold:
                direction = DIRECTION_FASTER
            else:
                direction = DIRECTION_ALIGNED
            if tvd > config.drift_threshold:
                flags.append('delay_drift')
            if direction == DIRECTION_SLOWER:
                flags.append(DIRECTION_SLOWER)
            elif direction == DIRECTION_FASTER:
                flags.append(DIRECTION_FASTER)
    elif observed == 0:
        flags.append('zero_observed')

    return {
        'delay_tvd': tvd,
        'direction': direction,
        'observed_mean_lag': observed_mean,
        'model_mean_lag': model_mean,
        'model_mass_through_observed_lag': model_mass if lag is not None else None,
        'repairs': cohort.repairs,
        'flags': flags,
    }


def _rng_for(config, as_of_day, cohort):
    digest = zlib.crc32(f'{as_of_day}:{cohort}'.encode('utf-8'))
    return random.Random(config.base_seed ^ digest)


def _simulate_interval(config, draws_uncertain, lower_bound, upper_bound):
    clipped = sorted(_clip(value, lower_bound, upper_bound) for value in draws_uncertain)
    tail = (1.0 - config.confidence) / 2.0
    lower = _quantile(clipped, tail)
    median = _quantile(clipped, 0.5)
    upper = _quantile(clipped, 1.0 - tail)
    return lower, median, upper


def nowcast_cohort(cohort, as_of_day, model, donor_states, config):
    """对单个未成熟分群给最终量区间；不满足支持条件时返回拒绝结果。"""
    result = {
        'cohort': cohort.cohort,
        'channel': cohort.channel,
        'start_day': cohort.start_day,
        'age': cohort.age,
        'exposures': cohort.exposures,
        'observed_lag': cohort.observed_lag,
        'observed_conversions': cohort.observed_conversions,
        'status': 'refused',
        'refusal_reason': None,
        'pooling': None,
        'estimate': None,
        'interval': None,
        'observed_share': None,
        'model_expected_share': None,
        'diagnostics': compute_diagnostics(
            cohort, model['pmf'] if model else [0.0] * (config.max_lag + 1), config),
    }
    if model is None:
        result['refusal_reason'] = REFUSAL_NO_DONORS
        return result

    lag = cohort.observed_lag
    observed = cohort.observed_conversions
    if lag is None or observed is None:
        result['refusal_reason'] = REFUSAL_NO_VIEW
        return result
    if observed > cohort.exposures:
        result['refusal_reason'] = REFUSAL_EXPOSURE
        return result

    pmf = model['pmf']
    model_mass = math.fsum(pmf[:lag + 1])
    result['model_expected_share'] = model_mass
    result['pooling'] = {
        'level': model['level'],
        'source_channels': model['source_channels'],
        'donor_cohorts': model['donors'],
        'donor_count': len(model['donors']),
    }

    # 零暴露分群：合法最终量只能是零。
    if cohort.exposures == 0:
        if observed > 0:  # 理论上被上面的 exposure 拦截，保留双保险。
            result['refusal_reason'] = REFUSAL_EXPOSURE
            return result
        result.update(status='estimated', interval=[0.0, 0.0], observed_share=1.0)
        result['estimate'] = {
            'point': 0.0, 'median': 0.0, 'lower': 0.0, 'upper': 0.0,
            'confidence': config.confidence, 'simulations': 0,
            'method': 'zero_exposure'}
        return result

    if model_mass < config.min_model_mass:
        result['refusal_reason'] = REFUSAL_SUPPORT
        return result

    rng = _rng_for(config, as_of_day, cohort.cohort)
    lower_bound = observed
    upper_bound = cohort.exposures

    if observed > 0:
        point = _clip(observed / model_mass, lower_bound, upper_bound)
        # 借样分群在同一 lag 的完成率离散度，刻画分群间异质性。
        completion = []
        weights = []
        for donor in donor_states:
            donor_observed = donor.curve.get(lag)
            donor_final = donor.curve[config.max_lag]
            if donor_observed is None or donor_final <= 0:
                continue
            ratio = donor_observed / donor_final
            if ratio > 0:
                completion.append(ratio)
                weights.append(donor_final)
        sigma = _weighted_log_std(completion, weights)
        draws = []
        for _ in range(config.simulations):
            sampled_pmf = _dirichlet(rng, model['alpha'])
            sampled_mass = math.fsum(sampled_pmf[:lag + 1])
            if sampled_mass <= 0:
                continue
            heterogeneity = math.exp(sigma * rng.gauss(0.0, 1.0)) if sigma > 0 else 1.0
            draws.append(observed / sampled_mass * heterogeneity)
        method = 'ratio_with_dirichlet_heterogeneity'
    else:
        rate = model.get('conversion_rate')
        if not rate or rate <= 0:
            result['refusal_reason'] = REFUSAL_NO_RATE
            return result
        # 零观测时比率法退化为零，改用借样分群的单位暴露转化率作显式先验。
        rates = [donor.curve[config.max_lag] / donor.exposures
                 for donor in donor_states if donor.exposures > 0]
        donor_weights = [donor.exposures for donor in donor_states
                         if donor.exposures > 0]
        tau = _weighted_log_std(rates, donor_weights)
        point = _clip(rate * cohort.exposures, lower_bound, upper_bound)
        draws = []
        for _ in range(config.simulations):
            multiplier = math.exp(tau * rng.gauss(0.0, 1.0)) if tau > 0 else 1.0
            draws.append(rate * cohort.exposures * multiplier)
        result['diagnostics']['flags'].append('rate_prior_zero_observed')
        method = 'rate_prior_zero_observed'

    lower, median, upper = _simulate_interval(
        config, draws, lower_bound, upper_bound)
    # 区间必须包住点估计，且不破坏物理边界。
    lower = min(lower, point)
    upper = max(upper, point)
    result.update(
        status='estimated',
        interval=[lower, upper],
        observed_share=(observed / point) if point > 0 else None)
    result['estimate'] = {
        'point': point,
        'median': median,
        'lower': lower,
        'upper': upper,
        'confidence': config.confidence,
        'simulations': config.simulations,
        'method': method,
    }
    return result


def run_nowcast(records, as_of_day, config=None):
    config = config or NowcastConfig()
    config.validate()
    view = latest_revisions(records, as_of_day)
    cohorts = build_cohorts(records, as_of_day, config)
    fitted = fit_delay_models(cohorts, config)

    mature, immature = [], []
    for cohort in cohorts:
        if cohort.age >= config.maturity_window:
            mature.append({
                'cohort': cohort.cohort,
                'channel': cohort.channel,
                'start_day': cohort.start_day,
                'age': cohort.age,
                'exposures': cohort.exposures,
                'complete': cohort.is_complete(config.max_lag),
                'final_conversions': cohort.curve.get(config.max_lag),
                'observed_lag': cohort.observed_lag,
                'repairs': cohort.repairs,
            })
        else:
            model = fitted['channels'].get(cohort.channel)
            donor_states = [fitted['donor_lookup'][name]
                            for name in model['donors']] if model else []
            immature.append(
                nowcast_cohort(cohort, as_of_day, model, donor_states, config))

    training = {
        'global_fit': _public_fit(fitted['global_fit']),
        'channel_fits': {
            channel: _public_fit(fit) for channel, fit in fitted['channel_fits'].items()},
        'resolved_levels': {
            channel: None if fit is None else
            {'level': fit['level'], 'source_channels': fit['source_channels'],
             'donor_cohorts': fit['donors'], 'donor_count': len(fit['donors'])}
            for channel, fit in fitted['channels'].items()},
        'excluded_from_training': fitted['excluded'],
    }
    return {
        'product': PRODUCT,
        'report': 'nowcast',
        'schema_version': 1,
        'unit': UNIT,
        'as_of_day': as_of_day,
        'config': config.as_dict(),
        'view_fingerprint': fingerprint(sorted(view.values(),
                                               key=lambda row: row['observation_id'])),
        'mature_cohorts': mature,
        'immature_cohorts': immature,
        'training': training,
    }


def _public_fit(fit):
    if fit is None:
        return None
    return {
        'pmf': fit['pmf'],
        'alpha': fit['alpha'],
        'pooled_total': fit['pooled_total'],
        'pooled_exposures': fit['pooled_exposures'],
        'conversion_rate': fit['conversion_rate'],
        'donor_cohorts': fit['donors'],
        'donor_count': len(fit['donors']),
    }
