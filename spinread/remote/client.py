"""Durable submit/poll/recover; a submission ambiguity never auto-resubmits."""
from __future__ import annotations

from datetime import timedelta, timezone
from pathlib import Path
import tempfile
import time

from sqlalchemy import select

from spinread.core.ids import new_id
from spinread.core.models import Job, RemoteExecution, Video, utcnow
from spinread.remote.protocol import RemoteError, digest, unpack
from spinread.remote.provider import LeaseLost, ModalProvider, RemotePending


def lease(db, job, *, lock=False):
    query = select(Job.id, Job.claimed_by, Job.done_at).where(Job.id == job.id)
    if lock:
        query = query.with_for_update()
    with db.no_autoflush:
        current = db.execute(query).first()
    if not current or current.done_at or current.claimed_by != job.claimed_by:
        raise LeaseLost("JOB_LEASE_LOST")
    return current


def before_commit(db):
    job = db.info.get("dispatcher_job")
    if job is not None:
        lease(db, job, lock=True)


class RemoteClient:
    def __init__(self, settings, s3, *, provider=None, stopping=lambda: False):
        self.settings, self.s3 = settings, s3
        self.provider = provider or ModalProvider(settings.remote_app)
        self.stopping = stopping

    def run(self, db, job, payload, root, *, gpu=False):
        operation = payload["operation"]
        request_hash = digest(payload)
        receipt = db.scalar(select(RemoteExecution).where(RemoteExecution.job_id == job.id,
            RemoteExecution.operation == operation))
        if receipt is None:
            receipt = RemoteExecution(id=new_id("rex"), job_id=job.id, operation=operation,
                request_hash=request_hash, provider=self.settings.remote_provider,
                output_key=f"remote/{job.id}/{new_id('out')}.zip", state="PREPARED",
                deadline_at=utcnow() + timedelta(seconds=self.settings.remote_timeout_seconds))
            db.add(receipt)
            db.commit()  # Also persists RUNNING stage; safe to resume after restart.
        if receipt.request_hash != request_hash or receipt.provider != self.settings.remote_provider:
            raise RemoteError("REMOTE_INPUT_CHANGED")
        identity = {"execution_id": receipt.id, "request_hash": request_hash}
        if receipt.state in {"FAILED", "TIMED_OUT", "CANCELLED"}:
            raise RemoteError(receipt.error_code or receipt.state)
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        if self.settings.remote_provider == "local":
            # Explicit offline test path; never allowed by cloud settings.
            from spinread.remote.executor import execute
            if receipt.state == "PREPARED":
                with tempfile.TemporaryDirectory(prefix="remote-local-", dir=self.settings.tmp_dir) as scratch:
                    source = Path(scratch) / "input"
                    self.s3.download_file(payload["source"]["key"], str(source))
                    bundle = execute({"identity": identity, "payload": payload,
                        "max_bytes": self.settings.remote_max_bundle_bytes}, Path(scratch) / "compute",
                        source_path=source, settings=self.settings)
                    self.s3.upload_file(str(bundle), receipt.output_key)
                    receipt.state = "SUCCEEDED"
                    db.commit()
        elif receipt.state == "PREPARED":
            # Transaction commits BEFORE the external side effect. A crash after
            # this point is explicitly ambiguous, not a license to pay twice.
            receipt.state = "SUBMITTING"
            db.commit()
            expiry = self.settings.remote_timeout_seconds + 1800
            request = {"identity": identity, "payload": payload,
                "max_bytes": self.settings.remote_max_bundle_bytes,
                "source_url": self.s3.client.generate_presigned_url("get_object",
                    Params={"Bucket": self.s3.bucket, "Key": payload["source"]["key"]}, ExpiresIn=expiry),
                "output_url": self.s3.client.generate_presigned_url("put_object",
                    Params={"Bucket": self.s3.bucket, "Key": receipt.output_key}, ExpiresIn=expiry)}
            try:
                receipt.call_id = self.provider.submit(request, gpu=gpu)
            except Exception:
                raise RemotePending("SUBMISSION_UNCERTAIN_CHECK_RECEIPT") from None
            receipt.state = "RUNNING"
            db.commit()
        while True:
            lease(db, job)
            video = db.get(Video, payload["video_id"], populate_existing=True)
            if not video or video.deleted_at:
                self._stop(receipt, "CANCELLED", "VIDEO_UNAVAILABLE")
                db.commit()
                raise RemoteError("VIDEO_UNAVAILABLE")
            if self.stopping():
                db.rollback()
                raise RemotePending("DISPATCHER_STOPPING")
            # An uploaded bundle is a durable receipt, including the crash window
            # after spawn but before its call ID reached the database.
            try:
                head = self.s3.head(receipt.output_key)
            except Exception:
                db.rollback()
                raise RemotePending("REMOTE_STORAGE_UNAVAILABLE") from None
            if head is not None:
                size = int(head.get("ContentLength", -1))
                if not 0 < size <= self.settings.remote_max_bundle_bytes:
                    raise RemoteError("OUTPUT_TOO_LARGE")
                import shutil
                if shutil.disk_usage(root).free < 2 * size + 256 * 1024**2:
                    raise RemoteError("INSUFFICIENT_SCRATCH_SPACE")
                bundle = root / "result.zip"
                # Download bounded by ContentLength in a streaming body; avoid
                # races where a changed object grows beyond the scratch limit.
                self._download_bounded(receipt.output_key, bundle, size)
                result, files = unpack(bundle, root / "files", identity,
                    max_bytes=self.settings.remote_max_bundle_bytes)
                bundle.unlink()
                receipt.state = "SUCCEEDED"
                db.commit()
                return result, files
            deadline = receipt.deadline_at.replace(tzinfo=timezone.utc) if receipt.deadline_at.tzinfo is None else receipt.deadline_at
            if utcnow() > deadline:
                self._stop(receipt, "TIMED_OUT", "REMOTE_DEADLINE_EXCEEDED")
                db.commit()
                raise RemoteError("REMOTE_DEADLINE_EXCEEDED")
            if receipt.call_id:
                try:
                    status = self.provider.poll(receipt.call_id)
                except Exception:
                    db.rollback()
                    raise RemotePending("PROVIDER_POLL_UNAVAILABLE") from None
                if status is not None:
                    if status.get("status") != "SUCCEEDED":
                        receipt.state = "FAILED"
                        receipt.error_code = "REMOTE_COMPUTE_FAILED"
                        db.commit()
                        raise RemoteError(receipt.error_code)
                    # The PUT may have finished between HEAD and poll. Re-check
                    # the durable object on the next iteration, not a false failure.
                    receipt.state = "RESULT_PENDING"
            # No DB transaction held while waiting. Heartbeat uses another connection.
            db.commit()
            time.sleep(self.settings.remote_poll_seconds)

    def _stop(self, receipt, state, code):
        if receipt.call_id:
            try:
                self.provider.cancel(receipt.call_id)
            except Exception:
                # Function also has a hard platform timeout. Retain ambiguity for
                # operator inspection; never start a replacement automatically.
                code += "_CANCEL_UNCONFIRMED"
        receipt.state, receipt.error_code = state, code

    def _download_bounded(self, key, path, size):
        if self.settings.remote_provider == "local":
            self.s3.download_file(key, str(path))
            if path.stat().st_size != size:
                raise RemoteError("OUTPUT_SIZE_MISMATCH")
            return
        body = self.s3.client.get_object(Bucket=self.s3.bucket, Key=key)["Body"]
        try:
            total = 0
            with path.open("wb") as output:
                for chunk in iter(lambda: body.read(1024 * 1024), b""):
                    total += len(chunk)
                    if total > size:
                        raise RemoteError("OUTPUT_SIZE_MISMATCH")
                    output.write(chunk)
            if total != size:
                raise RemoteError("OUTPUT_SIZE_MISMATCH")
        finally:
            body.close()
