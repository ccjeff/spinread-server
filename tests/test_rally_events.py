"""RALLY/EVENTS hierarchy on the 60 s synthetic fixture (real stack)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("require_services")


def test_rally_and_hit_hierarchy(client, ready_video):
    video_id, headers = ready_video
    resp = client.get(f"/api/videos/{video_id}/timelines/active", headers=headers)
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    by_id = {i["item_id"]: i for i in items}

    top = [i for i in items if i["parent_id"] is None]
    rally_like = [i for i in top if i["type"] == "RALLY_LIKE"]
    assert len(rally_like) >= 2, top

    rallies = [i for i in items if i["type"] == "RALLY"]
    assert rallies, "expected RALLY children"
    for r in rallies:
        assert r["parent_id"] in by_id
        parent = by_id[r["parent_id"]]
        assert parent["type"] == "RALLY_LIKE"
        assert parent["start_ms"] <= r["start_ms"] <= parent["end_ms"]
        assert r["attributes"]["hits"] >= 3
        assert r["attributes"]["poc"] == "rally-av-reset-0.3.0"

    hits = [i for i in items if i["type"] == "HIT_CANDIDATE"]
    assert hits, "expected HIT_CANDIDATE items"
    for h in hits:
        assert h["parent_id"] in by_id
        assert by_id[h["parent_id"]]["type"] == "RALLY"
        assert h["actor"] is None
        assert 0 < h["end_ms"] - h["start_ms"] <= 40


def test_rally_boundaries_match_synthetic_structure(client, ready_video):
    """Clicks every 2 s inside both active segments => one rally per segment,
    each closing at its segment end (POC rule: open rally closes at segment end)."""
    video_id, headers = ready_video
    items = client.get(
        f"/api/videos/{video_id}/timelines/active", headers=headers
    ).json()["items"]
    by_id = {i["item_id"]: i for i in items}
    rallies = sorted(
        (i for i in items if i["type"] == "RALLY"), key=lambda i: i["start_ms"]
    )
    assert len(rallies) == 2, rallies
    first, second = rallies
    assert first["start_ms"] < 3000
    assert first["end_ms"] <= by_id[first["parent_id"]]["end_ms"]
    assert 35000 <= second["start_ms"] < 38000
    assert second["end_ms"] <= by_id[second["parent_id"]]["end_ms"]
