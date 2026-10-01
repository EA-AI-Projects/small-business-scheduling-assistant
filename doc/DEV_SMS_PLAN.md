# Two-way SMS in dev: authorization packet (issue #91)

This packet asks Enrique to authorize texting in the synthetic `dev` environment, one stage at a time, for himself and a few authorized testers only. Nothing in this document authorizes a deployment, a Twilio change, or a live text. Each stage below needs its own explicit "go" on #91.

## Summary for Enrique

- **What you get:** you and your testers can text the dev number to ask for times, book, approve by text, cancel, and reschedule. Testers must have given in-person consent and been entered in the owner app.
- **What it costs:** about $7 a month in Twilio during an active test month ($3.15 of it fixed), out of your $46.41 prepaid balance and inside your $10 cap. AWS rises to about $3.85 a month, inside its $10 budget. OpenAI is capped by the budget you set there.
- **Safety:** onboarding in the owner app is the only send gate: the app texts only people whose in-person consent the owner has recorded (which also verifies the phone) and who have not opted out, and numbers in the fictional 555-0100 to 555-0199 range are always refused. Texting can be turned off within minutes (section 6).
- **Before stage 1:** three code changes must be reviewed and merged. Two more, which need your answers on #91, must be merged before stage 3 (section 2). None of them involves a live action.
- **Your decisions:** the two questions on #91 (phone verification, old pending texts), then go or no-go for each stage in section 4, in order.

## 1. Decisions already made (from #91)

| Topic | Decision |
| --- | --- |
| Twilio account and number | Paid account; US number bought; Sole proprietor A2P brand and campaign approved. |
| Secrets | `/scheduling/dev/twilio/auth-token` and `/scheduling/dev/openai/api-key` exist as SSM Standard SecureStrings. |
| Testers | Enrique plus a few friends and family. Each needs in-person consent, an owner-verified profile with placeholder name and address, and a California number. Numbers never go in the repository or GitHub. |
| Spending | Twilio dev spend stays within about $10/month from the prepaid credit (balance $46.41 on 2026-10-01). Keep auto-recharge off so the balance is a hard stop. OpenAI is limited by the budget Enrique set there. |
| Plain-language texts | Approved (OpenAI interpreter). |
| Keyword handling | Twilio defaults; Advanced Opt-Out stays off. No keyword code changes before live testing. Keyword behavior is checked live in stages 2 and 3. |
| Evidence | Once testers text, dev is not torn down while their consent and opt-out evidence is retained (four years). |

## 2. Code prerequisites (reviewed PRs; no live action)

