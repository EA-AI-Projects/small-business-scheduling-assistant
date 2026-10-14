# AWS infrastructure and operations

**Scope:** Current template and operator procedures; reviewed 2026-10-14. This document does not inventory live resources or grant deployment/SMS authorization. The original dev checkpoint is historical; see [DECISION_HISTORY.md](DECISION_HISTORY.md).

## Current stack boundary

`template.yaml` defines the authenticated owner/client HTTP API, separate conditional signed Twilio API, Lambda functions, a retained on-demand DynamoDB table with PITR/TTL and two due indexes, two SQS queues with DLQs, Cognito, scheduled workers, and CloudWatch/SNS monitoring. The static app, artifact bucket, account budget and SecureString values are set up separately. Runtime topology and data rules belong in [ARCHITECTURE.md](ARCHITECTURE.md).

Use `us-west-1` and a dedicated dev account; pilot needs its own reviewed account configuration. Keep account numbers, real ARNs, app/pool IDs, queue URLs, bucket names containing account identifiers, phones, mailboxes and deployment outputs in private operator records. Resolve them from AWS at run time. Reference examples use placeholders or logical resource names.

## Build and preflight

```sh
sam validate --lint --template-file template.yaml --region us-west-1
sam build --template-file template.yaml
```

Before an authorized AWS operation, verify `aws sts get-caller-identity` against the privately recorded target account and verify the region. Review resources, trigger changes, costs, alarm mailbox, permissions and rollback before execution. `scripts/dev/` and `infra/dev-deploy-roles.yaml` have deployment-specific restrictions; they are not generic pilot tools. Their account guards remain unchanged by documentation cleanup.

## Create the role stack

Create a private encrypted/versioned artifact bucket and an approved budget. Prepare the separate role-stack change set from `infra/dev-deploy-roles.yaml`, review its IAM trust and boundary, then execute only under the applicable authorization. [AWS_DEPLOYMENT_ROLES.md](AWS_DEPLOYMENT_ROLES.md) documents commands, permissions and scoping limits.

Deploy the runtime stack with `PermissionsBoundaryArn` from the role stack's output. After Cognito exists, update the role stack with `OwnerUserPoolId` before client invitations: both the API role and its boundary must allow `AdminGetUser` and `AdminCreateUser` on that pool. A successful login does not prove invitation permissions.

## Application bootstrap

1. Supply an inert `OwnerAppOrigin`, such as `https://example.invalid`, and a bootstrap owner-subject placeholder on first creation. Keep traffic and delivery disabled.
2. Create/approve the owner Cognito account and its business identity link using the [account procedure](ACCOUNTS.md). Authentication alone grants no access.
3. Create an Amplify static app with platform `WEB`. Dev uses manual uploads with no Git connection. Apply the `customHttp.yml` header entries in the non-monorepo `customHeaders:` format for a manual app; it cannot read headers from a repository.
4. Build using `OwnerApiUrl`, `OwnerCognitoDomain`, `OwnerAppClientId` and the stack's `BusinessId` for the public frontend variables. Export and check the app as described in [frontend/README.md](../frontend/README.md).
5. Update `OwnerAppOrigin` to the exact HTTPS app origin (no path or trailing slash) and `OwnerSub` to the approved subject. Callback/logout uses that origin plus `/`. Verify CSP/hosting headers, preflight, unauthenticated refusal and linked owner/client scope.
6. Seed the scheduling policy and save the one-service business profile before default-duration booking. Confirm the SNS alarm subscription and test alarm delivery.

## Deploy to dev with scripts

Sign in using the local administrator SSO profile; the deployer profile assumes its role from that session. Profile setup lives in local AWS configuration. The scripts default to `scheduling-dev-admin` and `scheduling-dev-deployer` and guard a fixed dev account and `us-west-1`. `--dry-run` still attempts a read-only identity check and skips it if no credentials are usable.

| Script | Purpose |
| --- | --- |
| `scripts/dev/deploy-backend.sh` | Build, prepare/review change set, preserve guarded trigger states, execute and smoke-check. |
| `scripts/dev/deploy-frontend.sh` | Build from stack outputs, retain commit-named artifact, manually upload to Amplify and check headers. |
| `scripts/dev/status.sh` | Read stack, schedules, app, alarms and budget. |
| `scripts/dev/schedules.sh` | Change a supported schedule parameter through a full backend deployment; outbox dispatch uses the live-SMS path. |

