"""
Payment Received Lambda: Marks invoice as paid and cancels outstanding reminders.

Designed to be triggered by an EventBridge event from your payment gateway, so
the event carries the payload under `detail`. It also accepts a top-level
`invoice_id` so you can invoke it directly for testing.

Event shape (EventBridge):
    {"detail": {"invoice_id": "abc123"}}
Event shape (direct invoke):
    {"invoice_id": "abc123"}
"""
import json
from datetime import datetime

from billing_config import (
    scheduler, invoice_table,
    REMINDER_GROUP, REMINDER_LABELS
)


def lambda_handler(event, context):
    detail = event.get("detail") or event
    invoice_id = detail["invoice_id"]

    # --- Mark invoice as paid ---
    invoice_table.update_item(
        Key={"invoice_id": invoice_id},
        UpdateExpression="SET #status = :paid, paid_at = :now",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":paid": "paid",
            ":now": datetime.utcnow().isoformat()
        }
    )

    # --- Cancel all outstanding reminder schedules ---
    canceled = []
    for label in REMINDER_LABELS:
        try:
            scheduler.delete_schedule(
                Name=f"remind-{invoice_id}-{label}",
                GroupName=REMINDER_GROUP
            )
            canceled.append(label)
        except scheduler.exceptions.ResourceNotFoundException:
            pass  # Already fired or auto-deleted

    return {
        "message": "Payment recorded",
        "invoice_id": invoice_id,
        "reminders_canceled": canceled,
    }
