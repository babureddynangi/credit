# governance/queue.py
# SQS case event publisher — AWS Free Tier (1M requests/month free)
# Silently skipped when SQS_QUEUE_URL is not configured

import os
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
UTC = timezone.utc


def publish_case_event(case_id: str, status: str, metadata: dict = None):
    """
    Publish a case status event to SQS.
    No-op if SQS_QUEUE_URL is not set (local dev / mock mode).
    """
    queue_url = os.getenv("SQS_QUEUE_URL")
    if not queue_url:
        return

    try:
        import boto3
        sqs = boto3.client("sqs", region_name=os.getenv("AWS_REGION", "us-east-1"))
        message = {
            "case_id": case_id,
            "status": status,
            "timestamp": datetime.now(UTC).isoformat(),
            **(metadata or {}),
        }
        sqs.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(message),
            MessageAttributes={
                "event_type": {
                    "StringValue": "case_status_change",
                    "DataType": "String",
                }
            },
        )
        logger.info(f"Published case event: case_id={case_id} status={status}")
    except Exception as e:
        logger.warning(f"SQS publish failed (non-fatal): {e}")
