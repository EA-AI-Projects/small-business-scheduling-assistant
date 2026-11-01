# Decision history and retired plans

This is a selected record of material changes, not a second specification. Current behavior belongs in the documents linked below. Detailed discussion, dated costs, checkpoint output and prior text remain in Git history and the cited issues/PRs. Historical decisions do not grant new deployment, spending, publication or live-SMS authorization. Account-specific details are intentionally omitted.

## Deployment isolation and delivery

- **Initial direction (#17):** shared-account deployment and reuse of the reference project's serverless pattern, including a possible GitHub OIDC/Git-connected delivery path.
- **Replacement (#43; 2026-09-30):** dedicated dev member account, separate pilot account planned, scoped operator/CloudFormation roles and manual Amplify uploads. Dev was kept running after its synthetic checkpoint. The repository implements CI, not OIDC deployment. Account inventories belong in private operator records.
- **Retired artifact:** the September `DEV_STACK_PLAN.md` pre-authorization packet, fixed-price scenarios and resource-count snapshot. Its checkpoint recorded transaction/IAM/hosting/rollback findings for that version; it is not today's deployment proof or price estimate. Current [operations](PILOT_INFRASTRUCTURE.md), [IAM guidance](AWS_DEPLOYMENT_ROLES.md), [validation](AWS_VALIDATION.md) and [recovery](DEV_STACK_PLAN.md) replace its active instructions.

## SMS enrollment and keywords

- **Earlier rules (#22, #91):** the owner could not verify a client phone, a deploy-time recipient list limited sending, and Twilio default keyword handling was chosen with Advanced Opt-Out off.
- **Replacement (#91, #114):** in-person consent recording atomically verifies the recorded profile phone; onboarding and current eligibility replace the recipient list. A changed phone needs fresh consent. Dev Advanced Opt-Out removes CANCEL so plain cancellation language reaches the app; pilot registration/keyword alignment remains a separate gate.
- **Retired artifact:** the staged `DEV_SMS_PLAN.md` launch packet, old pending prerequisites, provider balances and per-stage live-setting snapshots. Keep pause/resume and retirement constraints in [current SMS operations](DEV_SMS_PLAN.md). Tester evidence retention continues to constrain teardown.

## Owner conversation

- **Earlier choices (#60, #174, #177, #175):** narrow short-reply decision allowlists, deterministic calendar/offer parsing and separate reply drafting.
- **Replacement (#297–#300):** bounded owner tool loop interprets decisions and follow-ups, reads current pending/calendar data, and uses versioned lifecycle/counteroffer tools. The backend requires a trusted sent preview of exact client text before a counteroffer can be queued. See [CONVERSATION.md](CONVERSATION.md#owner-approval-and-decline-tool-loop-298).

## Client conversation and wording

- **Earlier approach (#271–#275):** typed intent plus separate result-based reply drafting, including a removed read-only drafter.
- **Replacement (#340–#343):** bounded client read/action tools, stored-offer acceptance, two-message cancellation, replacement-first moves and sender-bound counteroffer answers; deterministic handling remains fallback.
- **Accepted wording boundary (#283, #359, #361; October 2026):** backend checks facts/format and authoritative state, but does not prove status prose or option presentation. The loop uses a 480-septet limit and one rewrite; the legacy result drafter uses one GSM-7 segment. Model phrasing risk remains explicit.
- **Names and concise replies (#348; 2026-10-13 wording decision):** saved business/service names replace hard-coded trade wording. Routine receipts omit unnecessary references/help tails; references remain available for disambiguation. Next appointment means the earliest upcoming confirmed visit, with pending requests separate.

## SMS response dispatch

- **Earlier approach:** every saved conversation reply waited for the once-a-minute outbox dispatcher before reaching the sending queue.
- **Replacement (#384; owner decision, 2026-10-19):** immediately hand off notifications saved during receipt processing after reply drafting finishes, retaining the scheduled sweep for recovery and retries. This reduces avoidable response latency without adding paid acknowledgment texts. Transactional persistence, sender claims and recipient checks remain in place. This records the repository change, not deployment or measured live latency.

## Service duration

- **Earlier choice:** small/medium/large home-size categories with 1/2/3-hour client defaults.
- **Replacement (#336, #339):** one saved business service default drives every booking path, with explicit owner appointment overrides. Existing appointments retain duration/buffer snapshots; quoted SMS offers cannot silently change length.
- **Compatibility:** client saves drop legacy home-size/default-duration attributes. A pre-#339 release cannot read clients saved by the new release without a reviewed attribute-restoration/data plan. This is an operational rollback constraint, not a second supported duration model.

## Client portal and Settings

- **Earlier MVP baseline (#224):** client sign-in and a minimal landing state, with scheduling by SMS.
- **Replacement (#230–#234, #313–#317, #329):** client Calendar and Appointments now support scoped reads, explicit requests, withdrawal/cancellation and pending moves using shared calendar components. SMS remains primary.
- **Settings (#366–#370; accepted 2026-10-13):** four saved-value cards and focused editors replace the repeated full policy summary. The [Settings UI spec](OWNER_SETTINGS_UI_SPEC.md) is retained because it describes current supported interaction requirements, not an outstanding delivery plan.

## Retention and erasure

- **Retention (#16, #21, #199, #201):** client SMS uses a last-exchange clock; owner texts use an individual-message clock. Ordinary notes use completed-visit/creation retention. Minimal consent/opt-out evidence and active suppression are separate.
- **Erasure (#186, #189):** explicit client erasure overrides ordinary retention/holds and keeps only approved hashed client-ID/provider-message replay markers. Owner-thread text is not redacted by client erasure and expires individually. Durable erasure fences and send claims prevent late work recreating erased data.

## Documentation consolidation (2026-10-14)

Architecture/current design no longer embeds issue chronology, account IDs, completed authorization packets, dated prices or UI implementation diaries. Useful operational details have focused homes; detailed historical text remains in Git history. See the [documentation index](README.md) for each document's scope and disposition.

## Per-client visit reminder timing (#399; 2026-11-01)

- **Earlier choice (#390–#394 / PR #395):** one owner-configured business schedule in Settings, with enrollment-only client preferences.
- **Replacement:** the owner approved one shared enrollment/timing preference per client, editable through owner/client UI and SMS, to support individual timing and authoritative reminder reads. Remove global timing; preserve existing enrollment and copy saved business timing once, including for unenrolled clients. New profiles start off at one day/10 AM.
- **Boundary:** the [PRD](PRD.md) and [specification](VISIT_REMINDERS_SPEC.md) own current requirements. #400 implements the owner-to-delivery slice before portal and SMS extensions. No catch-up/retry, consent/STOP, or deployment/live-SMS authorization changes.

## Deployment authorization flag (#396; 2026-10-31)

- **Earlier behavior:** every deployment retaining authorized invitation/reminder gates required both the long live-SMS acknowledgment flag and repeated explicit gate parameters.
- **Replacement:** `--live-sms` acknowledges live-SMS authorization for the deployment and permits preserving existing authorized delivery gates. New settings and deliberate parameter/trigger changes still require explicit values. The shorter flag is shared by backend deployment and schedule commands. The owner approved the change to remove repetitive arguments; it does not authorize this code change to deploy or send messages.
