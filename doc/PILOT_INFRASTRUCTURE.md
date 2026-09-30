# Pilot infrastructure plan (code only)

Issue [#23](https://github.com/EA-AI-Projects/small-business-scheduling-assistant/issues/23) targets AWS account `339713090487` in `us-west-1`. The `template.yaml` stack is a reviewable foundation for the authenticated owner calendar. It has **not** been deployed; provisioning, owner account creation, live SMS, and customer data onboarding each need a separate authorized step.

## Current stack boundary

- The owner HTTP API exposes only the owner API under `/v1/owner/{proxy+}`; the owner web app is a separate static Amplify app (`frontend/`). API Gateway checks a Cognito JWT on the explicit GET, POST, PUT, PATCH, and DELETE owner API routes; there is deliberately no `ANY` or `OPTIONS` route, so the HTTP API CORS configuration answers browser preflight requests without the authorizer; the FastAPI verifier also requires the exact issuer, client ID, access-token type, owner subject, and business ID. The local synthetic API and Twilio webhooks are not routed by this stack.
- A single on-demand DynamoDB table has the `PK`/`SK` primary key, `HoldDueIndex`, and `OutboxDueIndex` expected by the adapters. Point-in-time recovery is enabled and CloudFormation retains the table on stack deletion or replacement. Deleting a stack therefore does **not** delete customer records.
- Two encrypted SQS Standard queues, each with a dead-letter queue, are defined: one for outbox delivery and one for SMS conversation receipts. The SMS sender has a **disabled** SQS event source and requires `SmsSendEnabled=authorized` plus an explicit recipient allowlist, so deployment alone cannot send a text. Hold expiry, outbox dispatch, note retention, and SMS retention rules are also **disabled**. Operators must only enable them as part of an approved, monitored deployment. In particular, enabling outbox dispatch without its consumer would accumulate messages.
- Lambda logs have 30-day retention. Error alarms cover the API and each worker; each dead-letter queue has a depth alarm. The expiry and outbox workers emit structured JSON with their oldest observed due age, and CloudWatch metric filters alarm when either exceeds 15 minutes. Every alarm publishes to an SNS topic with an email subscription supplied through the required `AlarmEmail` parameter. The mailbox must confirm its subscription before alarms can be relied on. No email address is stored in the repository.
- The Twilio webhook API is conditional and off by default. When enabled, it verifies the exact signed inbound/status URLs and retrieves the auth token from the environment-specific SSM SecureString path `/scheduling/{dev|pilot}/twilio/auth-token` at cold start; a plaintext `String` parameter is rejected. The SQS sender uses the same scoped parameter and refuses to send unless the active ingress API base and both exact callback URLs agree. The token is never in the template or Lambda environment. CloudFormation cannot create the SecureString value. A customer-managed KMS key would need a separately scoped decrypt grant. No token, real phone number, or recipient list belongs in this repository.
- SMS evidence legal-hold management and a GitHub OIDC deploy role remain to be integrated. The SMS conversation layer is in the template (a receipt queue, its dead-letter queue, and a worker) but stays inert: it runs only when SMS ingress is on, `SmsSendEnabled=authorized`, and `EnableSmsConversations=authorized`, and it reads an OpenAI key from a SecureString that CloudFormation cannot create. This foundation does not authorize real messaging.

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

This is the proposed **separate** authorization boundary, not an instruction to deploy now. The first change set is a `dev` stack in account `339713090487`, region `us-west-1`, using synthetic records and one owner test account. Show the CloudFormation change set, monthly cost estimate, intended alarm mailbox, and rollback steps to Enrique before executing it. The estimate, the exact resource list and parameters, and the rollback and cleanup plan are in [Synthetic dev stack: pre-authorization packet](DEV_STACK_PLAN.md). Use a role scoped to the named stack, its resources, and the CloudFormation execution role; a GitHub OIDC deploy role is not present in this repository, but [Dev deployment roles](#dev-deployment-roles) defines a scoped operator role and CloudFormation execution role for review. Do not use broad personal administrator credentials as a substitute for the scoped role. Do not put real credentials or customer records in parameters or change-set output.

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

Run once with the owner's own operator IAM user (the same principal that `TrustedPrincipalArn` names, or an administrator the owner designates), in `us-west-1` (IAM is global, so create only one role stack per account for a given `StackName`). This is the **first** thing created, before the artifact bucket and the dev stack. Do not use these commands until the owner authorizes creating the roles. The account is **shared with other proof-of-concept projects**; the roles are scoped so they cannot reach those projects (see "Tag scoping in a shared account" below).

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

Then deploy the dev stack with `PermissionsBoundaryArn=<LambdaRoleBoundaryArn output>` added to its parameters and with `--tags Project=scheduling-dev` (see below). After the dev stack exists, optionally tighten Cognito to that one pool by re-running the command with `OwnerUserPoolId=<pool id>` added.

### Assume the deployer role

The deployer role's ARN is in the role stack's `DeployerRoleArn` output. It requires MFA by default (`RequireMfa=true`). MFA is practical for a CLI session: an IAM user with a virtual MFA device assumes the role with a token code, and the temporary credentials last one hour, so a stolen long-term key alone cannot deploy. Example `~/.aws/config` profile (fill in from the outputs; keep it out of the repository):

```ini
[profile scheduling-dev-deployer]
role_arn = <DeployerRoleArn>
source_profile = <operator profile>
mfa_serial = <operator MFA device ARN>
region = us-west-1
```

The operator is an IAM user, so keep `RequireMfa=true`. The user needs an MFA device registered and its ARN as `mfa_serial`; the CLI then prompts for a token code when the profile first assumes the role. The operator IAM user now has an MFA device (owner answer, 2026-09-30); use its ARN as `mfa_serial`. Because the account is shared, dropping the MFA requirement is not recommended. (`RequireMfa=false` exists only for a federated session that does not carry the MFA flag.)

Every change-set creation and stack deletion must pass the execution role, or the deployer role's own deny statement rejects it:

```sh
sam deploy --profile scheduling-dev-deployer --stack-name scheduling-dev --region us-west-1 \
  --role-arn <CloudFormationExecutionRoleArn> --s3-bucket <artifact bucket> --s3-prefix scheduling-dev \
  --tags Project=scheduling-dev \
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
| Cognito `AdminCreateUser`, `AdminGetUser`, `AdminSetUserPassword`, `AdminDeleteUser` (only a pool tagged `Project=scheduling-dev`, and only the pool in `OwnerUserPoolId` once set) | 3, teardown 4 |
| `cloudformation:TagResource` and `UntagResource` (stack `scheduling-dev`); `CreateChangeSet` is **denied** unless the request carries `Project=scheduling-dev`, and the `Project` tag cannot be removed or changed | 2 |
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

- Lambda functions `scheduling-dev-*`, their event source mappings (by `lambda:FunctionArn`) and permissions; HTTP APIs (API Gateway v2) that carry the `Project=scheduling-dev` tag; EventBridge rules `scheduling-dev-*`; log groups `/aws/lambda/scheduling-dev-*` with retention and metric filters; CloudWatch alarms `scheduling-dev-*`; table `scheduling-dev` (create and update, **not** delete: it is `DeletionPolicy: Retain` and is removed by hand); queues `scheduling-*-dev`; topic `scheduling-alarms-dev` and its subscription; Cognito user pool, client and domain (pools tagged `Project=scheduling-dev` only); read of the artifact prefix so CloudFormation can fetch the Lambda zips.
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

### Tag scoping in a shared account

Account `339713090487` is shared with other proof-of-concept projects (owner answer, 2026-09-30). Lambda functions, roles, rules, alarms, log groups, the table, queues and the topic are scoped by name. **HTTP APIs and Cognito user pools get random IDs**, so they are scoped by a tag instead:

- **Tag:** key `Project`, value = the stack name (`scheduling-dev`). The key is a literal in the policies (a condition key cannot be a template parameter); the value is the role stack's `StackName` parameter, which is also what the roles' name scoping uses. One value therefore ties the roles, the stack and the tag together. The `pilot` stack will use its own stack name and role stack.
- **Where the tag comes from:** (1) `template.yaml` sets it explicitly: `UserPoolTags` on the user pool, and `Tags` on both HTTP APIs (SAM writes it into the OpenAPI body that creates the API and onto the `$default` stage). Checked offline with the SAM translator; the resource count is unchanged (61 with the dev settings, 69 with SMS ingress enabled). (2) `sam deploy --tags Project=scheduling-dev` makes CloudFormation add it to every other taggable resource (functions, roles, queue, topic, table, rules, alarms, log groups, event source mappings), which is what the cost-allocation budget filter uses. The deployer role's `CreateChangeSet` is denied without that stack tag, so it cannot be forgotten.
- **What the execution role may do:** create an API (`POST /apis`) or a user pool only with the tag in the request; change or delete an existing API (and its routes, integrations, authorizers, stages, deployments) or user pool (and its clients and domain) only if it carries the tag; `UpdateUserPool` and `TagResource` on a pool need only the resource tag. The tag cannot be removed (`aws:TagKeys` on untag) or set to another value (`aws:RequestTag` on tag), for API Gateway, Cognito and the stack. The deployer role cannot create a resource-import change set, which closes the one path that could adopt another project's API.
- **Verified against the AWS service authorization reference** (`servicereference.us-east-1.amazonaws.com`, fetched anonymously 2026-09-30; no AWS account call): `apigateway:POST`, `PUT`, `PATCH`, `DELETE` list `aws:RequestTag` and `aws:TagKeys`; the Api, Route, Integration, Authorizer, Deployment and Stage resource types list `aws:ResourceTag`; the Tags resource type (`/tags/{arn}`) lists no resource condition key. `cognito-idp:CreateUserPool` lists `aws:RequestTag`; `TagResource` lists `aws:RequestTag` and `aws:TagKeys`; `UpdateUserPool` lists `aws:RequestTag`; `UntagResource` lists `aws:TagKeys`; the `userpool` resource type (the resource of every client, domain and admin action) lists `aws:ResourceTag`. `cloudformation:CreateChangeSet` lists `aws:RequestTag`, `aws:TagKeys` and `cloudformation:RoleArn`; the change-set and execute operations also authorize `cloudformation:TagResource` and `UntagResource`. `lambda:FunctionArn` is listed for Create, **Get**, Update and Delete of an event source mapping (the earlier assumption that Get lacked it was wrong, so Get is now conditioned too).

Per action, what is tag-scoped now and what is not:

| Action (service) | Scope now | Why |
| --- | --- | --- |
| `apigateway:POST` on `/apis` (CreateApi) | Requires `aws:RequestTag/Project` | Supported |
| `apigateway:PUT` on `/apis` (ImportApi) | **Not tag-conditioned** | SAM emits an OpenAPI body, so this is the call CloudFormation really makes. The reference lists `aws:RequestTag` for `PUT /apis`, but ImportApi has no tags parameter (tags come from the OpenAPI body) and it is not known whether IAM evaluates the body tags, so requiring the key could deny every create. It can create a new, untagged API; it cannot modify an existing one. An untagged API cannot be deleted by the execution role (deletes need the tag), so stack deletion would end in `DELETE_FAILED` and the administrator cleans it up. Residual: clutter and cost, no access to other projects' APIs |
| `apigateway:PUT/PATCH/POST/DELETE` on `/apis/*` (update, reimport, delete, routes, integrations, authorizers, stages, deployments) | Requires `aws:ResourceTag/Project` | Resource types list `aws:ResourceTag`. **Unverified:** whether it resolves to the parent API's tag for sub-resource paths. The API Gateway developer guide documents tag inheritance for attribute-based access control only for v1 APIs, and v2 routes and integrations are not tagged. The specific call at risk is `CreateStage` (`POST /apis/{id}/stages`) for the `$default` stage; routes, integrations and the authorizer come in the OpenAPI body, and later body changes are `PUT /apis/{id}`, which checks the API's own tag. See "If CreateStage is denied" below |
| `apigateway:GET` on `/apis`, `/apis/*`, `/tags/*` | Account-wide, read-only | List has no single resource, and CloudFormation reads an API right after creating it. Other projects' API metadata, including exports, is readable |
| `apigateway:POST/PUT` on `/tags/*` (tag) | Requires `aws:RequestTag/Project` = stack name; a different value is denied | The Tags resource type has no `aws:ResourceTag`, so the **target** API cannot be checked. Overwriting another project's API tag through this path needs CloudFormation to adopt that API, which requires a resource-import change set; the deployer role denies those (`DenyResourceImport`, `cloudformation:ImportResourceTypes`), and a create or update on a foreign API ID fails the `aws:ResourceTag` check before any tag call. The `/tags/*` writes are not narrowed to the URL-encoded `/apis/` ARN prefix: the exact encoding IAM matches on could not be confirmed from AWS documentation, and a wrong pattern would deny every tag call. Requires the right value in the request; also protected by the deny on other values |
| `cloudformation:CreateChangeSet` with `--change-set-type IMPORT` | Denied on the stack | Import is never needed for this stack |
| `apigateway:DELETE/PATCH` on `/tags/*` (untag) | Allowed except when `aws:TagKeys` contains `Project` | Protects the tag |
| `cognito-idp:CreateUserPool` | Requires `aws:RequestTag/Project` | Supported |
| `cognito-idp:UpdateUserPool`, `TagResource` | Requires `aws:ResourceTag/Project`; setting `Project` to any value other than the stack name is denied | CloudFormation tags a pool by diff, so requiring the tag on every request would deny legitimate changes to other tags |
| Other pool, client, domain and MFA actions, including `DeleteUserPool`, `UntagResource`, `ListTagsForResource` | Requires `aws:ResourceTag/Project` | All use the `userpool` ARN |
| `cognito-idp:UntagResource` of `Project` | Denied | Protects the tag |
| `cognito-idp:DescribeUserPoolDomain` | Account-wide, read-only | No resource type in the reference; reveals only whether a domain prefix is taken |
| Deployer `AdminCreateUser`, `AdminGetUser`, `AdminSetUserPassword`, `AdminDeleteUser` | Requires `aws:ResourceTag/Project` (and the pool ID once `OwnerUserPoolId` is set) | Replaces the `aws:cloudformation:stack-name` condition, which was unverified |
| `cloudformation:CreateChangeSet` | Denied without `aws:RequestTag/Project` = stack name | Guarantees stack-tag propagation |
| `cloudformation:TagResource/UntagResource` | Named stack and change sets only; `Project` cannot be removed or changed | Protects the tag |
| `lambda:Create/Get/Update/DeleteEventSourceMapping` | `lambda:FunctionArn` is a `scheduling-dev-*` function | Random ID, so the function is the scope |
| `lambda:TagResource`, `UntagResource`, `ListTags` on `event-source-mapping:*` | Region-wide | The FunctionArn key is not listed for them. Tagging another project's mapping does not let this role change or delete it |
| `lambda:ListEventSourceMappings`, `logs:DescribeLogGroups`, `logs:DescribeResourcePolicies`, `cloudwatch:DescribeAlarms` and metric reads, `dynamodb:ListBackups` | Account-wide, read-only | No resource-level support. They expose other projects' names and metadata but change nothing |

Everything else in the roles is scoped by ARN or name, unchanged.

### If CreateStage is denied

If IAM does not resolve the API's tag for `POST /apis/{id}/stages`, the first create ends in `ROLLBACK_COMPLETE` with the retained table left behind. Recovery: delete the failed stack and the retained synthetic table by hand (the deployer role can do both; an API imported before the failure may need the administrator), then the owner authorizes a role-stack change. The fallback below is drafted now so the owner can review it together with the roles. It is **not** in the template.

If `aws:ResourceTag` does not resolve for the stage path, no IAM condition can tie `POST /apis/{id}/stages` to a tagged API: the request carries the stage's own tags, which the caller controls, and the API ID is random. A `RequestTag`-only allow would therefore let the role add a stage (with auto-deploy, which publishes that API's routes at a new URL) to another project's API whose ID is known, so it is **not** an acceptable fallback. The drafted option pins the exact API ID, and needs the ID to exist and be stable before the stage is created:

```yaml
# Fallback, not applied. New role-stack parameter HttpApiIdForStages: the ID of an API that
# already exists and carries the Project tag. Allows stage changes on that one API only.
- Sid: HttpApiStagesPinned
  Effect: Allow
  Action: [apigateway:POST, apigateway:PATCH, apigateway:DELETE]
  Resource:
    - !Sub 'arn:${AWS::Partition}:apigateway:${AWS::Region}::/apis/${HttpApiIdForStages}/stages'
    - !Sub 'arn:${AWS::Partition}:apigateway:${AWS::Region}::/apis/${HttpApiIdForStages}/stages/*'
```

The catch: a failed create rolls the API back and a retry gets a new ID, so this works only if the API ID is fixed in advance (for example a two-step deploy where the owner reviews the first, API-only, result). If the owner cannot accept that process, the only remaining choice is an explicitly accepted wider grant, which is the owner's risk decision and must not be added silently.

### Known limits of the scoping

- **Changing stack tags later.** CloudFormation tags API Gateway resources by diff, and every tag call must carry `Project`. Changing any stack tag other than `Project` after creation sends a tag call without it, which is denied. Update the role stack first (a reviewed change) before changing other stack tags on the dev stack.

- **Shared account.** Other projects' Lambda functions, roles, rules, alarms, log groups, tables, queues, topics, HTTP APIs and user pools are out of reach for writes, with the exceptions in the table above: the untagged `ImportApi` create, tagging through `/tags/*`, and the unverified sub-resource tag resolution. Reads of names and metadata are not restricted where listed. Two other risks come from the account being shared: an IAM policy on another project's principals cannot be seen or constrained from here, and the account's service quotas (for example Cognito domain prefixes) are shared.
- **Event source mappings** have no resource type for create, so create, get, update and delete use `Resource: "*"` with the `lambda:FunctionArn` condition; tag and list-tags on a mapping cannot use that condition and are open to every mapping in the region. `lambda:ListEventSourceMappings` needs `*`.
- **`ListBackups`, `DescribeAlarms`, alarm history and metric reads, `DescribeLogGroups`, and `DescribeUserPoolDomain`** (and the Logs `DescribeResourcePolicies`) do not support narrower resources. They are read-only.
- **Tag-scoping assumptions not testable offline.** That CloudFormation's stack tags reach the API Gateway and Cognito resources, that IAM resolves the API's tag for its sub-resource paths, and that a Cognito pool is created with its tags in `CreateUserPool` (required by the create condition; if CloudFormation tags it separately, the create is denied and the fix is a reviewed change). The first authorized run confirms them; each failure mode is AccessDenied on a change (closed), never a wider grant. The confused-deputy `aws:SourceArn` condition on the execution role's trust policy likewise could not be exercised offline.
- Name-prefix scoping depends on CloudFormation's auto-naming (`<stack>-<LogicalId>-<random>`). Adding a resource with an explicit name in `template.yaml` (for example a queue outside `scheduling-*-dev`) needs a matching change to the execution role.
- A stack stuck in `UPDATE_ROLLBACK_FAILED` needs `ContinueUpdateRollback`, which is not granted; the administrator handles that case.
- **Not covered:** creating the artifact bucket, the Amplify app, and the AWS Budget. Those one-time owner actions are done with the owner's own credentials under the existing authorization (the packet already lists them as separate steps). Amplify and Budgets have limited resource-level support and are used once, so a scoped role would add review cost for little safety. A narrow add-on can be proposed separately if Enrique prefers. Tag the Amplify app and the artifact bucket `Project=scheduling-dev` when creating them, so their cost is included in the tag-filtered budget ([budget approach](DEV_STACK_PLAN.md#16-approved-aws-budget-scoped-to-this-project)).
- If CloudFormation rejects the execution role's trust conditions (`aws:SourceAccount` and `aws:SourceArn` are unverified with CloudFormation; the symptom is a change-set failure saying the role cannot be assumed), the fallback is for the owner to update the role stack with `TrustCloudFormationSourceArn=false`, which keeps only `aws:SourceAccount`, through a separately reviewed change. If `aws:SourceAccount` is also rejected, the trust policy has to be reviewed again.
- **A pre-existing unbounded `scheduling-dev-*` role can still be passed to a function.** `iam:PassRole` has no `iam:PermissionsBoundary` key, so the boundary deny cannot stop it (it does stop writes to such a role). Repeat the pre-creation role check (no `scheduling-dev-*` roles exist) before every deploy, and during change-set review confirm that every function's `Role` refers to a role created by the stack.
- **Verify on the first authorized run** whether `lambda:FunctionArn` is populated for `DeleteEventSourceMapping`. Create, update and delete are conditioned on it; if delete is denied, stack deletion fails on the two mappings (see the teardown note).
- Nothing here has been evaluated against a real account. The template passes `cfn-lint` offline; run IAM Access Analyzer policy validation and a change-set review on the first authorized run.

### Teardown order

1. Finish the dev stack teardown in `doc/DEV_STACK_PLAN.md` section 3.3 first: stack deleted (with `--role-arn`, step 5), retained table deleted (step 6), leftover log groups deleted (step 7), artifact bucket emptied and deleted (step 10).
2. Delete the role stack **last**, as the administrator, not through the deployer role: `aws cloudformation delete-stack --stack-name scheduling-dev-roles --region us-west-1`. The roles must outlive the dev stack because CloudFormation needs the execution role to delete the stack's resources; deleting the roles first strands the stack in `DELETE_FAILED`.
   If stack deletion fails on the event source mappings (an AccessDenied on `DeleteEventSourceMapping`), the fix is a reviewed change to the role stack (for example dropping the `lambda:FunctionArn` condition on delete), applied by the administrator before retrying. Do not work around it with broader credentials.
3. Remove the operator's AWS CLI profile and confirm no role named `deploy-scheduling-dev-*` remains.

## Expected running costs before deployment

The dated `us-west-1` estimate is in [Synthetic dev stack: pre-authorization packet](DEV_STACK_PLAN.md#1-cost-estimate-for-us-west-1). It prices the stack from the public AWS Price List (not a Pricing Calculator share link) for an idle stack, an active verification month, and a worst case with every schedule enabled all month. It replaces the earlier provisional planning envelope. The estimate is not a spending cap. Enrique approved a $10 per month budget for the `dev` checkpoint on 2026-09-30 (actual alerts at $5, $8 and $10, forecast alert at $10), filtered to the `Project=scheduling-dev` cost-allocation tag because the account is shared; the `pilot` stack needs its own. The fixed monitoring footprint is eleven standard alarms with SMS ingress off and twelve with it on, plus two log-derived custom metrics once workers emit data. If all four schedules were enabled, hold expiry would run 8,640 times, outbox dispatch 43,200 times, and the two daily retention workers 60 times per month; the initial synthetic stack keeps them disabled. Twilio, OpenAI, taxes, and a custom domain are priced separately. Prices and account-level free tiers change, so refresh the estimate before authorizing.
