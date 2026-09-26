# Human–agent team agreement

This agreement governs work on the Small Business Scheduling Assistant. Enrique owns business decisions. The implementation manager coordinates the work and may delegate bounded tasks to other agents.

## Where decisions and progress live

- The [GitHub Project board](https://github.com/orgs/EA-AI-Projects/projects/2), issues, and pull requests are the source of truth for work state. An issue states the outcome, acceptance checks, dependencies, and next owner. A pull request shows the proposed change and validation.
- The PRD, HLD, architecture, and scheduling contracts record accepted product and technical decisions. A chat discussion or issue comment that changes a decision must be reflected in the relevant document.
- Chats are for coordination. The dedicated implementation-manager chat runs the project workflow; it does not replace GitHub records.

## Ownership and handoff

| Situation | Enrique | Implementation manager and agents |
| --- | --- | --- |
| A business rule or priority is unclear | Answer the numbered questions in the issue, including any constraints or examples. A partial answer is fine. | Mark `needs-owner-input`, ask the smallest specific question, and explain what work it blocks. Continue independent work. |
| Enrique answers an issue | No label, status, or closure action is needed. | Read the answer, confirm which questions it resolves, record the decision in the relevant docs, change the ownership label to `agent-owned` when ready, and resume work. If an answer is incomplete, ask only the remaining question. |
| An issue is ready for implementation | Clarify priorities if asked. | Own the next step, implement in dependency order, and keep the board and issue current. Delegate bounded tasks when useful; review and integrate their results. |
| A change is ready for product review | Review the described behavior and answer any requested product questions. | Complete independent agent review and validation first, mark the PR ready, then explain the change, risks, and exact merge order. |
| A merge or other external action needs approval | Explicitly authorize each merge in a PR comment or the manager chat; authorize a deployment, purchase, or other irreversible action separately. | Prepare a concrete, reviewable result first. Perform only the authorized action and record the outcome. |

An answer on GitHub is enough to hand work back to the manager. If Enrique answers in chat, the manager records the decision on the issue so the board remains understandable without chat history. Enrique should leave the issue open; the manager closes it after the acceptance checks are met and the change is merged and verified.

## Labels and status

- On each open issue, use exactly one next-owner label: `needs-owner-input` for a specific unanswered question Enrique must resolve, or `agent-owned` for work the manager or an agent can perform. Remove or change a stale label promptly after a handoff.
- Add `blocked` only when a named dependency prevents the next owner from progressing. State the dependency and what will unblock it in the issue. It may coexist with an ownership label.
- `Todo` means unstarted; `In Progress` means implementation or review is underway; `Done` means acceptance checks passed and the merged change was verified. A pending pull request remains `In Progress`.
- The manager links pull requests to issues and updates GitHub at meaningful transitions: start, blocker, review request, merge, and completion. Routine checks without a change do not need comments.

## Pull request review and merge

1. A draft PR means agent implementation or validation is incomplete. Do not ask Enrique to review or merge a draft.
2. Before marking a PR ready, have an independent agent review its diff against the linked issue acceptance checks. The manager addresses findings, records that review in a PR comment, and reruns focused tests, lint/type checks, and any GitHub checks. State explicitly when no GitHub check runs exist; local checks are not CI.
3. Once the independent review and validation pass, the manager marks the PR ready and gives Enrique a concise product/behavior review request, material risks, and the exact stack order. Enrique reviews business behavior rather than serving as the only code reviewer.
4. Enrique explicitly authorizes **each** merge in a PR comment or the manager chat. The manager performs the merge after that authorization, reconciles downstream branches and bases, verifies the merged result, and only then closes the linked issues and marks them Done. For the current stack the order is [#8](https://github.com/EA-AI-Projects/small-business-scheduling-assistant/pull/8) → [#9](https://github.com/EA-AI-Projects/small-business-scheduling-assistant/pull/9) → [#10](https://github.com/EA-AI-Projects/small-business-scheduling-assistant/pull/10).
5. PRs use Enrique's `ealemank` GitHub identity, so he cannot submit a formal GitHub approval on his own PR. His PR comment or manager-chat authorization is the approval record. The independent agent's review is recorded in the PR discussion.

At this agreement's adoption, PRs #8–#10 have no GitHub check runs. Report local tests and checks by name; do not describe them as passing CI.

## Working rules

1. Work in dependency order, while allowing policy-independent work to proceed when an owner decision is pending. Do not invent a business policy to remove a blocker.
2. Keep changes small enough to review. Each pull request identifies its issue, explains the behavior or document change, and includes focused validation or a reason validation was unavailable.
3. Agents can choose routine technical details within the accepted architecture. Escalate a material product tradeoff, scope change, cost, or irreversible action to Enrique with a recommendation and its consequences.
4. The manager reviews delegated work, resolves overlap, and remains accountable for the issue and board state. Subagents do not independently change business policy.
5. The scheduled manager checks for updates every minute in its dedicated chat. It acts on ready agent-owned work, reports meaningful progress or questions there, and stays quiet when nothing actionable changes. Avoid duplicate implementation if work is already underway.
