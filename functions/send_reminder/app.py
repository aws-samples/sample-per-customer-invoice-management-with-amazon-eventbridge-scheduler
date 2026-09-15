"""
Send Reminder Lambda: Sends payment reminder email with idempotency.
Triggered by EventBridge Scheduler (one-time, per-invoice-reminder).

If SENDER_EMAIL is configured (a verified Amazon SES identity), the reminder is
emailed to the customer. Otherwise the function runs in log-only mode, which is
convenient for testing without leaving the SES sandbox.
"""
import json
from datetime import datetime, timedelta

from billing_config import (
    ses, invoice_table, reminder_log_table, customer_table,
    SENDER_EMAIL,
)

REMINDER_SUBJECTS = {
    "7day": "Your invoice is due in 7 days",
    "3day": "Your invoice is due in 3 days",
    "due": "Your invoice is due today",
}


def lambda_handler(event, context):
    customer_id = event["customer_id"]
    invoice_id = event["invoice_id"]
    reminder_type = event["reminder_type"]

    # --- Idempotency: check if this reminder was already sent ---
    idempotency_key = f"{invoice_id}-{reminder_type}"
    if reminder_already_sent(idempotency_key):
        return {"action": "skipped", "reason": "already_sent"}

    # --- Check if the invoice is still unpaid ---
    response = invoice_table.get_item(Key={"invoice_id": invoice_id})
    invoice = response.get("Item")

    if not invoice:
        return {"action": "skipped", "reason": "invoice_not_found"}

    if invoice.get("status") == "paid":
        return {"action": "skipped", "reason": "already_paid"}

    # --- Send the reminder (email through SES, or log-only if no sender) ---
    delivery = send_reminder(
        customer_id=customer_id,
        invoice=invoice,
        reminder_type=reminder_type,
        due_date=event["due_date"],
    )

    # --- Record that this reminder was sent (for idempotency) ---
    reminder_log_table.put_item(
        Item={
            "idempotency_key": idempotency_key,
            "sent_at": datetime.utcnow().isoformat(),
            "customer_id": customer_id,
            "invoice_id": invoice_id,
            "reminder_type": reminder_type,
            "delivery": delivery,
            "ttl": int((datetime.utcnow() + timedelta(days=90)).timestamp()),
        }
    )

    return {"action": "reminder_sent", "type": reminder_type, "delivery": delivery}


def send_reminder(customer_id, invoice, reminder_type, due_date):
    """Email the reminder through SES when configured; otherwise log it."""
    amount = invoice.get("amount", "unknown")
    currency = invoice.get("currency", "USD")
    subject = REMINDER_SUBJECTS.get(reminder_type, "Payment reminder")
    body = (
        f"This is a reminder that invoice {invoice.get('invoice_id')} "
        f"for {amount} {currency} is due on {due_date}."
    )

    recipient = get_customer_email(customer_id)

    if SENDER_EMAIL and recipient:
        ses.send_email(
            Source=SENDER_EMAIL,
            Destination={"ToAddresses": [recipient]},
            Message={
                "Subject": {"Data": subject},
                "Body": {"Text": {"Data": body}},
            },
        )
        return "emailed"

    # Log-only mode (no verified sender, or customer has no email on file).
    print(json.dumps({
        "event": "reminder_logged",
        "reason": "no_sender_configured" if not SENDER_EMAIL else "no_recipient_email",
        "customer_id": customer_id,
        "invoice_id": invoice.get("invoice_id"),
        "reminder_type": reminder_type,
        "due_date": due_date,
        "amount": amount,
    }))
    return "logged"


def get_customer_email(customer_id: str):
    """Look up the customer's email address from the customer table."""
    response = customer_table.get_item(Key={"customer_id": customer_id})
    item = response.get("Item")
    return item.get("email") if item else None


def reminder_already_sent(idempotency_key: str) -> bool:
    """Check if a reminder was already sent using DynamoDB."""
    response = reminder_log_table.get_item(Key={"idempotency_key": idempotency_key})
    return "Item" in response
