#!/usr/bin/env bash
# Shared helpers for the scripts/dev/ scripts. Source it; do not run it.
#
# Fixed targets (issue #94, doc/DEV_STACK_PLAN.md): the long-lived synthetic dev
# environment only. Every script refuses any other account or region.
# shellcheck shell=bash
# shellcheck disable=SC2034

EXPECTED_ACCOUNT="214965372605"
EXPECTED_REGION="us-west-1"
STACK_NAME="scheduling-dev"
ARTIFACT_BUCKET="scheduling-dev-artifacts-214965372605"
ARTIFACT_PREFIX="scheduling-dev"
AMPLIFY_APP_NAME="scheduling-owner-dev"
AMPLIFY_BRANCH="main"
ADMIN_PROFILE="scheduling-dev-admin"
DEPLOYER_PROFILE="scheduling-dev-deployer"

DRY_RUN=0
# Scripts set PROFILE (default: the deployer). An empty PROFILE means "use the
# ambient credentials" (CI), selected with --no-profile.
PROFILE="${DEPLOYER_PROFILE}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

info() {
  printf '%s\n' "$*"
}

need_tool() {
  local tool
  for tool in "$@"; do
    command -v "${tool}" >/dev/null 2>&1 || die "required tool not found: ${tool}"
  done
}

# Region guard: never let an ambient region override the fixed one.
pin_region() {
  local var
  for var in AWS_REGION AWS_DEFAULT_REGION; do
    if [[ -n "${!var:-}" && "${!var}" != "${EXPECTED_REGION}" ]]; then
      die "${var} is set to a region other than ${EXPECTED_REGION}; refusing."
    fi
  done
  export AWS_REGION="${EXPECTED_REGION}"
  export AWS_DEFAULT_REGION="${EXPECTED_REGION}"
}

# aws <args>: the AWS CLI with the fixed profile and region.
aws_cli() {
  if [[ -n "${PROFILE}" ]]; then
    aws --profile "${PROFILE}" --region "${EXPECTED_REGION}" "$@"
  else
    aws --region "${EXPECTED_REGION}" "$@"
  fi
}

# Print a command the way a shell would need it typed.
show_cmd() {
  local out="+" arg
  for arg in "$@"; do
    out+=" $(printf '%q' "${arg}")"
  done
  printf '%s\n' "${out}"
}

# run <cmd...>: execute, or only print under --dry-run. Use for anything that
# changes state or calls AWS.
run() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    if [[ "$1" == "aws_cli" ]]; then
      shift
      if [[ -n "${PROFILE}" ]]; then
        show_cmd aws --profile "${PROFILE}" --region "${EXPECTED_REGION}" "$@"
      else
        show_cmd aws --region "${EXPECTED_REGION}" "$@"
      fi
    else
      show_cmd "$@"
    fi
  else
    "$@"
  fi
}

# Account guard. The sts call is read-only, so it also runs under --dry-run; a
# dry run without credentials only skips the check.
check_identity() {
  local account
  if ! account="$(aws_cli sts get-caller-identity --query Account --output text 2>/dev/null)"; then
    if [[ "${DRY_RUN}" -eq 1 ]]; then
      info "[dry-run] identity check skipped: no usable AWS credentials."
      return 0
    fi
    # The deployer profile assumes its role through the admin SSO session, so that is the
    # profile to sign in with; "aws sso login" on the deployer profile itself fails.
    if [[ -z "${PROFILE}" ]]; then
      die "sts get-caller-identity failed: no usable ambient AWS credentials (--no-profile). Provide them, or drop --no-profile and sign in with: aws sso login --profile ${ADMIN_PROFILE}"
    fi
    die "sts get-caller-identity failed for profile ${PROFILE}. Sign in first: aws sso login --profile ${ADMIN_PROFILE}"
  fi
  if [[ "${account}" != "${EXPECTED_ACCOUNT}" ]]; then
    die "caller account is not ${EXPECTED_ACCOUNT}; refusing."
  fi
  info "Account and region verified (${EXPECTED_ACCOUNT}, ${EXPECTED_REGION}), profile: ${PROFILE:-ambient credentials}."
}

