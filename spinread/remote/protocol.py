"""Versioned JSON + bounded ZIP_STORED result envelope. No pickle/SQL/code."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
import zipfile

import httpx

VERSION = 1
COMPUTE_STAGES = {"PROBE", "NORMALIZE", "QUALITY", "ACTIVITY", "RALLY", "EVENTS"}
MAX_MANIFEST_BYTES = 32 * 1024**2
MAX_FILES = 20000


class RemoteError(RuntimeError):
    """Public error codes only; URLs/provider exceptions may contain credentials."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def checked_url(url):
    parsed = urlsplit(url)
    # Only R2 signed requests are accepted by the remote runtime. No arbitrary
    # URL fetches, local metadata service, redirects, or credentials in userinfo.
    if (parsed.scheme != "https" or not parsed.hostname or parsed.port not in (None, 443)
            or parsed.username or parsed.password
            or not parsed.hostname.endswith(".r2.cloudflarestorage.com")):
        raise RemoteError("INVALID_STORAGE_ORIGIN")
    return url


def fetch(url, dest, *, max_bytes, expected_hash=None):
    from spinread.core.storage import file_sha256
    path = Path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with httpx.stream("GET", checked_url(url), timeout=120, follow_redirects=False) as response:
            response.raise_for_status()
            with path.open("wb") as output:
                for block in response.iter_bytes(1024 * 1024):
                    size += len(block)
                    if size > max_bytes:
                        raise RemoteError("INPUT_TOO_LARGE")
                    output.write(block)
        if expected_hash and file_sha256(str(path)) != expected_hash:
            raise RemoteError("INPUT_HASH_MISMATCH")
    except RemoteError:
        path.unlink(missing_ok=True)
        raise
    except Exception:
        path.unlink(missing_ok=True)
        raise RemoteError("INPUT_DOWNLOAD_FAILED") from None


def pack(path, identity, result, files, *, max_bytes):
    from spinread.core.storage import file_sha256
    if len(files) > MAX_FILES:
        raise RemoteError("TOO_MANY_OUTPUTS")
    metadata = []
    total = 0
    for index, (key, source) in enumerate(sorted(files.items())):
        source = Path(source)
        if source.is_symlink() or not source.is_file():
            raise RemoteError("INVALID_OUTPUT_FILE")
        size = source.stat().st_size
        total += size
        if total > max_bytes:
            raise RemoteError("OUTPUT_TOO_LARGE")
        metadata.append({"name": f"f{index}", "key": key, "size": size, "sha256": file_sha256(str(source))})
    manifest = canonical({"version": VERSION, "identity": identity, "result": result, "files": metadata})
    if len(manifest) > MAX_MANIFEST_BYTES:
        raise RemoteError("MANIFEST_TOO_LARGE")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        archive.writestr("manifest.json", manifest)
        for spec, source in zip(metadata, (v for _, v in sorted(files.items()))):
            archive.write(source, spec["name"])
    if Path(path).stat().st_size > max_bytes:
        raise RemoteError("OUTPUT_TOO_LARGE")


def unpack(path, root, identity, *, max_bytes):
    from spinread.core.storage import file_sha256
    if Path(path).stat().st_size > max_bytes:
        raise RemoteError("OUTPUT_TOO_LARGE")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if (len(infos) > MAX_FILES + 1 or len({i.filename for i in infos}) != len(infos)
                    or any(i.compress_type != zipfile.ZIP_STORED for i in infos)
                    or sum(i.file_size for i in infos) > max_bytes):
                raise RemoteError("INVALID_ARCHIVE")
            info = archive.getinfo("manifest.json")
            if info.file_size > MAX_MANIFEST_BYTES:
                raise RemoteError("MANIFEST_TOO_LARGE")
            manifest = json.loads(archive.read(info))
            if manifest["version"] != VERSION or manifest["identity"] != identity:
                raise RemoteError("RESULT_IDENTITY_MISMATCH")
            specs = manifest["files"]
            expected = {"manifest.json", *(f"f{i}" for i in range(len(specs)))}
            if set(archive.namelist()) != expected:
                raise RemoteError("INVALID_ARCHIVE_MEMBERS")
            files = {}
            for index, spec in enumerate(specs):
                if spec["name"] != f"f{index}" or spec["key"] in files:
                    raise RemoteError("INVALID_FILE_MANIFEST")
                member = archive.getinfo(spec["name"])
                if type(spec["size"]) is not int or member.file_size != spec["size"]:
                    raise RemoteError("OUTPUT_SIZE_MISMATCH")
                target = root / spec["name"]  # Never extract a remote path.
                with archive.open(member) as src, target.open("wb") as out:
                    import shutil
                    shutil.copyfileobj(src, out, length=1024 * 1024)
                if file_sha256(str(target)) != spec["sha256"]:
                    raise RemoteError("OUTPUT_HASH_MISMATCH")
                files[spec["key"]] = target
            return manifest["result"], files
    except RemoteError:
        raise
    except Exception:
        raise RemoteError("INVALID_RESULT_BUNDLE") from None
