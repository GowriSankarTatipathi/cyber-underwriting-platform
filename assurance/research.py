"""Reproducible synthetic research demonstration; never writes real loss outcomes."""
import math
import random
from statistics import mean
from portfolio.analytics import validate_dataset
from underwriting.snapshots import checksum


def fixture():
    rows = []
    for i in range(80):
        train = i < 60
        band = 'higher' if i % 2 else 'lower'
        incidents = (1 + i % 3) if band == 'higher' else (1 if i % 4 == 0 else 0)
        severity = 12000 + i * 113 + (8500 if band == 'higher' else 0)
        rows.append({'id': f'SYNTHETIC-{i:03}', 'applicant': f'FICTIONAL-BUSINESS-{i:03}',
            'policy': f'FICTIONAL-POLICY-{i:03}', 'start': '2022-01-01' if train else '2024-01-01',
            'end': '2023-01-01' if train else '2025-01-01', 'years': 1., 'band': band,
            'incidents': incidents, 'loss': incidents * severity, 'downtime_hours': incidents * (10 + i % 16),
            'currency': 'USD', 'maturity': 'mature', 'source': 'Generated fictional teaching fixture v1'})
    return rows


def percentile(values, q):
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    low = int(pos)
    return ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * (pos - low)


def evaluate(rows, cutoff='2024-01-01'):
    metrics = validate_dataset(rows, cutoff)
    if metrics['status'] == 'insufficient_data':
        return metrics
    ids = set(metrics['train_ids'])
    train = [r for r in rows if r['id'] in ids]
    held = [r for r in rows if r['id'] in set(metrics['holdout_ids'])]
    rng = random.Random(3701)
    frequencies, severities, downtimes = [], [], []
    for _ in range(300):
        sample = [rng.choice(train) for _ in train]
        events = sum(r['incidents'] for r in sample)
        frequencies.append(events / sum(r['years'] for r in sample))
        if events:
            severities.append(sum(r['loss'] for r in sample) / events)
            downtimes.append(sum(r['downtime_hours'] for r in sample) / events)
    intervals = {key: [round(percentile(values, .025), 3), round(percentile(values, .975), 3)]
        for key, values in [('frequency', frequencies), ('severity', severities), ('downtime_hours', downtimes)]}
    events = sum(r['incidents'] for r in train)
    downtime = sum(r['downtime_hours'] for r in train) / events
    held_events = sum(r['incidents'] for r in held)
    metrics.update(downtime_hours_per_incident=round(downtime, 3), bootstrap_95_percent_intervals=intervals,
        downtime_holdout_mae=round(sum(abs(r['downtime_hours'] / r['incidents'] - downtime) * r['incidents'] for r in held if r['incidents']) / held_events, 3),
        stability={'training_frequency': metrics['baseline']['frequency'], 'holdout_frequency': held_events / sum(r['years'] for r in held)},
        uncertainty_method='300 seeded bootstrap resamples of independent training businesses; percentile intervals. Not predictive loss bounds.',
        assumptions=['Entirely synthetic data, complete one-year observation periods, one currency, fully developed losses.',
                     'Risk bands are specified before simulated outcomes. Holdout businesses are disjoint.',
                     'No tail dependence, loss inflation, reserves, indirect supplier chains or production calibration.'])
    return metrics


def report():
    rows = fixture()
    return {'label': 'SYNTHETIC DEMO ONLY — not a validated insurance model', 'version': 'synthetic-research-v1',
        'dataset_sha256': checksum(rows), 'cutoff': '2024-01-01', 'dataset': rows, 'metrics': evaluate(rows)}
