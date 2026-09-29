"""SQS handoff carries receipt IDs only; message bodies remain in DynamoDB."""

import json
from typing import Any, Protocol


class SQSClient(Protocol):
    def send_message(self, **kwargs: Any) -> dict[str, Any]: ...


class SQSReceiptQueue:
    def __init__(self, client: SQSClient, queue_url: str) -> None:
        if not queue_url:
            raise ValueError("Receipt queue URL is required")
        self._client = client
        self._queue_url = queue_url

    def enqueue(self, business_id: str, provider_id: str) -> None:
        if not business_id or not provider_id:
            raise ValueError("Receipt identity is required")
        result = self._client.send_message(
            QueueUrl=self._queue_url,
            MessageBody=json.dumps({"business_id": business_id, "provider_id": provider_id}),
        )
        if not isinstance(result.get("MessageId"), str) or not result["MessageId"]:
            raise RuntimeError("Receipt queue did not accept the handoff")