# Handles the options every script shares. Sets REMAINING_ARGS to the rest.
# Usage: parse_common "$@"; set -- "${REMAINING_ARGS[@]}"
REMAINING_ARGS=()
parse_common() {
  REMAINING_ARGS=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) DRY_RUN=1 ;;
      --profile)
        [[ $# -ge 2 ]] || die "--profile needs a value"
        PROFILE="$2"
        shift
        ;;
      --no-profile) PROFILE="" ;;
      *) REMAINING_ARGS+=("$1") ;;
    esac
    shift
  done
}

# report_commit <allow_dirty 0|1> <enforce 0|1>: print the commit this checkout would deploy and
# whether the tree is dirty. These scripts deploy the LOCAL checkout, not GitHub. A dirty tree is
# refused when enforce is 1 and allow_dirty is 0; otherwise it only warns that uncommitted changes
# will ship. Sets COMMIT and DIRTY (0|1). Run it from inside the repository.
COMMIT=""
DIRTY=0
report_commit() {
  local allow_dirty="${1:-0}" enforce="${2:-1}"
  COMMIT="$(git rev-parse HEAD)"
  info "Local checkout (not GitHub): commit ${COMMIT}."
  if [[ -n "$(git status --porcelain)" ]]; then
    DIRTY=1
    if [[ "${enforce}" -eq 1 && "${allow_dirty}" -ne 1 ]]; then
      die "the working tree is dirty; commit or stash, or pass --allow-dirty."
    fi
    info "WARNING: the working tree is dirty; uncommitted changes will ship."
  else
    DIRTY=0
    info "Working tree is clean."
  fi
}

# warn_if_unpushed: warn, never block, when HEAD is on no remote-tracking branch. First a quiet,
# non-interactive git fetch of the default remote, bounded by GIT_FETCH_TIMEOUT seconds (default
# 10); if it fails or times out, warn and use the local remote-tracking refs as they are.
# The fetch never prompts (no tty, ssh BatchMode, no credential prompt). It runs in its own
# process group so a timeout kills git and its transport (ssh, git-remote-https) together.
warn_if_unpushed() {
  local timeout="${GIT_FETCH_TIMEOUT:-10}" fetch_pid fetched=1 ticks=0 commit
  commit="$(git rev-parse HEAD)"
  if [[ -n "$(git remote)" ]]; then
    local grouped=0
    command -v perl >/dev/null 2>&1 && grouped=1
    # Scoped to the fetch only; honor a configured core.sshCommand (for example a chosen key).
    local base_ssh fetch_ssh
    base_ssh="${GIT_SSH_COMMAND:-$(git config core.sshCommand || echo ssh)}"
    fetch_ssh="${base_ssh} -o BatchMode=yes -o ConnectTimeout=5"
    if [[ "${grouped}" -eq 1 ]]; then
      GIT_SSH_COMMAND="${fetch_ssh}" GIT_TERMINAL_PROMPT=0 perl -e 'setpgrp(0, 0); exec @ARGV or exit 127' -- \
        git -c credential.interactive=never fetch --quiet </dev/null >/dev/null 2>&1 &
    else
      GIT_SSH_COMMAND="${fetch_ssh}" GIT_TERMINAL_PROMPT=0 git -c credential.interactive=never fetch --quiet </dev/null >/dev/null 2>&1 &
    fi
    fetch_pid=$!
    while kill -0 "${fetch_pid}" 2>/dev/null; do
      if [[ "${ticks}" -ge $((timeout * 10)) ]]; then
        if [[ "${grouped}" -eq 1 ]]; then
          kill -TERM -- "-${fetch_pid}" 2>/dev/null || true
          sleep 0.2
          kill -KILL -- "-${fetch_pid}" 2>/dev/null || true
        else
          kill -KILL "${fetch_pid}" 2>/dev/null || true
        fi
        fetched=0
        break
      fi
      sleep 0.1
      ticks=$((ticks + 1))
    done
    wait "${fetch_pid}" 2>/dev/null || fetched=0
    if [[ "${fetched}" -eq 0 ]]; then
      info "WARNING: git fetch failed or timed out; checking against the local remote-tracking refs, which may be stale."
    fi
  else
    info "WARNING: no git remote is configured."
  fi
  if [[ -z "$(git branch -r --contains HEAD 2>/dev/null)" ]]; then
    info "WARNING: commit ${commit} is not on GitHub yet (on no remote branch); push it so the deployed code can be reviewed."
  else
    info "Commit is on a remote branch."
  fi
  return 0
}