Before stage 1:
1. **Delivery switches as parameters** (PR #111). The outbox dispatch schedule and the SMS sender's SQS trigger were hard-coded disabled in `template.yaml`, so outbound texts could not be turned on without a template change. Add `OutboxDispatchScheduleState` and `SmsSenderMappingState` parameters (default `DISABLED`). Extend the `deploy-backend.sh` guard so a change to either requires a matching `--param` and the live-SMS authorization flag, as it already does for the three schedules (DEV_STACK_PLAN section 2.5).
2. **Phone-number parameters handled privately** (PR #111). Let `deploy-backend.sh` read `OwnerNumber`, `TwilioAccountSid` and `TwilioBusinessNumber` from a hidden prompt instead of the command line. It must never print them. Values remain visible to account administrators in the Lambda configuration; they are never written to the repository, GitHub, or logs this project controls.
3. **Consent capture in the owner app** (PR #110). The deployed owner API already serves the routes that record in-person consent (`POST .../clients/{client_id}/sms-consent`) and list delivery failures, but the OpenAPI export omitted them, so the owner app has no screen for either. Export both routes, regenerate the contracts, and add a "Record in-person text consent" action on the client screen: a clear-yes checkbox, the profile's placeholder name, and script version 1 from the public consent page. Add a delivery-failures list, so failed or refused texts are visible.

Before stage 3 (each waits for Enrique's answer on #91):

4. **Phone verification (decided on #91, option (b); built in this change).** Incoming texts are acted on, and texts are sent, only for a client whose profile phone is marked verified (`phone_verified_at`). Recording in-person consent in the owner app now marks that phone verified in the same atomic write as the consent records, and onboarding a new client shows the consent step right away (consent can be recorded later from the client screen; until then the client can't text). The owner reads the number back to the client first, because nothing proves the client holds the phone. Changing a profile's phone clears the verification, so the new number needs fresh consent. No standalone verify route and no DynamoDB hand-editing.
5. **Old pending texts.** Earlier synthetic testing left pending owner notifications in the dev outbox (for example "Scheduling policy updated" and "Owner calendar updated"). The dispatcher sends every due record, with no age limit. Purging the SQS queue does not help, because the records stay pending in DynamoDB and are re-queued. Before stage 3: count the due records read-only through `OutboxDueIndex`, then, if Enrique agrees on #91, run a reviewed one-off step that marks every record created before stage 3 as retired, not sent.
6. **Doc fixes (in this PR).** `PILOT_INFRASTRUCTURE.md` and the README said Advanced Opt-Out handles STOP and HELP before tester texting; they now record the Twilio-defaults decision.

## 3. What changes in AWS

| Setting | Now | After stage 1 | After stage 3 |
| --- | --- | --- | --- |
| `EnableSmsIngress` | false | true (signed webhook API created) | true |
| `TwilioInboundUrl`, `TwilioStatusUrl` | `example.invalid` | exact `SmsApiUrl` + `/webhooks/sms/inbound` and `/status` | same |
| `TwilioAccountSid`, `TwilioBusinessNumber`, `OwnerNumber` | placeholders | real values (hidden prompt) | same |
| `SmsSendEnabled` | disabled | disabled | authorized |
| `EnableSmsConversations` | disabled | disabled | authorized |
| `OutboxDispatchScheduleState`, `SmsSenderMappingState` | `DISABLED` | `DISABLED` | `ENABLED` |

Stage 1 adds the resources that `EnableSmsIngress=false` leaves out: the SMS HTTP API with its stage, the ingress Lambda with its role, two Lambda permissions, its log group and its error alarm (DEV_STACK_PLAN section 2.2). The deploy also updates environment variables on the existing, still inert sender and conversation functions.

## 4. Stages (each needs a separate "go")

**Stage 1: signed webhook, no texts.** Deploy the merged prerequisites 1 to 3. Enable ingress with the inert URLs, read `SmsApiUrl`, and redeploy with the exact URLs (the two-step bootstrap in PILOT_INFRASTRUCTURE). Then check that an unsigned POST and a POST with a wrong signature both get 403. A correctly signed request needs the real auth token, which no agent may read, so the positive check happens in stage 2 with real Twilio traffic. The Twilio number still points elsewhere. *No SMS is sent or received.*

**Stage 2: inbound only, from your phone.** You point the Twilio number's incoming-message webhook at the exact inbound URL in the Twilio console. Sending and conversations stay off, so the app cannot text anyone. From your own phone, text the number: `HELP`, `YES`, `CANCEL`, `START`, then one plain sentence. Do not text STOP (see the note below). For each text, record what Twilio replied and what the app stored: keyword, role, and whether the body was kept. The first stored receipt is also the positive signature check from stage 1. Check the logs for phone numbers or message text. *Only Twilio's own default keyword replies are sent; the app sends nothing.*

> **Owner-phone note.** In the app, STOP from the owner number blocks every owner text, and it can't be cleared the way a client's can (owners have no consent record). So the owner phone never texts STOP. Under Twilio defaults, a bare `CANCEL` is also an opt-out on Twilio's side, which `START` reverses; that is exactly what stage 2 observes, so `CANCEL` is followed by `START`. If the app turns out to record that `CANCEL` as a STOP, stop and report it before stage 3.

**Stage 3: full flow with you and one tester.** Requires prerequisites 4 and 5. Record the tester's consent in the owner app, which also marks their phone verified. Retire the old pending texts. Then turn on `SmsSendEnabled`, conversations, the sender trigger and outbox dispatch. Run one booking end to end: ask for times, pick one, you approve by text, the tester gets the confirmation, then cancel and reschedule.

Next, check that a client without recorded consent is refused. Use a synthetic or un-onboarded client (profile only, no consent recorded) and book a visit for them in the owner app; its confirmation must fail with `CONSENT_REQUIRED` in delivery failures, and nothing is sent. Then turn texting off with section 6, steps 1 and 2, timing it, and turn it back on.

Last: the tester texts STOP and you confirm no further texts reach them. To bring them back, record a fresh in-person consent, then have them text START. *Live texts go to two numbers only.*

**Stage 4: remaining testers.** For each new tester: in-person consent, a profile with a placeholder name, the consent record and phone verification. Onboarding is the only step: no deploy is needed, and the tester can receive texts as soon as the consent is recorded. *Live texts go only to people onboarded this way.*

Results of each stage are recorded on #91 without numbers or message text.

## 5. Cost (fetched 2026-10-01; list prices)

**Twilio** ([US SMS pricing](https://www.twilio.com/en-us/sms/pricing/us), [A2P 10DLC fees](https://support.twilio.com/hc/en-us/articles/1260803965530-Pricing-and-Fees-for-A2P-10DLC-Service)):
- Fixed: the number at $1.15/month plus the Sole Proprietor campaign at $2/month, so **$3.15/month** whether or not anyone texts. The brand ($4.50) and vetting ($15) fees are one-time and already paid. The A2P fee amounts come from search results, not the help page itself; re-check them in the Twilio console before stage 3.
- Per text segment: $0.0083 plus a carrier fee of $0.0035 to $0.005 outbound or $0 to $0.0035 inbound, so about **$0.008 to $0.013**. A full booking (about 5 texts in, 5 out) costs about $0.12; longer replies take 2 segments.
- Estimate for an active test month with 20 bookings plus some cancels and reschedules: about 300 segments, under **$4**. Total about **$7/month**, inside the $10 cap. The $46.41 balance covers roughly 6 to 7 such months, or about 14 idle months.
- Sole Proprietor limits (one number, a low daily volume) are far above test needs.

**AWS:** DEV_STACK_PLAN puts dev between $2.43 (active) and $3.20 (all schedules all month) at list price; outbox dispatch every minute is already in the $3.20 case. Turning on both SQS triggers adds about $0.52 a month in queue polling (DEV_STACK_PLAN section 1.4), and one more alarm adds $0.10. Expect **about $3.85/month**, inside the $10 AWS budget.

**OpenAI:** one interpreter call per plain-language text (`gpt-6-luna`, at most 512 output tokens). Exact commands and offer replies don't call it. Spend is capped by Enrique's OpenAI budget limit.

## 6. Turning texting off (runbook, verified once in stage 3)

In order of speed; the first two take effect within a minute:
1. **Stop outbound:** disable the sender trigger. Run `aws lambda list-event-source-mappings --function-name <SmsSenderFunction>`, then `update-event-source-mapping --uuid <id> --no-enabled`. Pending texts stay pending, in the queue and in the DynamoDB outbox, and are sent when the trigger is turned back on. Before turning it back on, count due records through `OutboxDueIndex` and decide whether to retire any (prerequisite 5); purging the queue alone does not stop them.
2. **Stop inbound:** in the Twilio console, clear the number's incoming-message webhook (or point it back to the previous value). Twilio still answers STOP and HELP itself and blocks later texts to anyone who sent STOP, but the app records no opt-out evidence for STOPs received while the webhook is cleared.
3. Disable the outbox dispatch rule: `aws events disable-rule`.
4. **Stop one tester** before they send STOP: mark their profile inactive in the owner app. An inactive profile is refused by the sender (`CLIENT_UNAVAILABLE` for notifications, `CONSENT_REQUIRED` for conversation replies), and the verified-phone lookup returns nothing for it, so their inbound texts are stored as an unknown sender with no command body and are never run as client commands (STOP is still recorded). Pending texts to them fail permanently rather than waiting. Their stored consent evidence is kept.
5. Make it permanent with one deploy: `SmsSendEnabled=disabled`, `EnableSmsConversations=disabled`, both new states `DISABLED`, and optionally `EnableSmsIngress=false`. Record the stop on #91.

Ingress can stay on while sending is off; it only stores receipts. Turning ingress off deletes the webhook API, so stage 1 has to be repeated to turn it back on. Turning texting off never deletes data (section 1, Evidence).

## 7. Risks and how they're handled

| Risk | Handling |
| --- | --- |
| A text reaches someone not authorized | Checks before every send: a verified phone and recorded consent for the client, no opt-out, and a refusal of fictional 555-0100 to 555-0199 numbers. Tested in code; checked live in stage 3. |
| A stranger texts the number | No reply, and no scheduling change (PRD). The body is not processed. |
| Forged webhook calls | Twilio signature required on the exact URL; checked in stages 1 and 2. A flood of rejected calls costs little (API Gateway and Lambda per-request pricing) and is visible in the ingress error alarm. |
| Keyword surprises (for example a bare "cancel" opts someone out under Twilio defaults) | Observed in stage 2 before testers. Any fix is a separate decision. |
| Old synthetic texts sent to real phones | Prerequisite 5 before stage 3. |
| Testers' message text goes to OpenAI | Plain-language texts are sent to OpenAI for interpretation. A text that matches the access-code pattern (phrases such as "gate code") is dropped before processing, but other wording, such as a bare number at "the side door", still reaches OpenAI. Tell testers to keep texts to scheduling and never send access codes. |
| Real numbers in logs | The SMS code logs no numbers or bodies; error tracebacks are checked in stage 2. Log groups keep 30 days. |
| Cost runaway | Prepaid balance with auto-recharge off; AWS budget alarm; OpenAI budget limit; a DLQ alarm on both queues. |
| A bad deploy | The change set is reviewed before execution; code rollback redeploys the previous artifact (DEV_STACK_PLAN section 3.2). |

## 8. What this packet does not do

It does not cover `pilot`, real clients, a business-registered sender, Advanced Opt-Out, or any change to keyword handling. It does not change retention, consent rules, or the conversation wording.
