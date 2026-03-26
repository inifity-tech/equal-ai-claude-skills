---
name: cdk-deploy
description: Run CDK synth, diff, and deploy for the myequal-ai-cdk repo against a target environment (test, maxtest, preprod, prod). Handles SSM parameter validation, ECR image seeding, and transient AWS error retries.
disable-model-invocation: false
---

# /cdk-deploy — CDK Infrastructure Deployment Skill

Deploy CDK infrastructure stacks for MyEqual AI environments. Runs synth, diff, and deploy in sequence with approval gates. Automatically handles common blockers (missing SSM params, empty ECR repos, transient AWS errors).

## Parameters

`$ARGUMENTS` can include:
- Environment name: `test`, `maxtest`, `preprod`, `prod` (required)
- `--skip-synth` to skip the synth step
- `--skip-diff` to skip the diff step
- `--auto` to skip approval prompts (use with caution)

Examples: `/cdk-deploy maxtest`, `/cdk-deploy test --skip-synth`, `/cdk-deploy preprod`

---

## Environment Model

| Environment | CDK Context Value | AWS Profile | Approval Required |
|-------------|------------------|-------------|-------------------|
| test / staging | `test` | `ai-dev` | No |
| maxtest | `maxtest` | `ai-dev` | No |
| preprod | `preprod` | `ai-dev` | Yes (always) |
| prod | `prod` | `equalai-prod-cdk` | Yes (always, double confirm) |

**Safety**: For `prod`, always require explicit user confirmation regardless of `--auto` flag.

---

## Step 1: Pre-flight Checks

### 1a. Verify AWS credentials
```bash
aws sts get-caller-identity --profile <profile> 2>&1
```
If this fails, stop and ask the user to fix their AWS credentials.

### 1b. Navigate to CDK repo and pull latest
```bash
cd /Users/akshay/myequal-ai-cdk
git checkout master && git pull origin master
```
Confirm with user before switching branches if working tree is not clean or not on master.

### 1c. Install dependencies and build
```bash
cd /Users/akshay/myequal-ai-cdk
npm install
npm run build
```
If build fails due to missing type definitions, install them:
```bash
cd commons-lib && npm install --save-dev @types/babel__generator @types/babel__template @types/istanbul-lib-report @types/yargs-parser
cd .. && npm run build
```

---

## Step 2: CDK Synth (unless `--skip-synth`)

```bash
cd /Users/akshay/myequal-ai-cdk/equalai
npx cdk synth -c env=<ENV> -c type=cdk-pipeline --profile <PROFILE> --no-change-set
```

Check output for errors. If synth fails, diagnose and report to user.

---

## Step 3: CDK Diff (unless `--skip-diff`)

```bash
cd /Users/akshay/myequal-ai-cdk/equalai
npx cdk diff '**' -c env=<ENV> -c type=cdk-pipeline --profile <PROFILE> --no-change-set
```

Parse the output and present a summary table to the user:
- Count stacks with differences
- For each stack: count additions `[+]`, modifications `[~]`, deletions `[-]` grouped by AWS resource type
- Highlight any IAM or security group changes

Ask for user approval before proceeding to deploy (unless `--auto`).

---

## Step 4: CDK Deploy

```bash
cd /Users/akshay/myequal-ai-cdk/equalai
npx cdk deploy '**' -c env=<ENV> -c type=cdk-pipeline --require-approval never --profile <PROFILE>
```

Run in background with a 10-minute timeout. When complete, check results.

### Success Verification
```bash
grep -E "(✅|❌)" <output_file>
```
All stacks should show ✅.

---

## Step 5: Error Handling & Auto-Recovery

### SSM Parameter Type Mismatch
**Symptom**: `Parameters [...] referenced by template have types not supported by CloudFormation`

**Diagnosis**: Lambda environment variables require `String` type SSM params, not `SecureString`.

**Fix**:
1. Check the parameter type: `aws ssm get-parameter --name "<param>" --profile <PROFILE> --query 'Parameter.Type'`
2. If `SecureString`, check the test env equivalent for the correct value pattern
3. Delete and recreate as `String`:
   ```bash
   aws ssm delete-parameter --name "<param>" --profile <PROFILE>
   aws ssm put-parameter --name "<param>" --value "<value>" --type "String" --profile <PROFILE>
   ```
4. For maxtest, the DB host is: `equalai-maxtest-consolidated-db-public.cb6ogmy0y5jr.ap-south-1.rds.amazonaws.com`
5. For test, the DB host is: `equal-ai-staging-postgres.cb6ogmy0y5jr.ap-south-1.rds.amazonaws.com`

### Missing ECR Images
**Symptom**: `Source image <ECR_URI>:latest does not exist`

**Diagnosis**: Lambda functions need a container image in ECR before CloudFormation can create them.

**Fix**:
1. Install `crane` if not available: `brew install crane`
2. Login to ECR: `crane auth login 545217748861.dkr.ecr.ap-south-1.amazonaws.com -u AWS -p "$(aws ecr get-login-password --region ap-south-1 --profile <PROFILE>)"`
3. Copy from test env:
   ```bash
   crane copy 545217748861.dkr.ecr.ap-south-1.amazonaws.com/<repo>00test:latest \
              545217748861.dkr.ecr.ap-south-1.amazonaws.com/<repo>00<ENV>:latest
   ```
4. If the stack is in `ROLLBACK_COMPLETE`, delete it first:
   ```bash
   aws cloudformation delete-stack --stack-name <stack-name> --profile <PROFILE>
   ```
5. Retry deploy.

### Transient AWS Errors
**Symptom**: `ENOTFOUND`, `SignatureDoesNotMatch: Signature expired`, `ThrottlingException`

**Fix**: Simply retry the deploy. Previously deployed stacks are idempotent (no-ops).

---

## CDK Environment Config Reference

Config files define all environment variables and SSM parameter paths per service:

| Environment | Config File |
|-------------|------------|
| test | `equalai/config/test/equalai-test-config.ts` |
| maxtest | `equalai/config/maxtest/equalai-maxtest-config.ts` |
| preprod | `equalai/config/preprod/equalai-preprod-config.ts` |
| prod | `equalai/config/prod/equalai-prod-config.ts` |

Lambda configs are in sibling files (e.g., `equalai-maxtest-lambda-config.ts`).

---

## Stack Deployment Order

CDK deploys stacks in dependency order:
1. CdkInfraPipeline (self-mutating pipeline)
2. ECRStack (container registries)
3. EventsStack (SNS/SQS)
4. NetworkStack (VPC, subnets)
5. PersistenceStack (DynamoDB)
6. FrontendStack (CloudFront/S3)
7. LoadBalancerStack (ALB)
8. LambdaStack (Lambda functions) — **most likely to fail on new environments**
9. ServicesStack (ECS services)
10. LambdaCiCdStack (Lambda CI/CD pipelines)
11. ServicesStackGrp2/Grp3 (overflow service stacks)
12. CICDStack (Docker service CI/CD pipelines)

---

## Final Report

Present a summary table of all stacks with their deployment status (✅/❌), deployment time, and any issues encountered with their resolutions.
