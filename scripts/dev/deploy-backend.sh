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
# Four rules are parameterized: HoldExpiry, NoteRetention, SmsRetention and OutboxDispatch.
#
# Sender mapping guard: the SmsSender outbox queue event source mapping works the same way. Its
# live State (lambda get-event-source-mapping) is compared with its target in the change set; a
# change is refused without --param SmsSenderMappingState=ENABLED|DISABLED. Turning outbox dispatch
# or the sender mapping on stays under live-SMS authorization (#91). The conversation mapping is
# governed by SmsConversationRequested and is not guarded here.
#
# Private values: OwnerNumber, TwilioAccountSid and TwilioBusinessNumber
# are never passed with --param (it would leave them in shell history). Use --prompt-param <Key>:
# the value is read with a hidden prompt, validated (E.164 phone numbers, an AC-prefixed
# 34-character SID), and never printed, logged or echoed
# (shown as ****, also in --dry-run). A rejected value is not echoed either. The value is passed
# to sam deploy as an argument, so it is briefly visible to other processes of the same user.
#
# This deploys the LOCAL checkout, not GitHub. It prints the commit and refuses a dirty working
# tree unless --allow-dirty (uncommitted changes then ship). It also warns, without blocking, when
# HEAD is on no remote branch. --smoke-only and --dry-run are never blocked by the dirty check.
#
# Usage: scripts/dev/deploy-backend.sh [--allow-dirty] [--yes] [--smoke-only] [--dry-run] [--param Key=Value]...
#                                      [--prompt-param Key]... [--profile NAME | --no-profile]
#   --allow-dirty Deploy a dirty working tree (uncommitted changes ship too).
#   --yes        Skip the confirmation prompt (CI only, issue #95 rules).
#   --param      Supply a parameter that the live stack does not have yet, or deliberately
#                change one (for example HoldExpiryScheduleState=ENABLED). Not allowed for the three
#                private keys above.
#   --i-have-live-sms-authorization   Required whenever OutboxDispatchScheduleState or
#                SmsSenderMappingState targets ENABLED. Pass it only with Enrique's separate
#                live-SMS authorization (#91).
#   --prompt-param Key   Read the value of OwnerNumber, TwilioAccountSid
#                or TwilioBusinessNumber from a hidden prompt. No other key is accepted. Needs a
#                terminal. --dry-run does not prompt and shows ****.
#   --smoke-only Run only the owner API smoke test (no build, no change set).
#   --dry-run    Print the commands without calling AWS (read-only identity check only).
set -euo pipefail

# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ASSUME_YES=0
SMOKE_ONLY=0
ALLOW_DIRTY=0
EXTRA_PARAMS=()
PROMPT_KEYS=()
LIVE_SMS_AUTH=0

usage() {
  sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
}

