"""Synthetic producer boundary checks, with no GPU or user recording."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from pingpong_training.analysis import nlf
from spinread.product import nlf_analysis
from spinread.product.pingpong3d_runner import UpstreamUnavailable


def producer(monkeypatch, tmp_path, *, truncate=False, shifted=False, mismatch=False):
    settings=SimpleNamespace(nlf_weights="",nlf_faces="",nlf_fps=12,nlf_batch_size=4)
    monkeypatch.setattr(nlf_analysis,"status",lambda *a,**k:{"available":True})
    def run(source,work,config):
        work.mkdir(exist_ok=True)
        frames=[{"time_ms":round(i*1000/12)+(1 if shifted else 0),"people":[]} for i in range(24)]
        if truncate:frames=frames[:1]
        pose={"model":"NLF","joint_schema":"NLF41","unit":"m","fps":12,
              "coordinate_space":"HIP_RELATIVE_MODEL_3D",
              "axes":"X_TABLE_LENGTH_Y_TABLE_WIDTH_Z_UP" if mismatch else "X_RIGHT_Y_DOWN_Z_FORWARD",
              "frames":frames}
        raw=json.dumps(pose).encode();(work/"pose.json").write_bytes(raw)
        return {"complete":True,"source_sha256":"source","duration_ms":2000,
                "statistics":{"frames":len(frames)},"identity":{},
                "assets":{"pose":{"file":"pose.json","byte_length":len(raw),"sha256":hashlib.sha256(raw).hexdigest()}},
                "pose_chunks":[{"asset_id":"pose"}],"mesh":{"chunks":[],"limitations":[]}}
    monkeypatch.setattr(nlf,"run_nlf",run)
    return settings


def test_complete_source_clock_and_axes_are_preserved(monkeypatch,tmp_path):
    settings=producer(monkeypatch,tmp_path)
    evidence,_=nlf_analysis.run(tmp_path/"unused",tmp_path/"out",settings,"source",2000)
    pose=evidence["pose"]
    assert len(pose["frames"])==24
    assert pose["frames"][-1]["time_ms"]==1917
    assert pose["axes"]==pose["frames"][0]["axes"]=="X_RIGHT_Y_DOWN_Z_FORWARD"
    assert evidence["capabilities"]["pose_3d"]=="UNAVAILABLE"


@pytest.mark.parametrize("option",["truncate","shifted"])
def test_complete_flag_cannot_hide_truncated_or_shifted_clock(monkeypatch,tmp_path,option):
    settings=producer(monkeypatch,tmp_path,**{option:True})
    with pytest.raises(UpstreamUnavailable,match="NLF_INVALID_POSE_CLOCK"):
        nlf_analysis.run(tmp_path/"unused",tmp_path/"out",settings,"source",2000)


def test_hip_relative_data_cannot_claim_world_axes(monkeypatch,tmp_path):
    settings=producer(monkeypatch,tmp_path,mismatch=True)
    with pytest.raises(UpstreamUnavailable,match="NLF_INVALID_POSE_COORDINATES"):
        nlf_analysis.run(tmp_path/"unused",tmp_path/"out",settings,"source",2000)
