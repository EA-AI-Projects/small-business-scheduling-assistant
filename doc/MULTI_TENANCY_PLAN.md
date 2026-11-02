# Shared application multi-tenancy assessment and implementation plan

**Status: Proposed; implementation not started.** Prepared 2026-11-01. This document records the shared-app direction discussed with Enrique and a proposed implementation sequence. It does not authorize deployment, migration of live data, purchases or live SMS. The current application remains deployed and configured for one business per stack.

This is a planning document, not a replacement for the current [Architecture](ARCHITECTURE.md), [Accounts](ACCOUNTS.md), [Scheduling contracts](SCHEDULING_CONTRACTS.md) or [Conversation](CONVERSATION.md). Refresh the assessment against the implementation before starting work. Candidate scope and testing boundaries below still need review before implementation issues are published.

## Assessment

We can serve multiple businesses from one application without creating an AWS account or deploying another copy of the app for each business. In this proposal, a **tenant** is one business and its authorized users, client profiles, calendar, configuration and messaging activity.

The repository already carries `business_id` through much of its domain, persistence and authorization code. Business profiles, policies, scheduling preferences and calendar revisions are business-scoped. Authenticated owner access resolves an active identity link; client access derives the business and client profile from that link. These are useful foundations for a shared app.

The deployed configuration is still business-specific. The frontend requires a build-time business ID. SMS ingress, conversation processing, sending and several scheduled workers use a deployment-time business ID, owner number or Twilio number. Identity storage resolves one business/role link per subject. Some records, including appointment metadata, use identifiers without a business prefix; existing authorization checks and every indirect lookup need review before pooling unrelated businesses.

This is a cross-cutting change to identity context, runtime configuration and operations. It should preserve the scheduling domain and transaction discipline rather than rewrite booking behavior. Adding another business record alone would not establish tenant isolation.

| Approach | Fit for this project | Main tradeoff |
| --- | --- | --- |
| AWS account and stack per business | Optional future isolation tier | More account provisioning, deployment and operational work. |
| Separate stacks in one account | Possible bridge for a few pilots | Duplicated resources and releases; existing environment-only resource names need changes. |
| Shared app and shared storage with enforced business scope | Proposed default for multiple small businesses | Efficient onboarding and releases; isolation must hold across every request, message and background operation. |

Keep development separate from the production environment. The existing dedicated-dev/separate-pilot account direction remains applicable; customer onboarding should not require a new AWS account by default. No infrastructure cost estimate or delivery date is established by this plan.

## Problem and intended experience

Adding a business should become an operator-controlled provisioning task rather than a new application deployment. Each owner should sign into the same application and see only their business. Each client should retain access only to the linked client profile and its business. SMS should reach the correct business even when the same person contacts several businesses.

For example, two businesses can offer the same start time independently. A pending request for one must never reserve time in the other's calendar. A person texting both business numbers must have separate conversation history, offers, consent state and scheduling activity within each business, subject to any broader provider suppression that still applies.

## User stories

