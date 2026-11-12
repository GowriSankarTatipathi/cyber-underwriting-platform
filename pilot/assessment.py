def assess_restore(test, targets):
    measured = (test.get("duration_minutes"), test.get("data_age_minutes"))
    if test.get("functional_checks_passed") is False:
        return "Functional checks failed"
    if any(value is None for value in measured) or test.get("functional_checks_passed") is None:
        return "Needs evidence"
    if any(test[key] > limit for key, limit in targets.items()):
        return "Target missed"
    return "Targets met"
