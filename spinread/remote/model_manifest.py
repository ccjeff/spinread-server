"""Verify operator-supplied model files; never download or invent weights."""
import hashlib
import json
import os
from pathlib import Path

from spinread.core.storage import file_sha256
from spinread.remote.protocol import RemoteError

MODEL_ENV = {"nlf_weights": "SPINREAD_NLF_WEIGHTS", "nlf_faces": "SPINREAD_NLF_FACES",
             "blurball_weights": "SPINREAD_BLURBALL_WEIGHTS", "pose_weights": "SPINREAD_POSE_WEIGHTS"}


def verify(revision, manifest_path):
    path = Path(manifest_path)
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 65536:
        raise RemoteError("MODEL_MANIFEST_MISSING")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != revision:
        raise RemoteError("MODEL_REVISION_MISMATCH")
    manifest = json.loads(raw)
    root = path.parent.resolve()
    for name, entry in manifest["files"].items():
        if name not in MODEL_ENV:
            raise RemoteError("UNSUPPORTED_MODEL_SETTING")
        candidate = root / entry["path"]
        model = candidate.resolve()
        if candidate.is_symlink() or not model.is_relative_to(root) or not model.is_file():
            raise RemoteError("INVALID_MODEL_PATH")
        if file_sha256(str(model)) != entry["sha256"]:
            raise RemoteError("MODEL_HASH_MISMATCH")
        os.environ[MODEL_ENV[name]] = str(model)
    return manifest
