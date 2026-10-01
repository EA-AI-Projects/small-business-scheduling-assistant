# Two-way SMS in dev: authorization packet (issue #91)

This packet asks Enrique to authorize texting in the synthetic `dev` environment, one stage at a time, for himself and a few authorized testers only. Nothing in this document authorizes a deployment, a Twilio change, or a live text. Each stage below needs its own explicit "go" on #91.

## Summary for Enrique

- **What you get:** you and your testers can text the dev number to ask for times, book, approve by text, cancel, and reschedule. Testers must have given in-person consent and been entered in the owner app.
- **What it costs:** about $3 to $6 a month in Twilio, out of your $46.41 prepaid balance, against a $10 cap. AWS goes up by roughly a dollar, and stays under $4.30 a month. OpenAI is capped by the budget you set there.
- **Safety:** the app can only text numbers on an explicit allowlist that you supply at deploy time, and only people with recorded consent who have not opted out. Texting can be turned off in under five minutes (section 6).
- **Before stage 1:** three small code changes must be reviewed and merged (section 2). They contain no live actions.
- **Your decisions:** go or no-go for each stage in section 4, in order.

## 1. Decisions already made (from #91)

| Topic | Decision |
| --- | --- |
| Twilio account and number | Paid account; US number bought; Sole proprietor A2P brand and campaign approved. |
| Secrets | `/scheduling/dev/twilio/auth-token` and `/scheduling/dev/openai/api-key` exist as SSM Standard SecureStrings. |
| Testers | Enrique plus a few friends and family. Each needs in-person consent, an owner-verified profile with placeholder name and address, and a California number. Numbers never go in the repository or GitHub. |
| Spending | Twilio dev spend stays within about $10/month from the prepaid credit (balance $46.41 on 2026-10-01). Keep auto-recharge off so the balance is a hard stop. OpenAI is limited by the budget Enrique set there. |
| Plain-language texts | Approved (OpenAI interpreter). |
| Keyword handling | Twilio defaults; Advanced Opt-Out stays off. No keyword code changes before live testing. CANCEL, STOP, START, HELP and YES behavior is checked live in stage 2. |
| Evidence | Once testers text, dev is not torn down while their consent and opt-out evidence is retained (four years). |

## 2. Code prerequisites (reviewed PRs; no live action)

1. **Delivery switches as parameters.** The outbox dispatch schedule and the SMS sender's SQS trigger are hard-coded disabled in `template.yaml`, so outbound texts cannot be turned on without a template change. Add `OutboxDispatchScheduleState` and `SmsSenderMappingState` parameters (default `DISABLED`). Extend the `deploy-backend.sh` guard so a change to either requires a matching `--param`, as it already does for the three schedules (DEV_STACK_PLAN section 2.5).
2. **Phone-number parameters handled privately.** Mark `AuthorizedSmsRecipients` `NoEcho`, as `OwnerNumber` already is, and let `deploy-backend.sh` read `OwnerNumber`, `AuthorizedSmsRecipients`, `TwilioAccountSid` and `TwilioBusinessNumber` from a hidden prompt instead of the command line. It must never print them. Values remain visible to account administrators in the Lambda configuration; they are never written to the repository, GitHub, or logs this project controls.
3. **Consent capture in the deployed owner app.** The API route that records in-person consent (`POST .../clients/{client_id}/sms-consent`) exists in `owner_api.py` but is not wired into the deployed owner Lambda, and the owner app has no screen for it. Without it, a tester's consent can only be written by hand into DynamoDB. Wire the SMS store into `create_cognito_owner_app`, regenerate both OpenAPI contracts, and add a "Record in-person text consent" action on the client screen: a clear-yes checkbox, the script version, and the placeholder participant name. Also wire the delivery-failures route, so failed texts are visible.
4. **Doc fix (in this PR).** `PILOT_INFRASTRUCTURE.md` said Advanced Opt-Out was required before tester texting; it now records the Twilio-defaults decision.

## 3. What changes in AWS

| Setting | Now | After stage 1 | After stage 3 |
| --- | --- | --- | --- |
| `EnableSmsIngress` | false | true (signed webhook API created) | true |
| `TwilioInboundUrl`, `TwilioStatusUrl` | `example.invalid` | exact `SmsApiUrl` + `/webhooks/sms/inbound` and `/status` | same |
| `TwilioAccountSid`, `TwilioBusinessNumber`, `OwnerNumber` | placeholders | real values (hidden prompt) | same |
| `SmsSendEnabled` | disabled | disabled | authorized |
| `EnableSmsConversations` | disabled | disabled | authorized |
| `AuthorizedSmsRecipients` | empty | empty | owner + first tester (stage 4 adds the rest) |
| Outbox dispatch schedule, sender trigger | disabled | disabled | enabled |

Stage 1 creates one new HTTP API, one Lambda, one log group, and one alarm. Nothing else is created; the conversation and sender functions already exist and are inert.

## 4. Stages (each needs a separate "go")

**Stage 1: signed webhook, no texts.** Deploy the merged prerequisites. Enable ingress with the inert URLs, read `SmsApiUrl`, and redeploy with the exact URLs (the two-step bootstrap in PILOT_INFRASTRUCTURE). Then run a synthetic check: an unsigned or wrongly signed POST gets a 403, and a correctly signed synthetic POST from a 555 number is accepted and stored with no reply. The Twilio number still points elsewhere. *No SMS is sent or received.*

