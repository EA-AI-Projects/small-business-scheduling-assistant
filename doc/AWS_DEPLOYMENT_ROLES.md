# AWS deployment roles

**Scope:** Current dev role-template configuration and constraints, reviewed 2026-10-14. This is not a live IAM inventory or proof that a changed policy works. Resolve real role/pool/app identifiers privately.

`infra/dev-deploy-roles.yaml` is the reviewable, least-privilege pair of roles used by the dev deployment workflow. It is a plain CloudFormation template deployed as its **own small stack**, separate from `template.yaml`. Creating or changing the role stack needs separate explicit authorization. Placeholders below (`<...>`) are supplied at run time and never committed.

**Account boundary.** The `dev` stack lives in a dedicated member account, so nothing else in the account belongs to another project and the account, not tags, isolates this project. Inside the account the roles still follow least privilege (names scoped to the stack, a Lambda permissions boundary, PassRole limits, escalation denies), which also protects the account's own administrator and deploy roles.

Two roles and six managed policies (one boundary, five attached to the roles), plus an optional seventh (`deploy-<StackName>-deployer-amplify`, created only when `AmplifyAppId` is set), all named `deploy-<StackName>-*` so they never match the `<StackName>-*` scope the execution role is allowed to manage:

| Role | Assumed by | Purpose |
| --- | --- | --- |
| `deploy-scheduling-dev-deployer` | The operator's Identity Center `AdministratorAccess` role in this account (or one explicit IAM principal, see below) | Create, review, execute and delete the dev stack's change sets and stack; run scoped post-deploy validation |
| `deploy-scheduling-dev-cfn-exec` | `cloudformation.amazonaws.com` only, for stack `scheduling-dev` in this account | Create the `template.yaml` resources |
| `deploy-scheduling-dev-lambda-boundary` (managed policy) | Not assumable | Enforced ceiling for the Lambda roles the stack creates (see the tradeoff section) |

### Create the role stack

Run once, from an administrator session in the approved dev account (Identity Center `AdministratorAccess`, signed in with MFA), in `us-west-1`. IAM is global, so create only one role stack per account for a given `StackName`. Do not run these commands until the owner authorizes creating the roles. Set an approved account budget and create the artifact bucket before deploying the runtime stack; keep its actual name in private operator configuration.

**Pre-creation check.** The administrator confirms that no IAM role named `scheduling-dev-*` and none named `deploy-scheduling-dev-*` already exists. The execution role is denied writes to any role without the boundary, so a pre-existing unbounded role would make the first deploy fail rather than be misused, but it should not be there. In a fresh dedicated account there should be none.

Create the change set, review it, then execute it, so the owner sees the final permissions before they exist:

```sh
aws cloudformation deploy --profile <admin profile> --stack-name scheduling-dev-roles --region us-west-1 \
  --template-file infra/dev-deploy-roles.yaml --capabilities CAPABILITY_NAMED_IAM \
  --no-execute-changeset \
  --parameter-overrides ArtifactBucketName=<artifact bucket>
aws cloudformation describe-change-set --profile <admin profile> --stack-name scheduling-dev-roles --region us-west-1 \
  --change-set-name <change set name printed by deploy>
# After the owner reviews the output:
aws cloudformation execute-change-set --profile <admin profile> --stack-name scheduling-dev-roles --region us-west-1 \
  --change-set-name <change set name>
```

Review `IdentityCenterPermissionSetName`, `IdentityCenterRegion` and `RequireMfa` for the operator setup before applying the change set; the template has dev defaults. Then deploy the dev stack with `PermissionsBoundaryArn=<LambdaRoleBoundaryArn output>` added to its parameters. After the dev stack exists, update the role stack with `OwnerUserPoolId=<pool id>` before enabling client account invitations: the Lambda permissions boundary allows `AdminGetUser` and `AdminCreateUser` only for that pool when this parameter is set. Without it, invitation requests fail even though the Lambda role's own policy permits those calls.

For a future `pilot` deployment in another account, prepare an account-specific role stack and deployment runbook; `infra/dev-deploy-roles.yaml` and `scripts/dev/` are restricted to the current dev account and must not be pointed at pilot. Before inviting a client in any account:

