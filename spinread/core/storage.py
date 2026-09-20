"""Object storage: ObjectStore protocol + boto3 S3 implementation.

Key layout follows HLD §6.3. The bucket is bootstrapped at startup (created
when missing, CORS configured for browser direct uploads from the dev
frontend on :5173).
"""

from __future__ import annotations

import hashlib
import logging
from typing import BinaryIO, Protocol

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

from spinread.config import Settings, get_settings

log = logging.getLogger(__name__)

CORS_RULES = {
    "CORSRules": [
        {
            "AllowedOrigins": ["http://localhost:5173", "http://127.0.0.1:5173"],
            "AllowedMethods": ["GET", "PUT", "HEAD"],
            "AllowedHeaders": ["*"],
            "ExposeHeaders": ["ETag"],
            "MaxAgeSeconds": 3600,
        }
    ]
}


# --- key layout (HLD §6.3) ---------------------------------------------------

def original_key(user_id: str, video_id: str, upload_id: str, filename: str) -> str:
    safe = filename.replace("/", "_")
    return f"users/{user_id}/videos/{video_id}/original/{upload_id}/{safe}"


def derived_key(user_id: str, video_id: str, media_version: int, rel: str) -> str:
    return f"users/{user_id}/videos/{video_id}/derived/{media_version}/{rel}"


def analysis_key(
    user_id: str, video_id: str, pipeline_run_id: str, stage: str, name: str
) -> str:
    return f"users/{user_id}/videos/{video_id}/analysis/{pipeline_run_id}/{stage}/{name}"


class ObjectStore(Protocol):
    bucket: str

    def put_bytes(self, key: str, data: bytes, content_type: str = "") -> str:
        """Store bytes; return the sha256 hex digest."""
        ...

    def get_bytes(self, key: str) -> bytes: ...

    def head(self, key: str) -> dict | None: ...

    def copy(self, src_key: str, dst_key: str) -> None: ...

    def presign_put_part(self, key: str, upload_id: str, part_number: int, expires_in: int) -> str: ...

    def create_multipart_upload(self, key: str, content_type: str) -> str: ...

    def complete_multipart_upload(self, key: str, upload_id: str, parts: list[dict]) -> None: ...

    def abort_multipart_upload(self, key: str, upload_id: str) -> None: ...


class S3ObjectStore:
    def __init__(self, settings: Settings | None = None):
        s = settings or get_settings()
        self.bucket = s.s3_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=s.s3_endpoint,
            aws_access_key_id=s.s3_access_key,
            aws_secret_access_key=s.s3_secret_key,
            region_name=s.s3_region,
            config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    # -- bootstrap -----------------------------------------------------------
    def bootstrap(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError:
            self.client.create_bucket(Bucket=self.bucket)
        try:
            self.client.put_bucket_cors(Bucket=self.bucket, CORSConfiguration=CORS_RULES)
        except ClientError as exc:
            # Recent MinIO does not implement PutBucketCors (501); CORS there is
            # configured server-side via MINIO_API_CORS_ALLOW_ORIGIN (see compose).
            log.warning("PutBucketCors unsupported, skipping (%s)", exc.response.get("Error", {}).get("Code"))

    # -- objects --------------------------------------------------------------
    def put_bytes(self, key: str, data: bytes, content_type: str = "") -> str:
        digest = hashlib.sha256(data).hexdigest()
        extra = {"ContentType": content_type} if content_type else {}
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, **extra)
        return digest

    def put_fileobj(self, key: str, fh: BinaryIO, content_type: str = "") -> None:
        extra = {"ContentType": content_type} if content_type else {}
        self.client.put_object(Bucket=self.bucket, Key=key, Body=fh, **extra)

    def get_bytes(self, key: str) -> bytes:
        resp = self.client.get_object(Bucket=self.bucket, Key=key)
        return resp["Body"].read()

    def get_range(self, key: str, start: int, end: int | None) -> tuple[bytes, str]:
        """Fetch a byte range; returns (body, Content-Range header value)."""
        range_header = f"bytes={start}-{'' if end is None else end}"
        resp = self.client.get_object(Bucket=self.bucket, Key=key, Range=range_header)
        body = resp["Body"].read()
        content_range = resp.get("ContentRange", "")
        return body, content_range

    def download_file(self, key: str, dest: str) -> None:
        self.client.download_file(self.bucket, key, dest)

    def upload_file(self, src: str, key: str, content_type: str = "") -> None:
        extra = {"ContentType": content_type} if content_type else {}
        self.client.upload_file(src, self.bucket, key, ExtraArgs=extra or None)

    def head(self, key: str) -> dict | None:
        try:
            return self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def copy(self, src_key: str, dst_key: str) -> None:
        self.client.copy_object(
            Bucket=self.bucket,
            Key=dst_key,
            CopySource={"Bucket": self.bucket, "Key": src_key},
        )

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    # -- multipart -------------------------------------------------------------
    def create_multipart_upload(self, key: str, content_type: str) -> str:
        resp = self.client.create_multipart_upload(
            Bucket=self.bucket, Key=key, ContentType=content_type
        )
        return resp["UploadId"]

    def presign_put_part(self, key: str, upload_id: str, part_number: int, expires_in: int) -> str:
        return self.client.generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "UploadId": upload_id,
                "PartNumber": part_number,
            },
            ExpiresIn=expires_in,
        )

    def complete_multipart_upload(self, key: str, upload_id: str, parts: list[dict]) -> None:
        self.client.complete_multipart_upload(
            Bucket=self.bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )

    def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        self.client.abort_multipart_upload(
            Bucket=self.bucket, Key=key, UploadId=upload_id
        )


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
