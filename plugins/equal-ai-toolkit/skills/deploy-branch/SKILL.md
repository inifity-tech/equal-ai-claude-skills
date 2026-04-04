---
name: deploy-branch
description: "Deploy a feature branch to a target environment (test, preprod, maxtest, typists) via AWS CodePipeline, or update environment variables on an ECS service without redeploying code. Also handles Statsig feature flag/gate/config updates when deploying features that depend on them. Use this skill whenever the user wants to deploy their current branch, push changes to an environment for testing, deploy a service to test/preprod/maxtest/typists, or mentions 'deploy to <env>', 'push to staging', 'test my changes on maxtest', 'deploy this to preprod', 'ship to typists'. Also triggers when the user says things like 'I need to test this on test env', 'can you deploy this', 'put this on maxtest', 'update preprod with my changes', or any variation of deploying code to a non-production environment. Also triggers for standalone env var updates like 'add WEBHOOK_TIMEOUT to maxtest', 'update env var on preprod', 'set X=Y on the backend in test', 'change the timeout config on maxtest'. Also triggers when user mentions Statsig alongside deployment: 'deploy and enable the feature gate', 'deploy this and turn on the flag on maxtest', 'deploy and update statsig'. If the user mentions environment variable changes or Statsig updates alongside deployment, handle those too."
disable-model-invocation: false
---

# Deploy Branch

Deploy a feature branch to a target environment by updating the AWS CodePipeline source branch and triggering the pipeline. Also supports standalone environment variable updates (no code deploy — just task definition update + ECS service restart). Handles the test environment's staging-branch merge flow, parallel multi-service deployments, environment variable updates, Statsig feature flag management, and post-deploy Datadog validation.

## Two Modes of Operation

This skill operates in two modes based on what the user asks for:

1. **Full deployment** (default) — Update pipeline branch, trigger CodePipeline, optionally handle env var changes, update Statsig feature flags if needed, validate via Datadog. Follow the full Deployment Flow (Steps 1-7).

2. **Env-var-only update** — No code deploy. Just update the ECS task definition with new/changed env vars and force a new ECS deployment. Use this when the user only wants to add, change, or remove environment variables on a running service without deploying new code. Follow the Env-Var-Only Flow below.

**How to detect the mode**: If the user's request is purely about environment variables (e.g., "add WEBHOOK_TIMEOUT=30 to backend on maxtest", "update the env var X on preprod", "set REDIS_POOL_SIZE=20 on user-services in test") and does NOT mention deploying code/branch changes, use the env-var-only flow. If the request mentions deploying, pushing, or testing code changes, use the full deployment flow (which may also include env var updates as part of Step 5).

---

## Env-Var-Only Flow

Use this when the user wants to update environment variables without deploying new code.

### Step 1: Resolve Inputs

1. **Service(s)**: From user request or cwd (same detection as full deploy)
2. **Target environment**: Parse from user request
3. **Env var changes**: Parse from user request. The user may specify:
   - Explicit values: "set WEBHOOK_TIMEOUT=30"
   - Auto-detect: "add the new settings from my branch" — read the settings file and diff against current task def

### Step 2: Get Current Task Definition

```bash
aws ecs describe-task-definition \
  --task-definition "eai00<service>00<env>" \
  --profile ai-dev \
  --region ap-south-1 \
  --query 'taskDefinition'
```

Extract the current `environment` array from the container definition.

### Step 3: Determine Changes

- If the user gave explicit key=value pairs, use those directly
- If the user asked to auto-detect, read the service's settings file and compare Pydantic `Settings` fields (uppercased) against the task definition's current env vars

Show the user a summary of what will change:
```
Env var changes for ai-backend on maxtest:
  + WEBHOOK_TIMEOUT = 30  (new)
  ~ REDIS_POOL_SIZE = 20  (was: 10)
  - OLD_FEATURE_FLAG       (remove)

Proceed?
```

Wait for confirmation.

### Step 4: Register New Task Definition

