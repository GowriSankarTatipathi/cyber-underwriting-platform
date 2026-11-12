"""Private storage. All reads go through an authorized Django view."""
import base64
import hashlib
import io
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class StorageUnavailable(Exception):
    pass


def s3_client():
    try:
        return boto3.client("s3", region_name=settings.EVIDENCE_S3_REGION,
                            endpoint_url=settings.EVIDENCE_S3_ENDPOINT_URL,
                            config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=20,
                                          retries={"max_attempts": 2}))
    except BotoCoreError as error:
        raise StorageUnavailable("S3 credentials or connection settings are unavailable.") from error


def check_s3(client=None, bucket=None):
    client = client or s3_client()
    bucket = bucket or settings.EVIDENCE_S3_BUCKET
    if not bucket:
        raise StorageUnavailable("Set EVIDENCE_S3_BUCKET before using S3.")
    try:
        versioning = client.get_bucket_versioning(Bucket=bucket)
        block = client.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"]
        encryption = client.get_bucket_encryption(Bucket=bucket)
        if versioning.get("Status") != "Enabled":
            raise StorageUnavailable("Evidence bucket versioning must be enabled.")
        if not all(block.get(name) for name in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")):
            raise StorageUnavailable("All S3 Block Public Access settings must be enabled.")
        if not encryption.get("ServerSideEncryptionConfiguration", {}).get("Rules"):
            raise StorageUnavailable("Evidence bucket encryption must be enabled.")
    except (BotoCoreError, ClientError) as error:
        raise StorageUnavailable("Cannot verify the private S3 bucket configuration.") from error


def local_path(key):
    root = Path(settings.EVIDENCE_LOCAL_ROOT).resolve()
    path = (root / key).resolve()
    if not path.is_relative_to(root) or path == root:
        raise StorageUnavailable("Invalid private storage reference.")
    return path


def put_private(key, data, content_type, sha256):
    backend = settings.EVIDENCE_STORAGE_BACKEND
    if backend == "local":
        if not settings.DEBUG:
            raise StorageUnavailable("Local evidence storage is restricted to development.")
        path = local_path(key)
        created = False
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Exclusive creation: a version can never replace an existing file.
            with path.open("xb") as stream:
                created = True
                stream.write(data)
            path.chmod(0o600)
        except OSError as error:
            if created:
                path.unlink(missing_ok=True)
            raise StorageUnavailable("Private evidence storage is unavailable.") from error
        return {"storage_backend": "local", "storage_key": key, "storage_bucket": "", "object_version": ""}
    if backend != "s3":
        raise StorageUnavailable("Unknown evidence storage backend.")
    client = s3_client()
    check_s3(client)
    extra = {"ServerSideEncryption": "AES256"}
    if settings.EVIDENCE_S3_KMS_KEY_ID:
        extra = {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": settings.EVIDENCE_S3_KMS_KEY_ID}
    try:
        response = client.put_object(Bucket=settings.EVIDENCE_S3_BUCKET, Key=key, Body=data,
            ContentType=content_type, ContentDisposition="attachment", IfNoneMatch="*",
            Metadata={"sha256": sha256}, ChecksumSHA256=base64.b64encode(bytes.fromhex(sha256)).decode(), **extra)
        version = response.get("VersionId")
        if not version or version == "null":
            raise StorageUnavailable("S3 did not return a pinned object version.")
        pointer = {"storage_backend": "s3", "storage_key": key,
                   "storage_bucket": settings.EVIDENCE_S3_BUCKET, "object_version": version}
        head = client.head_object(Bucket=pointer["storage_bucket"], Key=key, VersionId=version)
        if head["ContentLength"] != len(data) or head.get("Metadata", {}).get("sha256") != sha256:
            delete_private(pointer)
            raise StorageUnavailable("The stored object's integrity check failed.")
        return pointer
    except (BotoCoreError, ClientError) as error:
        raise StorageUnavailable("S3 could not store this evidence. Please retry.") from error


def read_private(version):
    try:
        if version.storage_backend == "local":
            if not settings.DEBUG:
                raise StorageUnavailable("Local evidence files are restricted to development.")
            with local_path(version.storage_key).open("rb") as stream:
                data = stream.read(settings.EVIDENCE_MAX_BYTES + 1)
        elif version.storage_backend == "s3":
            response = s3_client().get_object(Bucket=version.storage_bucket, Key=version.storage_key,
                                               VersionId=version.object_version)
            try:
                data = response["Body"].read(settings.EVIDENCE_MAX_BYTES + 1)
            finally:
                response["Body"].close()
        else:
            raise StorageUnavailable("Unknown storage reference.")
    except (OSError, BotoCoreError, ClientError) as error:
        raise StorageUnavailable("This evidence file is temporarily unavailable.") from error
    if len(data) != version.byte_size or hashlib.sha256(data).hexdigest() != version.sha256:
        raise StorageUnavailable("Evidence integrity check failed. Contact the case reviewer.")
    return data


def delete_private(pointer):
    """Only used to clean up a failed intake before its DB transaction commits."""
    if pointer["storage_backend"] == "local":
        local_path(pointer["storage_key"]).unlink(missing_ok=True)
    else:
        s3_client().delete_object(Bucket=pointer["storage_bucket"], Key=pointer["storage_key"], VersionId=pointer["object_version"])
