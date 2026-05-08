"""GCS download/upload helpers — input fetch + output store.

Real `google.cloud.storage.Client` is injected via Protocol so tests can
swap in fake-gcs-server-backed or in-memory fakes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .failure_classification import GcsTransientError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GcsObjectRef:
    bucket: str
    name: str

    @property
    def uri(self) -> str:
        return f"gs://{self.bucket}/{self.name}"

    @classmethod
    def from_uri(cls, uri: str) -> "GcsObjectRef":
        if not uri.startswith("gs://"):
            raise ValueError(f"not a gs:// URI: {uri!r}")
        rest = uri[len("gs://") :]
        if "/" not in rest:
            raise ValueError(f"missing object name in URI: {uri!r}")
        bucket, name = rest.split("/", 1)
        if not bucket or not name:
            raise ValueError(f"empty bucket or object name: {uri!r}")
        return cls(bucket=bucket, name=name)


@runtime_checkable
class GcsBlobLike(Protocol):
    def download_as_bytes(self) -> bytes: ...
    def upload_from_string(
        self, data: bytes, content_type: str | None = ...
    ) -> None: ...


@runtime_checkable
class GcsBucketLike(Protocol):
    def blob(self, name: str) -> GcsBlobLike: ...


@runtime_checkable
class GcsClientLike(Protocol):
    def bucket(self, bucket_name: str) -> GcsBucketLike: ...


class GcsClient:
    """Thin wrapper over google-cloud-storage with retry classification.

    Network errors and 5xx are mapped to `GcsTransientError` so failure
    classification (failure_classification.py) can route them to NACK.
    """

    def __init__(self, client: GcsClientLike) -> None:
        self._client = client

    def download(self, uri: str) -> bytes:
        ref = GcsObjectRef.from_uri(uri)
        try:
            return self._client.bucket(ref.bucket).blob(ref.name).download_as_bytes()
        except Exception as exc:  # noqa: BLE001
            logger.warning("gcs_download_failed", extra={"uri": uri, "error": str(exc)})
            raise GcsTransientError(f"download {uri} failed: {exc}") from exc

    def upload(self, uri: str, data: bytes, content_type: str | None = None) -> None:
        ref = GcsObjectRef.from_uri(uri)
        try:
            self._client.bucket(ref.bucket).blob(ref.name).upload_from_string(
                data, content_type=content_type
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("gcs_upload_failed", extra={"uri": uri, "error": str(exc)})
            raise GcsTransientError(f"upload {uri} failed: {exc}") from exc

    @staticmethod
    def output_uri(prefix: str, index: int, ext: str = "png") -> str:
        if not prefix.endswith("/"):
            raise ValueError("output prefix must end in '/'")
        return f"{prefix}{index}.{ext}"
