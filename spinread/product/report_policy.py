"""Detector diagnostics are not player findings or training prescriptions."""
INTERNAL_FINDING_CATEGORIES = frozenset({'COVERAGE', 'RALLY_LENGTH', 'ACTIVITY_MIX'})
INTERNAL_METRICS = frozenset({'confidence_coverage', 'correction_rate'})


def public_finding(finding):
    return finding.category not in INTERNAL_FINDING_CATEGORIES and finding.state != 'INTERNAL'


def public_plan_item(db, item):
    from spinread.core.models import Finding
    if item.source != 'SYSTEM' or not item.finding_ids:
        return True
    return any((f := db.get(Finding, id)) is not None and public_finding(f) for id in item.finding_ids)
