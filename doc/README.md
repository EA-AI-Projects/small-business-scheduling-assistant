# Project documentation

Reference docs describe the current repository; they do not certify live AWS/provider state. Private operator records hold account details, physical resource IDs, mailboxes, phones and secrets. `docs/` is the public SMS policy site, not a second engineering-doc tree.

## Current references

| Document | Owns |
| --- | --- |
| [PRD](PRD.md) | Accepted product requirements, current scope and deferred work. |
| [HLD](HLD.md) | System responsibilities, logical model and workflows. |
| [Architecture](ARCHITECTURE.md) | Runtime modules, storage, authorization and concurrency invariants. |
| [Scheduling contracts](SCHEDULING_CONTRACTS.md) | Domain operations, transitions, counteroffers and duration guarantees. |
| [Conversation](CONVERSATION.md) | Current SMS routing, tools, context, fallbacks and outreach behavior. |
| [Accounts](ACCOUNTS.md) | Operator provisioning, client invitation and recovery process. |
| [Settings UI spec](OWNER_SETTINGS_UI_SPEC.md) | Implemented Settings interaction requirements; retained as a focused UI specification. |
| [Visit reminders spec](VISIT_REMINDERS_SPEC.md) | Accepted per-client reminder design and verification scope; tracked in #399, revising #390–#394 / PR #395. |
| [Team agreement](TEAM_AGREEMENT.md), [agent guides](agents/domain.md), [glossary](../GLOSSARY.md) | Work coordination and shared terminology. |

## Operations and evidence

| Document | Owns |
| --- | --- |
| [AWS infrastructure](PILOT_INFRASTRUCTURE.md) | Setup, configuration, deployment helpers, enablement and send-claim recovery. |
| [AWS deployment roles](AWS_DEPLOYMENT_ROLES.md) | Scoped IAM setup, boundary and known permission/trust limitations. |
| [Cost/rollback/teardown](DEV_STACK_PLAN.md) | Current planning and recovery; former pre-authorization packet retired. |
| [Dev SMS operations](DEV_SMS_PLAN.md) | Current messaging gates, pause/resume and backlog handling; former launch packet retired. |
| [AWS validation](AWS_VALIDATION.md) | Local versus deployed proof, test guards and side effects. |
| [Model evaluation](MODEL_EVAL.md) | Synthetic evaluation instructions. Dated [results](MODEL_EVAL_RESULTS.md) are evidence, not current acceptance gates. |
| [A2P registration](A2P_REGISTRATION.md), [policy site](../docs/README.md) | Registration guidance and public disclosures; verify provider state before submission. |
| [Decision history](DECISION_HISTORY.md) | Selected superseded choices and completed checkpoints, with issue provenance. |

## Proposed design research

These documents describe options for future consideration, not accepted architecture or authorized implementation.

| Document | Owns |
| --- | --- |
| [Agent framework research](AGENT_FRAMEWORK_RESEARCH.md) | Comparison of framework options against the existing SMS harness. |
| [OpenAI Agents SDK assessment and plan](OPENAI_AGENTS_SDK_PLAN.md) | Proposed integration boundary, refactoring scope, phased validation and rollback. |
| [Shared application multi-tenancy plan](MULTI_TENANCY_PLAN.md) | Proposed business isolation, shared runtime configuration, onboarding, phased validation and migration/rollback. |

## Maintenance rules

- Edit the owning reference section to state the final accepted behavior. Remove the superseded wording; do not append another correction to it.
- Record a material change's prior choice, replacement, reason and issue/PR in decision history. Git history and issues retain detailed diffs, discussions and work state.
- Keep requirements separate from implementation details, run commands, evaluation results, personal configuration and progress reports. Link to the owning document instead of copying it.
- A proposal needs a visible proposed status. A completed packet stops presenting prerequisites as outstanding. Keep a focused spec only while it still describes supported behavior.
- When removing a feature/document, fix inbound links and record its disposition. Never treat an old deployment/SMS approval as authorization for a new action.

## Review disposition (2026-10-14)

HLD and Architecture were consolidated into current references. Stale home-size duration, short owner-reply allowlists, owner-only account descriptions, undeployed claims, and planned OIDC deployment were corrected. Deployment checkpoints/old cost totals were retired in favor of history and current runbooks. AWS test and IAM details were moved to focused references; the Settings spec remains useful. Published policy pages retain their actual disclosures/contact and were not changed by this engineering-doc review.