Save the current task definition to a temp file, strip the fields AWS rejects on register (`taskDefinitionArn`, `revision`, `status`, `requiresAttributes`, `compatibilities`, `registeredAt`, `registeredBy`), apply the env var changes to the `environment` array, then register:

```bash
aws ecs register-task-definition \
  --cli-input-json file:///tmp/taskdef-<service>-<env>-updated.json \
  --profile ai-dev \
  --region ap-south-1
```

### Step 5: Update ECS Service

Force a new deployment with the updated task definition:

```bash
aws ecs update-service \
  --cluster "equalai-<env>" \
  --service "eai00<service>00<env>" \
  --task-definition "eai00<service>00<env>" \
  --force-new-deployment \
  --profile ai-dev \
  --region ap-south-1
```

Wait for the service to stabilize:

```bash
aws ecs wait services-stable \
  --cluster "equalai-<env>" \
  --services "eai00<service>00<env>" \
  --profile ai-dev \
  --region ap-south-1
```

### Step 6: Validate via Datadog

Same as the full deployment flow — check for startup errors and confirm the service is healthy using `mcp__datadog__get_logs`.

### Step 7: Clean Up

Remove temp files (`/tmp/taskdef-*.json`).

Report summary:
```
## Env Var Update Summary — <env>

| Service | Changes | ECS Status | Datadog Health |
|---------|---------|------------|----------------|
| ai-backend | +WEBHOOK_TIMEOUT=30 | Stabilized (2/2 tasks) | Healthy |
```

For multi-service env-var-only updates, spawn parallel subagents (same as full deploy).

---

## Environment Model

| Environment | AWS Profile | Branch Strategy | Approval |
|-------------|-------------|-----------------|----------|
| test | `ai-dev` | Merge into `staging`, push staging, trigger pipeline | No |
| maxtest | `ai-dev` | Update pipeline source to feature branch directly | No |
| preprod | `ai-dev` | Update pipeline source to feature branch directly | Confirm with user |
| typists | `ai-dev` | Update pipeline source to feature branch directly | No |

**Region**: `ap-south-1` (all environments)

## Resource Naming

All resources follow CDK naming conventions:

| Resource | Pattern | Example |
|----------|---------|---------|
| CodePipeline | `EAI00<service>00<env>` | `EAI00ai-backend00maxtest` |
| ECS Cluster | `equalai-<env>` | `equalai-maxtest` |
| ECS Service | `eai00<service>00<env>` | `eai00ai-backend00maxtest` |
| Task Definition | `eai00<service>00<env>` | `eai00ai-backend00maxtest` |

### Service Registry

| Service Key | Datadog Tag | Code Path | Settings File |
|-------------|-------------|-----------|---------------|
| ai-backend | `ai-backend` | `myequal-ai-backend/backend/` | `backend/settings.py` |
| user-services | `user-services` | `myequal-ai-user-services/app/` | `app/settings.py` |
| memory-service | `memory-service` | `memory-service/app/` | `app/settings.py` |
| api-gateway | `api-gateway` | `myequal-api-gateway/app/` | `app/settings.py` |
| post-processing | `post-processing-service` | `myequal-post-processing-service/app/` | `app/settings.py` |
| evaluations | `evaluations` | `myequal-evaluations/core/` | `core/settings.py` |

### Directory-to-Service Mapping

Detect the service from the current working directory:

| Directory Contains | Service |
|-------------------|---------|
| `myequal-ai-backend` | ai-backend |
| `myequal-ai-user-services` | user-services |
| `memory-service` | memory-service |
| `myequal-api-gateway` | api-gateway |
| `myequal-post-processing-service` | post-processing |
| `myequal-evaluations` | evaluations |

If the cwd doesn't match any service, ask the user which service(s) to deploy.

---

## Statsig Configuration

The Statsig Console API key is stored in `.claude/config/toolkit-config.yaml` under:

