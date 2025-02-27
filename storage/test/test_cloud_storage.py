import os
import time

from io import BytesIO

import pytest

import botocore.exceptions
import boto3

from moto import mock_s3

from storage import S3Storage, StorageContext
from storage.cloud import _CloudStorage, _PartUploadMetadata
from storage.cloud import _CHUNKS_KEY
from storage.cloud import _build_endpoint_url

from datetime import timedelta

_TEST_CONTENT = os.urandom(1024)
_TEST_BUCKET = "somebucket"
_TEST_USER = "someuser"
_TEST_PASSWORD = "somepassword"
_TEST_PATH = "some/cool/path"
_TEST_UPLOADS_PATH = "uploads/ee160658-9444-4950-8ec6-30faab40529c"
_TEST_CONTEXT = StorageContext("nyc", None, None, None)


@pytest.fixture(scope="function")
def storage_engine():
    with mock_s3():
        # Create a test bucket and put some test content.
        boto3.client("s3").create_bucket(Bucket=_TEST_BUCKET)
        engine = S3Storage(_TEST_CONTEXT, "some/path", _TEST_BUCKET, _TEST_USER, _TEST_PASSWORD)
        engine.put_content(_TEST_PATH, _TEST_CONTENT)

        yield engine


@pytest.mark.parametrize(
    "hostname, port, is_secure, expected",
    [
        pytest.param("somehost", None, False, "http://somehost"),
        pytest.param("somehost", 8080, False, "http://somehost:8080"),
        pytest.param("somehost", 8080, True, "https://somehost:8080"),
        pytest.param("https://somehost.withscheme", None, False, "https://somehost.withscheme"),
        pytest.param("http://somehost.withscheme", None, True, "http://somehost.withscheme"),
        pytest.param("somehost.withport:8080", 9090, True, "https://somehost.withport:8080"),
    ],
)
def test_build_endpoint_url(hostname, port, is_secure, expected):
    assert _build_endpoint_url(hostname, port, is_secure) == expected


def test_basicop(storage_engine):
    # Ensure the content exists.
    assert storage_engine.exists(_TEST_PATH)

    # Verify it can be retrieved.
    assert storage_engine.get_content(_TEST_PATH) == _TEST_CONTENT

    # Retrieve a checksum for the content.
    storage_engine.get_checksum(_TEST_PATH)

    # Remove the file.
    storage_engine.remove(_TEST_PATH)

    # Ensure it no longer exists.
    with pytest.raises(IOError):
        storage_engine.get_content(_TEST_PATH)

    with pytest.raises(IOError):
        storage_engine.get_checksum(_TEST_PATH)

    assert not storage_engine.exists(_TEST_PATH)


def test_storage_setup(storage_engine):
    storage_engine.setup()


def test_remove_dir(storage_engine):
    # Ensure the content exists.
    assert storage_engine.exists(_TEST_PATH)

    # Verify it can be retrieved.
    assert storage_engine.get_content(_TEST_PATH) == _TEST_CONTENT

    # Retrieve a checksum for the content.
    storage_engine.get_checksum(_TEST_PATH)

    # Remove the "directory".
    storage_engine.remove(_TEST_PATH.split("/")[0])

    assert not storage_engine.exists(_TEST_PATH)


@pytest.mark.parametrize(
    "bucket, username, password",
    [
        pytest.param(_TEST_BUCKET, _TEST_USER, _TEST_PASSWORD, id="same credentials"),
        pytest.param("another_bucket", "blech", "password", id="different credentials"),
    ],
)
def test_copy(bucket, username, password, storage_engine):
    # Copy the content to another engine.
    another_engine = S3Storage(
        _TEST_CONTEXT, "another/path", _TEST_BUCKET, _TEST_USER, _TEST_PASSWORD
    )
    boto3.client("s3").create_bucket(Bucket="another_bucket")
    storage_engine.copy_to(another_engine, _TEST_PATH)

    # Verify it can be retrieved.
    assert another_engine.get_content(_TEST_PATH) == _TEST_CONTENT


