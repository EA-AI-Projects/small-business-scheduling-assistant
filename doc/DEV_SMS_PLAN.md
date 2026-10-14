# Dev SMS operations

**Scope:** Current messaging gates and operating procedures, reviewed 2026-10-14. The staged #91 authorization packet is historical; it is not an uncompleted launch checklist or standing permission to send. Live provider settings must be checked in the private operator account.

## Recipient and conversation gates

Inbound scheduling requires a recognized eligible sender. Client profiles must be active with a verified phone and matching in-person consent; the worker rechecks them. The owner records client consent after reading the phone back; that atomic operation verifies the profile phone. Changing the phone clears verification. This proves the recorded agreement, not possession of the handset.

Outbound delivery rechecks recipient, verified phone, client consent, opt-out, relevant event state and live authorization. Fictional 555-0100–555-0199 numbers are refused. There is no deploy-time recipient list. First client enrollment queues one welcome intent; fresh consent after STOP does not queue another welcome. Client STOP remains active until newer in-person consent and START are both recorded.

## Configure and enable

Before an authorized enablement, confirm exact signed ingress/status URLs, provider configuration, consent process, alarm delivery, queue/outbox backlog, retention and delivery permissions. Resolve account IDs, provider IDs, phones, URLs and secrets privately. The SSM paths are `/scheduling/<environment>/twilio/auth-token` and `/scheduling/<environment>/openai/api-key`; values must be SecureString and never enter docs/logs.

| Capability | Required configuration |
| --- | --- |
| Inbound webhooks | `EnableSmsIngress=true`, valid exact URLs, Twilio secret and provider forwarding. |
| Ordinary outbound | `SmsSendEnabled=authorized`, `SmsSenderMappingState=ENABLED`, outbox dispatch and eligible recipient. |
| Model conversations | `EnableSmsConversations=authorized` plus ingress/sending authorization and OpenAI secret. |
| Scheduled outreach | Separate schedule/delivery parameters, eligible clients and saved settings. |
| Manual outreach | `ManualInvitationDeliveryEnabled=authorized`, retention schedule, eligible clients and confirmed owner action. |

Enable one separately authorized capability at a time and record redacted results in its issue. Use [AWS_VALIDATION.md](AWS_VALIDATION.md) for deployed checks; complete a separately authorized booking/decision/cancel/move flow to establish end-to-end messaging proof.

## Provider keywords

The recorded dev decision uses Twilio Advanced Opt-Out with CANCEL removed so ordinary cancellation language reaches scheduling. The app's STOP set excludes CANCEL. Twilio handles keyword replies; the app handles scheduling. STOP/HELP tags are authoritative; a START tag is honored as a keyword only when the body is START or UNSTOP, so YES can reach confirmation handling.

The dev exception conflicts with the campaign's recorded CANCEL opt-out keyword. Before pilot, resolve it by updating the registration or restoring the provider keyword. Keep YES out of provider opt-in keywords. [A2P_REGISTRATION.md](A2P_REGISTRATION.md) owns registration/consent guidance. The owner number has no client-style fresh-consent reset; do not use an owner STOP test as if it were a recoverable client test.

## Turning texting off

1. Disable the sender's SQS event-source mapping and confirm `Disabled`. Already-running sends may finish.
2. Disable outbox dispatch. Disable conversation/ingress triggers or parameters as required by the incident; disabling outbound alone does not stop incoming receipts.
3. Record the pause and inspect backlog and failures privately. Queue purge alone does not retire DynamoDB outbox intents.
4. Before resume, review due records and appointment state, decide how stale work is handled, and deliberately restore both live parameters and trigger states through the reviewed deployment path. Do not blindly enable a backlog.

Emergency commands and rollback are in [DEV_STACK_PLAN.md](DEV_STACK_PLAN.md#stop-on-a-failed-safety-gate). `deploy-backend.sh` preserves guarded trigger states; any temporary rule disable must be reflected in the next deployment's parameters.

## One-off backlog retirement

`backend/scheduling/tools/retire_outbox.py` is the historical pre-live-SMS retirement tool. It is restricted to the fixed dev account/table/region and is not a general purge operation. It conditionally marks pre-cutoff unleased unsent intents `FAILED` with `RETIRED_BEFORE_LIVE_SMS`, removing due-index keys. Leased/conflicting records are left for review; it does not inspect message bodies/phones or delete callback status records.

Its original one-time authorization does not cover later backlog retirement. Review the cutoff, business, dry-run counts, leases and audit requirements before proposing another execution. Do not retire a newly queued consent welcome by choosing an indiscriminate later cutoff.

## Retention and failure handling

Provider acceptance is distinct from delivery. Signed terminal failures remain visible to the owner for manual follow-up and do not authorize a blind resend. Ordinary uncertain provider acceptance may duplicate a retry; invitations retain a held claim for reconciliation. SMS body/evidence retention and client erasure are defined in [ARCHITECTURE.md](ARCHITECTURE.md#7-privacy-retention-and-erasure). Keep dev while required tester evidence remains within retention; stopping SMS does not authorize teardown.
