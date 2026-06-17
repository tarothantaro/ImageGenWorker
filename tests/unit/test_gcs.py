from __future__ import annotations

import pytest

from imagegen.failure_classification import GcsTransientError
from imagegen.gcs import GcsClient, GcsObjectRef

# --- GcsObjectRef -------------------------------------------------------------


def test_gcs_object_ref_roundtrips_uri() -> None:
    ref = GcsObjectRef.from_uri("gs://bucket-x/path/to/object.jpg")

    assert ref.bucket == "bucket-x"
    assert ref.name == "path/to/object.jpg"
    assert ref.uri == "gs://bucket-x/path/to/object.jpg"


def test_gcs_object_ref_rejects_non_gs_uri() -> None:
    with pytest.raises(ValueError, match="not a gs"):
        GcsObjectRef.from_uri("https://example.com/x")


def test_gcs_object_ref_rejects_uri_without_object() -> None:
    with pytest.raises(ValueError, match="missing object"):
        GcsObjectRef.from_uri("gs://just-a-bucket")


def test_gcs_object_ref_rejects_empty_bucket() -> None:
    with pytest.raises(ValueError, match="empty bucket"):
        GcsObjectRef.from_uri("gs:///object")


def test_gcs_object_ref_rejects_empty_object_name() -> None:
    with pytest.raises(ValueError, match="empty bucket"):
        GcsObjectRef.from_uri("gs://bucket/")


# --- GcsClient ----------------------------------------------------------------


class _FakeBlob:
    def __init__(
        self,
        *,
        payload: bytes = b"",
        upload_raises: BaseException | None = None,
        download_raises: BaseException | None = None,
    ) -> None:
        self._payload = payload
        self._upload_raises = upload_raises
        self._download_raises = download_raises
        self.uploads: list[tuple[bytes, str | None]] = []

    def download_as_bytes(self) -> bytes:
        if self._download_raises is not None:
            raise self._download_raises
        return self._payload

    def upload_from_string(self, data: bytes, content_type: str | None = None) -> None:
        if self._upload_raises is not None:
            raise self._upload_raises
        self.uploads.append((data, content_type))


class _FakeBucket:
    def __init__(self, blob: _FakeBlob) -> None:
        self._blob = blob
        self.requested_blobs: list[str] = []

    def blob(self, name: str) -> _FakeBlob:
        self.requested_blobs.append(name)
        return self._blob


class _FakeStorageClient:
    def __init__(self, bucket: _FakeBucket) -> None:
        self._bucket = bucket
        self.requested_buckets: list[str] = []

    def bucket(self, name: str) -> _FakeBucket:
        self.requested_buckets.append(name)
        return self._bucket


def test_download_returns_bytes_from_correct_bucket_and_object() -> None:
    blob = _FakeBlob(payload=b"hello")
    bucket = _FakeBucket(blob)
    client = GcsClient(_FakeStorageClient(bucket))

    data = client.download("gs://my-bucket/path/to/x.jpg")

    assert data == b"hello"
    assert bucket.requested_blobs == ["path/to/x.jpg"]


def test_download_wraps_underlying_failures_in_gcs_transient_error() -> None:
    blob = _FakeBlob(download_raises=RuntimeError("503"))
    client = GcsClient(_FakeStorageClient(_FakeBucket(blob)))

    with pytest.raises(GcsTransientError, match="download"):
        client.download("gs://b/o.jpg")


def test_upload_writes_bytes_with_content_type() -> None:
    blob = _FakeBlob()
    bucket = _FakeBucket(blob)
    client = GcsClient(_FakeStorageClient(bucket))

    client.upload("gs://b/path/0.png", b"\x89PNG", content_type="image/png")

    assert blob.uploads == [(b"\x89PNG", "image/png")]
    assert bucket.requested_blobs == ["path/0.png"]


def test_upload_wraps_underlying_failures_in_gcs_transient_error() -> None:
    blob = _FakeBlob(upload_raises=RuntimeError("connection reset"))
    client = GcsClient(_FakeStorageClient(_FakeBucket(blob)))

    with pytest.raises(GcsTransientError, match="upload"):
        client.upload("gs://b/o.png", b"data")


def test_upload_passes_content_type_none_by_default() -> None:
    blob = _FakeBlob()
    client = GcsClient(_FakeStorageClient(_FakeBucket(blob)))

    client.upload("gs://b/o.png", b"data")

    assert blob.uploads == [(b"data", None)]


def test_output_uri_builds_per_story_indexed_name() -> None:
    uri = GcsClient.output_uri("b", "uid", "story", index=2, ext="png")

    assert uri == "gs://b/uid/story/outputs/2.png"


def test_input_uri_builds_deterministic_per_story_name() -> None:
    uri = GcsClient.input_uri("b", "uid", "story", position=0)

    assert uri == "gs://b/uid_story_input_0.png"
