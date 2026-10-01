#!/usr/bin/env bash
# Deploy the backend to the synthetic dev stack: build, create a change set, show
# it, ask for confirmation, execute it, then smoke-test the owner API.
#
# Parameters are never retyped or printed: every stack parameter keeps its live
# value (UsePreviousValue), so NoEcho values such as OwnerNumber stay untouched.
# Only PermissionsBoundaryArn is passed explicitly, read from the live stack.
#
# Schedule guard: before the prompt, the target State of every EventBridge rule the change set
# adds, modifies or replaces is read from the processed template. A parameterized rule's live
# state (events:DescribeRule) is compared with it; the deploy is refused, even with --yes, if it
# would change and its *ScheduleState parameter was not given with --param. Any other rule must
# target DISABLED. A warning is printed on every run when live state and parameter differ.
#
# Usage: scripts/dev/deploy-backend.sh [--yes] [--smoke-only] [--dry-run] [--param Key=Value]...
#                                      [--profile NAME | --no-profile]
#   --yes        Skip the confirmation prompt (CI only, issue #95 rules).
#   --param      Supply a parameter that the live stack does not have yet, or deliberately
#                change one (for example HoldExpiryScheduleState=ENABLED).
#   --smoke-only Run only the owner API smoke test (no build, no change set).
#   --dry-run    Print the commands without calling AWS (read-only identity check only).
set -euo pipefail

# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ASSUME_YES=0
SMOKE_ONLY=0
EXTRA_PARAMS=()

usage() {
  sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
}

parse_common "$@"
set -- "${REMAINING_ARGS[@]+"${REMAINING_ARGS[@]}"}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes) ASSUME_YES=1 ;;
    --smoke-only) SMOKE_ONLY=1 ;;
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
    [[ "${SMOKE_ONLY}" -eq 1 ]] && break
    if ! grep -qx "${key}" <<<"${live_keys}"; then
      supplied=0
      for kv in "${EXTRA_PARAMS[@]+"${EXTRA_PARAMS[@]}"}"; do
        [[ "${kv%%=*}" == "${key}" ]] && supplied=1
      done
      [[ "${supplied}" -eq 1 ]] || die "template parameter ${key} is not on the live stack yet; pass --param ${key}=<value>. For a schedule state parameter (*ScheduleState) pass the schedule's CURRENT live state (see scripts/dev/status.sh), so the deploy does not change it."
    fi
  done
fi

OVERRIDES=("PermissionsBoundaryArn=${BOUNDARY_ARN}")
OVERRIDES+=("${EXTRA_PARAMS[@]+"${EXTRA_PARAMS[@]}"}")

CHANGESET=""
EXECUTING=0
cleanup() {
  # Delete an unexecuted change set on every exit path (decline, Ctrl-C, error, no changes).
  if [[ -n "${CHANGESET}" && "${EXECUTING}" -eq 0 ]]; then
    aws_cli cloudformation delete-change-set --change-set-name "${CHANGESET}" >/dev/null 2>&1 || true
  fi
  return 0
}
trap cleanup EXIT

