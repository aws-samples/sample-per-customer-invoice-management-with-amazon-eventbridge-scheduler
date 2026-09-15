"""
Shared configuration for billing scheduler functions.
All constants are loaded from environment variables set by the SAM template.
"""
import os
import boto3

# --- AWS Clients ---
scheduler = boto3.client('scheduler')
dynamodb = boto3.resource('dynamodb')
ses = boto3.client('ses')

# --- Schedule Groups ---
INVOICE_GROUP = os.environ.get("INVOICE_GROUP", "billing-invoices")
REMINDER_GROUP = os.environ.get("REMINDER_GROUP", "billing-reminders")

# --- DynamoDB Tables ---
INVOICE_TABLE_NAME = os.environ.get("INVOICE_TABLE", "billing-invoices")
CUSTOMER_TABLE_NAME = os.environ.get("CUSTOMER_TABLE", "billing-customers")
REMINDER_LOG_TABLE_NAME = os.environ.get("REMINDER_LOG_TABLE", "billing-reminder-log")

invoice_table = dynamodb.Table(INVOICE_TABLE_NAME)
customer_table = dynamodb.Table(CUSTOMER_TABLE_NAME)
reminder_log_table = dynamodb.Table(REMINDER_LOG_TABLE_NAME)

# --- Lambda ARNs ---
GENERATE_INVOICE_FUNCTION_ARN = os.environ.get("GENERATE_INVOICE_FUNCTION_ARN", "")
SEND_REMINDER_FUNCTION_ARN = os.environ.get("SEND_REMINDER_FUNCTION_ARN", "")

# --- IAM Roles for Scheduler ---
SCHEDULER_INVOICE_ROLE_ARN = os.environ.get("SCHEDULER_INVOICE_ROLE_ARN", "")
SCHEDULER_REMINDER_ROLE_ARN = os.environ.get("SCHEDULER_REMINDER_ROLE_ARN", "")

# --- DLQ ---
DLQ_ARN = os.environ.get("DLQ_ARN", "")

# --- Amazon SES sender identity (verified email). Empty = log-only mode. ---
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "")

# --- Reminder Labels ---
REMINDER_LABELS = ["7day", "3day", "due"]

# --- Default Retry Policy for all schedules ---
DEFAULT_RETRY_POLICY = {
    "MaximumEventAgeInSeconds": 3600,
    "MaximumRetryAttempts": 3
}

DEFAULT_DLQ_CONFIG = {
    "Arn": DLQ_ARN
}
