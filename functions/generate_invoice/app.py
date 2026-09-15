"""
Generate Invoice Lambda: Creates an invoice and schedules payment reminders.
Triggered by EventBridge Scheduler (recurring, per-customer).
"""
import json
import uuid
from datetime import datetime, timedelta

from billing_config import (
    scheduler, invoice_table,
    REMINDER_GROUP, SEND_REMINDER_FUNCTION_ARN,
    SCHEDULER_REMINDER_ROLE_ARN, DEFAULT_RETRY_POLICY, DEFAULT_DLQ_CONFIG,
    REMINDER_LABELS
)


def lambda_handler(event, context):
    customer_id = event["customer_id"]
    timezone = event.get("timezone", "UTC")

    # --- Idempotency: check if invoice already exists for this billing cycle ---
    billing_period = datetime.utcnow().strftime("%Y-%m")
    idempotency_key = f"{customer_id}-{billing_period}"

    existing_invoice = get_invoice_by_idempotency_key(idempotency_key)
    if existing_invoice:
        return {"invoice_id": existing_invoice["invoice_id"], "status": "already_exists"}

    # --- Generate the invoice ---
    invoice_id = str(uuid.uuid4())
    invoice_date = datetime.utcnow()
    due_date = invoice_date + timedelta(days=30)

    invoice_table.put_item(
        Item={
            "invoice_id": invoice_id,
            "customer_id": customer_id,
            "idempotency_key": idempotency_key,
            "invoice_date": invoice_date.isoformat(),
            "due_date": due_date.isoformat(),
            "amount": "99.00",  # Replace with actual billing logic
            "currency": "USD",
            "status": "unpaid",
            "created_at": invoice_date.isoformat()
        }
    )

    # --- Schedule payment reminders in the customer's timezone ---
    reminder_offsets = [
        ("7day", timedelta(days=-7)),
        ("3day", timedelta(days=-3)),
        ("due", timedelta(days=0)),
    ]

    for label, offset in reminder_offsets:
        reminder_time = due_date + offset

        # Don't schedule reminders in the past
        if reminder_time <= datetime.utcnow():
            continue

        schedule_kwargs = dict(
            Name=f"remind-{invoice_id}-{label}",
            GroupName=REMINDER_GROUP,
            ScheduleExpression=f"at({reminder_time.strftime('%Y-%m-%dT%H:%M:%S')})",
            ScheduleExpressionTimezone=timezone,
            FlexibleTimeWindow={"Mode": "OFF"},
            ActionAfterCompletion="DELETE",
            Target={
                "Arn": SEND_REMINDER_FUNCTION_ARN,
                "RoleArn": SCHEDULER_REMINDER_ROLE_ARN,
                "Input": json.dumps({
                    "customer_id": customer_id,
                    "invoice_id": invoice_id,
                    "reminder_type": label,
                    "due_date": due_date.isoformat()
                }),
                "RetryPolicy": DEFAULT_RETRY_POLICY,
                "DeadLetterConfig": DEFAULT_DLQ_CONFIG
            },
            State="ENABLED"
        )

        try:
            scheduler.create_schedule(**schedule_kwargs)
        except scheduler.exceptions.ConflictException:
            scheduler.update_schedule(**schedule_kwargs)

    return {"invoice_id": invoice_id, "status": "created"}


def get_invoice_by_idempotency_key(idempotency_key: str):
    """Check if an invoice already exists for this billing cycle."""
    response = invoice_table.query(
        IndexName="idempotency-index",
        KeyConditionExpression="idempotency_key = :key",
        ExpressionAttributeValues={":key": idempotency_key}
    )
    items = response.get("Items", [])
    return items[0] if items else None