1. As an operator, I want to provision a business without deploying another app, so that onboarding stays manageable.
2. As an operator, I want to provision businesses in an inactive state, so that incomplete setup cannot start processing or sending messages.
3. As an operator, I want to link an approved owner to the correct business, so that account access is explicit and auditable.
4. As an owner, I want sign-in to open my business automatically, so that I do not need a business-specific app build.
5. As an owner, I want access limited to my business, so that another owner cannot see or change my records.
6. As an owner, I want my business profile, weekly hours, date exceptions and scheduling preferences kept separate, so that other businesses cannot change my availability.
7. As an owner, I want client profiles and notes isolated, so that private information remains within my business.
8. As an owner, I want pending requests and confirmed visits isolated, so that another business's bookings cannot affect my calendar.
9. As a client, I want my account invitation to grant only its intended access, so that signing in cannot expose another profile or business.
10. As a client, I want calendar and appointment actions scoped to my linked profile, so that I can manage only my own visits.
11. As a client, I want each business number to reach that business, so that my messages cannot be routed to another owner.
12. As a client of two businesses, I want SMS history and stored offers separated, so that an answer for one business cannot accept the other's offer.
13. As an owner, I want outbound texts to use my configured business number, so that clients can recognize and reply to my business.
14. As a client, I want consent, STOP and reminder preference checks to use the correct business context, so that messages respect the applicable permissions and provider restrictions.
15. As an owner, I want visit reminders and booking invitations evaluated in my business timezone, so that another business's settings cannot change their timing.
16. As an operator, I want queued work to retain its originating business, so that retries cannot change who receives a message or which calendar is updated.
17. As an operator, I want delivery callbacks matched to their original messages, so that a callback cannot update another business's delivery record.
18. As an owner, I want client erasure to affect only my business's profile and related records, so that another business's data remains intact.
19. As an operator, I want retention and expiry work to cover every eligible business, so that onboarding cannot leave background maintenance unconfigured.
20. As an operator, I want to pause a business's application activity, so that I can contain a problem without disabling all businesses.
21. As an operator, I want usage and failures attributable to a business without exposing customer content, so that I can diagnose problems and control consumption.
22. As an owner, I want normal service when another business has excessive activity, so that shared infrastructure has bounded interference.
23. As an operator, I want existing records, account links and pending work preserved during migration, so that the original business can continue using the app.
24. As an operator, I want a reviewed rollback procedure, so that an unsuccessful rollout does not lose data or replay committed actions.

## Proposed runtime design

### Business registry and configuration

Introduce a server-owned registry of businesses and their lifecycle state. Keep existing business profiles and scheduling policies as the authorities for their current responsibilities. The registry owns operational configuration: activation state, messaging route references, owner notification destination, feature/send gates and usage controls. Store secret references rather than secret values in ordinary business records or browser configuration.

Begin with an operator command or equivalent controlled provisioning workflow. Make provisioning repeatable and validate required configuration before activation. A public signup flow and an administration portal are outside the initial scope.

Use one configured SMS number per business initially. A number must not resolve to two active businesses. Keep provider account/credential selection behind an adapter so the initial design does not require one Twilio account per business or commit to a particular subaccount arrangement. Provider registration, sender ownership and suppression behavior require validation before onboarding live businesses.

### Web identity and business context

Keep Cognito authentication and server-owned identity links. The first release can preserve one active business/role link per account; serving multiple businesses does not require a person to have several memberships. Multiple memberships, business switching and expanded staff roles are separate product work.

Add an authenticated session response, or extend the existing session boundary, so the owner app obtains its authorized business from the server instead of `NEXT_PUBLIC_BUSINESS_ID`. Client access continues to derive business/client identity from its link. A route parameter, browser state or model argument never grants access.

Use the same frontend deployment and authenticated API for all businesses. Deny missing, revoked, mismatched or inactive access. Check resource ownership even when an identifier is globally unique, and avoid returning another business's record details in failure responses. Scope browser caches and persisted request state by authenticated identity and business; clear them on sign-out or access loss.

### SMS, queues and model context

Resolve an inbound route from provider account and destination-number configuration, then validate the provider signature using the selected trusted configuration. Unverified webhook fields may help select a candidate route but cannot authorize processing. Reject unknown or ambiguous routes. Determine owner/client identity within the verified business, rather than from a global phone lookup.

Persist the resolved business on the receipt. Carry that identity through conversation work, tool dispatch, sent-aware history, stored offers and outbox intents. Treat queue payloads as references to authoritative records; validate business consistency before processing. Model tools receive trusted scope and cannot select another business. Any reusable configuration, prompt or history cache must include business scope and avoid carrying state between invocations.

Resolve sending credentials, sender number, owner destination and business gates from the outbox record's verified business. Match delivery callbacks to the original provider account/message and business; do not trust a caller-supplied business ID. Preserve current consent, suppression, erasure, claim and retry rules.

Business suspension must stop new scheduling actions and ordinary outbound sends after the authoritative gate check. Preserve necessary STOP/suppression handling, delivery reconciliation, expiry and privacy maintenance. Document the treatment of in-flight provider submissions and queued work; suspension cannot recall a text already accepted by the provider. Reactivation must not blindly release expired or newly ineligible work.

