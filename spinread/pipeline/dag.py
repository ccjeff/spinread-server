"""Pipeline DAG as data (LLD §5.1, trimmed to the MLP subset)."""

from __future__ import annotations

PIPELINE_VERSION = "1.0.0"

# stage -> list of prerequisite stages
STAGES: dict[str, list[str]] = {
    "PROBE": [],
    "NORMALIZE": ["PROBE"],
    "QUALITY": ["NORMALIZE"],
    "ACTIVITY": ["QUALITY"],
    "TIMELINE": ["ACTIVITY"],
}

# stage_runs statuses that satisfy a dependency
SATISFIED = {"SUCCEEDED", "PARTIAL_SUCCESS", "SKIPPED_UNSUPPORTED", "REUSED_CACHE"}

# per-stage job attempt ceilings (ffmpeg transience gets 3 per LLD §4.4 note)
STAGE_MAX_ATTEMPTS = {"NORMALIZE": 3}

# terminal stage_runs statuses
TERMINAL = SATISFIED | {"PERMANENT_FAILURE"}

# video state per running stage (LLD §14.1 text states)
STAGE_VIDEO_STATE = {
    "PROBE": "PROBING",
    "NORMALIZE": "NORMALIZING",
    "QUALITY": "QUALITY_CHECKING",
    "ACTIVITY": "SEGMENTING",
    "TIMELINE": "BUILDING_TIMELINE",
}
