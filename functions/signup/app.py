"""
Signup Lambda: Creates a customer record and a recurring billing schedule.
Invoked directly (aws lambda invoke) with the customer payload as the event.

Event shape:
    {"customer_id": "cust-001", "email": "test@example.com", "timezone": "America/New_York"}
"""
import json
from datetime import datetime

from billing_config import (
    scheduler, customer_table,
    INVOICE_GROUP, GENERATE_INVOICE_FUNCTION_ARN,
    SCHEDULER_INVOICE_ROLE_ARN, DEFAULT_RETRY_POLICY, DEFAULT_DLQ_CONFIG
)


def lambda_handler(event, context):
    customer_id = event["customer_id"]
    email = event.get("email", "")
    timezone = event.get("timezone", "UTC")
    signup_date = datetime.utcnow()

    # Store customer in DynamoDB
    customer_table.put_item(
        Item={
            "customer_id": customer_id,
            "email": email,
            "timezone": timezone,
            "signup_date": signup_date.isoformat(),
            "billing_day": signup_date.day,
            "status": "active"
        }
    )

    # Create recurring billing schedule
    create_billing_schedule(customer_id, signup_date, timezone)

    return {
        "message": "Customer created and billing schedule activated",
        "customer_id": customer_id,
        "billing_day": signup_date.day,
    }


def create_billing_schedule(customer_id: str, signup_date: datetime, timezone: str):
    """
    Create a recurring monthly invoice schedule anchored to the customer's signup day.
    Idempotent: uses update if the schedule already exists.
    """
    day_of_month = signup_date.day
    cron_expression = f"cron(0 9 {day_of_month} * ? *)"

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
                "billing_day": day_of_month,
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
