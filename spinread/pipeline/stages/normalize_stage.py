"""NORMALIZE stage (normalize-ffmpeg-0.1.0, LLD §4.4 subset).

One ffmpeg invocation produces proxy.mp4 + HLS (vod) from the same filter
graph; a second fast pass emits JPEG thumbnails every 5 s. All outputs are
uploaded under derived/{media_version}/ and registered as media_assets.
"""

from __future__ import annotations

import logging
import re

from pingpong_training.media.probe import FFmpegError, run_command
from sqlalchemy import select

from spinread.core.models import MediaAsset
from spinread.core.storage import derived_key, file_sha256
from spinread.pipeline.stage import (
    RetryableStageError,
    StageContext,
    StageError,
    StageResult,
)

log = logging.getLogger(__name__)

MEDIA_VERSION = 1

FILTER_GRAPH = "scale='min(1280,iw)':-2,fps=30,setsar=1"


def _active_original(ctx: StageContext) -> MediaAsset:
    original = ctx.session.scalar(
        select(MediaAsset).where(
            MediaAsset.video_id == ctx.video.id,
            MediaAsset.class_ == "ORIGINAL",
            MediaAsset.status == "ACTIVE",
        )
    )
    if original is None:
        raise StageError("NO_ORIGINAL", "video has no ACTIVE ORIGINAL media asset")
    return original


class NormalizeStage:
    stage = "NORMALIZE"
    stage_version = "normalize-ffmpeg-0.1.0"

    def run(self, ctx: StageContext) -> StageResult:
        original = _active_original(ctx)
        local_in = ctx.work_dir / "original_input"
        ctx.s3.download_file(original.object_key, str(local_in))

        duration_s = (ctx.video.duration_ms or 60_000) / 1000.0
        timeout_s = max(300.0, duration_s * 6)

        proxy_path = ctx.work_dir / "proxy.mp4"
        hls_dir = ctx.work_dir / "hls"
        hls_dir.mkdir(exist_ok=True)
        master_path = hls_dir / "master.m3u8"

        vf = f"[0:v]{FILTER_GRAPH},split=2[v1][v2]"
        cmd = [
            ctx.settings.ffmpeg_bin, "-y", "-v", "error",
            "-i", str(local_in),
            "-filter_complex", vf,
            "-map", "[v1]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-g", "180", "-keyint_min", "180", "-sc_threshold", "0",
            "-force_key_frames", "expr:gte(t,n_forced*6)",
            "-c:a", "aac", "-b:a", "96k", "-ac", "1",
            "-movflags", "+faststart",
            str(proxy_path),
            "-map", "[v2]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-g", "180", "-keyint_min", "180", "-sc_threshold", "0",
            "-force_key_frames", "expr:gte(t,n_forced*6)",
            "-c:a", "aac", "-b:a", "96k", "-ac", "1",
            "-f", "hls", "-hls_time", "6", "-hls_playlist_type", "vod",
            "-hls_segment_filename", str(hls_dir / "seg_%05d.ts"),
            str(master_path),
        ]
        try:
            run_command(cmd, timeout_s=timeout_s, what="normalize transcode")
        except FFmpegError as exc:
            raise RetryableStageError(f"ffmpeg transcode failed: {exc}", "FFMPEG_ERROR") from exc
        if not proxy_path.exists() or not master_path.exists():
            raise RetryableStageError("ffmpeg outputs missing", "FFMPEG_ERROR")

        # --- thumbnails: one JPEG per 5 s -----------------------------------
        thumb_dir = ctx.work_dir / "thumbs"
        thumb_dir.mkdir(exist_ok=True)
        thumb_cmd = [
            ctx.settings.ffmpeg_bin, "-y", "-v", "error",
            "-i", str(local_in),
            "-vf", "fps=1/5,scale=320:-2",
            "-q:v", "4",
            str(thumb_dir / "f_%05d.jpg"),
        ]
        try:
            run_command(thumb_cmd, timeout_s=timeout_s, what="thumbnail extraction")
        except FFmpegError as exc:
            raise RetryableStageError(f"ffmpeg thumbs failed: {exc}", "FFMPEG_ERROR") from exc

        uid, vid = ctx.video.owner_id, ctx.video.id
        base = f"derived/{MEDIA_VERSION}"

        def key(rel: str) -> str:
            return derived_key(uid, vid, MEDIA_VERSION, rel)

        def add_asset(class_: str, path, rel: str, manifest=None) -> MediaAsset:
            ctx.s3.upload_file(str(path), key(rel))
            asset = MediaAsset(
                video_id=vid,
                class_=class_,
                media_version=MEDIA_VERSION,
                object_key=key(rel),
                content_hash=file_sha256(str(path)),
                byte_size=path.stat().st_size,
                status="ACTIVE",
                manifest=manifest,
            )
            ctx.session.add(asset)
            return asset

        add_asset("PROXY", proxy_path, "proxy.mp4")

        segments = sorted(p.name for p in hls_dir.glob("seg_*.ts"))
        for seg_name in segments:
            ctx.s3.upload_file(str(hls_dir / seg_name), key(f"stream/{seg_name}"))
        ctx.s3.upload_file(str(master_path), key("stream/master.m3u8"))
        master_bytes = master_path.read_bytes()
        import hashlib as _hl

        ctx.session.add(
            MediaAsset(
                video_id=vid,
                class_="STREAM",
                media_version=MEDIA_VERSION,
                object_key=key("stream/master.m3u8"),
                content_hash=_hl.sha256(master_bytes).hexdigest(),
                byte_size=len(master_bytes),
                status="ACTIVE",
                manifest={"segments": segments, "hls_time": 6, "playlist_type": "vod"},
            )
        )

        n_thumbs = 0
        for thumb in sorted(thumb_dir.glob("f_*.jpg")):
            seq = int(thumb.stem.split("_")[1])  # 1-based ordinal
            ts_ms = (seq - 1) * 5000  # fps=1/5 => frame k at ~k*5000 ms
            rel = f"thumbs/{ts_ms}.jpg"
            ctx.s3.upload_file(str(thumb), key(rel))
            ctx.session.add(
                MediaAsset(
                    video_id=vid,
                    class_="THUMBNAIL",
                    media_version=MEDIA_VERSION,
                    object_key=key(rel),
                    content_hash=file_sha256(str(thumb)),
                    byte_size=thumb.stat().st_size,
                    status="ACTIVE",
                )
            )
            n_thumbs += 1

        ctx.session.flush()
        return StageResult(
            artifact_name="normalize.json",
            artifact_json={
                "stage": self.stage,
                "stage_version": self.stage_version,
                "video_id": vid,
                "media_version": MEDIA_VERSION,
                "outputs": {
                    "proxy": f"{base}/proxy.mp4",
                    "stream": f"{base}/stream/master.m3u8",
                    "segments": len(segments),
                    "thumbs": n_thumbs,
                },
                "rotation_applied": True,
            },
            metrics={
                "proxy_bytes": proxy_path.stat().st_size,
                "n_segments": len(segments),
                "n_thumbs": n_thumbs,
            },
        )