def test_copy_with_error(storage_engine):
    another_engine = S3Storage(_TEST_CONTEXT, "another/path", "anotherbucket", "foo", "bar")

    with pytest.raises(IOError):
        storage_engine.copy_to(another_engine, _TEST_PATH)


def test_stream_read(storage_engine):
    # Read the streaming content.
    data = b"".join(storage_engine.stream_read(_TEST_PATH))
    assert data == _TEST_CONTENT


def test_stream_read_file(storage_engine):
    with storage_engine.stream_read_file(_TEST_PATH) as f:
        assert f.read() == _TEST_CONTENT


def test_stream_write(storage_engine):
    new_data = os.urandom(4096)
    storage_engine.stream_write(_TEST_PATH, BytesIO(new_data), content_type="Cool/Type")
    assert storage_engine.get_content(_TEST_PATH) == new_data


def test_stream_write_error():
    with mock_s3():
        # Create an engine but not the bucket.
        engine = S3Storage(_TEST_CONTEXT, "some/path", _TEST_BUCKET, _TEST_USER, _TEST_PASSWORD)

        # Attempt to write to the uncreated bucket, which should raise an error.
        with pytest.raises(IOError):
            engine.stream_write(_TEST_PATH, BytesIO(b"hello world"), content_type="Cool/Type")

        with pytest.raises(botocore.exceptions.ClientError) as excinfo:
            engine.exists(_TEST_PATH)
            assert s3r.value.response["Error"]["Code"] == "NoSuchBucket"


@pytest.mark.parametrize(
    "chunk_count",
    [
        0,
        1,
        2,
        50,
    ],
)
@pytest.mark.parametrize("force_client_side", [False, True])
def test_chunk_upload(storage_engine, chunk_count, force_client_side):
    if chunk_count == 0 and force_client_side:
        return

    upload_id, metadata = storage_engine.initiate_chunked_upload()
    final_data = b""

    for index in range(0, chunk_count):
        chunk_data = os.urandom(1024)
        final_data = final_data + chunk_data
        bytes_written, new_metadata, error = storage_engine.stream_upload_chunk(
            upload_id, 0, len(chunk_data), BytesIO(chunk_data), metadata
        )
        metadata = new_metadata

        assert bytes_written == len(chunk_data)
        assert error is None
        assert len(metadata[_CHUNKS_KEY]) == index + 1

    # Complete the chunked upload.
    storage_engine.complete_chunked_upload(
        upload_id, "some/chunked/path", metadata, force_client_side=force_client_side
    )

    # Ensure the file contents are valid.
    assert storage_engine.get_content("some/chunked/path") == final_data


@pytest.mark.parametrize(
    "chunk_count",
    [
        0,
        1,
        50,
    ],
)
def test_cancel_chunked_upload(storage_engine, chunk_count):
    upload_id, metadata = storage_engine.initiate_chunked_upload()

    for _ in range(0, chunk_count):
        chunk_data = os.urandom(1024)
        _, new_metadata, _ = storage_engine.stream_upload_chunk(
            upload_id, 0, len(chunk_data), BytesIO(chunk_data), metadata
        )
        metadata = new_metadata

    # Cancel the upload.
    storage_engine.cancel_chunked_upload(upload_id, metadata)

    # Ensure all chunks were deleted.
    for chunk in metadata[_CHUNKS_KEY]:
        assert not storage_engine.exists(chunk.path)


def test_large_chunks_upload(storage_engine):
    # Make the max chunk size much smaller for testing.
    storage_engine.maximum_chunk_size = storage_engine.minimum_chunk_size * 2

    upload_id, metadata = storage_engine.initiate_chunked_upload()

    # Write a "super large" chunk, to ensure that it is broken into smaller chunks.
    chunk_data = os.urandom(int(storage_engine.maximum_chunk_size * 2.5))
    bytes_written, new_metadata, _ = storage_engine.stream_upload_chunk(
        upload_id, 0, -1, BytesIO(chunk_data), metadata
    )
    assert len(chunk_data) == bytes_written

    # Complete the chunked upload.
    storage_engine.complete_chunked_upload(upload_id, "some/chunked/path", new_metadata)

    # Ensure the file contents are valid.
    assert len(chunk_data) == len(storage_engine.get_content("some/chunked/path"))
    assert storage_engine.get_content("some/chunked/path") == chunk_data


