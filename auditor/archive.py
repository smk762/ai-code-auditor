from __future__ import annotations

from pathlib import Path

import boto3
from botocore.client import BaseClient

from auditor.settings import get_settings


def _s3_client() -> BaseClient | None:
    settings = get_settings()
    if not settings.s3_endpoint_url or not settings.s3_bucket:
        return None
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
        region_name=settings.s3_region,
    )


def archive_reports(output_dir: str, run_id: str) -> list[str]:
    client = _s3_client()
    if client is None:
        return []
    settings = get_settings()
    uploaded: list[str] = []
    for path in Path(output_dir).glob("*.md"):
        key = f"ai-audit/{run_id}/{path.name}"
        client.upload_file(str(path), settings.s3_bucket, key)
        uploaded.append(key)
    return uploaded
