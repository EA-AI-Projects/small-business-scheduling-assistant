#!/usr/bin/env bash
# Enable or disable one scheduled rule of the dev stack by its logical ID, then
# print the line to record on the issue (#43 for dev).
#
# The state is a template parameter (issue #97), so this runs
#   scripts/dev/deploy-backend.sh --param <X>ScheduleState=ENABLED|DISABLED
# and the template stays the source of truth. That is a full backend deploy: it builds,
# shows the change set (including the schedule states, live -> target) and asks for
# confirmation, so it also ships any pending code. It never calls enable-rule/disable-rule.
#
# Only the three listed logical IDs are accepted. Outbox dispatch (OutboxDispatch*) is refused:
# it stays off until live SMS is separately authorized.
#
# Usage: scripts/dev/schedules.sh enable|disable <LogicalId> [--dry-run]
#                                 [--profile NAME | --no-profile]
#   Logical IDs: HoldExpiryFunctionSweep, NoteRetentionFunctionDaily,
#                SmsRetentionFunctionDaily
set -euo pipefail

# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() {
  sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
}

parse_common "$@"
set -- "${REMAINING_ARGS[@]+"${REMAINING_ARGS[@]}"}"
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
[[ $# -eq 2 ]] || {
  usage >&2
  exit 2
}
ACTION="$1"
NAME="$2"
case "${ACTION}" in
  enable | disable) ;;
  *) die "action must be enable or disable" ;;
esac
[[ "${NAME}" =~ ^[A-Za-z0-9]+$ ]] || die "the schedule name must be a stack logical ID"

# Allowlist, checked before any AWS call. Outbox dispatch stays disabled until live SMS is
# separately authorized, and any future schedule needs a reviewed change here.
case "${NAME}" in
  HoldExpiryFunctionSweep | NoteRetentionFunctionDaily | SmsRetentionFunctionDaily) ;;
  *)
    shopt -s nocasematch
    if [[ "${NAME}" == OutboxDispatch* ]]; then
      die "outbox dispatch stays disabled until live SMS is separately authorized; refusing."
    fi
    die "${NAME} is not an allowed schedule (HoldExpiryFunctionSweep, NoteRetentionFunctionDaily, SmsRetentionFunctionDaily)."
    ;;
esac

case "${NAME}" in
  HoldExpiryFunctionSweep) PARAM="HoldExpiryScheduleState" ;;
  NoteRetentionFunctionDaily) PARAM="NoteRetentionScheduleState" ;;
  SmsRetentionFunctionDaily) PARAM="SmsRetentionScheduleState" ;;
esac
WANT="ENABLED"
[[ "${ACTION}" == "enable" ]] || WANT="DISABLED"

need_tool aws jq
pin_region
check_identity

DEPLOY=("$(dirname "${BASH_SOURCE[0]}")/deploy-backend.sh" --param "${PARAM}=${WANT}")
[[ "${DRY_RUN}" -ne 1 ]] || DEPLOY+=(--dry-run)
if [[ -z "${PROFILE}" ]]; then DEPLOY+=(--no-profile); else DEPLOY+=(--profile "${PROFILE}"); fi

if [[ "${DRY_RUN}" -eq 1 ]]; then
  info "+ aws events describe-rule --name <physical name of ${NAME} in ${STACK_NAME}>"
  show_cmd "${DEPLOY[@]}"
  info "+ aws events describe-rule --name <physical name of ${NAME}>"
  info "Line to record on the issue: dev schedule ${NAME}: <before> -> ${WANT} (scripts/dev/schedules.sh ${ACTION})"
  exit 0
fi

RULE_NAME="$(stack_resources AWS::Events::Rule | awk -F'\t' -v id="${NAME}" '$1 == id {print $2}')"
[[ -n "${RULE_NAME}" ]] || die "${NAME} is not an EventBridge rule of stack ${STACK_NAME}."

rule_state() {
  aws_cli events describe-rule --name "${RULE_NAME}" --query State --output text
}
BEFORE="$(rule_state)"
"${DEPLOY[@]}"
AFTER="$(rule_state)"
[[ "${AFTER}" == "${WANT}" ]] || die "the rule is ${AFTER}, expected ${WANT} (was the change set declined?)."

info ""
info "Record on the issue:"
info "dev schedule ${NAME}: ${BEFORE} -> ${AFTER} at $(date -u +%Y-%m-%dT%H:%MZ) (scripts/dev/schedules.sh ${ACTION}, parameter ${PARAM})"
