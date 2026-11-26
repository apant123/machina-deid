"""Shared configuration: R2 source client, AWS names, local paths.

Secrets come from the environment (or a local, owner-only `.env` file),
never from source code.
"""
from __future__ import annotations

import os
from pathlib import Path

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parent.parent
_DOTENV = ROOT / ".env"


def _load_dotenv() -> None:
    if not _DOTENV.exists():
        return
    for line in _DOTENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()


def env(name: str, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if val is None:
        raise RuntimeError(f"Missing required environment variable {name} (set it or add it to .env)")
    return val


R2_BUCKET = env("R2_BUCKET", "machina-deid-challenge-v1")
R2_PREFIX = env("R2_PREFIX", "sources/")

AWS_REGION = env("AWS_REGION", "us-east-2")
ACCOUNT_SUFFIX = "arav-065371277807"
STAGING_BUCKET = env("STAGING_BUCKET", f"machina-deid-raw-{ACCOUNT_SUFFIX}")
OUTPUT_BUCKET = env("OUTPUT_BUCKET", f"machina-deid-output-{ACCOUNT_SUFFIX}")
CODE_BUCKET = env("CODE_BUCKET", f"machina-deid-code-{ACCOUNT_SUFFIX}")

DATA_DIR = Path(env("DEID_DATA_DIR", str(Path.home() / "data" / "machina")))
RAW_DIR = DATA_DIR / "raw"  # local mirror of R2, laid out as <RAW_DIR>/<key>
MANIFEST_DIR = DATA_DIR / "manifest"


def r2_client():
    return boto3.client(
        "s3",
        endpoint_url=env("R2_ENDPOINT"),
        aws_access_key_id=env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
        config=Config(retries={"max_attempts": 10, "mode": "adaptive"}, max_pool_connections=64),
    )


def s3_client():
    return boto3.client("s3", region_name=AWS_REGION,
                        config=Config(retries={"max_attempts": 10, "mode": "adaptive"}, max_pool_connections=64))
