# OpenAI Agents SDK assessment and implementation plan

**Status: Proposed; deferred implementation.** Prepared 2026-10-31. Enrique authorized saving and pushing this documentation. Adoption, implementation, billable model evaluation, deployment and live SMS are not authorized by this plan. The application continues to use its existing harness.

This document records the project-specific assessment and a possible implementation sequence. The broader [agent framework research](AGENT_FRAMEWORK_RESEARCH.md) compares alternatives. [Conversation](CONVERSATION.md) and [Architecture](ARCHITECTURE.md) remain the current behavior references. Refresh this assessment against the code and selected SDK release before starting work.

## Assessment

OpenAI Agents SDK is a plausible replacement for our custom model/tool runner. It runs inside the application: our code still owns deployment, tools, storage and business approval decisions. The SDK provides agent definitions and the model → tool → model loop. [SDK overview](https://developers.openai.com/api/docs/guides/agents/sdk), [runner](https://developers.openai.com/api/docs/guides/agents/running-agents)

The architectural change is small; implementation and regression verification are moderate. The existing `OwnerToolLoop` and `ClientToolLoop` protocols in [conversation.py](../backend/scheduling/domain/conversation.py) already accept trusted tool dispatchers; the client loop also accepts a final-text validator. These are the proposed replacement boundary. Preserve those interfaces initially so the domain does not depend on SDK classes.

Expected value is less custom HTTP/response parsing and loop maintenance, plus structured diagnostics. Tracing can record model calls, tool results and guardrail activity; its configuration must preserve current privacy and retention requirements. Neither lower cost, faster replies nor better model decisions has been demonstrated. [Observability](https://developers.openai.com/api/docs/guides/agents/integrations-observability)

## Proposed runtime design

For each verified inbound receipt, the conversation service would load eligible sent-aware history and invoke a bounded SDK run. Application code would select one of two agent configurations based on the verified role:

| Configuration | Tool surface |
| --- | --- |
| Client scheduling | Business information, own bookings, availability, booking/reschedule requests, cancellation preparation/confirmation, counteroffer responses and reminder preference. |
| Owner scheduling | Business information, pending requests, calendar, approval/decline and counteroffer drafting/sending/cancellation. |

An SDK `Agent` packages instructions, model settings and tools. Initially reuse current instructions, model choice and strict argument schemas. Register thin SDK function-tool wrappers that call the existing dispatchers. The model must not select its own role, business or client identity. SDK-local context may carry trusted dependencies; only the existing approved context should enter model input. [Agent definitions](https://developers.openai.com/api/docs/guides/agents/define-agents)

Example: a client asks about Friday → the client agent calls the wrapped availability tool → existing domain logic reads the calendar and stores an offer → the runner obtains final wording → existing validation and outbox handling complete the receipt. A later acceptance starts another run against the persisted offer.

Keep application-built history authoritative. Do not automatically persist every SDK-generated message as a sent SMS. Offers, pending requests and appointment versions remain domain records. Continue the existing provider-storage setting and avoid adding a provider-managed conversation as another history authority. The SDK supports multiple continuation strategies, but their storage semantics are different. [History strategies](https://developers.openai.com/api/docs/guides/agents/running-agents)

Keep owner approval as a pending scheduling request followed by a separately verified owner action. SDK approval interruptions are available, but adopting them here would create another state to coordinate with expiry, versions and the owner web app. They are outside the initial scope. [SDK approvals](https://developers.openai.com/api/docs/guides/agents/guardrails-approvals)

## Change surface

| Component | Proposed change |
| --- | --- |
| [OpenAI adapter](../backend/scheduling/adapters/openai_messages.py) | Replace `run_owner_loop` and `run_client_loop` internals with an SDK-backed implementation; extract a runner module if it improves ownership. Retain the existing facade initially. |
| Tool schemas and registration | Reuse strict schemas through SDK function tools first. Typed wrapper generation is optional later; do not broaden accepted arguments. |
| Conversation integration | Keep existing protocols, dispatchers, final-text checks, exact-command routing, action tracking and deterministic recovery. Normalize SDK failures into the existing failure contract. |
| [Client tools](../backend/scheduling/domain/client_tools.py) and owner callbacks | Reuse existing authorization and domain calls; add only the adapter boundary required to invoke them. |
| Worker and local entry points | Adjust construction and sync/async integration only as required. Check the local FastAPI event-loop context before choosing a synchronous runner API. |
| [Python project dependencies](../backend/pyproject.toml) and [SAM dependencies](../backend/requirements.txt) | Add a reviewed SDK version/range consistently; verify Python 3.12, packaging and transitive dependencies. |
| Adapter tests and synthetic evaluations | Replace raw `urlopen` mocks for migrated loops with scripted SDK model responses. Retain domain workflows and evaluation entry points where possible. |
| Intent extraction, preference interpretation and fallback drafting | Leave their existing single-call implementations initially. A separate decision can consider later consolidation. |

Availability, holds, lifecycle transactions, DynamoDB records, SMS ingress, queues, outbox delivery and frontend behavior need no planned redesign. Do not remove code from these areas merely because it resembles SDK functionality.

Preliminary planning range: a few runtime modules and roughly 8–15 files including tests, configuration and documentation; several hundred lines rewritten or reorganized. This is not a measured diff or delivery commitment. The current adapter is about 813 lines and includes single-call responsibilities beyond the two loops. Most conversation/domain complexity would remain. Re-estimate after a compatibility experiment; verification may take more effort than runner implementation.

## Required behavior preservation

- Verified role and sender scope determine tool access and record identity; model arguments cannot override them.
- Preserve serial tools, a maximum of five model calls and three tool calls, and the client/owner 30/36-second budgets. Count the final rewrite within the budget. SDK turn limits alone do not establish every bound; account for provider retries, timeouts and malformed calls explicitly.
- Preserve one effective action per receipt and the prohibition on booking in the initial message that creates an offer. Existing stored-offer, second-message cancellation, version, expiry and conflict checks remain authoritative.
- Preserve final-text validation: GSM-7/size rules and the client's successful-tool-result date/time/reference checks. Allow one rewrite with tools disabled, then use the existing fallback. Do not assume a generic output guardrail supplies this recovery sequence.
- A refused or committed action must not be attempted again because final wording fails. A timeout or cancellation must not allow a late tool operation to escape receipt handling or race with fallback work; establish this for the chosen sync/async integration.
- Preserve receipt idempotency, sent-aware history, consent/STOP and erasure checks, and transactional outbox behavior. Trace payloads must not silently introduce another unrestricted copy of SMS history.

These conditions come from the current [conversation contract](CONVERSATION.md). SDK guardrails can support checks, but cannot replace authorization and transaction checks next to side effects. [Guardrail boundaries](https://developers.openai.com/api/docs/guides/agents/guardrails-approvals)

## Phased implementation, if adopted later

| Phase | Depends on | Work and exit condition |
| --- | --- | --- |
| 1. Compatibility experiment | Explicit decision to begin implementation | Select an SDK release; use fake models/tools to verify strict schemas, serial execution, total-call and elapsed-time budgets, no-tool rewrite, exception mapping, sync/async behavior and cancellation. Confirm packaging and trace controls. Stop and reassess if these require a domain rewrite. |
| 2. Owner loop | Phase 1 | Implement the SDK runner behind the owner protocol. Preserve calendar reads, approvals/declines, counteroffer preview/send guards and failure-after-commit behavior. Pass focused adapter and owner workflow tests. |
| 3. Client loop | Phase 2 | Reuse the runner with client tools and reply checking. Verify booking, stored offers, cancellation, rescheduling, counteroffers and reminders, including deterministic fallback and prompt cleanup. |
| 4. Repository integration and review | Phase 3 | Verify worker/local wiring, dependency builds, retained backend checks and synthetic evaluations. Review the final diff independently under the team agreement. Document measured benefits, gaps and rollback before adoption. |
| 5. Authorized evaluation and rollout | Phase 4; separate authorization for external effects | If authorized, run fictional live-model scenarios and compare against the existing harness. Deployment and live SMS require their own applicable authorization and validation. A successful local test is not deployed proof. |

These are candidate implementation stages, not published issues or scheduled work. If implementation is selected, propose a parent issue and dependent, reviewable sub-issues through the [team workflow](TEAM_AGREEMENT.md); get the breakdown approved before publishing issues.

## Validation and adoption decision

Use scripted models first to compare conversation outcomes and state changes. Cover owner decisions and counteroffer delivery, client booking/cancel/move/reminders, ambiguous references, stale offers, conflicts, duplicate receipts, STOP during processing, malformed/excess tool calls, model timeout, invalid final wording and failure after commit. Include concurrent work and late completion around timeouts. Assert committed state and queued intents as well as reply text.

Run retained backend tests, Ruff and mypy after implementation, plus applicable dependency/build and repository checks. Run frontend checks only when the actual change or CI requires them. No new implementation-mirroring test suite is required. Fake-model results prove adapter and workflow behavior, not model interpretation quality or actual provider delivery.

For any separately authorized live-model comparison, measure scenario completion, calls, tokens, latency, validation/fallback rates and trace usefulness. Evaluate Lambda dependency footprint and cold-start impact separately. Adopt only if the SDK preserves the contracts and offers enough maintenance or diagnostic value to justify its dependency and integration cost. Record remaining semantic limitations; the SDK cannot prove all model-written claims true.

## Rollback and scope control

Keep the change free of data/schema migrations. During evaluation, retain a selectable legacy implementation or a clearly reversible commit so a future authorized deployment can restore the previous runner using the normal deployment process. Choose the rollback mechanism before rollout; do not leave two active implementations indefinitely. Preserve receipt keys, pending requests and saved outbox intents across rollback, and never replay committed actions to regenerate wording.

No multi-agent handoffs, SDK approval persistence, MCP adoption, autonomous background agent, frontend redesign or broad conversation cleanup is part of this plan. Any of those needs a separate demonstrated requirement.
