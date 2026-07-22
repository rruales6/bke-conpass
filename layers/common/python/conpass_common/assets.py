"""Program image assets on S3 (public-read).

Merchants personalize a program's icon (512x512 PNG) and background (1032x336 JPG).
The browser uploads the file directly to S3 via a short-lived presigned PUT URL (no
image bytes flow through the Lambda), then persists the returned storage key through
PATCH /programs. The public URL is used on the in-app card preview and on the Google
Wallet pass (`logo` / `heroImage`), so the bucket must serve the objects publicly.

boto3 ships in the AWS Lambda runtime; it is imported lazily so the shared layer stays
importable (and unit-testable) in environments without it.
"""
from __future__ import annotations

import uuid

from .config import settings

# Presigned PUT URLs are short-lived — the browser uses them immediately after the
# owner picks a file.
UPLOAD_URL_TTL_SECONDS = 300

_EXT = {"image/png": "png", "image/jpeg": "jpg"}


def public_asset_url(storage_key: str | None) -> str | None:
    """Resolve a stored S3 key to its public HTTPS URL (None if unset/unconfigured)."""
    if not storage_key:
        return None
    base = settings.program_assets_base_url
    return f"{base}/{storage_key}" if base else None


def presign_upload(*, program_id: str, kind: str, content_type: str) -> dict:
    """Return a presigned S3 PUT target for a program asset.

    Keyed `programs/<programId>/<kind>-<uuid>.<ext>` — the uuid means a re-upload never
    collides with (or requires deleting) the previous asset.
    """
    bucket = settings.program_assets_bucket
    if not bucket:
        raise RuntimeError("program assets bucket not configured")

    ext = _EXT.get(content_type)
    if ext is None:
        raise ValueError(f"unsupported content type: {content_type}")

    storage_key = f"programs/{program_id}/{kind}-{uuid.uuid4().hex}.{ext}"

    import boto3  # lazy: only present in the Lambda runtime

    client = boto3.client("s3", region_name=settings.aws_region)
    upload_url = client.generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": storage_key, "ContentType": content_type},
        ExpiresIn=UPLOAD_URL_TTL_SECONDS,
    )
    return {
        "uploadUrl": upload_url,
        "storageKey": storage_key,
        "publicUrl": public_asset_url(storage_key),
    }
