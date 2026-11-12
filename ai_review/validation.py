from datetime import date
from decimal import Decimal, InvalidOperation
import re

from django.utils import timezone
from .schema import Extraction

WORDS = {name: str(index) for index, name in enumerate(("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"))}
TIME_RE = re.compile(r"(?<![\w.])(-?\d+(?:\.\d+)?|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b", re.I)
LABELS = {"restore_duration_minutes": "Elapsed restore time (minutes)", "recovered_data_age_minutes": "Recovered data age (minutes)",
    "functional_checks_passed": "Functional checks passed", "service": "Service", "scope": "Scope", "test_date": "Test date",
    "backup_status": "Backup job status", "mfa_coverage": "MFA coverage", "access_review_date": "Access review date"}


def times_in(text):
    values = set()
    for amount, unit in TIME_RE.findall(text.lower()):
        value = Decimal(WORDS.get(amount, amount))
        if unit.startswith("h"):
            value *= 60
        elif unit.startswith("s"):
            value /= 60
        values.add(value)
    return values


def normalized_value(field, value):
    value = str(value).strip()
    if field.endswith("_minutes"):
        number = Decimal(value)
        if not number.is_finite() or number < 0 or number > 10_000_000:
            raise ValueError("Use a nonnegative, finite measurement in minutes.")
        return format(number.normalize(), "f")
    if field == "functional_checks_passed":
        if value.lower() not in ("true", "false", "passed", "failed"):
            raise ValueError("Use true or false, supported by the functional-check result.")
        return value.lower() in ("true", "passed")
    if field.endswith("_date"):
        parsed = date.fromisoformat(value)
        if parsed > timezone.localdate():
            raise ValueError("The cited date is in the future.")
        return parsed.isoformat()
    if field == "backup_status" and value.lower() not in ("success", "failed", "partial", "unknown"):
        raise ValueError("Use success, failed, partial, or unknown for a backup job.")
    return value


def validate_fact(fact, passages):
    errors, citations = [], []
    by_id = {row["id"]: row for row in passages}
    try:
        value = normalized_value(fact["field"], fact["value"])
    except (ValueError, InvalidOperation, TypeError):
        value = None
        errors.append("The extracted value does not match the field's type or allowed range.")
    for citation in fact["citations"]:
        passage = by_id.get(citation["passage_id"])
        quote = citation["quote"]
        if not passage or not quote or quote not in passage["text"]:
            errors.append("A citation is not an exact passage from this document version.")
            continue
        start = passage["text"].index(quote)
        citations.append({"passage_id": passage["id"], "quote": quote, "start": start, "end": start + len(quote),
            "passage_sha256": passage["sha256"], "locator": passage["locator"], "method": passage["method"]})
    if not citations:
        errors.append("At least one verifiable source passage is required.")
    quotes = " ".join(row["quote"] for row in citations)
    lowered = quotes.lower()
    if value is not None and citations:
        field = fact["field"]
        if field.endswith("_minutes"):
            measured = times_in(quotes)
            context = r"restor|recovery\s+(?:time|duration|took|completed)|elapsed" if field == "restore_duration_minutes" else r"data age|recovered data|data loss|rpo"
            if Decimal(value) not in measured or len(measured) != 1 or not re.search(context, lowered):
                errors.append("Cite one clearly identified measurement with units; its value must match the extracted minutes.")
            if re.search(r"\b(target|objective|maximum|required)\b", lowered):
                errors.append("A target is not a measured test result. Select a passage stating the observed measurement.")
        elif field == "functional_checks_passed":
            passed = bool(re.search(r"\b(passed|successful|worked)\b", lowered))
            failed = bool(re.search(r"\b(failed|unsuccessful|not (?:passed|successful)|did not (?:work|pass)|didn't (?:work|pass))\b", lowered))
            if "functional" not in lowered or (value and (not passed or failed)) or (not value and not failed):
                errors.append("The passage must explicitly support the functional-check outcome.")
        elif field == "backup_status":
            if "backup" not in lowered or (value == "success" and (not re.search(r"\b(success|successful|completed)\b", lowered) or re.search(r"\b(not|failed|unsuccessful|partial(?:ly)?)\b", lowered))) or (value != "success" and value not in lowered):
                errors.append("The passage does not support this backup status.")
        elif " ".join(str(value).lower().split()) not in " ".join(lowered.split()):
            errors.append("The value must appear in the cited passage. Correct it using the source's wording.")
    return {**fact, "normalized_value": value, "citations": citations, "validation_errors": list(dict.fromkeys(errors)), "valid": not errors}


def validate_extraction(payload, passages):
    extraction = Extraction.model_validate(payload)
    facts = []
    for index, fact in enumerate(extraction.facts, 1):
        facts.append({"key": f"f{index:03d}", **validate_fact(fact.model_dump(), passages)})
    return facts, [message[:500] for message in extraction.warnings]
