#!/usr/bin/env bash
# Deploy the backend to the synthetic dev stack: build, create a change set, show
# it, ask for confirmation, execute it, then smoke-test the owner API.
#
# Parameters are never retyped or printed: every stack parameter keeps its live
# value (UsePreviousValue), so NoEcho values such as OwnerNumber stay untouched.
# Only PermissionsBoundaryArn is passed explicitly, read from the live stack.
#
# Usage: scripts/dev/deploy-backend.sh [--yes] [--dry-run] [--param Key=Value]...
#                                      [--profile NAME | --no-profile]
#   --yes        Skip the confirmation prompt (CI only, issue #95 rules).
#   --param      Supply a parameter that the live stack does not have yet.
#   --dry-run    Print the commands without calling AWS (read-only identity check only).
set -euo pipefail

# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ASSUME_YES=0
EXTRA_PARAMS=()

usage() {
  sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
}

parse_common "$@"
set -- "${REMAINING_ARGS[@]+"${REMAINING_ARGS[@]}"}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes) ASSUME_YES=1 ;;
    --param)
      [[ $# -ge 2 && "$2" == *=* ]] || die "--param needs Key=Value"
      EXTRA_PARAMS+=("$2")
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

need_tool aws sam jq curl
pin_region
cd "${REPO_ROOT}"
check_identity

template_parameter_keys() {
  awk '/^Parameters:/ {on=1; next} /^[A-Za-z]/ {on=0} on && /^  [A-Za-z0-9]+:[[:space:]]*$/ {sub(/^  /, ""); sub(/:.*/, ""); print}' template.yaml
}

if [[ "${DRY_RUN}" -eq 1 ]]; then
  ROLE_ARN="LIVE_STACK_ROLE_ARN"
  BOUNDARY_ARN="LIVE_STACK_PERMISSIONS_BOUNDARY_ARN"
else
  ROLE_ARN="$(stack_query 'Stacks[0].RoleARN')"
  [[ -n "${ROLE_ARN}" && "${ROLE_ARN}" != "None" ]] || die "the live stack has no CloudFormation execution role"
  BOUNDARY_ARN="$(stack_parameter PermissionsBoundaryArn)"

  # A parameter added to template.yaml has no previous value; it must be supplied once.
  live_keys="$(stack_query 'Stacks[0].Parameters[].ParameterKey' | tr '\t' '\n')"
  for key in $(template_parameter_keys); do
    if ! grep -qx "${key}" <<<"${live_keys}"; then
      supplied=0
      for kv in "${EXTRA_PARAMS[@]+"${EXTRA_PARAMS[@]}"}"; do
        [[ "${kv%%=*}" == "${key}" ]] && supplied=1
      done
      [[ "${supplied}" -eq 1 ]] || die "template parameter ${key} is not on the live stack yet; pass --param ${key}=<value>"
    fi
  done
fi

OVERRIDES=("PermissionsBoundaryArn=${BOUNDARY_ARN}")
OVERRIDES+=("${EXTRA_PARAMS[@]+"${EXTRA_PARAMS[@]}"}")

START_EPOCH="$(date +%s)"

info "Building."
run sam build

info "Creating the change set (not executing it)."
SAM_DEPLOY=(sam deploy
  --stack-name "${STACK_NAME}" --region "${EXPECTED_REGION}"
  --role-arn "${ROLE_ARN}"
  --capabilities CAPABILITY_IAM
  --no-execute-changeset --no-confirm-changeset --no-fail-on-empty-changeset
  --s3-bucket "${ARTIFACT_BUCKET}" --s3-prefix "${ARTIFACT_PREFIX}"
  --parameter-overrides "${OVERRIDES[@]}")
[[ -z "${PROFILE}" ]] || SAM_DEPLOY+=(--profile "${PROFILE}")

if [[ "${DRY_RUN}" -eq 1 ]]; then
  show_cmd "${SAM_DEPLOY[@]}"
  info "+ (all other parameters keep their live values via UsePreviousValue)"
  info "+ aws cloudformation list-change-sets / describe-change-set: print action, logical ID, type, replacement"
  info "+ ask y/N (skipped with --yes), then aws cloudformation execute-change-set and wait stack-update-complete"
  info "+ smoke test: GET owner API without a token expects 401; CORS preflight from the app origin"
  exit 0
fi

# SAM echoes the parameter overrides it was given; drop that line so no value is printed.
"${SAM_DEPLOY[@]}" 2>&1 | sed '/[Pp]arameter overrides/d'

# Find the change set this run created (newest, created at or after the start).
CHANGESET_JSON="$(aws_cli cloudformation list-change-sets --stack-name "${STACK_NAME}" --output json |
  jq -c --argjson start "$((START_EPOCH - 120))" '[.Summaries[]
    | select((.CreationTime[0:19] + "Z" | fromdate) >= $start)] | sort_by(.CreationTime) | last // empty')"

if [[ -z "${CHANGESET_JSON}" ]]; then
  info "No change set was created: the stack is already up to date."
  exit 0
fi
CHANGESET="$(jq -r .ChangeSetName <<<"${CHANGESET_JSON}")"
CS_STATUS="$(jq -r .Status <<<"${CHANGESET_JSON}")"

if [[ "${CS_STATUS}" == "FAILED" ]]; then
  reason="$(jq -r '.StatusReason // ""' <<<"${CHANGESET_JSON}")"
  if grep -qiE "didn't contain changes|No updates are to be performed" <<<"${reason}"; then
    info "No changes to deploy. Deleting the empty change set."
    aws_cli cloudformation delete-change-set --stack-name "${STACK_NAME}" --change-set-name "${CHANGESET}"
    exit 0
  fi
  die "change set ${CHANGESET} failed: ${reason}"
fi
[[ "${CS_STATUS}" == "CREATE_COMPLETE" ]] || die "change set ${CHANGESET} is ${CS_STATUS}, not CREATE_COMPLETE"

DESCRIPTION="$(aws_cli cloudformation describe-change-set --stack-name "${STACK_NAME}" --change-set-name "${CHANGESET}" --output json)"

info ""
info "Change set ${CHANGESET}"
{
  printf 'ACTION\tLOGICAL ID\tTYPE\tREPLACEMENT\n'
  jq -r '.Changes[] | .ResourceChange | [.Action, .LogicalResourceId, .ResourceType, (.Replacement // "-")] | @tsv' <<<"${DESCRIPTION}"
} | column -t -s "$(printf '\t')"

COUNT="$(jq '.Changes | length' <<<"${DESCRIPTION}")"
RISKY="$(jq -r '[.Changes[].ResourceChange | select(.Action == "Remove" or .Replacement == "True" or .Replacement == "Conditional") | .LogicalResourceId] | join(", ")' <<<"${DESCRIPTION}")"
info "${COUNT} resource change(s)."
[[ -z "${RISKY}" ]] || info "WARNING: removed or replaced (possibly): ${RISKY}"

# Safety net: report which parameters would change (names only, never values).
# NoEcho values are masked on both sides, so they compare equal.
LIVE_PARAMS="$(aws_cli cloudformation describe-stacks --stack-name "${STACK_NAME}" --query 'Stacks[0].Parameters' --output json)"
CHANGED_PARAMS="$(jq -r --argjson live "${LIVE_PARAMS}" '
  [.Parameters[] | . as $p
    | select(($live | map(select(.ParameterKey == $p.ParameterKey)) | first | .ParameterValue) != $p.ParameterValue)
    | .ParameterKey] | join(", ")' <<<"${DESCRIPTION}")"
if [[ -n "${CHANGED_PARAMS}" ]]; then
  info "Parameters that differ from the live stack (names only): ${CHANGED_PARAMS}"
else
  info "Parameters: unchanged from the live stack."
fi

if [[ "${COUNT}" -eq 0 ]]; then
  info "The change set has no changes. Deleting it."
  aws_cli cloudformation delete-change-set --stack-name "${STACK_NAME}" --change-set-name "${CHANGESET}"
  exit 0
fi

if [[ "${ASSUME_YES}" -ne 1 ]]; then
  [[ -t 0 ]] || die "no terminal for the confirmation prompt; review the change set and pass --yes only under the CI rules."
  read -r -p "Execute this change set on ${STACK_NAME}? [y/N] " answer
  if [[ "${answer}" != "y" && "${answer}" != "Y" ]]; then
    info "Not executed. Deleting the change set."
    aws_cli cloudformation delete-change-set --stack-name "${STACK_NAME}" --change-set-name "${CHANGESET}"
    exit 1
  fi
fi

info "Executing."
aws_cli cloudformation execute-change-set --stack-name "${STACK_NAME}" --change-set-name "${CHANGESET}"
if ! aws_cli cloudformation wait stack-update-complete --stack-name "${STACK_NAME}"; then
  info "Final stack status: $(stack_query 'Stacks[0].StackStatus')"
  die "the stack update did not complete; inspect the stack events."
fi
info "Stack status: $(stack_query 'Stacks[0].StackStatus')"

info "Smoke test."
API_URL="$(stack_output OwnerApiUrl)"
API_URL="${API_URL%/}"
APP_ORIGIN="$(stack_parameter OwnerAppOrigin)"
FAILED=0

code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "${API_URL}/v1/owner/appointments")"
if [[ "${code}" == "401" ]]; then
  info "  ok: owner API without a token returns 401"
else
  info "  FAIL: owner API without a token returned ${code}, expected 401"
  FAILED=1
fi

preflight_allow_origin() {
  curl -sS -o /dev/null -D - --max-time 20 -X OPTIONS \
    -H "Origin: $1" -H "Access-Control-Request-Method: GET" \
    -H "Access-Control-Request-Headers: authorization" \
    "${API_URL}/v1/owner/appointments" | tr -d '\r' | awk -F': ' 'tolower($1) == "access-control-allow-origin" {print $2}'
}
if [[ "$(preflight_allow_origin "${APP_ORIGIN}")" == "${APP_ORIGIN}" ]]; then
  info "  ok: CORS preflight from the app origin is allowed"
else
  info "  FAIL: CORS preflight from the app origin was not allowed"
  FAILED=1
fi
if [[ -z "$(preflight_allow_origin "https://example.invalid")" ]]; then
  info "  ok: CORS preflight from a foreign origin is not allowed"
else
  info "  FAIL: CORS preflight from a foreign origin was allowed"
  FAILED=1
fi
[[ "${FAILED}" -eq 0 ]] || die "smoke test failed"
info "Backend deploy complete."