# Smoke test the owner API on a real GET route of the live business: 401 without a token
# and CORS preflights (allowed from the app origin, not from a foreign origin).
smoke_test() {
  info "Smoke test."
  local api_url business_id app_origin route code failed=0
  api_url="$(stack_output OwnerApiUrl)"
  api_url="${api_url%/}"
  business_id="$(stack_parameter BusinessId)"
  app_origin="$(stack_parameter OwnerAppOrigin)"
  route="${api_url}/v1/owner/businesses/${business_id}/policy"

  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "${route}")"
  if [[ "${code}" == "401" ]]; then
    info "  ok: owner API without a token returns 401"
  else
    info "  FAIL: owner API without a token returned ${code}, expected 401"
    failed=1
  fi

  preflight_allow_origin() {
    curl -sS -o /dev/null -D - --max-time 20 -X OPTIONS \
      -H "Origin: $1" -H "Access-Control-Request-Method: GET" \
      -H "Access-Control-Request-Headers: authorization" \
      "${route}" | tr -d '\r' | awk -F': ' 'tolower($1) == "access-control-allow-origin" {print $2}'
  }
  if [[ "$(preflight_allow_origin "${app_origin}")" == "${app_origin}" ]]; then
    info "  ok: CORS preflight from the app origin is allowed"
  else
    info "  FAIL: CORS preflight from the app origin was not allowed"
    failed=1
  fi
  if [[ -z "$(preflight_allow_origin "https://example.invalid")" ]]; then
    info "  ok: CORS preflight from a foreign origin is not allowed"
  else
    info "  FAIL: CORS preflight from a foreign origin was allowed"
    failed=1
  fi
  [[ "${failed}" -eq 0 ]] || die "smoke test failed"
}

if [[ "${SMOKE_ONLY}" -eq 1 ]]; then
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    info "+ smoke test: GET /v1/owner/businesses/<BusinessId>/policy without a token expects 401; CORS preflights"
    exit 0
  fi
  smoke_test
  exit 0
fi

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
  info "+ take the change set ARN from SAM's output, then aws cloudformation describe-change-set: print action, logical ID, type, replacement"
  info "+ refuse if any parameter not given with --param would differ from the live stack"
  info "+ schedule guard: aws cloudformation get-template --change-set-name <arn> --template-stage Processed gives each added, modified or replaced AWS::Events::Rule its target State (Ref resolved, missing means ENABLED); aws events describe-rule (live State) vs target for the three parameterized rules; a rule without a parameter must target DISABLED; refuse (even with --yes) if it would change without --param <X>ScheduleState"
  info "+ ask y/N (skipped with --yes), then aws cloudformation execute-change-set and wait stack-update-complete"
  info "+ an exit trap deletes the change set unless execution started"
  info "+ smoke test: GET /v1/owner/businesses/<BusinessId>/policy without a token expects 401; CORS preflights"
  exit 0
fi

SAM_LOG="$(mktemp)"
trap 'rm -f "${SAM_LOG}"; cleanup' EXIT
# SAM echoes the parameter overrides it was given; drop that line so no value is printed.
"${SAM_DEPLOY[@]}" 2>&1 | sed '/[Pp]arameter overrides/d' | tee "${SAM_LOG}"

# The change set this run created: the ARN SAM printed.
CHANGESET="$(grep -o 'arn:aws[a-z-]*:cloudformation:[^ ]*:changeSet/[^ ]*' "${SAM_LOG}" | head -n 1 || true)"

if [[ -z "${CHANGESET}" ]]; then
  # Only SAM's explicit no-change message may lead to a "nothing to deploy" success.
  grep -q "No changes to deploy" "${SAM_LOG}" ||
    die "SAM printed no change set ARN and no 'No changes to deploy' message; check the output above and any change set left on ${STACK_NAME}"
  # SAM prints no ARN when there is nothing to deploy; it may leave a FAILED empty change set.
  CHANGESET="$(aws_cli cloudformation list-change-sets --stack-name "${STACK_NAME}" --output json |
    jq -r --argjson start "$((START_EPOCH - 120))" '[.Summaries[]
      | select(.Status == "FAILED")
      | select((.CreationTime[0:19] + "Z" | fromdate) >= $start)] | sort_by(.CreationTime) | last | .ChangeSetId // empty')"
  info "No changes to deploy; the stack is up to date. The empty change set is deleted on exit."
  smoke_test
  exit 0
fi

DESCRIPTION="$(aws_cli cloudformation describe-change-set --change-set-name "${CHANGESET}" --output json)"
CS_STATUS="$(jq -r .Status <<<"${DESCRIPTION}")"
if [[ "${CS_STATUS}" == "FAILED" ]]; then
  reason="$(jq -r '.StatusReason // ""' <<<"${DESCRIPTION}")"
  if grep -qiE "didn't contain changes|No updates are to be performed" <<<"${reason}"; then
    info "No changes to deploy. The empty change set is deleted on exit."
    smoke_test
    exit 0
  fi
  die "change set failed: ${reason}"
fi
[[ "${CS_STATUS}" == "CREATE_COMPLETE" ]] || die "change set is ${CS_STATUS}, not CREATE_COMPLETE"

