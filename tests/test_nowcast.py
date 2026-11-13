import copy
import csv
import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from analysis_app.backtest import backtest
from analysis_app.io import load_records
from analysis_app.nowcast import (
    DIRECTION_FASTER,
    REFUSAL_EXPOSURE,
    REFUSAL_NO_DONORS,
    REFUSAL_SUPPORT,
    NowcastConfig,
    build_cohorts,
    latest_revisions,
    legalize_curve,
    run_nowcast,
)
from analysis_app.schema import FIELDS

FIXTURE = 'data/observations.csv'
CONFIG = NowcastConfig(simulations=600)


def write_rows(directory, rows):
    path = Path(directory) / 'input.csv'
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(FIELDS))
        writer.writeheader()
        writer.writerows(rows)
    return path


def row(obs_id, cohort, channel, start_day, lag, conversions, exposures,
        reported_day, revision=0):
    return {
        'observation_id': obs_id,
        'cohort': cohort,
        'channel': channel,
        'start_day': start_day,
        'lag': lag,
        'conversions': conversions,
        'exposures': exposures,
        'reported_day': reported_day,
        'revision': revision,
    }


def cumulative_rows(prefix, cohort, channel, start_day, values, exposures,
                    first_reported=None, with_revisions=False):
    """按累计曲线生成行；values 是各 lag 的累计转化。"""
    rows = []
    first_reported = start_day if first_reported is None else first_reported
    for lag, value in enumerate(values):
        reported = first_reported + lag
        rows.append(row(f'{prefix}-{cohort}-{lag}r0', cohort, channel, start_day,
                        lag, value, exposures, reported, 0))
        if with_revisions:
            rows.append(row(f'{prefix}-{cohort}-{lag}r1', cohort, channel, start_day,
                            lag, value, exposures, reported + 1, 1))
    return rows


def stable_history(channel, start_days, pmf, total=24, exposures=200):
    """按给定延迟 pmf 生成稳定的成熟训练分群。"""
    rows = []
    for index, start_day in enumerate(start_days):
        values = []
        running = 0
        for lag, probability in enumerate(pmf):
            running += round(probability * total)
            values.append(float(running))
        values[-1] = float(total)
        rows.extend(cumulative_rows(
            f'obs-{channel}', f'{channel}-C{index}', channel, start_day,
            values, exposures, with_revisions=True))
    return rows


class CutoffViewTest(unittest.TestCase):
    def test_latest_revision_wins_and_future_rows_are_hidden(self):
        records = [
            row('a', 'X', 'H', 0, 0, 1.0, 10, 0, 0),
            row('b', 'X', 'H', 0, 0, 2.0, 10, 1, 1),
            row('c', 'X', 'H', 0, 1, 3.0, 10, 5, 0),
        ]
        view = latest_revisions(records, as_of_day=2)
        self.assertEqual(len(view), 1)
        self.assertEqual(view[('X', 0)]['conversions'], 2.0)
        view_all = latest_revisions(records, as_of_day=5)
        self.assertEqual(len(view_all), 2)

    def test_duplicate_revision_number_rejected(self):
        records = [
            row('a', 'X', 'H', 0, 0, 1.0, 10, 0, 0),
            row('b', 'X', 'H', 0, 0, 2.0, 10, 1, 0),
        ]
        with self.assertRaisesRegex(ValueError, '修订号重复'):
            latest_revisions(records, as_of_day=5)

    def test_view_is_independent_of_row_order(self):
        records = [
            row('a', 'X', 'H', 0, 0, 1.0, 10, 0, 0),
            row('b', 'X', 'H', 0, 0, 2.0, 10, 1, 1),
        ]
        forward = latest_revisions(records, as_of_day=5)
        backward = latest_revisions(list(reversed(records)), as_of_day=5)
        self.assertEqual(forward, backward)
        self.assertEqual(forward[('X', 0)]['conversions'], 2.0)

    def test_identical_duplicate_rows_are_idempotent(self):
        records = [
            row('a', 'X', 'H', 0, 0, 1.0, 10, 0, 0),
            row('a2', 'X', 'H', 0, 0, 1.0, 10, 0, 0),
        ]
        view = latest_revisions(records, as_of_day=5)
        self.assertEqual(len(view), 1)

    def test_legalize_curve_handles_negative_and_decreasing_revisions(self):
        curve, repairs = legalize_curve([(0, 2.0), (1, -1.0), (2, 1.5), (3, 5.0)])
        self.assertEqual(curve, {0: 2.0, 1: 2.0, 2: 2.0, 3: 5.0})
        self.assertEqual([item['reason'] for item in repairs],
                         ['negative_value', 'decreasing_cumulative'])
        self.assertTrue(all(value >= 0 for value in curve.values()))


class NowcastFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = load_records(FIXTURE)

    def _find(self, result, cohort):
        return next(item for item in result['immature_cohorts']
                    if item['cohort'] == cohort)

    def test_negative_revision_is_legalized_and_keeps_repair_ledger(self):
        result = run_nowcast(self.records, 29, CONFIG)
        states = {state.cohort: state
                  for state in build_cohorts(self.records, 29, CONFIG)}
        state = states['C25']
        # 最新修订让 lag1=8、lag2=7，累计下降不合法；lag2 被钳回前值 8。
        self.assertEqual(state.curve[1], 8.0)
        self.assertEqual(state.curve[2], 8.0)
        self.assertEqual(state.curve[3], 9.0)
        reasons = [item['reason'] for item in state.repairs]
        self.assertIn('decreasing_cumulative', reasons)
        repair = next(item for item in state.repairs
                      if item['reason'] == 'decreasing_cumulative')
        self.assertEqual(repair['lag'], 2)
        self.assertEqual(repair['raw'], 7.0)
        self.assertEqual(repair['fixed'], 8.0)
        # 估计仍然给出合法且包含已观测值的区间。
        item = self._find(result, 'C25')
        lower, upper = item['interval']
        self.assertLessEqual(lower, item['estimate']['point'])
        self.assertLessEqual(item['estimate']['point'], upper)
        self.assertGreaterEqual(lower, item['observed_conversions'] - 1e-12)

    def test_zero_exposure_cohort_pins_to_zero_without_simulation(self):
        result = run_nowcast(self.records, 29, CONFIG)
        item = self._find(result, 'C26')
        self.assertEqual(item['status'], 'estimated')
        self.assertEqual(item['estimate']['method'], 'zero_exposure')
        self.assertEqual(item['interval'], [0.0, 0.0])
        self.assertEqual(item['observed_share'], 1.0)
        self.assertIn('zero_observed', item['diagnostics']['flags'])

    def test_truncated_mature_cohort_is_excluded_from_training(self):
        result = run_nowcast(self.records, 29, CONFIG)
        excluded = {item['cohort']: item['reason']
                    for item in result['training']['excluded_from_training']}
        self.assertEqual(excluded.get('C28'), 'tail_truncated')
        for level in result['training']['resolved_levels'].values():
            self.assertNotIn('C28', level['donor_cohorts'])
        # 截断分群出现在成熟列表里但标记为不完整，不被当作真值。
        mature = next(item for item in result['mature_cohorts']
                      if item['cohort'] == 'C28')
        self.assertFalse(mature['complete'])
        self.assertIsNone(mature['final_conversions'])

    def test_burst_reporting_delay_triggers_drift_diagnostic(self):
        result = run_nowcast(self.records, 29, CONFIG)
        item = self._find(result, 'C27')
        flags = item['diagnostics']['flags']
        self.assertIn('delay_drift', flags)
        self.assertGreater(item['diagnostics']['delay_tvd'],
                           CONFIG.drift_threshold)
        # 已观测转化全在 lag 0，条件均值显著早于模型 -> 快上报（后续量仍在路上）。
        self.assertEqual(item['diagnostics']['direction'], DIRECTION_FASTER)
        self.assertGreater(item['estimate']['upper'], item['observed_conversions'])

    def test_intervals_respect_physical_bounds(self):
        result = run_nowcast(self.records, 29, CONFIG)
        states = {state.cohort: state
                  for state in build_cohorts(self.records, 29, CONFIG)}
        for item in result['immature_cohorts']:
            if item['status'] != 'estimated':
                continue
            lower, upper = item['interval']
            observed = item['observed_conversions']
            exposure = states[item['cohort']].exposures
            self.assertGreaterEqual(lower, 0.0)
            self.assertGreaterEqual(lower, observed - 1e-12)
            self.assertLessEqual(upper, exposure + 1e-12)
            self.assertLessEqual(lower, item['estimate']['point'] + 1e-12)
            self.assertGreaterEqual(upper, item['estimate']['point'] - 1e-12)

    def test_simulation_is_deterministic_with_seed(self):
        first = run_nowcast(self.records, 29, CONFIG)
        second = run_nowcast(self.records, 29, CONFIG)
        self.assertEqual(
            [item['interval'] for item in first['immature_cohorts']],
            [item['interval'] for item in second['immature_cohorts']])

    def test_output_is_finite_and_json_serializable(self):
        result = run_nowcast(self.records, 29, CONFIG)
        payload = json.dumps(result, allow_nan=False, sort_keys=True)
        self.assertIn('nowcast', payload)
        # 所有区间数值必须有限，且下界不超过上界。
        for item in result['immature_cohorts']:
            if item['status'] == 'estimated':
                lower, upper = item['interval']
                self.assertTrue(math.isfinite(lower) and math.isfinite(upper))
                self.assertLessEqual(lower, upper)

    def test_as_of_result_is_immune_to_later_file_appends(self):
        before = run_nowcast(self.records, 27, CONFIG)
        extended = copy.deepcopy(self.records)
        # 模拟以后文件追加：同一截点不可见的新行与新修订。
        extended.append(row('future-1', 'C27', 'H2', 27, 3, 9.0, 370, 30, 0))
        extended.append(row('future-2', 'C25', 'H1', 25, 4, 20.0, 350, 30, 2))
        after = run_nowcast(extended, 27, CONFIG)
        keys = ['as_of_day', 'view_fingerprint', 'immature_cohorts',
                'mature_cohorts', 'training']
        for key in keys:
            self.assertEqual(before[key], after[key], f'{key} 受未来追加影响')


