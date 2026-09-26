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
| A change is ready for review | Review the linked pull request and answer any requested product questions. | Explain the change, validation, remaining risks, and exact review action. Address review comments and failing checks. |
| An external action needs approval | Authorize the specific merge, deployment, purchase, or other irreversible action when satisfied. | Prepare a concrete, reviewable result first. Perform the action after authorization and record the outcome. |

An answer on GitHub is enough to hand work back to the manager. If Enrique answers in chat, the manager records the decision on the issue so the board remains understandable without chat history. Enrique should leave the issue open; the manager closes it after the acceptance checks are met and the change is merged and verified.

## Labels and status

- On each open issue, use exactly one next-owner label: `needs-owner-input` for a specific unanswered question Enrique must resolve, or `agent-owned` for work the manager or an agent can perform. Remove or change a stale label promptly after a handoff.
- Add `blocked` only when a named dependency prevents the next owner from progressing. State the dependency and what will unblock it in the issue. It may coexist with an ownership label.
- `Todo` means unstarted; `In Progress` means implementation or review is underway; `Done` means acceptance checks passed and the merged change was verified. A pending pull request remains `In Progress`.
- The manager links pull requests to issues and updates GitHub at meaningful transitions: start, blocker, review request, merge, and completion. Routine checks without a change do not need comments.

## Working rules

1. Work in dependency order, while allowing policy-independent work to proceed when an owner decision is pending. Do not invent a business policy to remove a blocker.
2. Keep changes small enough to review. Each pull request identifies its issue, explains the behavior or document change, and includes focused validation or a reason validation was unavailable.
3. Agents can choose routine technical details within the accepted architecture. Escalate a material product tradeoff, scope change, cost, or irreversible action to Enrique with a recommendation and its consequences.
4. The manager reviews delegated work, resolves overlap, and remains accountable for the issue and board state. Subagents do not independently change business policy.
5. The scheduled manager checks for updates every minute in its dedicated chat. It acts on ready agent-owned work, reports meaningful progress or questions there, and stays quiet when nothing actionable changes. Avoid duplicate implementation if work is already underway.