```yaml
statsig:
  console_api_key: console-XXXXXXXXXXXX
  environment_mapping:
    test: development
    maxtest: staging
    preprod: staging
    typists: development
```

### Retrieving the API Key

1. Read `.claude/config/toolkit-config.yaml` and look for `statsig.console_api_key`
2. If the key exists and is not the placeholder (`console-XXXXXXXXXXXX`), use it
3. If the key is missing or is the placeholder, ask the user:
   > "This feature uses Statsig feature flags. I need a Console API key to update them. You can find it in Statsig Console → Project Settings → Keys & Environments → Console API Keys (starts with `console-`). Paste it here and I'll save it so you don't need to provide it again."
4. Once the user provides the key, write it back to `toolkit-config.yaml` under `statsig.console_api_key` so future runs pick it up automatically

The Console API key (`console-*`) is different from the Server SDK key (`secret-*`). The server key returns 403 on Console API endpoints.

### Environment Tier Mapping

Equal AI environments map to Statsig tiers. When updating a gate's environment-scoped rules, use this mapping:

| Deploy Environment | Statsig Tier |
|-------------------|--------------|
| test | `development` |
| maxtest | `staging` |
| preprod | `staging` |
| typists | `development` |

If the config has a custom `statsig.environment_mapping`, use that instead of these defaults.

---

## Statsig Auto-Detection

During Step 1 (Resolve Inputs), scan the code changes for Statsig dependencies. This tells you whether the feature being deployed relies on feature gates, dynamic configs, or experiments that may need to be enabled in the target environment.

### What to Scan

Run a diff of the feature branch against the base branch to find Statsig references:

```bash
# For maxtest/preprod/typists — diff against main
git diff origin/main..HEAD -- '*.py' '*.kt' '*.ts' '*.js'

# For test — diff against staging
git diff origin/staging..HEAD -- '*.py' '*.kt' '*.ts' '*.js'
```

### Patterns to Look For

Search the diff output for these patterns (case-insensitive):

**Python (backend services)**:
- `check_gate("gate_name")` / `check_gate('gate_name')`
- `get_config("config_name")` / `get_config('config_name')`
- `get_experiment("experiment_name")` / `get_experiment('experiment_name')`
- `statsig.check_gate(` / `statsig.get_config(` / `statsig.get_experiment(`
- String constants like `STATSIG_GATE_*` or `StatsigGate.*`

**Kotlin (mobile app)**:
- `checkGate("gate_name")` / `checkGateWithExposureLoggingDisabled(`
- `getConfig("config_name")`
- `getExperiment("experiment_name")`

**TypeScript/JavaScript**:
- `checkGate("gate_name")`
- `getConfig("config_name")`
- `getExperiment("experiment_name")`

Extract the gate/config/experiment names from the matched patterns. Only look at **added** lines (lines starting with `+` in the diff) — removed lines don't need Statsig updates.

### Detection Result

If Statsig references are found in the diff, inform the user early:

> "I detected Statsig dependencies in your code changes:
> - Gate: `eai_new_voice_model` (in `backend/services/voice.py`)
> - Dynamic config: `voice_model_params` (in `backend/services/voice.py`)
>
> After deployment, I'll check their status in Statsig and help you enable them for the target environment."

If the user explicitly mentions Statsig updates in their request (e.g., "deploy and enable the feature gate"), skip auto-detection and ask them for the gate/config names directly.

---

## Deployment Flow

### Step 1: Resolve Inputs

1. **Service(s)**: Detect from cwd or parse from user request. If multiple services are mentioned, deploy them in parallel using subagents.
2. **Target environment**: Parse from user request. Must be one of: `test`, `maxtest`, `preprod`, `typists`.
3. **Current branch**: Run `git rev-parse --abbrev-ref HEAD` in the service repo.
4. **Env var changes**: Check if the user mentioned any. Also auto-detect by diffing the settings file against the target branch (see Step 5).
5. **Statsig dependencies**: Run the Statsig Auto-Detection scan (see section above) to identify any feature gates, dynamic configs, or experiments in the code diff. Note the results — they'll be used in Step 7.

