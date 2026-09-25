"""S3 helpers — the durable store for uploaded documents.

The EC2 disk is only a cache; every uploaded/saved document lives here first.
"""

import logging

import boto3
from botocore.config import Config

from config import (
    AWS_ACCESS_KEY_ID,
    AWS_REGION,
    AWS_SECRET_ACCESS_KEY,
    S3_BUCKET,
)

logger = logging.getLogger("s3")

_client = None


def get_client():
    """Lazily build a boto3 S3 client (so a missing bucket only fails on use)."""
    global _client
    if _client is None:
        if not S3_BUCKET:
            raise RuntimeError("S3_BUCKET is not set (add it to api/.env)")
        _client = boto3.client(
            "s3",
            region_name=AWS_REGION,
            aws_access_key_id=AWS_ACCESS_KEY_ID or None,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY or None,
            config=Config(signature_version="s3v4"),
        )
    return _client


def document_key(chat_id: str, document_id: str, filename: str) -> str:
    """Canonical key layout: chats/{chat_id}/{document_id}/{filename}."""
    return f"chats/{chat_id}/{document_id}/{filename}"


def upload_bytes(key: str, data: bytes, content_type: str | None = None) -> str:
    kwargs = {"Bucket": S3_BUCKET, "Key": key, "Body": data}
    if content_type:
        kwargs["ContentType"] = content_type
    get_client().put_object(**kwargs)
    logger.info("S3 upload | bucket=%s key=%s bytes=%d", S3_BUCKET, key, len(data))
    return key


def download_bytes(key: str) -> bytes:
    obj = get_client().get_object(Bucket=S3_BUCKET, Key=key)
    data = obj["Body"].read()
    logger.info("S3 download | key=%s bytes=%d", key, len(data))
    return data


def delete_object(key: str) -> None:
    get_client().delete_object(Bucket=S3_BUCKET, Key=key)
    logger.info("S3 delete | key=%s", key)


def delete_prefix(prefix: str) -> int:
    """Delete every object under a prefix (e.g. all of a chat's files)."""
    client = get_client()
    paginator = client.get_paginator("list_objects_v2")
    deleted = 0
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if keys:
            client.delete_objects(Bucket=S3_BUCKET, Delete={"Objects": keys})
            deleted += len(keys)
    if deleted:
        logger.info("S3 delete prefix | %s (%d objects)", prefix, deleted)
    return deleted
