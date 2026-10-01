#!/usr/bin/env bash
# Enable or disable one scheduled rule of the dev stack by its logical ID, then
# print the line to record on the issue (#43 for dev).
#
# Only the three listed logical IDs are accepted, and each must be an EventBridge rule of
# the stack. Outbox dispatch (OutboxDispatch*) is refused: it stays off until live SMS is separately
# authorized.
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

need_tool aws jq
pin_region
check_identity

if [[ "${DRY_RUN}" -eq 1 ]]; then
  info "+ aws events describe-rule --name <physical name of ${NAME} in ${STACK_NAME}>"
  info "+ aws events ${ACTION}-rule --name <physical name of ${NAME}>"
  info "+ aws events describe-rule --name <physical name of ${NAME}>"
  info "Line to record on the issue: dev schedule ${NAME}: <before> -> <after> (scripts/dev/schedules.sh ${ACTION})"
  exit 0
fi

RULE_NAME="$(stack_resources AWS::Events::Rule | awk -F'\t' -v id="${NAME}" '$1 == id {print $2}')"
[[ -n "${RULE_NAME}" ]] || die "${NAME} is not an EventBridge rule of stack ${STACK_NAME}."

rule_state() {
  aws_cli events describe-rule --name "${RULE_NAME}" --query State --output text
}
BEFORE="$(rule_state)"
aws_cli events "${ACTION}-rule" --name "${RULE_NAME}"
AFTER="$(rule_state)"

WANT="ENABLED"
[[ "${ACTION}" == "enable" ]] || WANT="DISABLED"
[[ "${AFTER}" == "${WANT}" ]] || die "the rule is ${AFTER}, expected ${WANT}."

info ""
info "Record on the issue:"
info "dev schedule ${NAME}: ${BEFORE} -> ${AFTER} at $(date -u +%Y-%m-%dT%H:%MZ) (scripts/dev/schedules.sh ${ACTION})"