1. Create the Cognito user pool and record its actual pool ID. Update the account's Lambda permissions boundary to allow `cognito-idp:AdminGetUser` and `cognito-idp:AdminCreateUser` on **that pool ARN only**. Keep the same two actions in `OwnerApiFunction`'s role policy in `template.yaml`.
2. Review and apply the role-stack change set, then verify that the deployed `OwnerApiFunction` role has the expected boundary and that both Cognito actions evaluate to `allowed` for the pool ARN (for example with `aws iam simulate-principal-policy --policy-source-arn <owner API role ARN> --action-names cognito-idp:AdminGetUser cognito-idp:AdminCreateUser --resource-arns <pool ARN>`). A successful owner login does not exercise these permissions.
3. Provision the approved owner identity link for the deployed business before testing owner login. Then test one explicitly authorized client invitation end to end and confirm its pending record and email delivery. Do not use the first real client invitation to discover missing IAM permissions.

### Who can assume the deployer role (trust design)

The operator does not sign in as an IAM user. They sign in through IAM Identity Center, which places them in a role named `AWSReservedSSO_AdministratorAccess_<random suffix>` at the path `/aws-reserved/sso.amazonaws.com/<region>/`. The suffix is random and changes if the permission set is ever removed and re-assigned, so the deployer role cannot name it in advance. Its trust policy therefore trusts the **account root** with a condition: `ArnLike` on `aws:PrincipalArn` matching `arn:aws:iam::<account>:role/aws-reserved/sso.amazonaws.com/<IdentityCenterRegion>/AWSReservedSSO_<IdentityCenterPermissionSetName>_*`. This is the pattern the AWS IAM Identity Center documentation gives for a role trusting a permission set (an `ArnLike` condition with a wildcard in place of the unique suffix), where the role ARN carries the Identity Center region unless the instance is in `us-east-1`. Trusting the root does not open the role to the whole account: the condition limits it to that one permission-set role, and the caller also needs `sts:AssumeRole` permission, which `AdministratorAccess` has.

- **MFA.** Identity Center sessions do not carry `aws:MultiFactorAuthPresent`, so requiring it would lock the operator out. `RequireMfa` therefore defaults to `false`, and a template rule rejects `RequireMfa=true` unless `TrustedPrincipalArn` is set, so that mistake fails at role-stack creation instead of creating an unassumable role. Enforce MFA at Identity Center sign-in in the operator setup. MFA is proved once per Identity Center session, so the session duration (default 8 hours) is the effective MFA window: the owner should shorten the permission-set and portal session duration in Identity Center settings, for example to 1 to 4 hours. The cached SSO token in `~/.aws/sso/cache` is sensitive while it is valid; do not copy or share it.
- **Name-prefix warning.** The `AWSReservedSSO_<PermissionSetName>_*` pattern also matches any permission set whose name begins with that name plus an underscore (for example `AdministratorAccess_x`). Do not create such permission sets in this account, or narrow `IdentityCenterPermissionSetName`.
- **Alternative operator.** Setting `TrustedPrincipalArn` to one IAM user or role ARN replaces the Identity Center pattern with that exact principal, and `RequireMfa=true` then requires an MFA-authenticated session, for an IAM-user operator with a virtual MFA device.
- **Validation boundary.** The `aws:PrincipalArn` pattern is checked against the AWS documentation and `cfn-lint` only. An authorized assume-role check confirms the deployed configuration; if it is denied, compare the role ARN in `aws sts get-caller-identity` (admin session) with the pattern.

### Assume the deployer role

The deployer role's ARN is in the role stack's `DeployerRoleArn` output. Keep the profiles in `~/.aws/config` and out of the repository.

1. Configure the administrator session once with `aws configure sso` (choose the account, the `AdministratorAccess` permission set, and region `us-west-1`) and sign in with `aws sso login --profile <admin profile>`. The browser sign-in asks for MFA. This profile is used only for one-time and administrator steps.
2. Add the deployer profile that assumes the role from the admin profile:

```ini
[profile scheduling-dev-deployer]
role_arn = <DeployerRoleArn>
source_profile = <admin profile>
region = us-west-1
```

No `mfa_serial` is set: MFA already happened at Identity Center sign-in. The temporary credentials of the assumed role last at most one hour (`MaxSessionDuration`), after which the CLI assumes it again from the still-valid SSO session.

