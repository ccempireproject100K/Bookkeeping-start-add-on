"""Offsite backup uploads (S3-compatible). Configuration from the environment:

  S3_BACKUP_BUCKET, S3_BACKUP_PREFIX (default "bookkeeping/"),
  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION (default us-east-1),
  S3_ENDPOINT_URL (optional — for MinIO/R2/etc.)

When S3_BACKUP_BUCKET is unset, upload is a graceful no-op returning
("skipped", reason). Only encrypted (.enc) payloads are ever uploaded —
plaintext exports are refused.
"""
import logging
import os

log = logging.getLogger(__name__)


def _config():
    bucket = os.environ.get("S3_BACKUP_BUCKET", "").strip()
    if not bucket:
        return None
    return {
        "bucket": bucket,
        "prefix": os.environ.get("S3_BACKUP_PREFIX", "bookkeeping/"),
        "region": os.environ.get("AWS_REGION", "us-east-1"),
        "endpoint": os.environ.get("S3_ENDPOINT_URL") or None,
    }


def upload_encrypted_backup(data: bytes, filename: str) -> tuple[str, str | None]:
    """Returns (status, error). status is 'sent' or 'skipped'."""
    cfg = _config()
    if not cfg:
        return ("skipped", "S3_BACKUP_BUCKET not configured")
    if not filename.endswith(".enc"):
        return ("skipped", "refusing to upload a plaintext backup offsite")
    try:
        import boto3
        kwargs = {"region_name": cfg["region"]}
        if cfg["endpoint"]:
            kwargs["endpoint_url"] = cfg["endpoint"]
        s3 = boto3.client("s3", **kwargs)
        key = f"{cfg['prefix']}{filename}"
        s3.put_object(Bucket=cfg["bucket"], Key=key, Body=data,
                       ServerSideEncryption="AES256",
                       ContentType="application/octet-stream")
        return ("sent", None)
    except Exception as e:
        log.warning("offsite backup upload failed: %s", e)
        return ("skipped", str(e))
