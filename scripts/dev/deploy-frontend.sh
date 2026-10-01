#!/usr/bin/env bash
# Deploy the owner app to the synthetic dev Amplify app (manual deploy, no Git
# connection): build with the stack's public values, keep the zip in S3, upload
# it to Amplify, wait for the job, then verify the security headers.
#
# Needs the deployer's Amplify grant (infra/dev-deploy-roles.yaml, AmplifyAppId).
# The Amplify app ID is derived from the live OwnerAppOrigin parameter
# (https://main.<app id>.amplifyapp.com) or given with --app-id / AMPLIFY_APP_ID;
# it is never committed.
#
# Usage: scripts/dev/deploy-frontend.sh [--allow-dirty] [--dry-run] [--app-id ID]
#                                       [--profile NAME | --no-profile]
#   --allow-dirty  Build a dirty tree; the zip name gets a -dirty suffix.
#   --dry-run      Print the commands without calling AWS (read-only identity check only).
set -euo pipefail

# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ALLOW_DIRTY=0
APP_ID="${AMPLIFY_APP_ID:-}"
JOB_TIMEOUT_SECONDS=900

usage() {
  sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
}

parse_common "$@"
set -- "${REMAINING_ARGS[@]+"${REMAINING_ARGS[@]}"}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --allow-dirty) ALLOW_DIRTY=1 ;;
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

need_tool aws jq curl zip git npm
pin_region
cd "${REPO_ROOT}"
check_identity

COMMIT="$(git rev-parse HEAD)"
ZIP_NAME="${COMMIT}"
if [[ -n "$(git status --porcelain)" ]]; then
  [[ "${ALLOW_DIRTY}" -eq 1 ]] || die "the working tree is dirty; commit or stash, or pass --allow-dirty."
  ZIP_NAME="${COMMIT}-dirty"
  info "Working tree is dirty: zip will be named ${ZIP_NAME}.zip"
fi
ZIP_KEY="owner-app/${ZIP_NAME}.zip"

if [[ "${DRY_RUN}" -eq 1 ]]; then
  API_URL="<OwnerApiUrl>"
  COGNITO_DOMAIN="<OwnerCognitoDomain>"
  CLIENT_ID="<OwnerAppClientId>"
  APP_ORIGIN="<OwnerAppOrigin>"
  APP_ID="${APP_ID:-<app id from OwnerAppOrigin>}"
