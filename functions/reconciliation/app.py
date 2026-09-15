"""
Reconciliation Lambda: Detects and repairs drift between customer database and schedules.
Run daily via a separate EventBridge Scheduler schedule or CloudWatch Events rule.
"""
import json
from datetime import datetime

from billing_config import (
    scheduler, customer_table,
    INVOICE_GROUP, GENERATE_INVOICE_FUNCTION_ARN,
    SCHEDULER_INVOICE_ROLE_ARN, DEFAULT_RETRY_POLICY, DEFAULT_DLQ_CONFIG
)


def lambda_handler(event, context):
    # Get all active customers from DynamoDB
    active_customers = scan_active_customers()
    active_customer_ids = {c["customer_id"] for c in active_customers}

    # List all existing billing schedules
    existing_schedules = set()
    paginator = scheduler.get_paginator('list_schedules')
    for page in paginator.paginate(GroupName=INVOICE_GROUP):
        for s in page["Schedules"]:
            customer_id = s["Name"].removeprefix("invoice-")
            existing_schedules.add(customer_id)

    # Find customers missing a schedule
    missing_schedules = active_customer_ids - existing_schedules
    for customer_id in missing_schedules:
        customer = get_customer(customer_id)
        if customer:
            create_billing_schedule(
                customer_id=customer_id,
                billing_day=int(customer.get("billing_day", 1)),
                timezone=customer.get("timezone", "UTC")
            )
            log_action("created_missing_schedule", customer_id)

    # Find orphaned schedules
    orphaned_schedules = existing_schedules - active_customer_ids
    for customer_id in orphaned_schedules:
        try:
            scheduler.delete_schedule(
                Name=f"invoice-{customer_id}",
                GroupName=INVOICE_GROUP
            )
            log_action("deleted_orphaned_schedule", customer_id)
        except scheduler.exceptions.ResourceNotFoundException:
            pass

    result = {
        "missing_repaired": len(missing_schedules),
        "orphaned_removed": len(orphaned_schedules),
        "total_active_customers": len(active_customer_ids),
        "total_schedules": len(existing_schedules)
    }

    print(json.dumps({"event": "reconciliation_complete", **result}))
    return result


def scan_active_customers():
    """Scan all active customers from DynamoDB."""
    items = []
    response = customer_table.scan(
        FilterExpression="#s = :active",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":active": "active"}
    )
    items.extend(response.get("Items", []))

    while "LastEvaluatedKey" in response:
        response = customer_table.scan(
            FilterExpression="#s = :active",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":active": "active"},
            ExclusiveStartKey=response["LastEvaluatedKey"]
        )
        items.extend(response.get("Items", []))

    return items


def get_customer(customer_id: str):
    """Get a single customer record."""
    response = customer_table.get_item(Key={"customer_id": customer_id})
    return response.get("Item")


def create_billing_schedule(customer_id: str, billing_day: int, timezone: str):
    """Create a recurring billing schedule for a customer."""
    cron_expression = f"cron(0 9 {billing_day} * ? *)"

    schedule_kwargs = dict(
        Name=f"invoice-{customer_id}",
        GroupName=INVOICE_GROUP,
        ScheduleExpression=cron_expression,
        ScheduleExpressionTimezone=timezone,
        FlexibleTimeWindow={"Mode": "OFF"},
        Target={
            "Arn": GENERATE_INVOICE_FUNCTION_ARN,
            "RoleArn": SCHEDULER_INVOICE_ROLE_ARN,
            "Input": json.dumps({
                "customer_id": customer_id,
                "billing_day": billing_day,
                "timezone": timezone
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


def log_action(action: str, customer_id: str):
    """Log reconciliation actions for audit trail."""
    print(json.dumps({
        "event": "reconciliation",
        "action": action,
        "customer_id": customer_id,
        "timestamp": datetime.utcnow().isoformat()
    }))