If the target is **preprod**, confirm with the user before proceeding — preprod is a shared pre-production environment and deploying to it may affect other team members' testing.

### Step 2: Prepare the Branch

#### For `test` environment (staging merge flow)

The test environment always deploys from the `staging` branch. To get your changes there:

```bash
# Ensure we have latest staging
git fetch origin staging

# Checkout staging and merge the feature branch
git checkout staging
git merge origin/staging --ff-only  # fast-forward to latest remote staging first
git merge <feature-branch> --no-edit  # merge feature branch in

# Push staging
git push origin staging

# Return to feature branch
git checkout <feature-branch>
```

If the merge has conflicts, stop and tell the user. Do NOT force-push or resolve conflicts automatically — the user needs to handle merge conflicts themselves.

#### For `maxtest`, `preprod`, `typists` (direct branch deploy)

Ensure the feature branch is pushed to the remote:

```bash
git push origin <feature-branch> -u
```

Then update the CodePipeline source to point at this branch:

```bash
# Get current pipeline definition
aws codepipeline get-pipeline \
  --name "EAI00<service>00<env>" \
  --profile ai-dev \
  --region ap-south-1 \
  --output json > /tmp/pipeline-<service>-<env>.json
```

Parse the JSON, find the Source stage action, and update the `BranchName` (for CodeStar/CodeConnections source) or `Branch` (for CodeCommit source) configuration to the feature branch name. Remove the `metadata` field from the top level (AWS rejects it on update).

```bash
# Update the pipeline with modified branch
aws codepipeline update-pipeline \
  --pipeline file:///tmp/pipeline-<service>-<env>-modified.json \
  --profile ai-dev \
  --region ap-south-1
```

### Step 3: Trigger the Pipeline

```bash
aws codepipeline start-pipeline-execution \
  --name "EAI00<service>00<env>" \
  --profile ai-dev \
  --region ap-south-1
```

Capture the `pipelineExecutionId` from the response.

### Step 4: Monitor Pipeline Execution

Poll the pipeline execution status until it completes or fails:

```bash
aws codepipeline get-pipeline-execution \
  --pipeline-name "EAI00<service>00<env>" \
  --pipeline-execution-id <execution-id> \
  --profile ai-dev \
  --region ap-south-1
```

Check every 30 seconds. Report status transitions to the user (e.g., "Source stage complete, Build stage in progress...").

If the pipeline fails, fetch the failed stage details:

```bash
aws codepipeline list-action-executions \
  --pipeline-name "EAI00<service>00<env>" \
  --filter "pipelineExecutionId=<execution-id>" \
  --profile ai-dev \
  --region ap-south-1
```

Report the failure details and stop. Don't retry automatically — let the user decide.

### Step 5: Handle Environment Variable Changes

This step runs in parallel with pipeline monitoring if env var changes are needed.

#### Auto-detection

Compare the service's settings file against what's currently in the task definition:

```bash
# Get current task definition
aws ecs describe-task-definition \
  --task-definition "eai00<service>00<env>" \
  --profile ai-dev \
  --region ap-south-1
```

Extract the current environment variables from the container definition. Then read the service's settings file from the feature branch and identify any new or changed settings that map to environment variables.

Look for Pydantic `Settings` classes — each field typically maps to an env var (the field name uppercased). Compare against the task definition's current env vars to find additions or changes.

#### Applying env var changes

If changes are detected (or the user explicitly requests them):

1. Show the user what will change (added/modified/removed env vars) and get confirmation.

2. Register a new task definition revision with the updated environment:

```bash
# Get current task def, strip fields AWS rejects on register
aws ecs describe-task-definition \
  --task-definition "eai00<service>00<env>" \
  --profile ai-dev \
  --region ap-south-1 \
  --query 'taskDefinition' > /tmp/taskdef-<service>-<env>.json
```

