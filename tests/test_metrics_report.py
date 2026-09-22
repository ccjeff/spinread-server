"""METRICS/REPORT stages: hand-checked values + finding rules + correction recompute."""

from __future__ import annotations

import time

import pytest

from spinread.worker.main import run_worker

pytestmark = pytest.mark.usefixtures("require_services")


def test_metrics_hand_check(client, ready_video):
    """60 s fixture: two RALLY_LIKE segments [0,~21s) + [35s,60s), 2 rallies."""
    video_id, headers = ready_video
    report = client.get(f"/api/videos/{video_id}/reports/active", headers=headers)
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["state"] == "PUBLISHED"
    assert body["timeline_version"] >= 1  # rerun tests may have advanced it

    metrics = body["structured"]["metrics"]
    assert metrics["rally_count"] == 2
    # both segments are RALLY_LIKE; hand-computed from the timeline itself
    items = client.get(
        f"/api/videos/{video_id}/timelines/active", headers=headers
    ).json()["items"]
    top = [i for i in items if i["parent_id"] is None]
    expected_valid = sum(i["end_ms"] - i["start_ms"] for i in top if i["type"] == "RALLY_LIKE")
    assert metrics["valid_duration_ms"] == expected_valid == 46000
    assert metrics["rally_duration_ms"]["max"] > 0
    assert sum(metrics["rally_length_distribution"].values()) == 2
    assert metrics["hits_per_rally"]["mean"] > 3
    assert metrics["hits_per_rally"]["max"] >= 10
    assert metrics["segment_type_distribution"] == {"RALLY_LIKE": 2}
    assert metrics["correction_rate"] == 0.0
    assert 0.0 <= metrics["confidence_coverage"] <= 1.0

    # findings: deterministic rules over this fixture
    # non-rally share = 1 - 46000/60000 ≈ 0.233 < 0.25 -> no ACTIVITY_MIX
    # rallies are ~21 s / ~25 s < 60 s -> no RALLY_LENGTH
    categories = {f["category"] for f in body["findings"]}
    assert "ACTIVITY_MIX" not in categories
    assert "RALLY_LENGTH" not in categories
    assert categories <= {"COVERAGE"}
    for f in body["findings"]:
        assert f["state"] in ("PUBLISHED", "LOW_EVIDENCE")
        assert f["sample_count"] >= 0

    # metric_versions recorded
    assert set(body["metric_versions"]) == {
        "valid_duration_ms", "rally_count", "rally_duration_ms",
        "rally_length_distribution", "hits_per_rally",
        "segment_type_distribution", "confidence_coverage", "correction_rate",
    }


def test_correction_run_recomputes_metrics(client, ready_video):
    """After a timeline edit, a CORRECTION run recomputes metric_values for v2."""
    video_id, headers = ready_video
    tl = client.get(f"/api/videos/{video_id}/timelines/active", headers=headers).json()
    top = sorted((i for i in tl["items"] if i["parent_id"] is None), key=lambda i: i["start_ms"])

    resp = client.post(
        f"/api/videos/{video_id}/timeline-edits",
        headers=headers,
        json={
            "base_timeline_version": tl["version"],
            "operations": [
                {"op": "UPDATE_BOUNDARY", "timeline_item_id": top[0]["item_id"],
                 "start_ms": top[0]["start_ms"], "end_ms": top[0]["end_ms"] - 3000},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    new_version = resp.json()["version"]
    assert new_version == tl["version"] + 1

    deadline = time.time() + 120
    while time.time() < deadline:
        run_worker(once=True, worker_id="pytest-metrics-correction")
        st = client.get(f"/api/videos/{video_id}/processing-status", headers=headers).json()
        if all(s["status"] in ("SUCCEEDED", "REUSED_CACHE") for s in st["stages"]):
            break
        time.sleep(0.5)
    else:
        raise AssertionError(f"correction run did not finish: {st}")

    report = client.get(f"/api/videos/{video_id}/reports/active", headers=headers).json()
    assert report["timeline_version"] == new_version
    metrics = report["structured"]["metrics"]
    # segment 1 shortened by 3 s
    assert metrics["valid_duration_ms"] == 46000 - 3000
    assert metrics["correction_rate"] > 0.0


def test_coverage_finding_evidence_truncated(client, ready_video):
    """A COVERAGE finding with >20 evidence intervals is capped at 20 (order
    preserved) and carries a truncation note in limitations (HLD §10.2)."""
    from sqlalchemy import select

    from spinread.core.db import make_engine, make_session_factory
    from spinread.core.models import MetricValue, PipelineRun
    from spinread.pipeline import orchestrator

    video_id, headers = ready_video
    report = client.get(f"/api/videos/{video_id}/reports/active", headers=headers).json()
    tl_version = report["timeline_version"]

    # Fabricate a low-coverage metric row carrying 25 evidence intervals.
    factory = make_session_factory(make_engine())
    session = factory()
    row = session.scalar(
        select(MetricValue).where(
            MetricValue.video_id == video_id,
            MetricValue.timeline_version == tl_version,
            MetricValue.metric_name == "confidence_coverage",
        )
    )
    assert row is not None
    intervals = [[i * 1000, i * 1000 + 500] for i in range(25)]
    row.value = {
        "result": 0.4,  # low_ratio 0.6 > 0.3 -> COVERAGE triggers
        "evidence_intervals": intervals,
        "video_duration_ms": 60000,
    }
    row.sample_count = 25
    run = PipelineRun(
        video_id=video_id, pipeline_version="1.1.0", trigger="CORRECTION"
    )
    session.add(run)
    session.flush()
    orchestrator.tick(session, run.id, only_stages={"REPORT"})
    session.commit()
    session.close()

    deadline = time.time() + 60
    while time.time() < deadline:
        run_worker(once=True, worker_id="pytest-truncation")
        st = client.get(f"/api/videos/{video_id}/processing-status", headers=headers).json()
        if all(s["status"] in ("SUCCEEDED", "REUSED_CACHE") for s in st["stages"]):
            break
        time.sleep(0.5)
    else:
        raise AssertionError(f"REPORT rerun did not finish: {st}")

    report = client.get(f"/api/videos/{video_id}/reports/active", headers=headers).json()
    coverage = next(f for f in report["findings"] if f["category"] == "COVERAGE")
    assert len(coverage["evidence_intervals"]) == 20
    assert coverage["evidence_intervals"] == intervals[:20]  # order preserved
    assert any(
        "evidence truncated: 25 intervals, showing first 20" in lim
        for lim in coverage["limitations"]
    ), coverage["limitations"]