else
  API_URL="$(stack_output OwnerApiUrl)"
  API_URL="${API_URL%/}"
  COGNITO_DOMAIN="$(stack_output OwnerCognitoDomain)"
  CLIENT_ID="$(stack_output OwnerAppClientId)"
  APP_ORIGIN="$(stack_parameter OwnerAppOrigin)"
  if [[ -z "${APP_ID}" ]]; then
    if [[ "${APP_ORIGIN}" =~ ^https://${AMPLIFY_BRANCH}\.([a-z0-9]+)\.amplifyapp\.com$ ]]; then
      APP_ID="${BASH_REMATCH[1]}"
    else
      die "cannot derive the Amplify app ID from OwnerAppOrigin; pass --app-id."
    fi
  fi
  found_name="$(aws_cli amplify get-app --app-id "${APP_ID}" --query app.name --output text)"
  [[ "${found_name}" == "${AMPLIFY_APP_NAME}" ]] || die "Amplify app is not named ${AMPLIFY_APP_NAME}; refusing."
  info "Amplify app ${AMPLIFY_APP_NAME}, branch ${AMPLIFY_BRANCH}."
fi

info "Building the owner app."
if [[ "${DRY_RUN}" -eq 1 ]]; then
  info "+ (cd frontend && NEXT_PUBLIC_API_BASE_URL=<OwnerApiUrl> NEXT_PUBLIC_COGNITO_DOMAIN=<OwnerCognitoDomain> NEXT_PUBLIC_COGNITO_CLIENT_ID=<OwnerAppClientId> NEXT_PUBLIC_BUSINESS_ID=dev-synthetic NEXT_PUBLIC_AUTH_MODE=cognito npm run build)"
  info "+ (cd frontend && npm run check:export)"
else
  (
    cd frontend
    [[ -d node_modules ]] || npm ci
    export NEXT_PUBLIC_API_BASE_URL="${API_URL}"
    export NEXT_PUBLIC_COGNITO_DOMAIN="${COGNITO_DOMAIN}"
    export NEXT_PUBLIC_COGNITO_CLIENT_ID="${CLIENT_ID}"
    export NEXT_PUBLIC_BUSINESS_ID="dev-synthetic"
    export NEXT_PUBLIC_AUTH_MODE="cognito"
    npm run build
    npm run check:export
  )
fi

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT
ZIP_PATH="${WORK_DIR}/${ZIP_NAME}.zip"
if [[ "${DRY_RUN}" -eq 1 ]]; then
  info "+ (cd frontend/out && zip -qr -X ${ZIP_PATH} .)"
else
  (cd frontend/out && zip -qr -X "${ZIP_PATH}" .)
fi

info "Keeping the zip at s3://${ARTIFACT_BUCKET}/${ZIP_KEY}"
run aws_cli s3 cp "${ZIP_PATH}" "s3://${ARTIFACT_BUCKET}/${ZIP_KEY}" --only-show-errors

if [[ "${DRY_RUN}" -eq 1 ]]; then
  info "+ aws amplify create-deployment --app-id ${APP_ID} --branch-name ${AMPLIFY_BRANCH}   (returns jobId and zipUploadUrl)"
  info "+ curl -fsS -X PUT --upload-file ${ZIP_PATH} <zipUploadUrl>"
  info "+ aws amplify start-deployment --app-id ${APP_ID} --branch-name ${AMPLIFY_BRANCH} --job-id <jobId>"
  info "+ poll aws amplify get-job until SUCCEED (FAILED or CANCELLED stops the script)"
  info "+ curl -sSI ${APP_ORIGIN}/  and check Content-Security-Policy, Strict-Transport-Security, X-Frame-Options"
  exit 0
fi

info "Creating the Amplify deployment."
read -r JOB_ID UPLOAD_URL < <(aws_cli amplify create-deployment --app-id "${APP_ID}" --branch-name "${AMPLIFY_BRANCH}" \
  --query '[jobId, zipUploadUrl]' --output text)
[[ -n "${JOB_ID}" && -n "${UPLOAD_URL}" ]] || die "create-deployment returned no job"

info "Uploading the zip."
curl -fsS -X PUT --upload-file "${ZIP_PATH}" "${UPLOAD_URL}" >/dev/null
unset UPLOAD_URL

info "Starting the deployment (job ${JOB_ID})."
aws_cli amplify start-deployment --app-id "${APP_ID}" --branch-name "${AMPLIFY_BRANCH}" --job-id "${JOB_ID}" >/dev/null

deadline=$((SECONDS + JOB_TIMEOUT_SECONDS))
while true; do
  status="$(aws_cli amplify get-job --app-id "${APP_ID}" --branch-name "${AMPLIFY_BRANCH}" --job-id "${JOB_ID}" --query job.summary.status --output text)"
  case "${status}" in
    SUCCEED)
      info "Amplify job ${JOB_ID}: SUCCEED"
      break
      ;;
    FAILED | CANCELLED) die "Amplify job ${JOB_ID} ended as ${status}." ;;
    *) info "  Amplify job status: ${status}" ;;
  esac
  [[ "${SECONDS}" -lt "${deadline}" ]] || die "timed out waiting for Amplify job ${JOB_ID} (last status ${status})."
  sleep 5
done

info "Verifying response headers."
header_ok() {
  # header_ok <name> <regex on the value>
  grep -iE "^$1:[[:space:]]*.*$2" <<<"${HEADERS}" >/dev/null
}
verified=0
for attempt in 1 2 3 4 5 6; do
  HEADERS="$(curl -sSI --max-time 20 "${APP_ORIGIN}/" | tr -d '\r' || true)"
  if header_ok Content-Security-Policy "frame-ancestors 'none'" &&
    header_ok Strict-Transport-Security "max-age=" &&
    header_ok X-Frame-Options "DENY"; then
    verified=1
    break
  fi
  info "  headers not all present yet (attempt ${attempt}); waiting."
  sleep 10
done
[[ "${verified}" -eq 1 ]] || die "Content-Security-Policy, Strict-Transport-Security or X-Frame-Options is missing or wrong on the app origin."
info "  ok: Content-Security-Policy, Strict-Transport-Security, X-Frame-Options"
info "Frontend deploy complete: ${ZIP_NAME}"
