"""Timeline edits: ops semantics, versioning, 409 conflict, 422 validation.

Uses a dedicated video (edits mutate the timeline; isolation keeps the module
independent of test ordering elsewhere). Test order within the module matters:
non-mutating/rejecting tests run before the ones that reshape the timeline.
"""

from __future__ import annotations

import time

import pytest

from conftest import login_headers, upload_video, wait_until_ready
from spinread.worker.main import run_worker

pytestmark = pytest.mark.usefixtures("require_services")


@pytest.fixture(scope="module")
def edit_video(client, synthetic_training_video):
    headers = login_headers(client)
    video_id = upload_video(client, headers, synthetic_training_video)
    status = wait_until_ready(client, headers, video_id)
    assert status["state"] == "READY", status
    return video_id, headers


def _active(client, headers, video_id):
    resp = client.get(f"/api/videos/{video_id}/timelines/active", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _edit(client, headers, video_id, base_version, ops):
    return client.post(
        f"/api/videos/{video_id}/timeline-edits",
        headers=headers,
        json={"base_timeline_version": base_version, "operations": ops},
    )


def test_overlapping_top_level_rejected(client, edit_video):
    video_id, headers = edit_video
    tl = _active(client, headers, video_id)
    top = sorted((i for i in tl["items"] if i["parent_id"] is None), key=lambda i: i["start_ms"])
    assert len(top) >= 2
    # stretch seg1 over seg2 -> 422 OVERLAPPING_SEGMENTS
    r = _edit(client, headers, video_id, tl["version"], [
        {"op": "UPDATE_BOUNDARY", "timeline_item_id": top[0]["item_id"],
         "start_ms": top[0]["start_ms"], "end_ms": top[1]["start_ms"] + 5000},
    ])
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "OVERLAPPING_SEGMENTS"


def test_set_label_rules(client, edit_video):
    video_id, headers = edit_video
    tl = _active(client, headers, video_id)
    top = [i for i in tl["items"] if i["parent_id"] is None]
    rally = next(i for i in tl["items"] if i["type"] == "RALLY")

    # RALLY relabel rejected (422)
    r = _edit(client, headers, video_id, tl["version"], [
        {"op": "SET_LABEL", "timeline_item_id": rally["item_id"],
         "field": "type", "value": "BREAK"},
    ])
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "LABEL_NOT_EDITABLE"

    # top-level relabel ok
    r = _edit(client, headers, video_id, tl["version"], [
        {"op": "SET_LABEL", "timeline_item_id": top[0]["item_id"],
         "field": "type", "value": "BREAK"},
    ])
    assert r.status_code == 200, r.text
    tl2 = _active(client, headers, video_id)
    top2 = sorted((i for i in tl2["items"] if i["parent_id"] is None), key=lambda i: i["start_ms"])
    assert top2[0]["type"] == "BREAK"


def test_update_boundary_and_split_merge_delete(client, edit_video):
    video_id, headers = edit_video
    tl = _active(client, headers, video_id)
    v = tl["version"]
    items = tl["items"]
    top = sorted((i for i in items if i["parent_id"] is None), key=lambda i: i["start_ms"])
    assert len(top) >= 2
    seg1, seg2 = top[0], top[1]

    # UPDATE_BOUNDARY on seg1 (note: every edit copies all rows with NEW ids)
    r = _edit(client, headers, video_id, v, [
        {"op": "UPDATE_BOUNDARY", "timeline_item_id": seg1["item_id"],
         "start_ms": 1000, "end_ms": 19000},
    ])
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["version"] == v + 1
    tl2 = _active(client, headers, video_id)
    assert tl2["version"] == v + 1  # pointer switched
    top2 = sorted((i for i in tl2["items"] if i["parent_id"] is None), key=lambda i: i["start_ms"])
    assert (top2[0]["start_ms"], top2[0]["end_ms"]) == (1000, 19000)

    # SPLIT seg2 at 45 s (re-resolve the v2 id); rallies/hits 归边
    seg2_v2 = top2[1]
    r = _edit(client, headers, video_id, tl2["version"], [
        {"op": "SPLIT", "timeline_item_id": seg2_v2["item_id"], "at_ms": 45000},
    ])
    assert r.status_code == 200, r.text
    tl3 = _active(client, headers, video_id)
    top3 = sorted((i for i in tl3["items"] if i["parent_id"] is None), key=lambda i: i["start_ms"])
    assert len(top3) == 3
    assert top3[1]["end_ms"] == 45000
    assert top3[2]["start_ms"] == 45000
    ids3 = {i["item_id"] for i in top3[1:]}
    for i in tl3["items"]:
        if i["parent_id"] in ids3:
            parent = next(t for t in top3 if t["item_id"] == i["parent_id"])
            assert parent["start_ms"] <= i["start_ms"]
            assert i["end_ms"] <= parent["end_ms"]

    # MERGE_NEXT the two halves back (same type required)
    r = _edit(client, headers, video_id, tl3["version"], [
        {"op": "MERGE_NEXT", "timeline_item_id": top3[1]["item_id"]},
    ])
    assert r.status_code == 200, r.text
    tl4 = _active(client, headers, video_id)
    top4 = [i for i in tl4["items"] if i["parent_id"] is None]
    assert len(top4) == 2

    # DELETE first segment: subtree goes with it
    r = _edit(client, headers, video_id, tl4["version"], [
        {"op": "DELETE", "timeline_item_id": top4[0]["item_id"]},
    ])
    assert r.status_code == 200, r.text
    tl5 = _active(client, headers, video_id)
    top5 = [i for i in tl5["items"] if i["parent_id"] is None]
    assert len(top5) == 1
    # every remaining item is the surviving segment or one of its descendants
    by_id = {i["item_id"]: i for i in tl5["items"]}
    for i in tl5["items"]:
        node = i
        while node["parent_id"] is not None:
            node = by_id[node["parent_id"]]
        assert node["item_id"] == top5[0]["item_id"]


def test_stale_base_version_conflict(client, edit_video):
    video_id, headers = edit_video
    tl = _active(client, headers, video_id)
    top = [i for i in tl["items"] if i["parent_id"] is None]
    r = _edit(client, headers, video_id, tl["version"] - 1, [
        {"op": "DELETE", "timeline_item_id": top[0]["item_id"]},
    ])
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "TIMELINE_VERSION_CONFLICT"
    assert body["error"]["details"]["current"]["version"] == tl["version"]


def test_version_read_and_correction_run(client, edit_video):
    """GET arbitrary version + edits seed a CORRECTION run that recomputes metrics."""
    video_id, headers = edit_video
    tl = _active(client, headers, video_id)
    v1 = client.get(f"/api/videos/{video_id}/timelines/1", headers=headers)
    assert v1.status_code == 200
    assert v1.json()["version"] == 1

    top = sorted((i for i in tl["items"] if i["parent_id"] is None), key=lambda i: i["start_ms"])
    r = _edit(client, headers, video_id, tl["version"], [
        {"op": "UPDATE_BOUNDARY", "timeline_item_id": top[0]["item_id"],
         "start_ms": top[0]["start_ms"], "end_ms": top[0]["end_ms"] - 2000},
    ])
    assert r.status_code == 200, r.text
    new_version = r.json()["version"]

    # drain the CORRECTION run (METRICS/REPORT only)
    deadline = time.time() + 120
    while time.time() < deadline:
        run_worker(once=True, worker_id="pytest-correction")
        st = client.get(f"/api/videos/{video_id}/processing-status", headers=headers).json()
        if all(s["status"] in ("SUCCEEDED", "REUSED_CACHE") for s in st["stages"]):
            break
        time.sleep(0.5)
    else:
        raise AssertionError(f"correction run did not finish: {st}")

    report = client.get(f"/api/videos/{video_id}/reports/active", headers=headers)
    assert report.status_code == 200
    assert report.json()["timeline_version"] == new_version


def test_rally_boundary_shrink_drops_out_of_range_hits(client, edit_video):
    """RALLY UPDATE_BOUNDARY shrink: 200, hits inside kept exactly, hits
    beyond the new boundary absent from the new version (REMOVED semantics)."""
    video_id, headers = edit_video
    tl = _active(client, headers, video_id)
    rally = next(i for i in tl["items"] if i["type"] == "RALLY")
    hits_before = [
        i for i in tl["items"] if i["type"] == "HIT_CANDIDATE" and i["parent_id"] == rally["item_id"]
    ]
    assert len(hits_before) >= 4

    new_start = rally["start_ms"] + 2000
    new_end = rally["end_ms"] - 2000
    assert new_start < new_end  # fixture rally is ~10 s long
    r = _edit(client, headers, video_id, tl["version"], [
        {"op": "UPDATE_BOUNDARY", "timeline_item_id": rally["item_id"],
         "start_ms": new_start, "end_ms": new_end},
    ])
    assert r.status_code == 200, r.text

    tl2 = _active(client, headers, video_id)
    assert tl2["version"] == tl["version"] + 1
    rally2 = next(i for i in tl2["items"] if i["type"] == "RALLY")
    assert (rally2["start_ms"], rally2["end_ms"]) == (new_start, new_end)
    hits_after = [
        i for i in tl2["items"] if i["type"] == "HIT_CANDIDATE" and i["parent_id"] == rally2["item_id"]
    ]
    # every surviving hit is valid and inside the new bounds
    for h in hits_after:
        assert h["end_ms"] > h["start_ms"]
        assert new_start <= h["start_ms"]
        assert h["end_ms"] <= new_end
    # in-range hits are preserved (start times match the old in-range subset)
    expected_starts = sorted(
        h["start_ms"] for h in hits_before
        if h["start_ms"] >= new_start and h["end_ms"] <= new_end
    )
    assert sorted(h["start_ms"] for h in hits_after) == expected_starts
    assert 0 < len(hits_after) < len(hits_before)


def test_rally_boundary_expand_keeps_hits_untouched(client, edit_video):
    """RALLY UPDATE_BOUNDARY expand: 200 and hit children neither dropped nor re-clamped."""
    video_id, headers = edit_video
    tl = _active(client, headers, video_id)
    rally = next(i for i in tl["items"] if i["type"] == "RALLY")
    hits_before = sorted(
        (i["start_ms"], i["end_ms"])
        for i in tl["items"]
        if i["type"] == "HIT_CANDIDATE" and i["parent_id"] == rally["item_id"]
    )

    r = _edit(client, headers, video_id, tl["version"], [
        {"op": "UPDATE_BOUNDARY", "timeline_item_id": rally["item_id"],
         "start_ms": rally["start_ms"] - 3000, "end_ms": rally["end_ms"] + 2000},
    ])
    assert r.status_code == 200, r.text

    tl2 = _active(client, headers, video_id)
    rally2 = next(i for i in tl2["items"] if i["type"] == "RALLY")
    assert (rally2["start_ms"], rally2["end_ms"]) == (
        rally["start_ms"] - 3000, rally["end_ms"] + 2000,
    )
    hits_after = sorted(
        (i["start_ms"], i["end_ms"])
        for i in tl2["items"]
        if i["type"] == "HIT_CANDIDATE" and i["parent_id"] == rally2["item_id"]
    )
    assert hits_after == hits_before


def test_mixed_top_and_rally_ops_single_submit(client, edit_video):
    """One request mixing top-level and RALLY ops applies atomically."""
    video_id, headers = edit_video
    tl = _active(client, headers, video_id)
    top = sorted((i for i in tl["items"] if i["parent_id"] is None), key=lambda i: i["start_ms"])
    rally = next(i for i in tl["items"] if i["type"] == "RALLY")

    r = _edit(client, headers, video_id, tl["version"], [
        {"op": "UPDATE_BOUNDARY", "timeline_item_id": top[0]["item_id"],
         "start_ms": top[0]["start_ms"], "end_ms": top[0]["end_ms"]},
        {"op": "UPDATE_BOUNDARY", "timeline_item_id": rally["item_id"],
         "start_ms": rally["start_ms"] + 1000, "end_ms": rally["end_ms"]},
        {"op": "SET_LABEL", "timeline_item_id": top[0]["item_id"],
         "field": "type", "value": "INSTRUCTION"},
    ])
    assert r.status_code == 200, r.text
    tl2 = _active(client, headers, video_id)
    assert tl2["version"] == tl["version"] + 1
    top2 = sorted((i for i in tl2["items"] if i["parent_id"] is None), key=lambda i: i["start_ms"])
    assert top2[0]["type"] == "INSTRUCTION"
    rally2 = next(i for i in tl2["items"] if i["type"] == "RALLY")
    assert rally2["start_ms"] == rally["start_ms"] + 1000
    for h in tl2["items"]:
        assert h["end_ms"] > h["start_ms"]
