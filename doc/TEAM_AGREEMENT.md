# Human–agent team agreement

This agreement governs work on Smart Scheduling Assistant. Enrique owns business decisions. The implementation manager coordinates the work and may delegate bounded tasks to other agents.

## Where decisions and progress live

- The [GitHub Project board](https://github.com/orgs/EA-AI-Projects/projects/2), issues, and pull requests are the source of truth for work state. An issue states the outcome, acceptance checks, dependencies, and next owner. A pull request shows the proposed change and validation.
- The PRD, HLD, architecture, and scheduling contracts record accepted product and technical decisions. A chat discussion or issue comment that changes a decision must be reflected in the relevant document.
- Chats are for coordination. The dedicated implementation-manager chat runs the project workflow; it does not replace GitHub records.
- Keep questions, issues, documents, and progress updates concise. Use short sentences and only the detail needed to make the outcome, decision, or next action clear.

## Product planning

- Clarify a large requirement with focused questions before splitting it into work. Record accepted product decisions in the relevant documents.
- Propose one parent issue for the overall outcome and small sub-issues for reviewable stages. State dependencies between sub-issues explicitly; parentage alone does not imply order.
- Enrique reviews the requirement and issue breakdown once before the issues are published. After approval, create the issues, link the hierarchy, and add them to the Project board. Routine implementation follows the ownership and merge rules below.

## Ownership and handoff

| Situation | Enrique | Implementation manager and agents |
| --- | --- | --- |
| A business rule or priority is unclear | Answer the numbered questions in the issue, including any constraints or examples. A partial answer is fine. | Mark `needs-owner-input`, ask the smallest specific question, and explain what work it blocks. Continue independent work. |
| Enrique answers an issue | No label, status, or closure action is needed. | Read the answer, confirm which questions it resolves, record the decision in the relevant docs, change the ownership label to `agent-owned` when ready, and resume work. If an answer is incomplete, ask only the remaining question. |
| An issue is ready for implementation | Clarify priorities if asked. | Own the next step, implement in dependency order, and keep the board and issue current. Delegate bounded tasks when useful; review and integrate their results. |
| A change is ready for review | Follow project progress at meaningful user-facing checkpoints; answer a business question only when one is needed. | Automatically assign an independent agent reviewer, address findings, run validation, and advance routine PRs without asking Enrique to approve each one. |
| A routine PR is ready to merge | No per-PR approval is needed. | Merge after independent review, applicable checks, and acceptance criteria pass; verify the merged result and update the issue and board. |
| Deployment, live SMS, purchase, publication, or another consequential external action is proposed | Review the concrete plan and explicitly authorize that action separately. | Prepare the result and its cost, risk, and rollback details before asking. Perform only the authorized action. |

An answer on GitHub is enough to hand work back to the manager. If Enrique answers in chat, the manager records the decision on the issue so the board remains understandable without chat history. Enrique should leave the issue open; the manager closes it after the acceptance checks are met and the change is merged and verified.

## Labels and status

- On each open issue, use exactly one next-owner label: `needs-owner-input` for a specific unanswered question Enrique must resolve, or `agent-owned` for work the manager or an agent can perform. Remove or change a stale label promptly after a handoff.
- Add `blocked` only when a named dependency prevents the next owner from progressing. State the dependency and what will unblock it in the issue. It may coexist with an ownership label.
- `Todo` means unstarted; `In Progress` means implementation or review is underway; `Done` means acceptance checks passed and the merged change was verified. A pending pull request remains `In Progress`.
- The manager links pull requests to issues and updates GitHub at meaningful transitions: start, blocker, review request, merge, and completion. Routine checks without a change do not need comments.

## Pull request review and merge

1. A draft PR means implementation, validation, or independent review is incomplete. When implementation is ready, the manager assigns a reviewer automatically. The reviewer must be a different agent from the implementer and must check the diff against the linked issue and accepted product decisions. If no reviewer is available, keep the PR draft and retry; report a persistent review blocker.
2. The manager addresses review findings, records the review and resolution in the PR discussion, and reruns focused tests, lint/type checks, and applicable GitHub checks. State explicitly when no GitHub check runs exist; local checks are not CI.
3. After review and checks pass, the manager marks a routine PR ready, merges it in dependency order, reconciles downstream branches, verifies the merged result, and then closes the linked issues and marks them Done. Enrique does not need to approve routine PRs or merges. A failed check, unresolved review finding, or unclear business decision prevents merge.
4. Report meaningful user-facing progress as a checkpoint: what changed in the app, how it can be seen or tried, the remaining limitations, and the next step. Do not request a product review or approval for every PR. Ask Enrique only for a material business decision, priority tradeoff, blocker he can resolve, or separate authorization for deployment, live SMS, spending, publication, or another consequential external action.
5. PRs use Enrique's `ealemank` GitHub identity, so he cannot submit a formal GitHub approval on his own PR. The independent agent's review is recorded in the PR discussion; the manager remains accountable for the merge decision.

## Testing strategy for new work

- Do not add isolated unit tests by default. For each feature or fix, choose focused checks at the behavior boundary: a scheduling workflow across actions, an API or adapter integration, or a functional or end-to-end flow. The retained backend and frontend suites cover selected workflows and boundaries; adding functional and end-to-end automation is a later effort (#133).
- Keep a small set of in-process checks for booking, cancellation, rescheduling, expiry, duplicate requests, and similar state changes where one action affects the next. A fake repository or scripted model is acceptable for these checks; describe the boundary and what the check does not cover. See `doc/HLD.md` section 11.
- A PR states which behavior was checked, how it was checked, and any gap that remains. Run applicable retained checks and quality gates. Do not add tests solely to mirror implementation details or meet a test-count target.

## Working rules

1. Work in dependency order, while allowing policy-independent work to proceed when an owner decision is pending. Do not invent a business policy to remove a blocker.
2. Keep changes small enough to review. Each pull request identifies its issue, explains the behavior or document change, and includes focused validation or a reason validation was unavailable.
3. Agents can choose routine technical details within the accepted architecture. Escalate a material product tradeoff, scope change, spending, deployment, live SMS, publication, or other consequential action outside routine PR merge to Enrique with a recommendation and its consequences.
4. The manager reviews delegated work, resolves overlap, and remains accountable for the issue and board state. Subagents do not independently change business policy.
5. When active, the scheduled manager checks for updates in its dedicated chat. It acts on ready agent-owned work, reports meaningful user-facing checkpoints or questions there, and stays quiet when nothing actionable changes. Avoid duplicate implementation if work is already underway.
6. Keep issues small. Work with phases or stages is normally split into one issue per phase or stage, linked from each other. When an issue grows beyond its original outcome, the remaining work moves to new issues instead of extending it.
