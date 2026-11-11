import math
import statistics
from collections import Counter


def describe(records):
    return {'observed_conversions': math.fsum(row['conversions'] for row in records), 'cohorts': len({row['cohort'] for row in records}), 'maximum_lag': max(row['lag'] for row in records)}