Remove `taskDefinitionArn`, `revision`, `status`, `requiresAttributes`, `compatibilities`, `registeredAt`, `registeredBy` from the JSON. Update the `environment` array in the appropriate container definition. Then register:

```bash
aws ecs register-task-definition \
  --cli-input-json file:///tmp/taskdef-<service>-<env>-updated.json \
  --profile ai-dev \
  --region ap-south-1
```

3. Update the ECS service to use the new task definition and force a new deployment:

```bash
aws ecs update-service \
  --cluster "equalai-<env>" \
  --service "eai00<service>00<env>" \
  --task-definition "eai00<service>00<env>" \
  --force-new-deployment \
  --profile ai-dev \
  --region ap-south-1
```

4. Wait for the service to stabilize:

```bash
aws ecs wait services-stable \
  --cluster "equalai-<env>" \
  --services "eai00<service>00<env>" \
  --profile ai-dev \
  --region ap-south-1
```

This can take a few minutes. Let the user know it's in progress.

### Step 6: Post-Deploy Validation via Datadog

After the pipeline succeeds and the ECS service is running the new task definition, validate using Datadog logs. Use the Datadog MCP tools.

#### Check for startup errors (first 5 minutes after deploy)

Query Datadog logs for the service in the target environment:

- **Filter**: `service:<datadog-tag> env:<env> status:error`
- **Time range**: last 5 minutes
- **Look for**: startup exceptions, import errors, connection failures, missing env vars

Use the `mcp__datadog__get_logs` tool:
- query: `service:<datadog-tag> env:<env> status:error`
- from: 5 minutes ago (epoch seconds)
- to: now (epoch seconds)

#### Check service health

Query for successful requests to confirm the service is handling traffic:

- query: `service:<datadog-tag> env:<env> status:ok`
- Verify there are recent log entries showing the service is alive and responding.

#### Report results

Summarize for the user:
- Pipeline execution: succeeded/failed
- ECS deployment: new tasks running / task count
- Env var changes: applied / none needed
- Datadog health: error count in last 5 min, whether service is responding
- Any issues found

### Step 7: Statsig Feature Flag Updates

This step runs only if Statsig dependencies were detected in Step 1 or the user explicitly requested Statsig updates. Skip it entirely if there are no Statsig changes needed.

#### 7a. Retrieve the Console API Key

Follow the process in "Statsig Configuration" above — read from config, or prompt user and persist.

#### 7b. Check Current State

For each detected gate/config/experiment, fetch its current state from the Statsig Console API:

```bash
# Feature gate
curl -s -X GET "https://statsigapi.net/console/v1/gates/<gate_name>" \
  -H "STATSIG-API-KEY: <console_api_key>"

# Dynamic config
curl -s -X GET "https://statsigapi.net/console/v1/dynamic_configs/<config_name>" \
  -H "STATSIG-API-KEY: <console_api_key>"

# Experiment
curl -s -X GET "https://statsigapi.net/console/v1/experiments/<experiment_name>" \
  -H "STATSIG-API-KEY: <console_api_key>"
```

If the API returns 404, the gate/config/experiment doesn't exist yet and needs to be created.

#### 7c. Present a Plan to the User

Based on the current state and the target environment, show the user what Statsig changes are needed:

```
Statsig updates for maxtest (tier: staging):

1. Gate: eai_new_voice_model
   Current: disabled (no rules)
   Action: Enable with 100% pass rate for "staging" tier

2. Dynamic config: voice_model_params
   Current: exists, no staging-specific rule
   Action: Add staging rule with default values

Proceed with these Statsig changes?
```

Wait for user confirmation. The user may want to adjust (e.g., "only enable at 50%" or "skip the config, just the gate").

#### 7d. Apply Changes