def test_large_chunks_with_ragged_edge(storage_engine):
    # Make the max chunk size much smaller for testing and force it to have a ragged edge.
    storage_engine.maximum_chunk_size = storage_engine.minimum_chunk_size * 2 + 10

    upload_id, metadata = storage_engine.initiate_chunked_upload()

    # Write a few "super large" chunks, to ensure that it is broken into smaller chunks.
    all_data = b""
    for _ in range(0, 2):
        chunk_data = os.urandom(int(storage_engine.maximum_chunk_size) + 20)
        bytes_written, new_metadata, _ = storage_engine.stream_upload_chunk(
            upload_id, 0, -1, BytesIO(chunk_data), metadata
        )
        assert len(chunk_data) == bytes_written
        all_data = all_data + chunk_data
        metadata = new_metadata

    # Complete the chunked upload.
    storage_engine.complete_chunked_upload(upload_id, "some/chunked/path", new_metadata)

    # Ensure the file contents are valid.
    assert len(all_data) == len(storage_engine.get_content("some/chunked/path"))
    assert storage_engine.get_content("some/chunked/path") == all_data


@pytest.mark.parametrize(
    "max_size, parts",
    [
        (
            50,
            [
                _PartUploadMetadata("foo", 0, 50),
                _PartUploadMetadata("foo", 50, 50),
            ],
        ),
        (
            40,
            [
                _PartUploadMetadata("foo", 0, 25),
                _PartUploadMetadata("foo", 25, 25),
                _PartUploadMetadata("foo", 50, 25),
                _PartUploadMetadata("foo", 75, 25),
            ],
        ),
        (
            51,
            [
                _PartUploadMetadata("foo", 0, 50),
                _PartUploadMetadata("foo", 50, 50),
            ],
        ),
        (
            49,
            [
                _PartUploadMetadata("foo", 0, 25),
                _PartUploadMetadata("foo", 25, 25),
                _PartUploadMetadata("foo", 50, 25),
                _PartUploadMetadata("foo", 75, 25),
            ],
        ),
        (
            99,
            [
                _PartUploadMetadata("foo", 0, 50),
                _PartUploadMetadata("foo", 50, 50),
            ],
        ),
        (
            100,
            [
                _PartUploadMetadata("foo", 0, 100),
            ],
        ),
    ],
)
def test_rechunked(max_size, parts):
    chunk = _PartUploadMetadata("foo", 0, 100)
    rechunked = list(_CloudStorage._rechunk(chunk, max_size))
    assert len(rechunked) == len(parts)
    for index, chunk in enumerate(rechunked):
        assert chunk == parts[index]


@pytest.mark.parametrize("path", ["/", _TEST_PATH])
def test_clean_partial_uploads(storage_engine, path):

    # Setup root path and add come content to _root_path/uploads
    storage_engine._root_path = path
    storage_engine.put_content(_TEST_UPLOADS_PATH, _TEST_CONTENT)
    assert storage_engine.exists(_TEST_UPLOADS_PATH)
    assert storage_engine.get_content(_TEST_UPLOADS_PATH) == _TEST_CONTENT

    # Test ensure fresh blobs are not deleted
    storage_engine.clean_partial_uploads(timedelta(days=2))
    assert storage_engine.exists(_TEST_UPLOADS_PATH)
    assert storage_engine.get_content(_TEST_UPLOADS_PATH) == _TEST_CONTENT

    # Test deletion of stale blobs
    time.sleep(1)
    storage_engine.clean_partial_uploads(timedelta(seconds=0))
    assert not storage_engine.exists(_TEST_UPLOADS_PATH)

    # Test if uploads folder does not exist
    storage_engine.remove("uploads")
    assert not storage_engine.exists("uploads")
    storage_engine.clean_partial_uploads(timedelta(seconds=0))