parse_common "$@"
set -- "${REMAINING_ARGS[@]+"${REMAINING_ARGS[@]}"}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --allow-dirty) ALLOW_DIRTY=1 ;;
    --yes) ASSUME_YES=1 ;;
    --smoke-only) SMOKE_ONLY=1 ;;
    --param)
      [[ $# -ge 2 && "$2" =~ ^[A-Za-z0-9]+=[^[:space:]]*$ ]] || die "--param needs Key=Value (letters and digits in the key, no whitespace in the value)"
      [[ "$2" != ParameterKey=* && "$2" != *ParameterValue=* && "$2" != *ParameterKey=* ]] || die "--param does not accept the ParameterKey=...,ParameterValue=... form; use Key=Value"
      ! is_private_param "${2%%=*}" || die "--param does not accept ${2%%=*}: use --prompt-param ${2%%=*} (hidden prompt, value never printed)"
      EXTRA_PARAMS+=("$2")
      shift
      ;;
    --i-have-live-sms-authorization) LIVE_SMS_AUTH=1 ;;
    --prompt-param)
      [[ $# -ge 2 ]] || die "--prompt-param needs a key"
      is_private_param "$2" || die "--prompt-param accepts only: ${PRIVATE_PARAMS[*]}"
      PROMPT_KEYS+=("$2")
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

need_tool aws sam jq curl git
pin_region
cd "${REPO_ROOT}"
ENFORCE_CLEAN=1
[[ "${SMOKE_ONLY}" -eq 0 && "${DRY_RUN}" -eq 0 ]] || ENFORCE_CLEAN=0
report_commit "${ALLOW_DIRTY}" "${ENFORCE_CLEAN}"
warn_if_unpushed
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
      for pk in "${PROMPT_KEYS[@]+"${PROMPT_KEYS[@]}"}"; do
        [[ "${pk}" == "${key}" ]] && supplied=1
      done
      [[ "${supplied}" -eq 1 ]] || die "template parameter ${key} is not on the live stack yet; pass --param ${key}=<value> (--prompt-param ${key} for a private key). For a schedule state parameter (*ScheduleState) pass the schedule's CURRENT live state (see scripts/dev/status.sh), so the deploy does not change it."
    fi
  done
fi

OVERRIDES=("PermissionsBoundaryArn=${BOUNDARY_ARN}")
OVERRIDES+=("${EXTRA_PARAMS[@]+"${EXTRA_PARAMS[@]}"}")
# The same list with private values masked; the only form that may be printed.
SHOWN_OVERRIDES=("${OVERRIDES[@]}")

# Hidden prompt for each --prompt-param key. The value is never printed, and a rejected value is
# not echoed. Under --dry-run nothing is read and the value is shown as ****.
SEEN_PROMPT=" "
for key in "${PROMPT_KEYS[@]+"${PROMPT_KEYS[@]}"}"; do
  [[ "${SEEN_PROMPT}" != *" ${key} "* ]] || continue
  SEEN_PROMPT+="${key} "
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    SHOWN_OVERRIDES+=("${key}=****")
    continue
  fi
  [[ -t 0 ]] || die "--prompt-param ${key} needs a terminal for the hidden prompt."
  read -r -s -p "Value for ${key} (hidden): " private_value
  printf '\n' >&2
  valid_private_value "${key}" "${private_value}" || die "the value for ${key} is not valid (${PRIVATE_FORMAT_HINT}); not echoed."
  OVERRIDES+=("$(private_override "${key}" "${private_value}")")
  SHOWN_OVERRIDES+=("${key}=****")
  private_value=""
done

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

schedule_parameter() {
  case "$1" in
    HoldExpiryFunctionSweep) printf 'HoldExpiryScheduleState' ;;
    NoteRetentionFunctionDaily) printf 'NoteRetentionScheduleState' ;;
    SmsRetentionFunctionDaily) printf 'SmsRetentionScheduleState' ;;
    OutboxDispatchFunctionSweep) printf 'OutboxDispatchScheduleState' ;;
    *) printf '' ;;
  esac
}

