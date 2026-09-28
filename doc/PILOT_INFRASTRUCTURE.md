# Pilot infrastructure plan (code only)

Issue [#23](https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/23) targets AWS account `339713090487` in `us-west-1`. The `template.yaml` stack is a reviewable foundation for the authenticated owner calendar. It has **not** been deployed; provisioning, owner account creation, live SMS, and customer data onboarding each need a separate authorized step.

## Current stack boundary

- The HTTP API exposes the owner page at `GET /owner` and only the owner API under `/v1/owner/{proxy+}`. API Gateway checks a Cognito JWT on owner API routes; the FastAPI verifier also requires the exact issuer, client ID, access-token type, owner subject, and business ID. The public page contains no customer data. The local synthetic API and Twilio webhooks are not routed by this stack.
- A single on-demand DynamoDB table has the `PK`/`SK` primary key, `HoldDueIndex`, and `OutboxDueIndex` expected by the adapters. Point-in-time recovery is enabled and CloudFormation retains the table on stack deletion or replacement. Deleting a stack therefore does **not** delete customer records.
- An encrypted SQS Standard queue and dead-letter queue are defined, but no sender consumer is attached. Hold expiry, outbox dispatch, note retention, and SMS retention rules are defined **disabled**. Their handlers are packaged and IAM-scoped; operators must only enable them as part of an approved, monitored deployment. In particular, enabling outbox dispatch without a consumer would accumulate messages.
- Lambda logs have 30-day retention. Error alarms cover the API and each scheduled worker; the dead-letter queue has a depth alarm. Alarm actions and notification destinations are not configured. A deployment plan must attach an owner-visible destination and verify delivery before real data is onboarded.
- The Twilio webhook API, SQS sender trigger, Twilio credential retrieval, SMS evidence legal-hold management, overdue-hold/outbox-age metrics, and GitHub OIDC deploy role remain to be integrated. This foundation cannot send SMS.

## Build and preflight

From the repository root, run `sam validate --lint --template-file template.yaml --region us-west-1` and `sam build --template-file template.yaml`. These commands only validate and build local artifacts. Before any separately authorized deployment, run `aws sts get-caller-identity` and reject any account other than `339713090487`; check the CLI region is `us-west-1`. Use separate `dev` and `pilot` stack names and a unique Cognito domain prefix. The owner redirect URI must be an exact HTTPS URL ending in `/owner` on the deployed API endpoint. It must match the Cognito callback and the Lambda setting. A custom domain is optional.

The first stack creation needs an `OwnerSub` placeholder because the real Cognito subject exists only after an administrator creates the single owner user. Keep the owner API unused until a separately authorized update sets `OwnerSub` to that user's exact Cognito `sub`. Then sign in through the hosted UI with authorization code and PKCE, verify that `/owner` loads, that an unauthenticated owner API call fails, and that only that exact subject can read the pilot business calendar. Seed the documented owner policy through the authenticated API before any booking writes. Do not use a broadly shared owner account.

## Integration proof still required

Use a synthetic `dev` stack after separate provisioning authorization. Exercise two simultaneous approvals for one slot, approval versus scheduled expiry, and a replacement swap racing another calendar write. The expected result is at most one committed reservation; losing commands return a conflict or current terminal state and write no partial outbox event. Check SQS duplicate delivery, worker retry and DLQ redrive with a fake sender, then attach an explicitly authorized SMS test number only after Twilio campaign/number approval and the consent/STOP gates. Verify scheduled note and SMS purge with synthetic expired records and legal holds before onboarding real records. No live integration results are claimed by this document.

## Rollback and monitoring

Keep the previous Lambda artifact/version for a code rollback. Disable schedules and event source mappings before rollback if workers cause errors. Roll back code separately from data; the table is retained and point-in-time recovery is the last-resort data recovery mechanism, not an automatic schema rollback. Inspect failed Lambda invocations, API errors, queue/DLQ depth, hold expiry age, outbox due age, and retention purge counts. Investigate a DLQ record before replay; the outbox record remains authoritative. Never replay an SMS blindly after an uncertain provider acceptance.

## Cost worksheet before deployment

The template has no provisioned Lambda concurrency or DynamoDB capacity, but the table/PITR, SQS requests, Cognito active users, API requests, Lambda invocations, logs, and alarms can all incur charges. Estimate monthly cost using the official [AWS Pricing Calculator](https://calculator.aws/) for `us-west-1` with expected API requests, transaction reads/writes, table and backup size, scheduled invocation frequency, queue messages, logs, and Cognito users. Add Twilio number, campaign, and message costs separately. Do not treat free-tier eligibility as a guaranteed budget. Set a budget and alarm threshold approved by the owner before provisioning; no spending threshold has been chosen yet.
