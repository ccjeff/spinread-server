"""Copy source allowlist into a fresh directory; never copy credentials/media/git.

Usage: python deploy/prepare_context.py --output /tmp/rallyloop-build-UNIQUE
Build with docker build --target api|dispatcher|compute -t NAME OUTPUT.
No uploads, builds, pushes or deployments are performed by this script.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil


def prepare(output):
    server = Path(__file__).resolve().parents[1]
    training = server.parent / "pingpong-training"
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    sources = {}

    def copy(src, dest):
        if src.is_symlink():
            raise ValueError(f"Refusing symlink: {src}")
        if src.is_dir():
            for item in sorted(src.iterdir()):
                if item.name in {"__pycache__", ".pytest_cache"} or item.name.endswith(".egg-info"):
                    continue
                copy(item, dest / item.name)
        elif src.suffix in {".py", ".json", ".txt", ".toml", ".md", ".ini", ".mako", ".lock"} or src.name == "Dockerfile":
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            sources[str(dest.relative_to(output))] = hashlib.sha256(dest.read_bytes()).hexdigest()
        else:
            raise ValueError(f"Non-source file in allowlist: {src}")

    for name in ("spinread", "alembic", "alembic.ini", "pyproject.toml", "README.md"):
        copy(server / name, output / "server" / name)
    for name in ("src", "pyproject.toml", "README.md"):
        copy(training / name, output / "training" / name)
    for name in ("Dockerfile", "requirements.lock", "requirements-gpu.lock"):
        copy(server / "deploy" / name, output / name)
    copy(server / "deploy" / "tests", output / "tests")
    (output / "source-manifest.json").write_text(json.dumps(sources, indent=2, sort_keys=True) + "\n")
    (output / ".dockerignore").write_text("**/.env*\n**/.git\n**/__pycache__\n**/*.mp4\n**/*.pt\n**/*.pth\n")
    print(f"Prepared {len(sources)} source files in {output}; no build/push/deploy performed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    prepare(parser.parse_args().output)
