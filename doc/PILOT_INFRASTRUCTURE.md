# Pilot infrastructure plan (code only)

Issue [#23](https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/23) targets AWS account `339713090487` in `us-west-1`. The `template.yaml` stack is a reviewable foundation for the authenticated owner calendar. It has **not** been deployed; provisioning, owner account creation, live SMS, and customer data onboarding each need a separate authorized step.

## Current stack boundary

- The owner HTTP API exposes only the owner API under `/v1/owner/{proxy+}`; the owner web app is a separate static Amplify app (`frontend/`). API Gateway checks a Cognito JWT on the explicit GET, POST, PUT, PATCH, and DELETE owner API routes; there is deliberately no `ANY` or `OPTIONS` route, so the HTTP API CORS configuration answers browser preflight requests without the authorizer; the FastAPI verifier also requires the exact issuer, client ID, access-token type, owner subject, and business ID. The local synthetic API and Twilio webhooks are not routed by this stack.
- A single on-demand DynamoDB table has the `PK`/`SK` primary key, `HoldDueIndex`, and `OutboxDueIndex` expected by the adapters. Point-in-time recovery is enabled and CloudFormation retains the table on stack deletion or replacement. Deleting a stack therefore does **not** delete customer records.
- An encrypted SQS Standard queue and dead-letter queue are defined. The SMS sender has a **disabled** SQS event source and requires `SmsSendEnabled=authorized` plus an explicit recipient allowlist, so deployment alone cannot send a text. Hold expiry, outbox dispatch, note retention, and SMS retention rules are also **disabled**. Operators must only enable them as part of an approved, monitored deployment. In particular, enabling outbox dispatch without its consumer would accumulate messages.
- Lambda logs have 30-day retention. Error alarms cover the API and each worker; the dead-letter queue has a depth alarm. The expiry and outbox workers emit structured JSON with their oldest observed due age, and CloudWatch metric filters alarm when either exceeds 15 minutes. Every alarm publishes to an SNS topic with an email subscription supplied through the required `AlarmEmail` parameter. The mailbox must confirm its subscription before alarms can be relied on. No email address is stored in the repository.
- The Twilio webhook API is conditional and off by default. When enabled, it verifies the exact signed inbound/status URLs and retrieves the auth token from the environment-specific SSM SecureString path `/scheduling/{dev|pilot}/twilio/auth-token` at cold start; a plaintext `String` parameter is rejected. The SQS sender uses the same scoped parameter and refuses to send unless the active ingress API base and both exact callback URLs agree. The token is never in the template or Lambda environment. CloudFormation cannot create the SecureString value. A customer-managed KMS key would need a separately scoped decrypt grant. No token, real phone number, or recipient list belongs in this repository.
- SMS evidence legal-hold management and a GitHub OIDC deploy role remain to be integrated. The conversation layer is still blocked by #24, and this foundation does not authorize real messaging.

## Build and preflight

From the repository root, run `sam validate --lint --template-file template.yaml --region us-west-1` and `sam build --template-file template.yaml`. These commands only validate and build local artifacts. Before any separately authorized deployment, run `aws sts get-caller-identity` and reject any account other than `339713090487`; check the CLI region is `us-west-1`. Use separate `dev` and `pilot` stack names, a unique Cognito domain prefix, and an owner-monitored `AlarmEmail` mailbox. `OwnerAppOrigin` is the exact Amplify owner app origin (`https://host[:port]`, no path or trailing slash). The owner HTTP API's CORS configuration allows only that origin, the owner route methods, and the `Authorization`, `Content-Type`, and `Idempotency-Key` headers, without credentials; API Gateway answers preflight requests itself. The same origin plus `/` is the Cognito callback and logout URL, and the origin reaches the Lambda as `OWNER_APP_ORIGIN`. Until the Amplify app exists, use an inert value such as `https://example.invalid`.

The first stack creation needs placeholders for `OwnerSub` and `OwnerAppOrigin`: the owner subject and the Amplify app origin do not yet exist. Use an inert origin such as `https://example.invalid`, and do not sign in or send any owner API traffic. Create the single owner user. Create the Amplify app with platform `WEB`, `AMPLIFY_MONOREPO_APP_ROOT=frontend`, and the build variables from the stack outputs `OwnerApiUrl`, `OwnerCognitoDomain`, and `OwnerAppClientId`, plus `NEXT_PUBLIC_BUSINESS_ID` equal to the stack's `BusinessId` parameter (see `frontend/README.md`). Then update `OwnerAppOrigin` to the exact Amplify origin and `OwnerSub` to that user's Cognito `sub`. Only after this update should the owner sign in through the hosted UI with authorization code and PKCE. Verify that the Amplify response headers from `customHttp.yml` are present, that a browser preflight is answered by API Gateway, that an unauthenticated owner API call fails and whether that 401 carries `Access-Control-Allow-Origin`, and that only that exact subject can read the pilot business calendar. Seed the documented owner policy through the authenticated API before any booking writes. Do not use a broadly shared owner account.

Keep `EnableSmsIngress=false` until its separately authorized setup is complete. Because `SmsApiUrl` exists only after enabling that resource, use a two-step bootstrap: create the SecureString at the exact `/scheduling/${Environment}/twilio/auth-token` path first; enable ingress with the inert `example.invalid` signed URLs while the Twilio number still points nowhere; read `SmsApiUrl`; immediately update both URL parameters to that exact base plus `/webhooks/sms/inbound` and `/webhooks/sms/status`. Then verify invalid signatures fail and a synthetic valid signature works before configuring Twilio's approved number. Keep the SQS trigger disabled and `SmsSendEnabled=disabled` throughout this bootstrap. Do not accept real traffic until the exact URLs, retention schedules, monitoring, consent gates, and separate live-SMS authorization are in place.

## Integration proof still required

Three opt-in tests in `backend/tests/test_dynamodb_local_races.py` exercise actual conditional transactions and the outbox adapter against [AWS DynamoDB Local](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/DynamoDBLocal.DownloadingAndRunning.html). Start the official local Java server on a loopback port and run `DYNAMODB_LOCAL_URL=http://127.0.0.1:8013 python -m pytest backend/tests/test_dynamodb_local_races.py`. The fixture creates a unique synthetic table with the two due indexes, then deletes it. It rejects non-loopback endpoints. On DynamoDB Local 3.3.1, approval versus expiry and replacement swap versus an adversarial stale cancellation each produced one committed transaction and one conflict, with no partial audit/outbox write. The cancellation bypasses `LifecycleService`, which would reject it while a replacement guard is active; this second check probes repository atomicity rather than a supported user action. A committed approval also produced one due outbox record that the real DynamoDB adapter handed to a fake queue after bounded polling for local GSI visibility; a fake sender claimed it once and duplicate consumption skipped. No SQS or Twilio traffic occurred. These local results do not prove AWS IAM, deployed GSI propagation timing, SQS/DLQ behavior, or deployed Lambda/EventBridge behavior.

Use a synthetic `dev` stack after separate provisioning authorization. Repeat the transaction races in AWS, including two simultaneous approvals for one slot; the expected result is at most one committed reservation, with a conflict or current terminal state and no partial outbox event for losing commands. Check SQS duplicate delivery, worker retry and DLQ redrive with a fake sender, then attach an explicitly authorized SMS test number only after Twilio campaign/number approval and the consent/STOP gates. Verify scheduled note and SMS purge with synthetic expired records and legal holds before onboarding real records. No live AWS integration results are claimed by this document.

## Rollback and monitoring

Keep the previous Lambda artifact/version for a code rollback. Disable schedules and event source mappings before rollback if workers cause errors. Roll back code separately from data; the table is retained and point-in-time recovery is the last-resort data recovery mechanism, not an automatic schema rollback. Before real data or scheduled workers are enabled, confirm the SNS subscription from the `AlarmEmail` mailbox, publish a synthetic test notification to the stack's `AlarmTopicArn`, and verify receipt. A subscription still pending confirmation is not a working destination. Check the `HoldOldestOverdueSeconds` and `OutboxOldestDueAgeSeconds` filters against synthetic worker logs and verify their 15-minute alarm transitions. The age values are the oldest due items **observed in each bounded worker page**, not a full-table maximum. Missing worker runs emit no age metric; the Lambda error alarms and schedule health need separate inspection. Inspect failed Lambda invocations, API errors, queue/DLQ depth, hold expiry age, outbox due age, and retention purge counts. Investigate a DLQ record before replay; the outbox record remains authoritative. Never replay an SMS blindly after an uncertain provider acceptance.

## Synthetic dev deployment checkpoint

This is the proposed **separate** authorization boundary, not an instruction to deploy now. The first change set is a `dev` stack in account `339713090487`, region `us-west-1`, using synthetic records and one owner test account. Show the CloudFormation change set, monthly cost estimate, intended alarm mailbox, and rollback steps to Enrique before executing it. Use a role scoped to the named stack, its resources, and the CloudFormation execution role; a GitHub OIDC deploy role is not present in this repository, but [Dev deployment roles](#dev-deployment-roles) defines a scoped operator role and CloudFormation execution role for review. Do not use broad personal administrator credentials as a substitute for the scoped role. Do not put real credentials or customer records in parameters or change-set output.

1. Confirm account and region, the approved spend limit, and that `AlarmEmail` is an owner-monitored mailbox. Build and lint the exact commit. Use `Environment=dev`, `EnableSmsIngress=false`, `SmsSendEnabled=disabled`, an empty recipient allowlist, and inert Twilio and owner redirect placeholders. Keep every EventBridge schedule and the SQS sender mapping disabled.
2. Create and inspect the change set; execute only after explicit provisioning authorization. Record stack ID, commit, parameter names (not secret values), resource ARNs, and the observed monthly cost baseline. Confirm the SNS email subscription, publish a synthetic notification, and verify receipt before using the alarms as a safety gate.
3. Complete the two-step owner callback/subject bootstrap above. Exercise authenticated calendar read/write with synthetic policy, clients, and appointments; reject a missing JWT and a different subject. Validate API asset routes and compare the rendered calendar with the synthetic records.
4. Run conditional transaction races against the dev table: two approvals for one slot, approval versus expiry, and replacement swap versus a stale competing write. Inspect the base records, audit entries, and outbox after each attempt; a losing transaction must leave no partial changes. Confirm the due GSIs and IAM grants work in the deployed environment.
5. Before enabling hold expiry, load synthetic due holds. Enable that schedule alone, watch its report, age metric, and Lambda errors, and verify due work completes while stale index entries are harmless. Keep the outbox dispatch schedule **disabled**: the stack has no fake SQS consumer, so enabling it would accumulate messages. Test `DispatchService` against the dev table through an integration harness with a fake `IntentQueue` that records handoffs without touching SQS. A later, separately reviewed harness must consume synthetic SQS messages without Twilio and exercise a controlled DLQ record before outbox dispatch can be enabled; do not claim that queue/DLQ gate passed until then. Turn on note and SMS retention schedules only after synthetic expiration and legal-hold checks. Keep the live SMS sender and Twilio number disconnected.
6. If a gate fails, disable the affected schedule or event source, revert the Lambda artifact, and keep the retained table for inspection. Record the failure on #23 or the deployment issue. Tear down only after confirming how retained records, logs, and the Cognito test user will be handled; stack deletion alone does not remove retained data.

## Dev deployment roles

`infra/dev-deploy-roles.yaml` is the reviewable, least-privilege pair of roles the checkpoint above calls for. It is a plain CloudFormation template deployed as its **own small stack**, separate from `template.yaml`. Writing it created nothing: creating the role stack is an account change that needs Enrique's separate explicit authorization, like the checkpoint itself. Placeholders below (`<...>`) are supplied at run time and never committed.

Two roles and six managed policies (one boundary, five attached to the roles), all named `deploy-<StackName>-*` so they never match the `<StackName>-*` scope the execution role is allowed to manage:

| Role | Assumed by | Purpose |
| --- | --- | --- |
| `deploy-scheduling-dev-deployer` | The one operator principal in `TrustedPrincipalArn` | Create, review, execute and delete the dev stack's change sets and stack; run the post-deploy checkpoint actions |
| `deploy-scheduling-dev-cfn-exec` | `cloudformation.amazonaws.com` only, for stack `scheduling-dev` in this account | Create the `template.yaml` resources |
| `deploy-scheduling-dev-lambda-boundary` (managed policy) | Not assumable | Enforced ceiling for the Lambda roles the stack creates (see the tradeoff section) |

### Create the role stack

Run once as an administrator the owner designates, in `us-west-1` (IAM is global, so create only one role stack per account for a given `StackName`). Do not use these commands until the owner authorizes creating the roles.

**Pre-creation check.** The administrator confirms that no IAM role named `scheduling-dev-*` and none named `deploy-scheduling-dev-*` already exists. The execution role is denied writes to any role without the boundary, so a pre-existing unbounded role would make the first deploy fail rather than be misused, but it should not be there.

Create the change set, review it, then execute it, so the owner sees the final permissions before they exist:

```sh
aws cloudformation deploy --stack-name scheduling-dev-roles --region us-west-1 \
  --template-file infra/dev-deploy-roles.yaml --capabilities CAPABILITY_NAMED_IAM \
  --no-execute-changeset \
  --parameter-overrides TrustedPrincipalArn=<operator user or role ARN> \
    ArtifactBucketName=<artifact bucket> RequireMfa=true
aws cloudformation describe-change-set --stack-name scheduling-dev-roles --region us-west-1 \
  --change-set-name <change set name printed by deploy>
# After the owner reviews the output:
aws cloudformation execute-change-set --stack-name scheduling-dev-roles --region us-west-1 \
  --change-set-name <change set name>
```

Then deploy the dev stack with `PermissionsBoundaryArn=<LambdaRoleBoundaryArn output>` added to its parameters. After the dev stack exists, optionally tighten Cognito to that one pool by re-running the command with `OwnerUserPoolId=<pool id>` added.

### Assume the deployer role

The deployer role's ARN is in the role stack's `DeployerRoleArn` output. It requires MFA by default (`RequireMfa=true`). MFA is practical for a CLI session: an IAM user with a virtual MFA device assumes the role with a token code, and the temporary credentials last one hour, so a stolen long-term key alone cannot deploy. Example `~/.aws/config` profile (fill in from the outputs; keep it out of the repository):

```ini
[profile scheduling-dev-deployer]
role_arn = <DeployerRoleArn>
source_profile = <operator profile>
mfa_serial = <operator MFA device ARN>
region = us-west-1
```

The operator is an IAM user, so keep `RequireMfa=true`. The user needs an MFA device registered and its ARN as `mfa_serial`; the CLI then prompts for a token code when the profile first assumes the role. Until a device exists the role cannot be assumed. (`RequireMfa=false` exists only for a federated session that does not carry the MFA flag.)

Every change-set creation and stack deletion must pass the execution role, or the deployer role's own deny statement rejects it:

```sh
sam deploy --profile scheduling-dev-deployer --stack-name scheduling-dev --region us-west-1 \
  --role-arn <CloudFormationExecutionRoleArn> --s3-bucket <artifact bucket> --s3-prefix scheduling-dev \
  --capabilities CAPABILITY_IAM --no-execute-changeset ...
aws cloudformation delete-stack --stack-name scheduling-dev --role-arn <CloudFormationExecutionRoleArn> ...
```

### What each role can do

Deployer role, mapped to the checkpoint step that needs it (step numbers refer to the checkpoint list above; "teardown" and "rollback" numbers refer to sections 3.3 and 3.1 of `doc/DEV_STACK_PLAN.md` as numbered in PR #66):

| Permission (scope) | Step |
| --- | --- |
| Create, describe, execute, delete change sets; describe stack, events and resources; get template; delete stack (only stack `scheduling-dev`; must name the execution role; plus the SAM transform) | 2, teardown 5 |
| `iam:PassRole` for the execution role only, only to `cloudformation.amazonaws.com` | 2 |
| Get/put/delete objects and versions, list, delete bucket (only the artifact bucket) | 2, teardown 10 |
| Cognito `AdminCreateUser`, `AdminGetUser`, `AdminSetUserPassword`, `AdminDeleteUser` (the stack's pool) | 3, teardown 4 |
| `sns:Publish`, get attributes, list subscriptions (topic `scheduling-alarms-dev`) | 2 |
| `events:EnableRule`, `DisableRule`, `DescribeRule` (rules `scheduling-dev-*`) | 5, rollback 1 |
| Lambda invoke, get function and configuration, put/delete reserved concurrency (functions `scheduling-dev-*`); get event source mapping; update event source mapping (to disable one; conditioned on the target function) | 5, rollback 1-2 |
| **Denied:** `lambda:UpdateFunctionConfiguration` and `lambda:UpdateFunctionCode`. Environment variables (SMS send switch, recipients, owner number) and code change only through a reviewed change set. Rollback brakes with reserved concurrency 0 and redeploys through the change set | none |
| DynamoDB item read/write and query/scan (table `scheduling-dev` and its indexes) | 4, 5 |
| DynamoDB describe, `UpdateContinuousBackups`, `DeleteTable` (that table) and `ListBackups` | teardown 2, 6 |
| Logs `FilterLogEvents`, `GetLogEvents`, describe streams, delete log group (`/aws/lambda/scheduling-dev-*`) | 4, 5, teardown 7 |
| CloudWatch describe alarms and history, get metric data/statistics, list metrics | 2, 5 |
| SQS `GetQueueAttributes`, `GetQueueUrl` (queues `scheduling-*-dev`) | 5, rollback 3 |

Execution role:

- Lambda functions `scheduling-dev-*`, their event source mappings and permissions; HTTP APIs (API Gateway v2); EventBridge rules `scheduling-dev-*`; log groups `/aws/lambda/scheduling-dev-*` with retention and metric filters; CloudWatch alarms `scheduling-dev-*`; table `scheduling-dev` (create and update, **not** delete: it is `DeletionPolicy: Retain` and is removed by hand); queues `scheduling-*-dev`; topic `scheduling-alarms-dev` and its subscription; Cognito user pool, client and domain; read of the artifact prefix so CloudFormation can fetch the Lambda zips.
- IAM: create and manage only roles `scheduling-dev-*`; attach only `AWSLambdaBasicExecutionRole` and `AWSLambdaSQSQueueExecutionRole`; pass those roles only to `lambda.amazonaws.com`. Explicit denies cover attaching `AdministratorAccess*`, `PowerUserAccess` or `IAMFullAccess`, and any IAM action on `deploy-*` roles and policies.
- Read permissions were checked against the published CloudFormation handler permissions for each resource type the SAM output produces (create, read, update, delete, list), limited to the features `template.yaml` uses. Not granted because unused: KMS customer keys, VPC and EFS, table replicas and Kinesis streaming, custom Cognito domains and analytics, and Lambda code signing.
- **SSM is deliberately omitted.** `template.yaml` only writes the `/scheduling/dev/*` parameter paths into the *Lambda roles'* policies; CloudFormation never reads or creates a parameter. The `/scheduling/dev/*` `ssm:GetParameter` grant lives only in the boundary policy.

### Privilege-escalation tradeoff

A CloudFormation role that can create IAM roles is an escalation path unless constrained. This template layers **name-prefix scoping, an allowlist of two attachable AWS managed policies, explicit denies on admin policies, and a permissions boundary that is always enforced** (there is no switch to turn it off). `iam:PutRolePolicy` cannot be filtered by policy content, so without a boundary the execution role could write an arbitrary *inline* policy on a `scheduling-dev-*` role and pass it to a Lambda function it created. The boundary closes that hole: `iam:CreateRole`, `PutRolePermissionsBoundary`, `PutRolePolicy`, `DeleteRolePolicy`, `AttachRolePolicy` and `DetachRolePolicy` are denied unless the target role carries the boundary policy from the role stack (so a pre-existing unbounded `scheduling-dev-*` role cannot be written to), `iam:DeleteRolePermissionsBoundary` and `iam:UpdateAssumeRolePolicy` are always denied, and every IAM action on `deploy-*` roles and policies (including the boundary policy and all five attached policies: edit, new version, detach, delete) is denied. Whatever an inline policy grants, the effective permissions of the function roles cannot exceed the boundary: logs in the stack's `/aws/lambda/scheduling-dev-*` log groups, the dev table, the `scheduling-*-dev` queues, and `/scheduling/dev/*` SSM reads.

The boundary limits **permissions, not who can assume a role**. `CreateRole` accepts any trust policy and no IAM condition key exists for the trust document, so a deliberate template from the deployer could create a bounded role that trusts an external account, which could then use the boundary's permissions (read and write the dev table, read `/scheduling/dev/*` parameters). Editing a trust policy after creation is denied, but the creation-time gap remains and is closed only by change-set review: the owner checks every `AssumeRolePolicyDocument` in the change set for `lambda.amazonaws.com` only.

`template.yaml` has an optional `PermissionsBoundaryArn` parameter (default empty), applied through `Globals.Function.PermissionsBoundary` under a condition. Empty means unchanged behavior for local, CI and any unscoped deployment; **it must be set when deploying through the scoped execution role**, or role creation is denied. Order of creation: role stack first, then the dev stack with `PermissionsBoundaryArn=<LambdaRoleBoundaryArn output>` (also exported as `<role stack name>-LambdaRoleBoundaryArn`).
| Dev stack parameter | Value at the checkpoint |
| --- | --- |
| `PermissionsBoundaryArn` | The role stack's `LambdaRoleBoundaryArn` output (never a real ARN in the repository) |

### Known limits of the scoping

- **Account-wide scope for API Gateway and Cognito.** **HTTP APIs** (`apigateway:GET/POST/PUT/PATCH/DELETE` on `/apis/*`) and **Cognito user pools** (`userpool/*`) have random IDs, so the execution role can manage every API and every pool in the account and region, not just this stack's (for example it could rewrite another pool's callback URLs). `cognito-idp:CreateUserPool` has no resource-level scope at all. This is acceptable only while the account is dedicated to this project; otherwise tag conditions must be added.
- **Event source mappings** have no resource type for create, update and delete, so those use `Resource: "*"` with the `lambda:FunctionArn` condition; get, tag and list-tags on a mapping cannot use that condition and are open to every mapping in the region. `lambda:ListEventSourceMappings` needs `*`.
- **`ListBackups`, `DescribeAlarms`, alarm history and metric reads, `DescribeLogGroups`, and `DescribeUserPoolDomain`** (and the Logs `DescribeResourcePolicies`) do not support narrower resources. They are read-only.
- The deployer's Cognito admin actions use the `aws:ResourceTag/aws:cloudformation:stack-name` condition until `OwnerUserPoolId` is set. That relies on CloudFormation tagging the pool with its stack name, which could not be verified because nothing may be created. If step 3 is denied, set `OwnerUserPoolId`. The confused-deputy `aws:SourceArn` condition on the execution role's trust policy likewise could not be exercised offline.
- Name-prefix scoping depends on CloudFormation's auto-naming (`<stack>-<LogicalId>-<random>`). Adding a resource with an explicit name in `template.yaml` (for example a queue outside `scheduling-*-dev`) needs a matching change to the execution role.
- A stack stuck in `UPDATE_ROLLBACK_FAILED` needs `ContinueUpdateRollback`, which is not granted; the administrator handles that case.
- **Not covered:** creating the artifact bucket, the Amplify app, and the AWS Budget. Those one-time owner actions are done with the owner's own credentials under the existing authorization (the packet already lists them as separate steps). Amplify and Budgets have limited resource-level support and are used once, so a scoped role would add review cost for little safety. A narrow add-on can be proposed separately if Enrique prefers.
- If CloudFormation rejects the execution role's trust conditions (`aws:SourceAccount` and `aws:SourceArn` are unverified with CloudFormation; the symptom is a change-set failure saying the role cannot be assumed), the fallback is for the owner to update the role stack with `TrustCloudFormationSourceArn=false`, which keeps only `aws:SourceAccount`, through a separately reviewed change. If `aws:SourceAccount` is also rejected, the trust policy has to be reviewed again.
- Nothing here has been evaluated against a real account. The template passes `cfn-lint` offline; run IAM Access Analyzer policy validation and a change-set review on the first authorized run.

### Teardown order

1. Finish the dev stack teardown in `doc/DEV_STACK_PLAN.md` section 3.3 first: stack deleted (with `--role-arn`, step 5), retained table deleted (step 6), leftover log groups deleted (step 7), artifact bucket emptied and deleted (step 10).
2. Delete the role stack **last**, as the administrator, not through the deployer role: `aws cloudformation delete-stack --stack-name scheduling-dev-roles --region us-west-1`. The roles must outlive the dev stack because CloudFormation needs the execution role to delete the stack's resources; deleting the roles first strands the stack in `DELETE_FAILED`.
3. Remove the operator's AWS CLI profile and confirm no role named `deploy-scheduling-dev-*` remains.

## Expected running costs before deployment

The following is a **planning scenario**, not a business volume forecast or price quote: one owner, 100 appointments and 300 synthetic notification intents per 30-day month, 5,000 owner API calls, a 1 GB table with 1 GB of point-in-time recovery data, 1 GB of logs, and no live SMS. If all four schedules were enabled, hold expiry would run 8,640 times, outbox dispatch 43,200 times, and the two daily retention workers 60 times per month. The initial synthetic stack keeps those schedules disabled, so their invocation charges start only when separately enabled. The fixed monitoring footprint is nine standard alarms with SMS ingress off, ten with it on, and two log-derived custom metrics once workers emit data.

| Cost driver | Planning quantity | Estimate treatment |
| --- | ---: | --- |
| DynamoDB on-demand + PITR | 1 GB table, 1 GB recovery data, low-volume conditional transactions | Include storage, backup, base-table and GSI reads/writes; transaction operations use more request units than ordinary reads/writes. |
| Lambda + EventBridge | About 52,000 scheduled runs/month when enabled, plus API and test traffic | Price request count and 256 MB duration; initial disabled schedules have no invocations. |
| API Gateway + Cognito | 5,000 HTTP API calls, one active owner | Price the HTTP API and selected Cognito user-pool tier; do not assume account-level free-tier eligibility. |
| CloudWatch + SNS | 30-day log retention, 9–10 alarms, 2 custom metrics, one email subscriber | Include log ingestion/storage, metric and alarm hours, and notification delivery. |
| SQS + SSM | 300 intents, low-volume queue operations, one standard SecureString if later configured | Include queue sends/receives and any applicable KMS charges; SMS ingress remains off initially. |
| Twilio and OpenAI | Zero for the synthetic infrastructure check | Price separately before live messaging or model use. |

Use a provisional **$10–$30/month AWS planning envelope** for this low-volume scenario, excluding free-tier credits, taxes, Twilio, OpenAI, a custom domain, and unexpected log or data growth. This is an engineering allowance, not an approved spending cap or a regional price quote. Replace it with a dated [`us-west-1` AWS Pricing Calculator estimate](https://calculator.aws/) and an account budget/alert threshold agreed with Enrique before any stack execution. Current [AWS pricing pages](https://aws.amazon.com/dynamodb/pricing/) describe DynamoDB request/storage charges, [CloudWatch metrics and alarms](https://aws.amazon.com/cloudwatch/pricing/), [Lambda invocations](https://aws.amazon.com/lambda/pricing/), [HTTP API requests](https://aws.amazon.com/api-gateway/pricing/), and [SNS email notifications](https://aws.amazon.com/sns/pricing/); prices and account-level free tiers can change.
