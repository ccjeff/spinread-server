"""Execute an operator-installed Pingpong3D checkout without coupling its DB/auth.

Only our isolated work directory is written. Upstream code, model licensing and
its Python/GPU environment remain operator-managed external dependencies.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess


class UpstreamUnavailable(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def configured(settings) -> bool:
    repo = Path(settings.pingpong3d_repo) if settings.pingpong3d_repo else None
    return bool(repo and (repo / "scripts/match/pipeline.py").is_file()
                and settings.pingpong3d_python and Path(settings.pingpong3d_python).is_file()
                and settings.pingpong3d_model_dir and Path(settings.pingpong3d_model_dir).is_dir())


def _execute(command: list[str], *, cwd: Path, env: dict, log: Path, timeout: int) -> int:
    # Do not keep an unbounded stdout buffer or return paths/tokens in API errors.
    with log.open("ab") as output:
        proc = subprocess.Popen(command, cwd=cwd, env=env, stdout=output,
                                stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            raise UpstreamUnavailable("ANALYSIS_TIMEOUT") from None


def run_match(source: Path, work: Path, settings, rallies: list[dict], players: list[dict]) -> Path:
    if not configured(settings):
        raise UpstreamUnavailable("PINGPONG3D_NOT_CONFIGURED")
    repo = Path(settings.pingpong3d_repo).resolve()
    work.mkdir(parents=True, exist_ok=True)
    # Do not inherit SpinRead storage/auth credentials into an optional process.
    allowed = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "CONDA_PREFIX", "VIRTUAL_ENV",
               "CUDA_VISIBLE_DEVICES", "MATCH_TABLE_PYTHON", "MATCH_ANNY_PYTHON",
               "NLF_FT_MODEL", "NLF_API_BASE", "NLF_API_TOKEN", "MATCH_GPU_PYTHON", "PP_GPU_HOME")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update({"PYTHON_BIN": settings.pingpong3d_python, "MATCH_PYTHON": settings.pingpong3d_python,
                "PP_MODEL_DIR": settings.pingpong3d_model_dir, "NLF_GPU": "local",
                "SELFREVIEW_MAX_RALLIES": str(settings.pingpong3d_max_rallies),
                "PRO_LIBRARY_DB": str(work / "pro_library.sqlite")})
    ids = ("p1", "p2")
    public_players = [{"id": ids[i], "name": f"Player {i+1}", "short": ids[i],
                       "color": ("#6750a4", "#168b92")[i], **p,
                       **({"height": p["height"] / 100} if p.get("height") is not None else {})}
                      for i, p in enumerate(players)]
    match = {"id": work.name, "title": "SpinRead analysis", "kind": "user", "me": "p1",
             "players": public_players, "options": {"gpu": "local", "split": True, "ball": True},
             "source_url": "", "upload": {"file": "input.video", "complete": True}}
    # Keep source PTS: upstream's supported ingest handles CFR/VFR explicitly.
    import shutil
    shutil.copyfile(source, work / "input.video")
    (work / "match.json").write_text(json.dumps(match), encoding="utf-8")
    command = [settings.pingpong3d_python, str(repo / "scripts/match/pipeline.py"), str(work)]
    log = work / "worker.log"
    rc = _execute(command, cwd=repo, env=env, log=log, timeout=settings.pingpong3d_timeout_seconds)
    status_path = work / "status.json"
    status = json.loads(status_path.read_text()) if status_path.exists() else {}
    if status.get("status") == "awaiting_selection":
        points = json.loads((work / "points.json").read_text())
        selected = []
        for r in points.get("rallies", []):
            a, b = float(r["t0"]) * 1000, float(r["t1"]) * 1000
            if any(max(a, s["start_ms"]) < min(b, s["end_ms"]) for s in rallies):
                selected.append(r["id"])
        selected = selected[:settings.pingpong3d_max_rallies]
        if not selected:
            raise UpstreamUnavailable("NO_MATCHING_RALLIES")
        (work / "selection.json").write_text(json.dumps({"rallies": selected, "by": "spinread"}))
        rc = _execute(command, cwd=repo, env=env, log=log, timeout=settings.pingpong3d_timeout_seconds)
        status = json.loads(status_path.read_text()) if status_path.exists() else {}
    if status.get("status") == "awaiting_table":
        raise UpstreamUnavailable("TABLE_CALIBRATION_REQUIRED")
    if status.get("status") == "awaiting_gpu":
        raise UpstreamUnavailable("GPU_NOT_CONFIGURED")
    if rc != 0 or status.get("status") != "ready":
        raise UpstreamUnavailable("PINGPONG3D_ANALYSIS_FAILED")
    return work
