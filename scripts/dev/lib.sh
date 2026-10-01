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