info ""
info "Change set $(jq -r .ChangeSetName <<<"${DESCRIPTION}")"
{
  printf 'ACTION\tLOGICAL ID\tTYPE\tREPLACEMENT\n'
  jq -r '.Changes[] | .ResourceChange | [.Action, .LogicalResourceId, .ResourceType, (.Replacement // "-")] | @tsv' <<<"${DESCRIPTION}"
} | column -t -s "$(printf '\t')"

COUNT="$(jq '.Changes | length' <<<"${DESCRIPTION}")"
RISKY="$(jq -r '[.Changes[].ResourceChange | select(.Action == "Remove" or .Replacement == "True" or .Replacement == "Conditional") | .LogicalResourceId] | join(", ")' <<<"${DESCRIPTION}")"
info "${COUNT} resource change(s)."
[[ -z "${RISKY}" ]] || info "WARNING: removed or replaced (possibly): ${RISKY}"

# Refuse parameter drift (names only, never values): any parameter not given with --param must
# keep its live value. NoEcho values are masked on both sides, so they compare equal.
LIVE_PARAMS="$(aws_cli cloudformation describe-stacks --stack-name "${STACK_NAME}" --query 'Stacks[0].Parameters' --output json)"
ALLOWED_KEYS="$(printf '%s\n' "${EXTRA_PARAMS[@]+"${EXTRA_PARAMS[@]}"}" | sed 's/=.*//' | jq -R . | jq -sc .)"
CHANGED_PARAMS="$(jq -r --argjson live "${LIVE_PARAMS}" --argjson allowed "${ALLOWED_KEYS}" '
  [.Parameters[] | . as $p
    | select(($allowed | index($p.ParameterKey)) == null)
    | select(($live | map(select(.ParameterKey == $p.ParameterKey)) | first | .ParameterValue) != $p.ParameterValue)
    | .ParameterKey] | join(", ")' <<<"${DESCRIPTION}")"
if [[ -n "${CHANGED_PARAMS}" ]]; then
  die "refusing: parameters would change that were not given with --param (names only): ${CHANGED_PARAMS}"
fi
info "Parameters: unchanged from the live stack."

