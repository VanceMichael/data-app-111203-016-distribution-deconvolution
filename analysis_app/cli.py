import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from .backtest import backtest
from .io import load_records
from .nowcast import NowcastConfig, run_nowcast
from .summary import summarize


def save_json(path, result):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.analysis-', dir=destination.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(result, handle, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _parse_int_list(value):
    if not value:
        return []
    return [int(part.strip()) for part in value.split(',') if part.strip()]


def build_parser():
    parser = argparse.ArgumentParser(description='本地观测数据分析')
    commands = parser.add_subparsers(dest='command', required=True)

    summary = commands.add_parser('summary', help='按业务维度核对观测口径')
    summary.add_argument('--input', type=Path, default=Path('data/observations.csv'))
    summary.add_argument('--output', type=Path)

    nowcast = commands.add_parser('nowcast', help='截点处的未成熟分群现报')
    nowcast.add_argument('--input', type=Path, default=Path('data/observations.csv'))
    nowcast.add_argument('--output', type=Path)
    nowcast.add_argument('--as-of', dest='as_of', type=int, required=True,
                         help='上报截点 reported_day，只使用该日及之前的行')
    _add_config_arguments(nowcast)

    rolling = commands.add_parser('backtest', help='滚动回测，重拟合延迟模型并核对区间')
    rolling.add_argument('--input', type=Path, default=Path('data/observations.csv'))
    rolling.add_argument('--output', type=Path)
    rolling.add_argument('--cutoffs', type=_parse_int_list, required=True,
                         help='逗号分隔的历史截点列表，例如 12,16,20,24')
    _add_config_arguments(rolling)
    return parser


def _add_config_arguments(parser):
    parser.add_argument('--max-lag', type=int, default=NowcastConfig.max_lag)
    parser.add_argument('--maturity-window', type=int,
                        default=NowcastConfig.maturity_window)
    parser.add_argument('--min-donor-cohorts', type=int,
                        default=NowcastConfig.min_donor_cohorts)
    parser.add_argument('--simulations', type=int, default=NowcastConfig.simulations)
    parser.add_argument('--confidence', type=float, default=NowcastConfig.confidence)
    parser.add_argument('--drift-threshold', type=float,
                        default=NowcastConfig.drift_threshold)
    parser.add_argument('--min-model-mass', type=float,
                        default=NowcastConfig.min_model_mass)


def config_from_args(args):
    return NowcastConfig(
        max_lag=args.max_lag,
        maturity_window=args.maturity_window,
        min_donor_cohorts=args.min_donor_cohorts,
        simulations=args.simulations,
        confidence=args.confidence,
        drift_threshold=args.drift_threshold,
        min_model_mass=args.min_model_mass)


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        records = load_records(args.input)
        if args.command == 'summary':
            result = summarize(records)
        elif args.command == 'nowcast':
            result = run_nowcast(records, args.as_of, config_from_args(args))
        else:
            result = backtest(records, args.cutoffs, config_from_args(args))
        if args.output:
            save_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    except (ValueError, OSError, AssertionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
