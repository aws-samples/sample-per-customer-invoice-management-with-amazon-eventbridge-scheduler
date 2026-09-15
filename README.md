# Per-customer invoice generation and payment reminders with Amazon EventBridge Scheduler

This is the companion code for the AWS Compute Blog post *Per-customer billing with
Amazon EventBridge Scheduler*. It shows how to use [Amazon EventBridge Scheduler](https://aws.amazon.com/eventbridge/scheduler/)
as the time-aware backbone of a per-customer billing system: a recurring schedule
per customer for invoice generation, and one-time schedules for payment reminders.

> This is sample code for demonstration purposes. Review and harden it before using
> it in production (billing amounts, authentication on any invocation front door you
> add, SES production access, and least-privilege scoping for your own account).

## Architecture

At signup, the app creates a recurring monthly schedule anchored to the customer's
signup day. On each billing day, that schedule invokes the invoice generation
function, which creates the invoice and three one-time reminder schedules (7 days
before due, 3 days before, and on the due date). Each reminder schedule invokes the
reminder function, which sends the reminder only if the invoice is still unpaid. When
a payment arrives, a handler deletes any outstanding reminder schedules.

| Component | Purpose |
|-----------|---------|
| Amazon EventBridge Scheduler | Per-customer recurring invoice schedules and one-time reminders |
| AWS Lambda | 5 functions: signup, generate invoice, send reminder, payment received, reconciliation |
| Amazon DynamoDB | 3 tables (customers, invoices, reminder log) with point-in-time recovery and CMK encryption |
| Amazon SES | Sends reminder email (optional; log-only mode without a verified sender) |
| Amazon SQS | Dead-letter queue for failed schedule invocations, encrypted with the same CMK |
| AWS KMS | Customer-managed key encrypting the DynamoDB tables and the SQS DLQ |

The signup and payment-received functions are invoked directly with the AWS CLI in
this sample. In your own application, wire them behind whatever front door fits your
stack (API Gateway, EventBridge, an SQS-triggered handler for a payment gateway
webhook, and so on).

## Prerequisites

- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- [Python 3.12](https://www.python.org/downloads/)
- AWS credentials configured (`aws configure`)
- (Optional) A verified [Amazon SES](https://docs.aws.amazon.com/ses/latest/dg/creating-identities.html)
  sender identity if you want reminders emailed rather than logged

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ResourcePrefix` | `billing` | Prefix for resource names so you can deploy multiple independent stacks in one account/Region. |
| `SenderEmail` | *(empty)* | A verified SES sender identity. Leave empty to run reminders in log-only mode. |

All physical resources (tables, queue, roles, schedule groups) are named by
CloudFormation, so the stack deploys to any account and Region without collisions.

## Deploy

```bash
# Build
sam build

# First deploy (guided — sets stack name, Region, and parameters, then saves them)
sam deploy --guided

# Subsequent deploys
sam deploy
```

To send real email, pass a verified sender:

```bash
sam deploy --parameter-overrides "SenderEmail=billing@example.com"
```

## Test

The sample runs end to end without any client or webhook: read the function and
schedule-group names from the stack outputs, then invoke each Lambda directly with
the AWS CLI. The outputs let these examples work with any `ResourcePrefix` and any
Region.

Requires [`jq`](https://stedolan.github.io/jq/) for the payment step (or replace
that one line with any JSON extraction of your choice).

### 0. Load names from stack outputs

```bash
STACK=billing-scheduler

get_output() {
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey==\`$1\`].OutputValue" --output text
}

SIGNUP_FN=$(get_output SignupFunctionName)
GENERATE_FN=$(get_output GenerateInvoiceFunctionName)
PAYMENT_FN=$(get_output PaymentReceivedFunctionName)
RECON_FN=$(get_output ReconciliationFunctionName)
SEND_FN=$(aws cloudformation describe-stack-resource --stack-name "$STACK" \
  --logical-resource-id SendReminderFunction \
  --query 'StackResourceDetail.PhysicalResourceId' --output text)
INVOICE_GROUP=$(get_output InvoiceScheduleGroupName)
REMINDER_GROUP=$(get_output ReminderScheduleGroupName)
```

### 1. Create a customer (creates the recurring billing schedule)

```bash
aws lambda invoke \
  --function-name "$SIGNUP_FN" \
  --payload '{"customer_id":"cust-001","email":"test@example.com","timezone":"America/New_York"}' \
  --cli-binary-format raw-in-base64-out \
  signup-output.json && cat signup-output.json
```

Expected: `{"message":"Customer created and billing schedule activated","customer_id":"cust-001","billing_day":<today>}`.

### 2. Verify the schedule was created

```bash
aws scheduler list-schedules --group-name "$INVOICE_GROUP"
```

You should see one schedule named `invoice-cust-001` with a cron expression like
`cron(0 9 <today> * ? *)`.

### 3. Manually invoke the invoice generator

This skips the wait for the billing day.

```bash
aws lambda invoke \
  --function-name "$GENERATE_FN" \
  --payload '{"customer_id":"cust-001","timezone":"America/New_York"}' \
  --cli-binary-format raw-in-base64-out \
  invoice-output.json && cat invoice-output.json
```

Expected: `{"invoice_id":"<uuid>","status":"created"}`.

### 4. Verify the three reminder schedules were created

```bash
aws scheduler list-schedules --group-name "$REMINDER_GROUP"
```

You should see `remind-<invoice_id>-7day`, `-3day`, and `-due`, each with an `at(...)`
expression roughly 23, 27, and 30 days from now.

### 5. Test idempotency: rerun the invoice generator

```bash
aws lambda invoke \
  --function-name "$GENERATE_FN" \
  --payload '{"customer_id":"cust-001","timezone":"America/New_York"}' \
  --cli-binary-format raw-in-base64-out \
  invoice-output-2.json && cat invoice-output-2.json
```

Expected: `{"invoice_id":"<same uuid as step 3>","status":"already_exists"}`. The
same invoice ID is returned, no duplicate is created, and no extra reminders are
scheduled.

### 6. Record a payment (cancels outstanding reminders)

Use the EventBridge event shape (`event["detail"]["invoice_id"]`), which is how a
payment gateway webhook forwarded through Amazon EventBridge would arrive.

```bash
INVOICE_ID=$(jq -r .invoice_id invoice-output.json)
echo "INVOICE_ID=$INVOICE_ID"

aws lambda invoke \
  --function-name "$PAYMENT_FN" \
  --payload "{\"detail\":{\"invoice_id\":\"$INVOICE_ID\"}}" \
  --cli-binary-format raw-in-base64-out \
  payment-output.json && cat payment-output.json
```

Expected: `{"message":"Payment recorded","invoice_id":"...","reminders_canceled":["7day","3day","due"]}`.

### 7. Verify the reminders were canceled

```bash
aws scheduler list-schedules --group-name "$REMINDER_GROUP"
```

Should return `"Schedules": []`.

### 8. Run reconciliation

```bash
aws lambda invoke \
  --function-name "$RECON_FN" \
  --payload '{}' \
  --cli-binary-format raw-in-base64-out \
  reconciliation-output.json && cat reconciliation-output.json
```

Expected: `{"missing_repaired":0,"orphaned_removed":0,"total_active_customers":1,"total_schedules":1}`.

### 9. (Optional) Manually test the reminder sender

Use this if you deployed with `SenderEmail` and want to confirm the email path.
Since the invoice from step 3 is now paid, generate a fresh unpaid invoice for a
second customer first.

```bash
aws lambda invoke \
  --function-name "$SIGNUP_FN" \
  --payload '{"customer_id":"cust-002","email":"you@example.com","timezone":"UTC"}' \
  --cli-binary-format raw-in-base64-out /dev/null

aws lambda invoke \
  --function-name "$GENERATE_FN" \
  --payload '{"customer_id":"cust-002","timezone":"UTC"}' \
  --cli-binary-format raw-in-base64-out \
  invoice-cust-002.json

INVOICE_ID_2=$(jq -r .invoice_id invoice-cust-002.json)

aws lambda invoke \
  --function-name "$SEND_FN" \
  --payload "{\"customer_id\":\"cust-002\",\"invoice_id\":\"$INVOICE_ID_2\",\"reminder_type\":\"7day\",\"due_date\":\"2026-12-01\"}" \
  --cli-binary-format raw-in-base64-out \
  reminder-output.json && cat reminder-output.json
```

Expected in log-only mode (no `SenderEmail`): `{"action":"reminder_sent","type":"7day","delivery":"logged"}`.

Expected with a verified sender: `{"action":"reminder_sent","type":"7day","delivery":"emailed"}` and an email in the recipient's inbox.

### 10. (Optional) Inspect the data

```bash
CUSTOMER_TABLE=$(get_output CustomerTableName)
INVOICE_TABLE=$(get_output InvoiceTableName)

aws dynamodb scan --table-name "$CUSTOMER_TABLE"
aws dynamodb scan --table-name "$INVOICE_TABLE"
```

## Cleanup

```bash
STACK=billing-scheduler
INVOICE_GROUP=$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --query 'Stacks[0].Outputs[?OutputKey==`InvoiceScheduleGroupName`].OutputValue' --output text)
REMINDER_GROUP=$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --query 'Stacks[0].Outputs[?OutputKey==`ReminderScheduleGroupName`].OutputValue' --output text)

# Delete schedules first (a group can't be deleted while it still contains schedules)
for GROUP in "$INVOICE_GROUP" "$REMINDER_GROUP"; do
  aws scheduler list-schedules --group-name "$GROUP" \
    --query 'Schedules[].Name' --output text | tr '\t' '\n' | \
    xargs -I {} aws scheduler delete-schedule --name {} --group-name "$GROUP"
done

# Delete the stack
sam delete --stack-name "$STACK"
```

## Project structure

```
billing-scheduler-sam/
├── template.yaml                     # SAM/CloudFormation template
├── samconfig.toml                    # SAM CLI deploy configuration
├── functions/
│   ├── signup/app.py                 # Direct invoke — create customer + billing schedule
│   ├── generate_invoice/app.py       # Scheduler target — generate invoice + reminders
│   ├── send_reminder/app.py          # Scheduler target — send payment reminder
│   ├── payment_received/app.py       # EventBridge or direct invoke — mark paid + cancel reminders
│   └── reconciliation/app.py         # Daily audit — detect/repair schedule drift
└── layers/
    └── shared/
        ├── requirements.txt          # Layer dependencies (none; boto3 is in the runtime)
        └── python/billing_config.py  # Shared constants, clients, config
```

## Security

- IAM roles are scoped to the specific Lambda function ARNs and schedule groups this
  stack creates; there are no wildcard resource grants for Scheduler actions.
- The Scheduler trust policy is constrained with an `aws:SourceAccount` condition.
- The SES send permission is granted only when `SenderEmail` is set, and is scoped to
  that `From` address.
- A customer-managed AWS KMS key encrypts all three DynamoDB tables and the SQS
  dead-letter queue. Automatic key rotation is enabled.
- Point-in-time recovery is enabled on all three DynamoDB tables (35-day
  continuous backup window).
- The signup and payment-received functions are invoked directly in this sample.
  In production, place them behind an authenticated front door (for example, an API
  Gateway route with an authorizer, or an EventBridge rule with a scoped source).

## Cost

At demo volumes, the running cost is dominated by fixed items:

- The customer-managed KMS key: ~$1/month while the stack exists (plus a tiny
  per-request charge). Delete the stack when you are done to stop the charge.
- DynamoDB (on-demand) with PITR and Lambda are effectively free at demo usage.
- EventBridge Scheduler is priced per invocation and, at 4 events per customer per
  month, is negligible for tens of thousands of customers.

If you need the sample to run at zero fixed cost and don't need CMK, swap the
`SSESpecification` KMS references for `KMSMasterKeyId: alias/aws/dynamodb` (or
remove `SSESpecification` entirely to fall back to AWS-owned encryption).

## License

This project is licensed under the MIT-0 License. See [LICENSE](LICENSE).