**Stage 2: inbound only, from your phone.** You point the Twilio number's incoming-message webhook at the exact inbound URL in the Twilio console. Sending and conversations stay off, so the app cannot text anyone. From your own phone, text the number: `HELP`, `YES`, `CANCEL`, `START`, `STOP`, `START`, then one plain sentence. For each text, record what Twilio replied and what the app stored (keyword, role, whether the body was kept). Check the logs for phone numbers or message text. This answers the CANCEL/STOP/START question with real Twilio behavior before any tester is involved. *Only Twilio's own default keyword replies are sent; the app sends nothing.*

**Stage 3: full flow with you and one tester.** Record the tester's consent in the owner app (prerequisite 3). Set the allowlist to your number and that tester's, turn on `SmsSendEnabled`, conversations, the sender trigger and outbox dispatch. Run one booking end to end: ask for times, pick one, you approve by text, the tester gets the confirmation, then cancel and reschedule. Confirm that a text to a number outside the allowlist is refused: book a visit in the owner app for a synthetic client with a 555 number and recorded consent, and check its confirmation fails with `RECIPIENT_NOT_AUTHORIZED` in delivery failures and that a STOP stops further texts. *Live texts go to two numbers only.*

**Stage 4: remaining testers.** For each new tester: in-person consent, a profile with a placeholder name, the consent record in the app. Then add their number to the allowlist with one deploy, all at once or a few at a time. *Live texts go to the listed numbers only.*

Results of each stage are recorded on #91 without numbers or message text.

## 5. Cost (fetched 2026-10-01; list prices)

**Twilio** ([US SMS pricing](https://www.twilio.com/en-us/sms/pricing/us), [A2P 10DLC fees](https://support.twilio.com/hc/en-us/articles/1260803965530-Pricing-and-Fees-for-A2P-10DLC-Service)):
- Fixed: the number at $1.15/month plus the Sole Proprietor campaign at $2/month, so **$3.15/month** whether or not anyone texts. The brand ($4.50) and vetting ($15) fees are one-time and already behind you.
- Per text segment: $0.0083 plus a carrier fee of $0.0025 to $0.0070, so about **$0.011 to $0.015**. A full booking (about 5 texts in, 5 out) costs about $0.13; longer replies take 2 segments.
- Estimate for an active test month with 20 bookings and some cancels and reschedules: about 300 segments, **$4**. Total about **$7/month**, inside the $10 cap. The $46.41 balance covers roughly 6 to 7 such months, or about 14 idle months.
- Sole Proprietor limits (one number, a low daily volume) are far above test needs.

**AWS:** DEV_STACK_PLAN puts dev between $2.43 (active) and $3.20 (all schedules all month) at list price. Outbox dispatch every minute is already in the $3.20 case. The new API, Lambda and one more alarm (+$0.10) add cents. Expect **under $4.30/month**, inside the $10 AWS budget.

**OpenAI:** one interpreter call per plain-language text (`gpt-6-luna`, at most 512 output tokens). Exact commands and offer replies don't call it. Spend is capped by Enrique's OpenAI budget limit.

## 6. Turning texting off (runbook, verified once in stage 3)

In order of speed; the first two take effect within a minute:
1. **Stop outbound:** disable the sender trigger. Run `aws lambda list-event-source-mappings --function-name <SmsSenderFunction>`, then `update-event-source-mapping --uuid <id> --no-enabled`. Texts already queued stay queued and are not sent; before turning sending back on, check the queue and purge anything stale (`aws sqs purge-queue` on `scheduling-outbox-dev`) so old texts are not delivered late.
2. **Stop inbound:** in the Twilio console, clear the number's incoming-message webhook (or point it back to the previous value). Twilio still answers STOP and HELP itself.
3. Disable the outbox dispatch rule: `aws events disable-rule`.
4. Make it permanent with one deploy: `SmsSendEnabled=disabled`, `EnableSmsConversations=disabled`, both new states `DISABLED`, and optionally `EnableSmsIngress=false`. Record the stop on #91.

Ingress can stay on while sending is off; it only stores receipts. Turning ingress off deletes the webhook API, so stage 1 has to be repeated to turn it back on. Data is never deleted by turning texting off (section 1, Evidence).

## 7. Risks and how they're handled

| Risk | Handling |
| --- | --- |
| A text reaches someone not authorized | Three checks before every send: the allowlist parameter, recorded consent for the client, and no opt-out. Tested in code; checked live in stage 3. |
| A stranger texts the number | No reply, and no scheduling change (PRD). The body is not processed. |
| Forged webhook calls | Twilio signature required on the exact URL; checked in stage 1. A flood of rejected calls costs little (API Gateway and Lambda per-request pricing) and is visible in the ingress error alarm. |
| Keyword surprises (for example a bare "cancel" opts someone out under Twilio defaults) | Observed in stage 2 before testers. Any fix is a separate decision. |
| Testers' message text goes to OpenAI | Plain-language texts are sent to OpenAI for interpretation; access codes are filtered out first. Tell testers to keep texts to scheduling. |
| Real numbers in logs | The SMS code logs no numbers or bodies; error tracebacks are checked in stage 2. Log groups keep 30 days. |
| Cost runaway | Prepaid balance with auto-recharge off; AWS budget alarm; OpenAI budget limit; a DLQ alarm on both queues. |
| A bad deploy | The change set is reviewed before execution; code rollback redeploys the previous artifact (DEV_STACK_PLAN section 3.2). |

## 8. What this packet does not do

It does not cover `pilot`, real clients, a business-registered sender, Advanced Opt-Out, or any change to keyword handling. It does not change retention, consent rules, or the conversation wording.
