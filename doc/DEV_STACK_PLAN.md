# AWS cost planning, rollback and teardown

**Scope:** Current operational guidance; reviewed 2026-10-14. The September 2026 pre-authorization packet and checkpoint are complete historical records, not a plan waiting to run. See [DECISION_HISTORY.md](DECISION_HISTORY.md). No procedure here grants a new external-action authorization.

## Cost planning

Prepare a dated estimate before authorizing infrastructure or messaging changes. Use the actual template's enabled schedules, Lambda invocations/duration, API calls, DynamoDB storage/PITR/read-write units, SQS, logs/metrics/alarms, Cognito, Amplify and artifact storage. Estimate Twilio number/campaign/carrier/message fees and OpenAI usage separately. Verify current region prices and the account's actual free allowances; do not reuse the original checkpoint's totals or provider balances.

Set the approved budget and notification thresholds in private operator configuration. Budgets and alerts do not themselves stop service spending. Confirm notifications and inspect actual costs after enablement. Resource definitions belong in `template.yaml`; account inventories and billing details stay outside reference documentation.

## Stop on a failed safety gate

1. Disable affected EventBridge rules and SQS mappings, resolving their physical names/UUIDs from the stack:

   ```sh
   aws events disable-rule --name <rule-name> --region us-west-1
   aws lambda update-event-source-mapping --uuid <mapping-id> --no-enabled --region us-west-1
   ```

   Confirm `DISABLED`/`Disabled` and account for work already in flight. If necessary, use a reviewed emergency concurrency brake; disabling a trigger does not cancel a running invocation.
2. Capture logs, alarm history, queue depth and relevant state privately; redact contacts and bodies from issue evidence.
3. Redeploy a compatible known-good backend artifact through a reviewed change set. Pass `DISABLED` for every stopped schedule/mapping parameter so the update cannot silently re-enable it. Review the sender and conversation authorization parameters too. Keep the previous artifacts until recovery is complete.
4. Restore the frontend separately from the retained commit-named zip, or rebuild the chosen version using the same public build configuration. Verify hosting headers and API compatibility.
5. Retain data. Plan any PITR restore separately; it creates a new table and requires a validated cutover. Inspect authoritative outbox state before any queue replay.

## Compatibility before rollback

The single-service-duration release (#339) stopped writing client `home_size` and per-client `default_duration_minutes`. Older releases index those attributes directly and cannot read a client saved by the new release. Do not roll back across that boundary without restoring required attributes under a reviewed data plan. Existing appointment snapshots remain valid. See [DECISION_HISTORY.md](DECISION_HISTORY.md#service-duration).

A release before the recipient-list removal (#114) also needs its old `AuthorizedSmsRecipients` parameter; an empty value makes the old sender fail closed. Review the target version's template rather than assuming today's parameters are accepted. Code and data rollback are separate operations.

## Teardown

**Retention gate:** after tester texting starts, keep dev while required consent/opt-out evidence remains within its four-year retention. Stopping texting and triggers remains possible; deleting/exporting the table or closing the account is not covered by the historical checkpoint. Explicit client erasure has its own approved semantics. Stop teardown at this gate unless a separately reviewed decision resolves the retained evidence.

For an authorized teardown whose data may be removed:

1. Disable ingress, schedules and queue mappings; confirm no work remains in flight.
2. Identify owned resources privately, including resources outside CloudFormation. Verify account/region and table-content disposition.
3. Delete the separately created Amplify app using an administrator session. Delete test identities if their pool will be retained.
4. Delete the runtime stack using its CloudFormation execution role. Wait for completion; investigate `DELETE_FAILED` rather than blindly retaining resources. An unexecuted `REVIEW_IN_PROGRESS` stack must also be deleted.
5. The table is retained on stack deletion/replacement. Delete it and handle backup retention **only** when the data disposition permits it. A retained fixed-name table prevents a fresh create with that name. Check current backup behavior before claiming erasure is complete.
6. Inspect leftover log groups, queues/DLQs, APIs, rules, mappings, topics/subscriptions, Cognito pool/domain and alarms. Do not remove shared resources.
7. Empty the dedicated artifact bucket, including every object version and delete marker, with paginated lists and batches of at most 1,000 deletes; then delete the bucket. A plain `aws s3 rm` is insufficient for versioned storage. Keep temporary manifests private.
8. Remove environment-scoped SecureString parameters and any separately created budget when authorized. Resolve the account for budget commands at run time; do not paste its ID into docs.
9. Delete the role stack **last** from an administrator session, after finishing checks that need the deployer. Remove local CLI profiles afterward. CloudFormation needs the execution role to delete runtime resources.
10. Verify absence of owned resources/backups, no remaining publishers for custom metrics, and the following bill. Retained resources kept intentionally should have a documented private owner and cost.

Account closure is a separate consequential action and does not establish immediate data deletion. Check current AWS procedures and retention obligations before proposing it.
