"""Transparent research baselines, never a production pricing or approval engine."""
import math


def validate_dataset(rows, cutoff):
    train = [r for r in rows if r["end"] <= cutoff]
    test = [r for r in rows if r["start"] >= cutoff]
    # Applicants are disjoint; older observations from holdout applicants are excluded.
    held_out = {r["applicant"] for r in test}
    train = [r for r in train if r["applicant"] not in held_out]
    reasons = []
    if len({r["applicant"] for r in train}) < 30:
        reasons.append("At least 30 distinct training applicants are required.")
    if len(held_out) < 10:
        reasons.append("At least 10 distinct later holdout applicants are required.")
    if sum(r["incidents"] for r in train) < 10 or sum(r["incidents"] for r in test) < 5:
        reasons.append("Require at least 10 training and 5 holdout incidents.")
    if any(sum(r["band"] == band for r in train) < 10 for band in ("higher", "lower")):
        reasons.append("Each historical risk band needs at least 10 training observations.")
    result = {"status": "insufficient_data" if reasons else "research_evaluation", "reasons": reasons,
        "train_ids": [r["id"] for r in train], "holdout_ids": [r["id"] for r in test],
        "excluded_rows": len(rows) - len(train) - len(test),
        "limitations": "Research only. Counts are observed incidents, not causal effects. Severity uses aggregate incurred loss per incident. Thresholds are pilot guardrails, not proof of statistical adequacy. No pricing, approval, binding or automatic priority changes."}
    if reasons:
        return result
    def fit(sample):
        incidents = sum(r["incidents"] for r in sample)
        return {"frequency": incidents / sum(r["years"] for r in sample),
            "severity": sum(r["loss"] for r in sample) / incidents if incidents else None}
    baseline = fit(train)
    bands = {band: fit([r for r in train if r["band"] == band]) for band in ("higher", "lower")}
    # Shrink empty-event bands to the overall baseline; never use held-out outcomes to fit.
    for value in bands.values():
        if value["frequency"] == 0:
            value["frequency"] = baseline["frequency"]
        if value["severity"] is None:
            value["severity"] = baseline["severity"]
    def score(banded):
        deviance, errors, events = 0., 0., 0
        for r in test:
            estimate = bands[r["band"]] if banded else baseline
            mu, y = estimate["frequency"] * r["years"], r["incidents"]
            deviance += 2 * (y * math.log(y / mu) - (y - mu)) if y else 2 * mu
            if y:
                errors += abs(r["loss"] / y - estimate["severity"]) * y
                events += y
        return {"mean_count_poisson_deviance": deviance / len(test), "incident_weighted_severity_mae": errors / events if events else None}
    observed = {band: fit([r for r in test if r["band"] == band]) for band in bands if any(r["band"] == band for r in test)}
    result.update(baseline=baseline, risk_bands=bands, baseline_holdout=score(False), banded_holdout=score(True), observed_holdout=observed)
    return result
