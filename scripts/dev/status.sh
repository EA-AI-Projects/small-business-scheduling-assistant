#!/usr/bin/env bash
# Read-only status of the synthetic dev environment: stack status, schedules and
# event source mappings, the current Amplify deployment, alarm states, and the
# month-to-date budget spend.
#
# The deployer role cannot read budgets. The budget line uses the admin profile
# when it is signed in, and is skipped otherwise (or with --no-budget).
#
# Usage: scripts/dev/status.sh [--no-budget] [--dry-run]
#                              [--profile NAME | --no-profile] [--app-id ID]
set -euo pipefail

# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SHOW_BUDGET=1
APP_ID="${AMPLIFY_APP_ID:-}"
BUDGET_NAME="scheduling-dev"

usage() {
  sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
}

parse_common "$@"
set -- "${REMAINING_ARGS[@]+"${REMAINING_ARGS[@]}"}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-budget) SHOW_BUDGET=0 ;;
    --app-id)
      [[ $# -ge 2 ]] || die "--app-id needs a value"
      APP_ID="$2"
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
  shift
done

need_tool aws jq
pin_region
check_identity

if [[ "${DRY_RUN}" -eq 1 ]]; then
  info "+ aws cloudformation describe-stacks --stack-name ${STACK_NAME} --query Stacks[0].StackStatus"
  info "+ aws cloudformation list-stack-resources, then aws events describe-rule for each AWS::Events::Rule"
  info "+ aws lambda get-event-source-mapping --uuid <id> for each AWS::Lambda::EventSourceMapping"
  info "+ aws amplify get-branch / get-job for ${AMPLIFY_APP_NAME}/${AMPLIFY_BRANCH}"
  info "+ aws cloudwatch describe-alarms --alarm-name-prefix ${STACK_NAME}-"
  [[ "${SHOW_BUDGET}" -eq 0 ]] || info "+ aws --profile ${ADMIN_PROFILE} budgets describe-budget --budget-name ${BUDGET_NAME}"
  exit 0
fi

info "== Stack"
info "${STACK_NAME}: $(stack_query 'Stacks[0].StackStatus')"

info ""
info "== Schedules (EventBridge rules)"
while IFS=$'\t' read -r logical physical; do
  [[ -n "${logical}" ]] || continue
  printf '%-34s %s\n' "${logical}" "$(aws_cli events describe-rule --name "${physical}" --query State --output text)"
done < <(stack_resources AWS::Events::Rule)

info ""
info "== Event source mappings"
while IFS=$'\t' read -r logical physical; do
  [[ -n "${logical}" ]] || continue
  printf '%-34s %s\n' "${logical}" "$(aws_cli lambda get-event-source-mapping --uuid "${physical}" --query State --output text)"
done < <(stack_resources AWS::Lambda::EventSourceMapping)

info ""
info "== Owner app deployment"
if [[ -z "${APP_ID}" ]]; then
  origin="$(stack_parameter OwnerAppOrigin)"
  if [[ "${origin}" =~ ^https://${AMPLIFY_BRANCH}\.([a-z0-9]+)\.amplifyapp\.com$ ]]; then
    APP_ID="${BASH_REMATCH[1]}"
  fi
fi
if [[ -z "${APP_ID}" ]]; then
  info "unknown (cannot derive the Amplify app ID; pass --app-id)"
elif ! job_id="$(aws_cli amplify get-branch --app-id "${APP_ID}" --branch-name "${AMPLIFY_BRANCH}" --query branch.activeJobId --output text 2>/dev/null)"; then
  info "unavailable (the profile has no Amplify access: apply the AmplifyAppId role-stack change)"
elif [[ -z "${job_id}" || "${job_id}" == "None" ]]; then
  info "${AMPLIFY_APP_NAME}/${AMPLIFY_BRANCH}: no deployment yet"
else
  aws_cli amplify get-job --app-id "${APP_ID}" --branch-name "${AMPLIFY_BRANCH}" --job-id "${job_id}" --output json |
    jq -r --arg app "${AMPLIFY_APP_NAME}" --arg branch "${AMPLIFY_BRANCH}" \
      '"\($app)/\($branch): job \(.job.summary.jobId) \(.job.summary.status), started \(.job.summary.startTime)"'
fi

info ""
info "== Alarms"
aws_cli cloudwatch describe-alarms --alarm-name-prefix "${STACK_NAME}-" --output json |
  jq -r '.MetricAlarms[] | [.StateValue, .AlarmName] | @tsv' | sort | awk -F'\t' '{printf "%-18s %s\n", $1, $2}'

if [[ "${SHOW_BUDGET}" -eq 1 ]]; then
  info ""
  info "== Budget (month to date)"
  if budget="$(aws --profile "${ADMIN_PROFILE}" --region us-east-1 budgets describe-budget \
    --account-id "${EXPECTED_ACCOUNT}" --budget-name "${BUDGET_NAME}" --output json 2>/dev/null)"; then
    jq -r '.Budget | "actual \(.CalculatedSpend.ActualSpend.Amount) \(.CalculatedSpend.ActualSpend.Unit) of \(.BudgetLimit.Amount) \(.BudgetLimit.Unit) limit"' <<<"${budget}"
  else
    info "unavailable (sign in with: aws sso login --profile ${ADMIN_PROFILE}); use --no-budget to skip"
  fi
fi