# stack_query <JMESPath>: read the live stack with a --query.
stack_query() {
  aws_cli cloudformation describe-stacks --stack-name "${STACK_NAME}" --query "$1" --output text
}

# stack_output <OutputKey>
stack_output() {
  local value
  value="$(stack_query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]")"
  [[ -n "${value}" && "${value}" != "None" ]] || die "stack output $1 not found on ${STACK_NAME}"
  printf '%s' "${value}"
}

# stack_parameter <ParameterKey>: value of a non-secret parameter. Never print it.
stack_parameter() {
  local value
  value="$(stack_query "Stacks[0].Parameters[?ParameterKey=='$1'].ParameterValue | [0]")"
  [[ -n "${value}" && "${value}" != "None" ]] || die "stack parameter $1 not found on ${STACK_NAME}"
  printf '%s' "${value}"
}

# stack_resources <Type>: "LogicalId<TAB>PhysicalId" lines of one resource type.
stack_resources() {
  aws_cli cloudformation list-stack-resources --stack-name "${STACK_NAME}" --output json |
    jq -r --arg type "$1" '.StackResourceSummaries[] | select(.ResourceType == $type) | [.LogicalResourceId, .PhysicalResourceId] | @tsv'
}

# Parameters that hold personal or account-identifying values. They are only ever set with a
# hidden prompt (deploy-backend.sh --prompt-param) and never printed.
PRIVATE_PARAMS=(OwnerNumber TwilioAccountSid TwilioBusinessNumber)
PRIVATE_FORMAT_HINT=""

is_private_param() {
  local k
  for k in "${PRIVATE_PARAMS[@]}"; do
    [[ "$1" == "${k}" ]] && return 0
  done
  return 1
}

# valid_private_value <key> <value>: format check only; never prints the value. Sets
# PRIVATE_FORMAT_HINT to the expected format (no value in it).
valid_private_value() {
  local key="$1" value="$2"
  local e164='^\+[1-9][0-9]{7,14}$'
  local sid='^AC[0-9a-fA-F]{32}$'
  case "${key}" in
    OwnerNumber | TwilioBusinessNumber)
      PRIVATE_FORMAT_HINT="expected E.164: a plus sign and 8 to 15 digits, no spaces"
      [[ "${value}" =~ ${e164} ]]
      ;;
    TwilioAccountSid)
      PRIVATE_FORMAT_HINT="expected AC followed by 32 hexadecimal characters"
      [[ "${value}" =~ ${sid} ]]
      ;;
    *) return 1 ;;
  esac
}

# sender_mapping_state <uuid>: the live mapping State folded to ENABLED or DISABLED.
sender_mapping_state() {
  local s
  s="$(aws_cli lambda get-event-source-mapping --uuid "$1" --query State --output text)" ||
    die "refusing: cannot read the live state of the sender mapping."
  case "${s}" in
    Enabled) printf 'ENABLED' ;;
    Disabled) printf 'DISABLED' ;;
    *) die "refusing: the sender mapping is in state ${s}; wait until it is Enabled or Disabled." ;;
  esac
}

# private_override <key> <value>: the Key=Value argument for sam deploy. An empty value is
# passed as Key="" because SAM rejects a bare Key=. Never print the result.
private_override() {
  if [[ -z "$2" ]]; then
    printf '%s=""' "$1"
  else
    printf '%s=%s' "$1" "$2"
  fi
}

# param_value <key>: the value given with --param for the key (last one wins), or empty.
param_value() {
  local kv v=""
  for kv in "${EXTRA_PARAMS[@]+"${EXTRA_PARAMS[@]}"}"; do
    [[ "${kv%%=*}" != "$1" ]] || v="${kv#*=}"
  done
  printf '%s' "${v}"
}
