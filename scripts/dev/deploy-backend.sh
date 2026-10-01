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
# Private values: OwnerNumber, AuthorizedSmsRecipients, TwilioAccountSid and TwilioBusinessNumber
# are never passed with --param (it would leave them in shell history). Use --prompt-param <Key>:
# the value is read with a hidden prompt, validated (E.164 phone numbers, a comma-separated E.164
# list for the allowlist, an AC-prefixed 34-character SID), and never printed, logged or echoed
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
#                change one (for example HoldExpiryScheduleState=ENABLED). Not allowed for the four
#                private keys above.
#   --prompt-param Key   Read the value of OwnerNumber, AuthorizedSmsRecipients, TwilioAccountSid
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
      [[ $# -ge 2 && "$2" == *=* ]] || die "--param needs Key=Value"
      ! is_private_param "${2%%=*}" || die "--param does not accept ${2%%=*}: use --prompt-param ${2%%=*} (hidden prompt, value never printed)"
      EXTRA_PARAMS+=("$2")
      shift
      ;;
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
  OVERRIDES+=("${key}=${private_value}")
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
# keep its live value. NoEcho values (OwnerNumber, AuthorizedSmsRecipients) come back as ****
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
schedule_parameter() {
  case "$1" in
    HoldExpiryFunctionSweep) printf 'HoldExpiryScheduleState' ;;
    NoteRetentionFunctionDaily) printf 'NoteRetentionScheduleState' ;;
    SmsRetentionFunctionDaily) printf 'SmsRetentionScheduleState' ;;
    OutboxDispatchFunctionSweep) printf 'OutboxDispatchScheduleState' ;;
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
    die "refusing: schedule state is not allowed to change: ${SCHEDULE_REFUSED}. A parameterized schedule needs its *ScheduleState parameter passed explicitly with --param (the live state keeps it); a rule without a parameter (a new one) must target DISABLED."
  fi
fi

# Sender mapping guard: the SmsSender outbox mapping's State must not change silently either.
# Live State comes from lambda get-event-source-mapping; the target is the Enabled property in the
# change set's processed template (a boolean, or an Fn::If on SmsSenderMappingEnabled resolved
# against the change set's SmsSenderMappingState). Only an added, modified or replaced mapping
# is checked.
SENDER_MAPPING="SmsSenderFunctionOutbox"
SENDER_PARAM="SmsSenderMappingState"
MAPPING_ROW="$(jq -r --arg id "${SENDER_MAPPING}" '.Changes[].ResourceChange
  | select(.ResourceType == "AWS::Lambda::EventSourceMapping" and .LogicalResourceId == $id and (.Action == "Add" or .Action == "Modify" or .Replacement == "True" or .Replacement == "Conditional"))
  | [.Action, (.PhysicalResourceId // "")] | @tsv' <<<"${DESCRIPTION}")"
if [[ -n "${MAPPING_ROW}" ]]; then
  IFS=$'\t' read -r map_action map_physical <<<"${MAPPING_ROW}"
  if [[ -z "${PROCESSED:-}" ]]; then
    PROCESSED="$(aws_cli cloudformation get-template --stack-name "${STACK_NAME}" --change-set-name "${CHANGESET}" \
      --template-stage Processed --query TemplateBody --output json | jq 'if type == "string" then fromjson else . end')" ||
      die "refusing: cannot read the change set's processed template, so the sender mapping target state is unknown."
  fi
  map_target="$(jq -r --arg id "${SENDER_MAPPING}" --argjson cs "${DESCRIPTION}" '
    def param: ([$cs.Parameters[] | select(.ParameterKey == "SmsSenderMappingState") | .ParameterValue] | first // "UNRESOLVED");
    def flag: if . == true or . == "true" then "ENABLED" elif . == false or . == "false" then "DISABLED" else "UNRESOLVED" end;
    (.Resources[$id].Properties.Enabled) as $e
    | if $e == null then "ENABLED"
      elif ($e | type) == "object" and ($e | keys) == ["Fn::If"] and $e["Fn::If"][0] == "SmsSenderMappingEnabled" then
        (if param == "ENABLED" then ($e["Fn::If"][1] | flag) elif param == "DISABLED" then ($e["Fn::If"][2] | flag) else "UNRESOLVED" end)
      else ($e | flag) end' <<<"${PROCESSED}")"
  [[ "${map_target}" == "ENABLED" || "${map_target}" == "DISABLED" ]] ||
    die "refusing: cannot resolve the target state of ${SENDER_MAPPING} (got ${map_target})."
  if [[ "${map_action}" == "Add" || -z "${map_physical}" ]]; then
    map_live="(new)"
  else
    map_live="$(sender_mapping_state "${map_physical}")"
  fi
  map_note=""
  if [[ "${map_live}" != "${map_target}" ]]; then
    map_explicit=0
    for kv in "${EXTRA_PARAMS[@]+"${EXTRA_PARAMS[@]}"}"; do
      [[ "${kv%%=*}" == "${SENDER_PARAM}" ]] && map_explicit=1
    done
    if [[ "${map_explicit}" -eq 1 ]]; then
      map_note="  (state change, requested with --param ${SENDER_PARAM})"
    else
      map_note="  (REFUSED: state would change without --param ${SENDER_PARAM})"
    fi
  fi
  info ""
  info "Sender mapping state (live -> target):"
  info "  ${SENDER_MAPPING} [${map_action}]: ${map_live} -> ${map_target}${map_note}"
  if [[ "${map_note}" == *REFUSED* ]]; then
    die "refusing: the sender mapping state is not allowed to change: pass --param ${SENDER_PARAM}=ENABLED|DISABLED explicitly (the live state keeps it). Turning it on needs live-SMS authorization (#91)."
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
while IFS=$'\t' read -r logical physical; do
  [[ "${logical}" == "${SENDER_MAPPING}" ]] || continue
  want="$(jq -r '[.Parameters[] | select(.ParameterKey == "SmsSenderMappingState") | .ParameterValue] | first // ""' <<<"${DESCRIPTION}")"
  have="$(sender_mapping_state "${physical}")"
  if [[ -n "${want}" && "${have}" != "${want}" ]]; then
    info "WARNING: ${logical} is ${have} live but ${SENDER_PARAM}=${want}. A later deploy that modifies the mapping would set it to ${want} (refused unless --param ${SENDER_PARAM} is passed)."
  fi
done < <(stack_resources AWS::Lambda::EventSourceMapping)

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
