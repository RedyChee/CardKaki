"""Daily SQLite snapshot + optional R2/B2 upload.

Per the README's "Backups & data portability" section. Uses
sqlite3.Connection.backup() rather than file copy so live writes from
the bot don't corrupt the snapshot.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


def snapshot(db_path: Path, out_dir: Path) -> Path:
    """Produce a consistent SQLite snapshot. Returns path to the file."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = out_dir / f"users-{ts}.sqlite"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(out)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return out


def upload_to_r2(local_path: Path, key: str) -> bool:
    """Upload to Cloudflare R2 (S3-compatible). False if creds missing."""
    account = os.environ.get("R2_ACCOUNT_ID")
    bucket = os.environ.get("R2_BUCKET")
    access = os.environ.get("R2_ACCESS_KEY_ID")
    secret = os.environ.get("R2_SECRET_ACCESS_KEY")
    if not (account and bucket and access and secret):
        return False
    import boto3  # local import: only needed when creds are present

    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=access,
        aws_secret_access_key=secret,
    )
    s3.upload_file(str(local_path), bucket, key)
    return True


def run_backup(db_path: Path, out_dir: Path) -> dict:
    """Snapshot + upload + optional cleanup. Safe for a scheduler to call."""
    snap = snapshot(Path(db_path), Path(out_dir))
    key = f"backups/{snap.name}"
    uploaded = upload_to_r2(snap, key)
    if uploaded:
        snap.unlink(missing_ok=True)
    log.info("backup snapshot=%s uploaded=%s", snap.name, uploaded)
    return {"snapshot": snap.name, "uploaded": uploaded}