# Warn before creating a change set: SAM may report no changes without returning an ARN.
warn_schedule_drift() {
  local logical physical param want have
  # A parameter that is not on the live stack yet (the first deploy after it was added) has no
  # value to compare: skip its warning instead of failing like stack_parameter does.
  live_parameter() {
    local value
    value="$(stack_query "Stacks[0].Parameters[?ParameterKey=='$1'].ParameterValue | [0]")" || return 1
    [[ "${value}" != "None" ]] || value=""
    printf '%s' "${value}"
  }
  while IFS=$'\t' read -r logical physical; do
    param="$(schedule_parameter "${logical}")"
    [[ -n "${param}" ]] || continue
    want="$(live_parameter "${param}")"
    have="$(aws_cli events describe-rule --name "${physical}" --query State --output text)"
    if [[ -n "${want}" && "${have}" != "${want}" ]]; then
      info "WARNING: ${logical} is ${have} live but ${param}=${want}. A later deploy that modifies the rule would set it to ${want} (refused unless --param ${param} is passed)."
    fi
  done < <(stack_resources AWS::Events::Rule)
  # The sender mapping drifts the same way (for example after an emergency --no-enabled).
  while IFS=$'\t' read -r logical physical; do
    [[ "${logical}" == "SmsSenderFunctionOutbox" ]] || continue
    want="$(live_parameter SmsSenderMappingState)"
    have="$(sender_mapping_state "${physical}")"
    if [[ -n "${want}" && "${have}" != "${want}" ]]; then
      info "WARNING: ${logical} is ${have} live but SmsSenderMappingState=${want}. A later deploy that modifies the mapping would set it to ${want} (refused unless --param SmsSenderMappingState=${want} is passed)."
    fi
  done < <(stack_resources AWS::Lambda::EventSourceMapping)
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
PROFILE_ARGS=()
[[ -z "${PROFILE}" ]] || PROFILE_ARGS=(--profile "${PROFILE}")
SAM_DEPLOY+=("${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}")

if [[ "${DRY_RUN}" -eq 1 ]]; then
  # Print the command with the masked overrides, never SAM_DEPLOY itself.
  show_cmd sam deploy --stack-name "${STACK_NAME}" --region "${EXPECTED_REGION}" \
    --role-arn "${ROLE_ARN}" --capabilities CAPABILITY_IAM \
    --no-execute-changeset --no-confirm-changeset --no-fail-on-empty-changeset \
    --s3-bucket "${ARTIFACT_BUCKET}" --s3-prefix "${ARTIFACT_PREFIX}" \
    --parameter-overrides "${SHOWN_OVERRIDES[@]}" "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}"
  info "+ (all other parameters keep their live values via UsePreviousValue)"
  info "+ take the change set ARN from SAM's output, then aws cloudformation describe-change-set: print action, logical ID, type, replacement"
  info "+ refuse if any parameter not given with --param would differ from the live stack"
  info "+ schedule guard: aws cloudformation get-template --change-set-name <arn> --template-stage Processed gives each added, modified or replaced AWS::Events::Rule its target State (Ref resolved, missing means ENABLED); aws events describe-rule (live State) vs target for the four parameterized rules; a rule without a parameter must target DISABLED; refuse (even with --yes) if it would change without --param <X>ScheduleState"
  info "+ sender mapping guard: aws lambda get-event-source-mapping (live State of SmsSenderFunctionOutbox) vs its Enabled target in the processed template; refuse (even with --yes) if it would change without --param SmsSenderMappingState"
  info "+ ask y/N (skipped with --yes), then aws cloudformation execute-change-set and wait stack-update-complete"
  info "+ an exit trap deletes the change set unless execution started"
  info "+ smoke test: GET /v1/owner/businesses/<BusinessId>/policy without a token expects 401; CORS preflights"
  exit 0
fi

warn_schedule_drift

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
# keep its live value. NoEcho values (OwnerNumber) come back as ****
# from describe-stacks and from the change set, so a pair with a masked side cannot be compared
# and is skipped (UsePreviousValue keeps it; a prompted key is in the allowed list anyway).
LIVE_PARAMS="$(aws_cli cloudformation describe-stacks --stack-name "${STACK_NAME}" --query 'Stacks[0].Parameters' --output json)"
ALLOWED_KEYS="$({
  printf '%s\n' "${EXTRA_PARAMS[@]+"${EXTRA_PARAMS[@]}"}" | sed 's/=.*//'
  printf '%s\n' "${PROMPT_KEYS[@]+"${PROMPT_KEYS[@]}"}"
} | jq -R . | jq -sc .)"
CHANGED_PARAMS="$(jq -r --argjson live "${LIVE_PARAMS}" --argjson allowed "${ALLOWED_KEYS}" '
  [.Parameters[] | . as $p
    | select(($allowed | index($p.ParameterKey)) == null)
    | ($live | map(select(.ParameterKey == $p.ParameterKey)) | first | .ParameterValue) as $was
    | select($was != "****" and $p.ParameterValue != "****")
    | select($was != $p.ParameterValue)
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
# - Any other rule (a new one): its target must be DISABLED, always.
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
    # A parameterized rule's State must be exactly {"Ref": <its own parameter>}; any other wiring
    # is unresolved and refused. A rule without a parameter may be a literal or missing.
    param="$(schedule_parameter "${logical}")"
    target_state="$(jq -r --arg id "${logical}" --arg param "${param}" --argjson cs "${DESCRIPTION}" '
      (.Resources[$id].Properties.State) as $s
      | if $param != "" then
          (if $s == {"Ref": $param} then
            ([$cs.Parameters[] | select(.ParameterKey == $param) | .ParameterValue] | first // "UNRESOLVED")
          else "UNRESOLVED" end)
        elif $s == null then "ENABLED"
        elif ($s | type) == "string" then $s
        elif ($s | type) == "object" and ($s | keys) == ["Ref"] then
          ([$cs.Parameters[] | select(.ParameterKey == $s.Ref) | .ParameterValue] | first // "UNRESOLVED")
        else "UNRESOLVED" end' <<<"${PROCESSED}")"
    [[ "${target_state}" == "ENABLED" || "${target_state}" == "DISABLED" ]] ||
      die "refusing: cannot resolve the target State of ${logical} (got ${target_state}; a parameterized rule's State must be exactly Ref ${param:-<its parameter>})."
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
      # A change counts as requested only when the --param value equals the resolved target.
      explicit=0
      [[ "$(param_value "${param}")" != "${target_state}" ]] || explicit=1
      if [[ "${explicit}" -eq 1 ]]; then
        note="  (state change, requested with --param ${param})"
      else
        note="  (REFUSED: state would change without --param ${param}=${target_state})"
        SCHEDULE_REFUSED="${SCHEDULE_REFUSED:+${SCHEDULE_REFUSED}, }${logical}"
      fi
    fi
    info "  ${logical} [${action}]: ${live_state} -> ${target_state}${note}"
    if [[ -n "${param}" && "${param}" == "OutboxDispatchScheduleState" && "${target_state}" == "ENABLED" && "${LIVE_SMS_AUTH}" -ne 1 ]]; then
      SMS_AUTH_MISSING="${SMS_AUTH_MISSING:+${SMS_AUTH_MISSING}, }${param}"
    fi
  done <<<"${RULE_ROWS}"
  if [[ -n "${SCHEDULE_REFUSED}" ]]; then
    die "refusing: schedule state is not allowed to change: ${SCHEDULE_REFUSED}. A parameterized schedule needs its *ScheduleState parameter passed explicitly with --param (the live state keeps it); a rule without a parameter (a new one) must target DISABLED."
  fi
fi

SMS_AUTH_MISSING="${SMS_AUTH_MISSING:-}"
if [[ -n "${RULE_ROWS}" && -n "${SMS_AUTH_MISSING}" ]]; then
  die "refusing: ${SMS_AUTH_MISSING} targets ENABLED, which is a live-SMS action: pass --i-have-live-sms-authorization only with Enrique's separate authorization (#91)."
fi

# Sender mapping guard: the SmsSender outbox mapping's State must not change silently either.
# Live State comes from lambda get-event-source-mapping; the target is the Enabled property in the
# change set's processed template (exact wiring only, see below). Only an added, modified or
# replaced mapping is checked. The conversation mapping must keep exactly
# Fn::If [SmsConversationRequested, true, false]; any other such mapping must be DISABLED.
SENDER_MAPPING="SmsSenderFunctionOutbox"
SENDER_PARAM="SmsSenderMappingState"
CONVERSATION_MAPPING="SmsConversationFunctionReceipts"
MAPPING_ROWS="$(jq -r '.Changes[].ResourceChange
  | select(.ResourceType == "AWS::Lambda::EventSourceMapping" and (.Action == "Add" or .Action == "Modify" or .Replacement == "True" or .Replacement == "Conditional"))
  | [.Action, .LogicalResourceId, (.PhysicalResourceId // "")] | @tsv' <<<"${DESCRIPTION}")"
if [[ -n "${MAPPING_ROWS}" ]]; then
  if [[ -z "${PROCESSED:-}" ]]; then
    PROCESSED="$(aws_cli cloudformation get-template --stack-name "${STACK_NAME}" --change-set-name "${CHANGESET}" \
      --template-stage Processed --query TemplateBody --output json | jq 'if type == "string" then fromjson else . end')" ||
      die "refusing: cannot read the change set's processed template, so mapping target states are unknown."
  fi
  info ""
  info "Event source mapping states (live -> target):"
  while IFS=$'\t' read -r map_action map_logical map_physical; do
    [[ -n "${map_logical}" ]] || continue
    # Sender mapping: Enabled must be exactly Fn::If [SmsSenderMappingEnabled, true, false] and the
    # condition exactly Equals [Ref SmsSenderMappingState, ENABLED]. Any other mapping: a literal
    # false. Anything else is unresolved and refused.
    map_target="$(jq -r --arg id "${map_logical}" --arg sender "${SENDER_MAPPING}" --arg conv "${CONVERSATION_MAPPING}" --argjson cs "${DESCRIPTION}" '
      def flag: if . == true or . == "true" then "ENABLED" elif . == false or . == "false" then "DISABLED" else "UNRESOLVED" end;
      (.Resources[$id].Properties.Enabled) as $e
      | if $id == $sender then
          (if $e == {"Fn::If": ["SmsSenderMappingEnabled", true, false]}
              and .Conditions.SmsSenderMappingEnabled == {"Fn::Equals": [{"Ref": "SmsSenderMappingState"}, "ENABLED"]} then
            ([$cs.Parameters[] | select(.ParameterKey == "SmsSenderMappingState") | .ParameterValue] | first // "UNRESOLVED")
            | if . == "ENABLED" or . == "DISABLED" then . else "UNRESOLVED" end
          else "UNRESOLVED" end)
        elif $id == $conv then
          (if $e == {"Fn::If": ["SmsConversationRequested", true, false]} then "GOVERNED" else "UNRESOLVED" end)
        elif $e == null then "ENABLED"
        else ($e | flag) end' <<<"${PROCESSED}")"
    # The conversation mapping is governed by the SmsConversationRequested condition (not by a
    # parameter of this guard), but only in its exact wiring; any other shape is refused.
    if [[ "${map_logical}" == "${CONVERSATION_MAPPING}" && "${map_target}" == "GOVERNED" ]]; then
      info "  ${map_logical} [${map_action}]: governed by SmsConversationRequested"
      continue
    fi
    [[ "${map_target}" == "ENABLED" || "${map_target}" == "DISABLED" ]] ||
      die "refusing: cannot resolve the target state of ${map_logical} (got ${map_target}; the sender mapping needs the exact SmsSenderMappingEnabled wiring and the conversation mapping the exact SmsConversationRequested wiring)."
    if [[ "${map_action}" == "Add" || -z "${map_physical}" ]]; then
      map_live="(new)"
    else
      map_live="$(sender_mapping_state "${map_physical}")"
    fi
    map_note=""
    if [[ "${map_logical}" != "${SENDER_MAPPING}" ]]; then
      [[ "${map_target}" == "DISABLED" ]] || map_note="  (REFUSED: other mappings must stay DISABLED)"
    else
      if [[ "${map_target}" == "ENABLED" && "${LIVE_SMS_AUTH}" -ne 1 ]]; then
        map_note="  (REFUSED: targeting ENABLED needs --i-have-live-sms-authorization, #91)"
      elif [[ "${map_live}" != "${map_target}" ]]; then
        # A change counts as requested only when the --param value equals the resolved target.
        if [[ "$(param_value "${SENDER_PARAM}")" == "${map_target}" ]]; then
          map_note="  (state change, requested with --param ${SENDER_PARAM})"
        else
          map_note="  (REFUSED: state would change without --param ${SENDER_PARAM}=${map_target})"
        fi
      fi
    fi
    info "  ${map_logical} [${map_action}]: ${map_live} -> ${map_target}${map_note}"
    [[ "${map_note}" != *REFUSED* ]] || die "refusing: ${map_logical}${map_note}. Turning the sender mapping on needs live-SMS authorization (#91)."
  done <<<"${MAPPING_ROWS}"
fi

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