Scripts deploy the **local checkout** and report its commit. Dirty writes require `--allow-dirty`; an unpushed commit produces a warning. Private parameters use hidden prompts rather than printed command arguments. Do not use these scripts for pilot by substituting credentials; prepare its own scoped configuration.

## Deployment and enablement gates

| Capability | Template parameter(s) / default |
| --- | --- |
| Signed Twilio ingress | `EnableSmsIngress=false`; exact inbound/status URLs and Twilio SecureString required before forwarding traffic. |
| SMS sending | `SmsSendEnabled=disabled`, `SmsSenderMappingState=DISABLED`. |
| SMS model conversations | `EnableSmsConversations=disabled`; requires authorized ingress/sending plus OpenAI SecureString. |
| Hold expiry | `HoldExpiryScheduleState=DISABLED`; runs every five minutes when enabled. |
| Outbox dispatch | `OutboxDispatchScheduleState=DISABLED`; every minute when enabled. |
| Note / SMS purge | `NoteRetentionScheduleState=DISABLED`, `SmsRetentionScheduleState=DISABLED`; daily at 09:00 / 10:00 UTC. |
| Scheduled invitations | `BookingInvitationScheduleState=DISABLED`, `BookingInvitationDeliveryEnabled=disabled`; selector runs every minute when enabled. |
| Manual invitations | `ManualInvitationDeliveryEnabled=disabled`; requires SMS retention and ordinary live-delivery gates. |

Defaults describe a new stack, not the live environment. Preserve current trigger state deliberately across deployments, including emergency-disabled rules. Never infer live-SMS authorization from a template setting, old checkpoint or campaign approval. Current messaging procedures are in [DEV_SMS_PLAN.md](DEV_SMS_PLAN.md).

## Validation and monitoring

[AWS_VALIDATION.md](AWS_VALIDATION.md) separates local race checks from opt-in deployed DynamoDB, Lambda, SQS/DLQ and retention proof. Review each test's side effects first. Record which commit and configuration passed; local tests do not prove IAM, deployed triggers or provider delivery.

CloudWatch log groups retain 30 days. Error and DLQ alarms plus oldest-due-age metrics publish to SNS. Confirm the email subscription and alarm delivery. Monitor overdue holds, due outbox backlog, expired claims, failures and both DLQs; logs must omit phone numbers and message bodies.

## Rollback and monitoring

Disable affected schedules and queue mappings first; record redacted evidence. Backend and frontend artifacts roll back separately. Retained data is not rolled back automatically, and PITR restores a new table. Follow [DEV_STACK_PLAN.md](DEV_STACK_PLAN.md); check compatibility of the proposed prior version before redeploying.

### Client deletion blocked by an SMS send claim

A `CLIENT_SEND#<sha256(client_id)>` item means a client text may still be in flight. It contains a random token and `acquired_at`. A deletion conflict is recoverable, but the claim has no automatic expiry: a timeout may occur after Twilio accepted the text. An operator must not clear it merely because the Lambda stopped or a retry is due.

1. Pause the SMS sender SQS event source mapping and confirm no sender invocation remains active. Keep it paused through deletion. Read the claim, profile, and related outbox records with strongly consistent reads; record the claim token and acquisition time in the private incident record, never in GitHub or logs.
2. Reconcile the provider outcome using Twilio's message records for the recipient and acquisition interval. If acceptance or delivery is uncertain, keep the claim and escalate; do not replay the outbox message or complete deletion. If accepted, wait for the provider result to settle and account for the outbound evidence before proceeding.
3. Only after the sender has stopped and the provider outcome is resolved, conditionally delete the exact claim using its recorded token. Immediately retry the owner deletion while the mapping remains paused; confirm the deletion fence is created and the API reports completion. If the condition fails, reread the claim and restart the reconciliation.
4. Resume the mapping only after deletion completes or after deciding to defer deletion with the claim intact. Check the outbox queue and alarms. A retry after deletion must find the client unavailable and send nothing.

This procedure describes recovery; it does not authorize a deployment, provider action, or live SMS.
