---
name: ship-issue
description: Take one agent-owned GitHub issue from ready to merged and verified using the project subagents, following doc/TEAM_AGREEMENT.md. Use when the implementation manager starts or resumes work on an issue.
argument-hint: <issue number>
---

Ship issue #$ARGUMENTS. You are the implementation manager. Delegate the steps named below to project subagents; do the steps marked "you" yourself.

## 1. Readiness (you)

- `gh issue view $ARGUMENTS --comments`. Confirm it has an outcome, acceptance checks, and no unmet dependency.
- Check for an open PR or branch for this issue. If work is already underway, resume from the matching step instead of starting over.
- If it needs an owner decision, post the smallest numbered question on the issue, have `board-clerk` set `needs-owner-input`, stop this skill, and move to independent work.
- If Enrique has answered a question since the last check, have `docs-recorder` record the decision before implementation.

## 2. Start

`board-clerk`: set status In Progress, keep `agent-owned`, post a start comment.

## 3. Implement

`implementer`: give it the issue number, the goal, relevant decisions or constraints, and any existing branch. Read its handoff report.
- If it lists open questions, handle them as in step 1. Only continue if the rest of the work stands without the answers.
- If it marked an acceptance check "not met" without a reason, send it back with specifics.

## 4. Validate

`validator` on the PR branch. On failure, send the failure lines to `implementer` (or fix it yourself if trivial) and validate again.

## 5. Independent review

`board-clerk`: review-request comment on the issue. Then `pr-reviewer` on the PR.
- Blocking findings: send them to `implementer` as a fix list, then run `validator` again, then have `pr-reviewer` re-check only those findings. Limit this to two fix rounds. If blocking findings remain after that, stop and report to Enrique with a recommendation.
- Non-blocking findings: fix the cheap ones, and note the rest on the PR as follow-ups.
- Record the resolution on the PR (`gh pr comment`): what changed for each finding, and which checks were rerun.

## 6. Merge (you)

Merge only when all of these hold: the review has no unresolved blocking finding; the local checks pass; `gh pr checks` passes, or you stated that no check runs exist; every acceptance check is met; and no business question is open.

- `gh pr ready <pr>`, then `gh pr merge <pr> --squash --delete-branch`.
- Update any downstream branch that depended on this one.

## 7. Verify and close

- `git fetch origin && git switch --detach origin/main`, then have `validator` run on the merged result.
- `board-clerk`: post a merge comment, close the issue, set status Done.

## 8. Report (you)

If the change is visible to users, send Enrique a checkpoint: what changed in the app, how to see or try it, remaining limitations, and the next step. Otherwise, stay quiet and pick the next ready `agent-owned` issue in dependency order.
