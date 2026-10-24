"""S3 access (boto3) and the per-file state marker.

Folder = lifecycle state:  incoming/ -> library/ or rejected/.
`_state/<name>.json` holds retry accounting, so state survives restarts and any S3 tool can read it.
"""
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from .config import Settings

INCOMING, LIBRARY, REJECTED, STATE = "incoming/", "library/", "rejected/", "_state/"


@dataclass
class FileState:
    name: str
    sha256: str = ""
    status: str = "pending"      # pending | failed | indexed | rejected
    attempts: int = 0
    last_error: str = ""
    updated_at: str = ""

    def to_json(self) -> bytes:
        self.updated_at = datetime.now(timezone.utc).isoformat()
        return json.dumps(asdict(self), indent=2).encode()

    @classmethod
    def from_json(cls, raw: bytes) -> "FileState":
        return cls(**json.loads(raw))


class Store:
    def __init__(self, settings: Settings, client=None):
        self.bucket = settings.s3_bucket
        self.s3 = client or boto3.client(
            "s3", endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key, aws_secret_access_key=settings.s3_secret_key,
            region_name="us-east-1", config=Config(s3={"addressing_style": "path"}))

    def list_keys(self, prefix: str) -> list[str]:
        keys = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            keys += [o["Key"] for o in page.get("Contents", [])]
        return keys

    def exists(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def get_bytes(self, key: str) -> bytes:
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def put_bytes(self, key: str, data: bytes) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data)

    def delete(self, key: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=key)

    def move(self, src: str, dst: str) -> None:
        """Server-side copy, then delete. Not atomic: a crash between the two leaves both objects,
        which the pipeline detects (same sha) and finishes by deleting the source."""
        self.s3.copy_object(Bucket=self.bucket, Key=dst, CopySource={"Bucket": self.bucket, "Key": src})
        self.delete(src)

    def read_state(self, name: str) -> "FileState | None":
        try:
            return FileState.from_json(self.get_bytes(f"{STATE}{name}.json"))
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise

    def write_state(self, state: FileState) -> None:
        self.put_bytes(f"{STATE}{state.name}.json", state.to_json())