**Creating a new gate** (if it doesn't exist):

```bash
curl -s -X POST "https://statsigapi.net/console/v1/gates" \
  -H "STATSIG-API-KEY: <console_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "<gate_name>",
    "description": "Created during deploy-branch to <env>",
    "isEnabled": true,
    "rules": [
      {
        "name": "<env> - All Users",
        "passPercentage": 100,
        "conditions": [{"type": "public"}],
        "environments": ["<statsig_tier>"]
      }
    ]
  }'
```

**Enabling an existing gate for the target environment**:

First GET the current gate to preserve existing rules, then PATCH with the additional environment rule appended. The rules array replaces the entire set, so always merge with existing rules:

```bash
# 1. GET current rules
curl -s -X GET "https://statsigapi.net/console/v1/gates/<gate_name>" \
  -H "STATSIG-API-KEY: <console_api_key>"

# 2. Parse the existing rules array, append the new environment rule
# 3. PATCH with the merged rules
curl -s -X PATCH "https://statsigapi.net/console/v1/gates/<gate_name>" \
  -H "STATSIG-API-KEY: <console_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "isEnabled": true,
    "rules": [
      ...existing_rules,
      {
        "name": "<env> - Deploy Enable",
        "passPercentage": 100,
        "conditions": [{"type": "public"}],
        "environments": ["<statsig_tier>"]
      }
    ]
  }'
```

If a rule for the same environment tier already exists, update its `passPercentage` rather than adding a duplicate.

**Updating a dynamic config** follows the same pattern — GET current state, merge rules, PATCH back. For configs, the rule also includes a `returnValue` object. Ask the user what values to set if they're not obvious from the code.

**Experiments** are more sensitive — don't create or start experiments automatically. If an experiment reference is detected, inform the user and ask them to handle it manually in the Statsig console, or provide specific instructions on what to do.

#### 7e. Verify Changes

After applying, re-fetch each gate/config to confirm the changes took effect:

```bash
curl -s -X GET "https://statsigapi.net/console/v1/gates/<gate_name>" \
  -H "STATSIG-API-KEY: <console_api_key>"
```

Confirm the rule for the target environment tier exists and has the expected `passPercentage`.

#### 7f. Report

Include Statsig changes in the deployment summary:

```
## Statsig Updates — <env> (tier: <statsig_tier>)

| Type | Name | Action | Status |
|------|------|--------|--------|
| Gate | eai_new_voice_model | Enabled (100%) for staging | Verified |
| Config | voice_model_params | Added staging rule | Verified |
```

---

## Multi-Service Parallel Deployment

When deploying multiple services, spawn one subagent per service. Each subagent follows the full flow (Steps 2-6) independently.

The subagent prompt should include:
- The service name
- The target environment
- The feature branch name
- Any env var changes specific to that service
- Any Statsig gate/config names detected for that service
- The Statsig Console API key (if Statsig updates are needed)
- The full deployment instructions from this skill

Aggregate results from all subagents and present a unified summary:

```
## Deployment Summary — <env>

| Service | Pipeline | ECS Status | Env Vars | Statsig | Datadog Health |
|---------|----------|------------|----------|---------|----------------|
| ai-backend | Succeeded | 2/2 tasks | No changes | Gate enabled (100%) | Healthy |
| user-services | Succeeded | 2/2 tasks | +1 new var | No changes | 2 errors (see below) |
```

---

## Important Safety Rules

1. **Never deploy to prod** — this skill is for non-production environments only. If the user asks to deploy to prod, refuse and point them to the CDK deployment pipeline (`/cdk-deploy`).

2. **Never force-push to staging** — if the merge to staging fails due to conflicts, stop and let the user resolve them.

3. **Confirm before preprod** — preprod is shared; always confirm before deploying.

4. **Don't modify pipeline permanently** — updating the CodePipeline source branch is expected to be temporary. CDK will reset it on the next infrastructure deploy, which is fine. Mention this to the user so they're not surprised.

5. **Clean up temp files** — remove any `/tmp/pipeline-*.json` and `/tmp/taskdef-*.json` files after use.

6. **Preserve task definition settings** — when updating env vars, only modify the `environment` array. Don't touch CPU, memory, image, log config, secrets, or any other container settings.