# Schedule guard: a deploy must never silently change an EventBridge rule's state.
# The target state of every added, modified or replaced rule is read from the change set's
# processed template: a literal State, or a Ref resolved against the change set's parameters
# (the live value, or the --param override). A missing State means ENABLED (EventBridge default).
# - A rule with a *ScheduleState parameter: live vs target; a change needs an explicit --param.
# - Any other rule (outbox dispatch, or a new one): its target must be DISABLED, always.
schedule_parameter() {
  case "$1" in
    HoldExpiryFunctionSweep) printf 'HoldExpiryScheduleState' ;;
    NoteRetentionFunctionDaily) printf 'NoteRetentionScheduleState' ;;
    SmsRetentionFunctionDaily) printf 'SmsRetentionScheduleState' ;;
    *) printf '' ;;
  esac
}
RULE_ROWS="$(jq -r '.Changes[].ResourceChange
  | select(.ResourceType == "AWS::Events::Rule" and (.Action == "Add" or .Action == "Modify" or .Replacement == "True" or .Replacement == "Conditional"))
  | [.Action, .LogicalResourceId, (.PhysicalResourceId // "")] | @tsv' <<<"${DESCRIPTION}")"
if [[ -n "${RULE_ROWS}" ]]; then
  PROCESSED="$(aws_cli cloudformation get-template --stack-name "${STACK_NAME}" --change-set-name "${CHANGESET}" \
    --template-stage Processed --query TemplateBody --output json | jq 'if type == "string" then fromjson else . end')" ||
    die "refusing: cannot read the change set's processed template, so rule target states are unknown."
  info ""
  info "Schedule states (live -> target):"
  SCHEDULE_REFUSED=""
  while IFS=$'\t' read -r action logical physical; do
    [[ -n "${logical}" ]] || continue
    # Resolve the target: literal, Ref to a change-set parameter, or missing (ENABLED).
    target_state="$(jq -r --arg id "${logical}" --argjson cs "${DESCRIPTION}" '
      (.Resources[$id].Properties.State) as $s
      | if $s == null then "ENABLED"
        elif ($s | type) == "string" then $s
        elif ($s | type) == "object" and ($s | keys) == ["Ref"] then
          ([$cs.Parameters[] | select(.ParameterKey == $s.Ref) | .ParameterValue] | first // "UNRESOLVED")
        else "UNRESOLVED" end' <<<"${PROCESSED}")"
    [[ "${target_state}" == "ENABLED" || "${target_state}" == "DISABLED" ]] ||
      die "refusing: cannot resolve the target State of ${logical} (got ${target_state})."
    param="$(schedule_parameter "${logical}")"
    if [[ "${action}" == "Add" || -z "${physical}" ]]; then
      live_state="(new)"
    else
      live_state="$(aws_cli events describe-rule --name "${physical}" --query State --output text)"
    fi
    note=""
    if [[ -z "${param}" ]]; then
      if [[ "${target_state}" != "DISABLED" ]]; then
        note="  (REFUSED: hard-coded rule must stay DISABLED)"
        SCHEDULE_REFUSED="${SCHEDULE_REFUSED:+${SCHEDULE_REFUSED}, }${logical}"
      fi
    elif [[ "${live_state}" != "${target_state}" ]]; then
      explicit=0
      for kv in "${EXTRA_PARAMS[@]+"${EXTRA_PARAMS[@]}"}"; do
        [[ "${kv%%=*}" == "${param}" ]] && explicit=1
      done
      if [[ "${explicit}" -eq 1 ]]; then
        note="  (state change, requested with --param ${param})"
      else
        note="  (REFUSED: state would change without --param ${param})"
        SCHEDULE_REFUSED="${SCHEDULE_REFUSED:+${SCHEDULE_REFUSED}, }${logical}"
      fi
    fi
    info "  ${logical} [${action}]: ${live_state} -> ${target_state}${note}"
  done <<<"${RULE_ROWS}"
  if [[ -n "${SCHEDULE_REFUSED}" ]]; then
    die "refusing: schedule state is not allowed to change: ${SCHEDULE_REFUSED}. A parameterized schedule needs its *ScheduleState parameter passed explicitly with --param (the live state keeps it); a hard-coded rule (outbox dispatch) must target DISABLED and cannot be enabled by a deploy."
  fi
fi

# Every run: warn when a parameterized schedule's live state differs from its parameter value
# (drift, for example after an emergency disable-rule), even if this change set leaves it alone.
while IFS=$'\t' read -r logical physical; do
  param="$(schedule_parameter "${logical}")"
  [[ -n "${param}" ]] || continue
  want="$(jq -r --arg k "${param}" '[.Parameters[] | select(.ParameterKey == $k) | .ParameterValue] | first // ""' <<<"${DESCRIPTION}")"
  have="$(aws_cli events describe-rule --name "${physical}" --query State --output text)"
  if [[ -n "${want}" && "${have}" != "${want}" ]]; then
    info "WARNING: ${logical} is ${have} live but ${param}=${want}. A later deploy that modifies the rule would set it to ${want} (refused unless --param ${param} is passed)."
  fi
done < <(stack_resources AWS::Events::Rule)

if [[ "${COUNT}" -eq 0 ]]; then
  info "The change set has no changes. It is deleted on exit."
  smoke_test
  exit 0
fi

if [[ "${ASSUME_YES}" -ne 1 ]]; then
  [[ -t 0 ]] || die "no terminal for the confirmation prompt; review the change set and pass --yes only under the CI rules."
  read -r -p "Execute this change set on ${STACK_NAME}? [y/N] " answer
  if [[ "${answer}" != "y" && "${answer}" != "Y" ]]; then
    info "Not executed. The change set is deleted on exit."
    exit 1
  fi
fi

info "Executing."
EXECUTING=1
aws_cli cloudformation execute-change-set --change-set-name "${CHANGESET}"
if ! aws_cli cloudformation wait stack-update-complete --stack-name "${STACK_NAME}"; then
  info "Final stack status: $(stack_query 'Stacks[0].StackStatus')"
  die "the stack update did not complete; inspect the stack events."
fi
info "Stack status: $(stack_query 'Stacks[0].StackStatus')"

smoke_test
info "Backend deploy complete."
