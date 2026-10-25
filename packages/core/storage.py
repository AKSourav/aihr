import json
from typing import Any

import boto3

from core.settings import get_settings


class Storage:
    def __init__(self) -> None:
        settings = get_settings()
        self._bucket = settings.s3_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
        )

    def upload_json(self, key: str, data: Any) -> str:
        body = json.dumps(data, default=str).encode("utf-8")
        self._client.put_object(Bucket=self._bucket, Key=key, Body=body, ContentType="application/json")
        return f"s3://{self._bucket}/{key}"

    def download_json(self, key: str) -> Any:
        obj = self._client.get_object(Bucket=self._bucket, Key=key)
        return json.loads(obj["Body"].read())