AWS documents `source_profile` as naming another profile that supplies the credentials for `role_arn`. Using an Identity Center profile as that source is standard in AWS CLI v2 (the SDK's source-profile chain includes the SSO provider); the CLI page's wording about "long-term credentials" does not spell the SSO case out, ; validate credential resolution with with `aws sts get-caller-identity --profile scheduling-dev-deployer` on first use. As a backup only, if credentials do not resolve, make the source profile a `credential_process` profile that runs `aws configure export-credentials --profile <admin profile> --format process`. `sam deploy` uses the same profile chain, and needs `--profile scheduling-dev-deployer` explicitly.

Every change-set creation and stack deletion must pass the execution role, or the deployer role's own deny statement rejects it. The deployer role also denies resource-import change sets (`cloudformation:ImportResourceTypes`) as defense in depth; import is never needed here.

```sh
sam deploy --profile scheduling-dev-deployer --stack-name scheduling-dev --region us-west-1 \
  --role-arn <CloudFormationExecutionRoleArn> --s3-bucket <artifact bucket> --s3-prefix scheduling-dev \
  --capabilities CAPABILITY_IAM --no-execute-changeset ...
aws cloudformation delete-stack --profile scheduling-dev-deployer --stack-name scheduling-dev --role-arn <CloudFormationExecutionRoleArn> ...
```

### What each role can do

The template is authoritative for policy statements. Deployer permissions cover:

- Create, describe, execute, delete change sets; describe stack, events and resources; get template; delete stack (only stack `scheduling-dev`; must name the execution role; plus the SAM transform)
- `iam:PassRole` for the execution role only, only to `cloudformation.amazonaws.com`
- Get/put/delete objects and versions, list, delete bucket (only the artifact bucket)
- Cognito `AdminCreateUser`, `AdminGetUser`, `AdminSetUserPassword`, `AdminDeleteUser` (the stack's pool)
- `sns:Publish`, get attributes, list subscriptions (topic `scheduling-alarms-dev`)
- `events:EnableRule`, `DisableRule`, `DescribeRule` (rules `scheduling-dev-*`)
- `amplify:GetApp`, `GetBranch`, `CreateDeployment`, `StartDeployment`, `GetJob` (the one app from `AmplifyAppId`, only when set)
- Lambda invoke, get function and configuration, put/delete reserved concurrency (functions `scheduling-dev-*`); get event source mapping; update event source mapping (to disable one; conditioned on the target function)
- **Denied:** `lambda:UpdateFunctionConfiguration` and `lambda:UpdateFunctionCode`. Environment variables (SMS send switch, recipients, owner number) and code change only through a reviewed change set. Rollback brakes with reserved concurrency 0 and redeploys through the change set
- DynamoDB item read/write and query/scan (table `scheduling-dev` and its indexes)
- DynamoDB describe, `UpdateContinuousBackups`, `DeleteTable` (that table) and `ListBackups`
- Logs `FilterLogEvents`, `GetLogEvents`, describe streams, delete log group (`/aws/lambda/scheduling-dev-*`)
- CloudWatch describe alarms and history, get metric data/statistics, list metrics
- SQS `GetQueueAttributes`, `GetQueueUrl` (queues `scheduling-*-dev`)
- SQS `SendMessage`, `ReceiveMessage`, `DeleteMessage` (queue `scheduling-outbox-dev` only) and `ReceiveMessage`, `DeleteMessage` (queue `scheduling-outbox-dlq-dev` only): the outbox queue proof. **Not granted:** purge, `SetQueueAttributes`, `ChangeMessageVisibility`, message-move tasks, the SMS conversation queues. Changes require a reviewed role-stack change set

Execution role:

- Lambda functions `scheduling-dev-*`, their event source mappings and permissions; HTTP APIs (API Gateway v2); EventBridge rules `scheduling-dev-*`; log groups `/aws/lambda/scheduling-dev-*` with retention and metric filters; CloudWatch alarms `scheduling-dev-*`; table `scheduling-dev` (create and update, **not** delete: it is `DeletionPolicy: Retain` and is removed by hand); queues `scheduling-*-dev`; topic `scheduling-alarms-dev` and its subscription; Cognito user pool, client and domain; read of the artifact prefix so CloudFormation can fetch the Lambda zips.
- IAM: create and manage only roles `scheduling-dev-*`; attach only `AWSLambdaBasicExecutionRole` and `AWSLambdaSQSQueueExecutionRole`; pass those roles only to `lambda.amazonaws.com`. Explicit denies cover attaching `AdministratorAccess*`, `PowerUserAccess` or `IAMFullAccess`, and any IAM action on `deploy-*` roles and policies.
- Read permissions were checked against the published CloudFormation handler permissions for each resource type the SAM output produces (create, read, update, delete, list), limited to the features `template.yaml` uses. Not granted because unused: KMS customer keys, VPC and EFS, table replicas and Kinesis streaming, custom Cognito domains and analytics, and Lambda code signing.
- SAM expansion requires `cloudformation:CreateChangeSet` on the AWS-owned transform `Serverless-2016-10-31`. HTTP API stage tags require `apigateway:TagResource` and `UntagResource`; event-source-mapping reads use the required read scope. Initial checkpoint failures drove these grants (#43); detailed execution history belongs in the issue.
- **SSM is deliberately omitted.** `template.yaml` only writes the `/scheduling/dev/*` parameter paths into the *Lambda roles'* policies; CloudFormation never reads or creates a parameter. The `/scheduling/dev/*` `ssm:GetParameter` grant lives only in the boundary policy.

### Privilege-escalation tradeoff

A CloudFormation role that can create IAM roles is an escalation path unless constrained. This template layers **name-prefix scoping, an allowlist of two attachable AWS managed policies, explicit denies on admin policies, and a permissions boundary that is always enforced** (there is no switch to turn it off). `iam:PutRolePolicy` cannot be filtered by policy content, so without a boundary the execution role could write an arbitrary *inline* policy on a `scheduling-dev-*` role and pass it to a Lambda function it created. The boundary closes that hole: `iam:CreateRole`, `PutRolePermissionsBoundary`, `PutRolePolicy`, `DeleteRolePolicy`, `AttachRolePolicy` and `DetachRolePolicy` are denied unless the target role carries the boundary policy from the role stack (so a pre-existing unbounded `scheduling-dev-*` role cannot be written to), `iam:DeleteRolePermissionsBoundary` and `iam:UpdateAssumeRolePolicy` are always denied, and every IAM action on `deploy-*` roles and policies (including the boundary policy and all five attached policies: edit, new version, detach, delete) is denied. Whatever an inline policy grants, the effective permissions of the function roles cannot exceed the boundary: logs in the stack's `/aws/lambda/scheduling-dev-*` log groups, the dev table, the `scheduling-*-dev` queues, `/scheduling/dev/*` SSM reads, and (once `OwnerUserPoolId` is set) the two invitation-related Cognito actions on that one pool.

The boundary limits **permissions, not who can assume a role**. `CreateRole` accepts any trust policy and no IAM condition key exists for the trust document, so a deliberate template from the deployer could create a bounded role that trusts an external account, which could then use the boundary's permissions (read and write the dev table, read `/scheduling/dev/*` parameters). Editing a trust policy after creation is denied, but the creation-time gap remains and is closed only by change-set review: the owner checks every `AssumeRolePolicyDocument` in the change set for `lambda.amazonaws.com` only.

`template.yaml` has an optional `PermissionsBoundaryArn` parameter (default empty), applied through `Globals.Function.PermissionsBoundary` under a condition. Empty means unchanged behavior for local, CI and any unscoped deployment; **it must be set when deploying through the scoped execution role**, or role creation is denied. Order of creation: role stack first, then the dev stack with `PermissionsBoundaryArn=<LambdaRoleBoundaryArn output>` (also exported as `<role stack name>-LambdaRoleBoundaryArn`).

| Dev stack parameter | Required configuration |
| --- | --- |
| `PermissionsBoundaryArn` | The role stack's `LambdaRoleBoundaryArn` output (never a real ARN in the repository) |

### Known limits of the scoping

- **Account-wide scope for API Gateway and Cognito.** **HTTP APIs** (`apigateway:GET/POST/PUT/PATCH/DELETE` on `/apis/*`) and **Cognito user pools** (`userpool/*`) have random IDs, so the execution role can manage every API and every pool in the account and region, not just this stack's. `cognito-idp:CreateUserPool` has no resource-level scope at all. This is acceptable because the account is dedicated to this project: nothing else in it can be reached. If other workloads are ever placed in the account, this scope must be tightened first.
- **Event source mappings** have no resource type for create, and update and delete match on `*` too, so all three use `Resource: "*"` with the `lambda:FunctionArn` condition; get, tag and list-tags on a mapping cannot use that condition and are open to every mapping in the region. `lambda:ListEventSourceMappings` needs `*`.
- **`ListBackups`, `DescribeAlarms`, alarm history and metric reads, `DescribeLogGroups`, and `DescribeUserPoolDomain`** (and the Logs `DescribeResourcePolicies`) do not support narrower resources. They are read-only.
- The deployer's Cognito admin actions use the `aws:ResourceTag/aws:cloudformation:stack-name` condition until `OwnerUserPoolId` is set. Set that parameter to the actual pool ID before testing invitations: it pins both the deployer's actions and the runtime boundary to the pool. The confused-deputy `aws:SourceArn` condition on the execution role's trust policy must also be checked on first deployment.
- Name-prefix scoping depends on CloudFormation's auto-naming (`<stack>-<LogicalId>-<random>`). Adding a resource with an explicit name in `template.yaml` (for example a queue outside `scheduling-*-dev`) needs a matching change to the execution role.
- A stack stuck in `UPDATE_ROLLBACK_FAILED` needs `ContinueUpdateRollback`, which is not granted; the administrator handles that case.
- **Not covered:** creating the artifact bucket, the Amplify app, and the AWS Budget. The bucket and the Amplify app are one-time steps done from the Identity Center administrator session under the applicable explicit authorization. The budget is created by the owner in the member account, signed in through Identity Center (see [cost planning](DEV_STACK_PLAN.md#cost-planning)). Budgets is used once, so a scoped role would add review cost for little safety. Amplify has an optional narrow add-on for repeat manual deployments (next bullet).
- **Optional Amplify deployment grant (issue #94).** The `AmplifyAppId` role-stack parameter is empty by default, and then the deployer has no Amplify access. After the owner creates the Amplify app, the owner updates the role stack through a reviewed change set with `AmplifyAppId=<app id>` (the segment between `main.` and `.amplifyapp.com` in the app origin; it is never committed). That attaches `deploy-scheduling-dev-deployer-amplify`, which allows only `amplify:GetApp`, `GetBranch`, `CreateDeployment`, `StartDeployment` and `GetJob` on that one app, its `main` branch, and that branch's jobs and deployments (Amplify checks `CreateDeployment` against `branches/main/deployments/*`, which the published Service Authorization Reference does not list; verified on dev, #94). It does not allow creating, updating or deleting apps or branches, `ListApps`, or any other app. Uploading the zip to `owner-app/` needs no new grant: the existing artifact-bucket object grant covers it. Amplify app deletion in teardown stays an administrator step.
- If CloudFormation rejects the execution role's trust conditions (for example, a change-set failure saying the role cannot be assumed), the fallback is for the owner to update the role stack with `TrustCloudFormationSourceArn=false`, which keeps only `aws:SourceAccount`, through a separately reviewed change. If `aws:SourceAccount` is also rejected, the trust policy has to be reviewed again.
- **A pre-existing unbounded `scheduling-dev-*` role can still be passed to a function.** `iam:PassRole` has no `iam:PermissionsBoundary` key, so the boundary deny cannot stop it (it does stop writes to such a role). Repeat the pre-creation role check (no `scheduling-dev-*` roles exist) before every deploy, and during change-set review confirm that every function's `Role` refers to a role created by the stack.
- **Verify on the first authorized run** whether `lambda:FunctionArn` is populated for `DeleteEventSourceMapping`. Create, update and delete are conditioned on it; if delete is denied, stack deletion fails on the two mappings (see the teardown note).
- Historical deployment findings are recorded in [DECISION_HISTORY.md](DECISION_HISTORY.md); they do not prove a changed policy. Run policy validation and review the change set for each applicable deployment.


## Teardown order

Stop at the retention gate in [DEV_STACK_PLAN.md](DEV_STACK_PLAN.md#teardown). Delete the runtime stack and its separately authorized retained resources before deleting the role stack as an administrator. The execution role must outlive resources CloudFormation needs it to delete. Remove local deployer profiles afterward.