### Persistence and background work

Use the existing shared DynamoDB table as the initial storage approach. Audit every key family and operation: direct reads, secondary indexes, idempotency, phone mappings, invitations, account targets, conversation state, history, provider receipts, outbox, audit, retention and erasure. Where a key lacks a business prefix, enforce ownership before reads become visible or mutations occur. Change key shapes only when necessary and include compatibility handling.

Retain per-business calendar revisions and atomic scheduling transactions. The same timeslot may be booked independently by different businesses; competing requests within one business retain the no-double-booking guarantee.

Some workers already discover work through table-wide due indexes; others explicitly select one configured business. Keep the former where valid. Replace fixed-business selection with bounded, paginated discovery of eligible businesses or business-specific work items. Isolate failures and bound work per business so one invalid configuration or busy calendar cannot starve the rest.

Add per-business usage attribution, bounded model/SMS processing and operational controls. Avoid customer content, phone numbers and secrets in logs or metric dimensions. Shared resources still have account/service limits and common failure modes; this proposal does not promise complete performance or infrastructure isolation.

## Change surface

| Component | Proposed change |
| --- | --- |
| Business configuration and provisioning | Add registry, activation checks and auditable operator workflow. |
| Identity resolution and session API | Return authorized business context; preserve current one-link model initially. |
| Frontend configuration and API client | Remove build-time business selection; load context after authentication and scope cached state. |
| SMS ingress and delivery callbacks | Resolve trusted provider/business routes and validate original-message ownership. |
| Conversation worker and tool construction | Replace fixed business/owner values with receipt-scoped configuration. |
| Outbox sender | Resolve sending configuration and gates from authoritative business context. |
| Scheduled workers | Discover all eligible work with bounded processing and failure isolation. |
| DynamoDB adapters | Audit direct and indirect access, enforce ownership and migrate keys only where required. |
| Infrastructure and deployment helpers | Remove business-specific runtime assumptions; distinguish environment configuration from tenant configuration. |
| Observability and documentation | Add business-scoped operational evidence; update current references when behavior is implemented. |

## Phased implementation, if adopted

| Phase | Depends on | Work and exit condition |
| --- | --- | --- |
| 1. Isolation inventory and migration design | Decision to begin implementation | Inventory all tenant-sensitive access and configuration. Confirm test boundaries, activation/suspension semantics, provider routing and compatibility strategy. Produce a reviewable key/access matrix and migration/rollback plan. |
| 2. Registry and runtime configuration | Phase 1 | Add repeatable inactive provisioning and validated activation. Represent the existing business without changing its scheduling behavior. Demonstrate two synthetic configurations with no fallback to a default business. |
| 3. Shared web application | Phase 2 | Resolve owner business after sign-in; remove build-time selection. Prove owner/client isolation, invitation and revocation behavior against shared storage. |
| 4. Shared messaging | Phase 2 and relevant persistence protections from Phase 1 | Route SMS, conversations, outbox and callbacks by verified business context. Prove a shared phone contacting two business numbers cannot mix activity. Preserve consent, STOP and failure-after-commit behavior. |
| 5. Background work and operational controls | Phases 2–4 | Cover all businesses in reminders, invitations, expiry and retention. Verify bounded processing, suspension, usage controls and failure isolation. |
| 6. Compatibility and repository validation | Phases 3–5 | Rehearse migration and rollback on synthetic copies, run retained checks and complete independent review. Update accepted architecture and operational references with measured limits. |
| 7. Authorized rollout | Phase 6 and separate live-action authorization | Prepare backups, cutover, smoke checks and rollback triggers; onboard additional businesses only after deployed isolation checks pass. Provider setup and live SMS require their applicable authorization. |

These are planning stages, not implementation tickets. Break each into reviewable issues through the [team workflow](TEAM_AGREEMENT.md) before scheduling work. The phase table states dependencies; it does not prescribe one large PR per phase.