class RefusalAndPoolingTest(unittest.TestCase):
    def test_estimation_refused_when_channel_history_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = cumulative_rows(
                'obs', 'NEW-0', 'NEW', 0, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
                100, with_revisions=True)
            # 另一个渠道的成熟分群也不存在 -> 全局池同样为空。
            path = write_rows(directory, rows)
            records = load_records(path)
            result = run_nowcast(records, 2, CONFIG)
            item = result['immature_cohorts'][0]
            self.assertEqual(item['status'], 'refused')
            self.assertEqual(item['refusal_reason'], REFUSAL_NO_DONORS)
            self.assertIsNone(item['estimate'])

    def test_global_pooling_declares_borrowed_channels(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = stable_history('HA', range(0, 24, 3),
                                  [0.25, 0.25, 0.2, 0.15, 0.1, 0.05])
            rows.extend(cumulative_rows(
                'obs', 'NEW-0', 'HB', 24, [2.0, 4.0, 6.0], 200))
            path = write_rows(directory, rows)
            records = load_records(path)
            result = run_nowcast(records, 26, CONFIG)
            item = next(entry for entry in result['immature_cohorts']
                        if entry['cohort'] == 'NEW-0')
            self.assertEqual(item['status'], 'estimated')
            self.assertEqual(item['pooling']['level'], 'global')
            self.assertEqual(item['pooling']['source_channels'], ['HA'])
            self.assertGreaterEqual(item['pooling']['donor_count'],
                                    CONFIG.min_donor_cohorts)

    def test_observed_above_exposures_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = stable_history('HA', range(0, 24, 3),
                                  [0.25, 0.25, 0.2, 0.15, 0.1, 0.05])
            rows.extend(cumulative_rows(
                'obs', 'BAD-0', 'HA', 24, [50.0, 60.0], 40))
            path = write_rows(directory, rows)
            records = load_records(path)
            result = run_nowcast(records, 26, CONFIG)
            item = next(entry for entry in result['immature_cohorts']
                        if entry['cohort'] == 'BAD-0')
            self.assertEqual(item['refusal_reason'], REFUSAL_EXPOSURE)

    def test_model_mass_below_floor_refuses_extrapolation(self):
        strict = NowcastConfig(simulations=400, min_model_mass=0.9)
        with tempfile.TemporaryDirectory() as directory:
            rows = stable_history('HA', range(0, 24, 3),
                                  [0.25, 0.25, 0.2, 0.15, 0.1, 0.05])
            rows.extend(cumulative_rows(
                'obs', 'YOUNG-0', 'HA', 24, [2.0], 200))
            path = write_rows(directory, rows)
            records = load_records(path)
            result = run_nowcast(records, 24, strict)
            item = next(entry for entry in result['immature_cohorts']
                        if entry['cohort'] == 'YOUNG-0')
            self.assertEqual(item['refusal_reason'], REFUSAL_SUPPORT)


class SyntheticBacktestTest(unittest.TestCase):
    def setUp(self):
        pmf = [0.2, 0.25, 0.25, 0.15, 0.1, 0.05]
        rows = stable_history('HA', range(0, 30, 3), pmf, total=20, exposures=200)
        rows.extend(stable_history('HB', range(1, 31, 3), pmf, total=20,
                                   exposures=240))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = write_rows(self.temp.name, rows)
        self.records = load_records(self.path)

    def test_rolling_backtest_refits_per_cutoff_and_covers_truth(self):
        cutoffs = [12, 15, 18, 21, 24, 27]
        result = backtest(self.records, cutoffs, CONFIG)
        self.assertEqual(sorted({entry['cutoff_day'] for entry in
                                 result['evaluations']}), cutoffs)
        # 每个截点的借样分群在当时必须已经成熟（backtest 内部带断言，这里再显式核对）。
        for entry in result['evaluations']:
            if entry['status'] == 'estimated':
                for donor in entry['donor_cohorts']:
                    index = int(donor.split('C')[1])
                    channel = donor.split('-')[0]
                    spacing = 3
                    start = index * spacing + (1 if channel == 'HB' else 0)
                    self.assertGreaterEqual(entry['cutoff_day'] - start,
                                            CONFIG.maturity_window)
        overall = result['metrics']['overall']
        self.assertGreaterEqual(overall['coverage'], 0.8)
        self.assertLess(overall['mean_absolute_percentage_error'], 0.25)

    def test_backtest_never_uses_full_period_distribution(self):
        early = backtest(self.records, [12], CONFIG)
        early_donors = {donor for entry in early['evaluations']
                        if entry['donor_cohorts']
                        for donor in entry['donor_cohorts']}
        # start_day >= 9 的分群在 cut=12 时未成熟，不可能进入训练池。
        late_donors = {donor for donor in early_donors
                       if int(donor.split('C')[1]) >= 3}
        self.assertEqual(late_donors, set())

    def test_backtest_records_refusals_and_rejects_empty_inputs(self):
        # 很早的截点没有成熟分群，所有案例都应被拒绝而不是静默丢弃。
        early = backtest(self.records, [6], CONFIG)
        self.assertTrue(early['evaluations'])
        self.assertTrue(all(entry['status'] == 'refused'
                            for entry in early['evaluations']))
        self.assertEqual(early['metrics']['refusals'], len(early['evaluations']))
        with self.assertRaises(ValueError):
            backtest([], [10], CONFIG)
        with self.assertRaises(ValueError):
            backtest(self.records, [], CONFIG)

    def test_immutability_under_appended_files_via_cli(self):
        extended = copy.deepcopy(self.records)
        extended.append(row('future-x', 'HA-C9', 'HA', 27, 0, 9.0, 200, 40, 0))
        later_dir = Path(self.temp.name) / 'later'
        later_dir.mkdir(parents=True, exist_ok=True)
        extended_path = write_rows(later_dir, extended)
        first = run_nowcast(self.records, 15, CONFIG)
        second = run_nowcast(load_records(extended_path), 15, CONFIG)
        self.assertEqual(first['view_fingerprint'], second['view_fingerprint'])
        self.assertEqual(first['immature_cohorts'], second['immature_cohorts'])

    def test_zero_observed_uses_rate_prior_with_non_degenerate_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = stable_history('HA', range(0, 24, 3),
                                  [0.25, 0.25, 0.2, 0.15, 0.1, 0.05])
            # 让借样分群的单位暴露转化率有真实离散度，异质性才能撑开区间。
            for index, factor in enumerate((0.6, 0.9, 1.1, 1.3, 0.8, 1.0, 1.2, 1.1)):
                cohort = f'HA-C{index}'
                for entry in rows:
                    if entry['cohort'] == cohort:
                        entry['conversions'] = round(
                            entry['conversions'] * factor, 3)
            rows.extend(cumulative_rows(
                'obs', 'ZERO-0', 'HA', 24, [0.0, 0.0], 200))
            path = write_rows(directory, rows)
            result = run_nowcast(load_records(path), 26, CONFIG)
            item = next(entry for entry in result['immature_cohorts']
                        if entry['cohort'] == 'ZERO-0')
            self.assertEqual(item['status'], 'estimated')
            self.assertEqual(item['estimate']['method'],
                             'rate_prior_zero_observed')
            lower, upper = item['interval']
            self.assertGreaterEqual(lower, 0.0)
            self.assertLess(lower, upper)
            self.assertLessEqual(upper, 200.0)
            self.assertIn('rate_prior_zero_observed',
                          item['diagnostics']['flags'])

    def test_cli_nowcast_and_backtest_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'nowcast.json'
            run = subprocess.run(
                [sys.executable, '-m', 'analysis_app', 'nowcast',
                 '--input', str(self.path), '--as-of', '24',
                 '--simulations', '300', '--output', str(output)],
                text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            payload = json.loads(output.read_text())
            self.assertEqual(payload['as_of_day'], 24)
            self.assertTrue(payload['immature_cohorts'])

            bt_output = Path(directory) / 'backtest.json'
            run = subprocess.run(
                [sys.executable, '-m', 'analysis_app', 'backtest',
                 '--input', str(self.path), '--cutoffs', '12,18,24',
                 '--simulations', '300', '--output', str(bt_output)],
                text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            bt = json.loads(bt_output.read_text())
            self.assertEqual(bt['cutoff_days'], [12, 18, 24])
            self.assertIn('overall', bt['metrics'])


class DiagnosticsTest(unittest.TestCase):
    def test_slow_reporting_flag_when_increments_are_back_loaded(self):
        pmf = [0.2, 0.25, 0.25, 0.15, 0.1, 0.05]
        with tempfile.TemporaryDirectory() as directory:
            rows = stable_history('HA', range(0, 24, 3), pmf)
            # 目标分群截至 lag2 只上报 2 个，且都压在 lag2（条件均值 2 > 模型约 1.2）。
            rows.extend(cumulative_rows(
                'obs', 'SLOW-0', 'HA', 24, [0.0, 0.0, 2.0], 200))
            path = write_rows(directory, rows)
            records = load_records(path)
            result = run_nowcast(records, 26, CONFIG)
            item = next(entry for entry in result['immature_cohorts']
                        if entry['cohort'] == 'SLOW-0')
            self.assertIn('slower_reporting', item['diagnostics']['flags'])
            self.assertGreater(item['diagnostics']['observed_mean_lag'],
                               item['diagnostics']['model_mean_lag'])

    def test_repair_flags_propagate_to_output(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = stable_history('HA', range(0, 24, 3),
                                  [0.25, 0.25, 0.2, 0.15, 0.1, 0.05])
            rows.extend([
                row('n0', 'N-0', 'HA', 24, 0, 2.0, 200, 24, 0),
                row('n1', 'N-0', 'HA', 24, 1, 1.0, 200, 25, 0),
            ])
            path = write_rows(directory, rows)
            result = run_nowcast(load_records(path), 25, CONFIG)
            item = next(entry for entry in result['immature_cohorts']
                        if entry['cohort'] == 'N-0')
            self.assertIn('curve_repaired', item['diagnostics']['flags'])


if __name__ == '__main__':
    unittest.main()