## Proposed testing boundaries and acceptance

Prefer the highest existing behavior boundaries: authenticated HTTP requests, signed SMS ingress, and queued/scheduled worker entry points. Web requests and asynchronous delivery have different entry points; a single HTTP test would not prove the whole feature. Reuse the existing cross-path workflow tests and DynamoDB Local integration harness rather than create isolated tests for every helper. Confirm these boundaries with Enrique before publishing implementation issues.

Use two synthetic businesses sharing one DynamoDB Local table and scripted provider/model adapters. Assert visible responses, committed state and outbound provider calls. Fake-provider tests establish routing and application behavior, not actual carrier delivery or live provider configuration.

- **Web isolation:** each owner/client can perform permitted actions in their business. Substituting another business, client, visit, invitation or review identifier discloses no data and commits no mutation. Check list routes, direct reads and writes, revoked links and inactive businesses.
- **Calendar independence:** both businesses can reserve the same time; competing writes within one still reject conflicts. Duplicate and stale requests retain current idempotency/version behavior.
- **Messaging isolation:** the same synthetic phone contacts both business numbers. History, tools, stored offers, approval destinations and outgoing sender numbers remain correct. Unknown routes, invalid signatures, mismatched queued references and callbacks cannot cross boundaries.
- **Async recovery:** duplicates, retries, DLQ recovery, model failure and failure after a committed action preserve tenant context and do not replay the action. A configuration cache cannot leak one business's values into another invocation.
- **Maintenance isolation:** reminders use each business timezone and settings. A large or failing business does not prevent later eligible work from being processed. Erasure and retention remove only eligible scoped records, preserving existing replay/suppression evidence rules.
- **Lifecycle gates:** incomplete businesses cannot activate; suspension blocks new actions and sends while preserving mandatory maintenance. Revocation, suspension and consent changes between selection and processing are rechecked before effects, with in-flight limits documented.
- **Migration compatibility:** existing links, profiles, confirmed visits, pending requests, outbox records and replay markers retain meaning. Old queued envelopes are either handled safely or explicitly drained; no guessed/default business is permitted for ambiguous work.

Prior art includes the portal/SMS cross-path tests, identity verification and link tests, delivery/outbox workflow tests, reminder cross-path checks and DynamoDB Local race tests. Extend those seams where possible. Run applicable retained backend, frontend, contract-generation and deployment-template checks after implementation; no test-count target or implementation-mirroring suite is required.

## Migration and rollback

Plan for the original business as the first tenant. Inventory compatibility before changing schema or message envelopes. Prefer additive registry/configuration records and explicit versioned readers over a wholesale key rewrite. If data movement is necessary, make it repeatable, verify counts and relationships, and preserve current privacy/retention requirements.

Rehearse cutover on synthetic data, including pending requests, expiring holds, queued sends, retries and callbacks. Coordinate producers and consumers so mixed application versions cannot route work incorrectly. Keep all existing SMS enablement gates off unless separately authorized; migration does not imply send authorization.

Define rollback eligibility before activating a second business. Once several businesses share storage and queues, a single-business release is not a safe blanket rollback target. Retain a compatible release or prepare an explicit isolation/drain procedure. Do not replay committed scheduling actions or accepted sends to reconstruct state. A paused system with preserved data is preferable to a rollback that silently routes another business's work to the original business.

## Out of scope and later decisions

- Multiple memberships per account, business switching, multiple staff roles and cross-business dashboards.
- Self-service signup, subscription billing, custom domains, branding systems and an operator administration portal.
- Multiple calendars, locations or independently bookable workers within a business.
- Dedicated AWS accounts, tables or queues sold as a customer isolation tier.
- A new agent framework, changes to booking rules, or broader scheduling/conversation cleanup.
- Live provider registration, purchases, deployment and migration execution.

The initial design assumes one business number per business and retains existing account-access cardinality. Provider account/subaccount layout, exact quotas, operational retention of usage data and activation procedures need concrete technical designs before implementation. Any material product tradeoff should be recorded explicitly rather than inferred from this proposal.
