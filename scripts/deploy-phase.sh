#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Deploy one phase to a GG V1 group.
#
# There is no inbound message path. Both handlers do their work at module import 
# time. A pinned GG V1 Lambda runs its top-level code as soon as the deployment 
# starts it.
#
# Usage:
#   scripts/deploy-phase.sh <smoke-test|readiness-checker|upgrade-executor>
#
# The group and the Lambda's version-qualified ARN are resolved automatically:
#   - group:  by name (GROUP_NAME env, default gg-v1-demo)
#   - lambda: the highest published version of the phase's function
# so you don't have to keep GROUP_ID / *_ARN exported across shells. You may
# still override either as positional args for advanced use:
#   scripts/deploy-phase.sh <phase> [GROUP_ID] [LAMBDA_VERSION_ARN]

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "usage: $0 <smoke-test|readiness-checker|upgrade-executor> [GROUP_ID] [LAMBDA_VERSION_ARN]" >&2
    exit 1
fi

PHASE="$1"
GROUP_ID="${2:-}"
LAMBDA_ARN="${3:-}"
GROUP_NAME="${GROUP_NAME:-gg-v1-demo}"

# The role alias name must match the prerequisites stack
# (templates/greengrass-upgrade-prereqs.yaml, output TokenExchangeRoleAlias).
TES_ROLE_ALIAS="${TES_ROLE_ALIAS:-GreengrassCoreTokenExchangeRoleAlias}"

case "$PHASE" in
    smoke-test)
        FUNCTION_ID="smoke-test"
        FUNCTION_NAME="GGv1-Upgrade-SmokeTest"
        SUB_ID="smoketest-to-cloud"
        TOPIC="greengrass/upgrade/smoketest/#"
        TIMEOUT=30
        ;;
    readiness-checker)
        FUNCTION_ID="readiness-checker"
        FUNCTION_NAME="GGv1-Upgrade-ReadinessChecker"
        SUB_ID="readiness-to-cloud"
        TOPIC="greengrass/upgrade/readiness/#"
        # 600s: covers the bandwidth probe + ~52 MB installer download on slow links.
        TIMEOUT=600
        ;;
    upgrade-executor)
        FUNCTION_ID="upgrade-executor"
        FUNCTION_NAME="GGv1-Upgrade-Executor"
        SUB_ID="upgrade-status-to-cloud"
        TOPIC="greengrass/upgrade/status/#"
        # 60s is plenty: this Lambda just writes a script + systemd unit,
        # then exits. The 15-min upgrade itself runs in the systemd unit.
        TIMEOUT=60
        ;;
    *) echo "Unknown phase: $PHASE" >&2; exit 1 ;;
esac

# Resolve the group id from its name unless one was passed explicitly. Keeps
# the common path working without a GROUP_ID exported in the current shell.
if [ -z "$GROUP_ID" ]; then
    GROUP_ID=$(aws greengrass list-groups \
        --query "Groups[?Name=='${GROUP_NAME}'].Id | [0]" --output text)
fi
if [ -z "$GROUP_ID" ] || [ "$GROUP_ID" = "None" ]; then
    echo "ERROR: no Greengrass group '${GROUP_NAME}' found (set GROUP_NAME or pass a GROUP_ID)." >&2
    exit 1
fi

# Resolve the Lambda's highest published version ARN unless one was passed.
if [ -z "$LAMBDA_ARN" ]; then
    LAMBDA_ARN=$(aws lambda list-versions-by-function \
        --function-name "$FUNCTION_NAME" \
        --query 'max_by(Versions[?Version!=`$LATEST`], &to_number(Version)).FunctionArn' \
        --output text 2>/dev/null)
    if [ -z "$LAMBDA_ARN" ] || [ "$LAMBDA_ARN" = "None" ]; then
        echo "ERROR: no published version of ${FUNCTION_NAME} found." >&2
        echo "       Build it first: scripts/create-or-update-lambda.sh ${PHASE} <role-arn>" >&2
        exit 1
    fi
fi
# Guard: whatever we ended up with must be version-qualified.
case "$LAMBDA_ARN" in
    arn:aws:lambda:*:function:*:[0-9]*) ;;
    *) echo "ERROR: '${LAMBDA_ARN}' is not a version-qualified Lambda ARN." >&2
       echo "       Expected something ending in ':<number>', e.g. ...:function:${FUNCTION_NAME}:1" >&2
       exit 1 ;;
esac
echo "Phase ${PHASE}: group ${GROUP_ID}, lambda ${LAMBDA_ARN}"

# 0. Resolve the account-specific IoT credentials-provider endpoint.
echo "Resolving IoT credentials-provider endpoint..."
IOT_CRED_ENDPOINT=$(aws iot describe-endpoint \
    --endpoint-type iot:CredentialProvider \
    --query 'endpointAddress' --output text)
echo "  iotCredEndpoint: $IOT_CRED_ENDPOINT"

# 1. Look up the group's current core definition version (we keep it as-is).
echo "Looking up current group..."
GROUP_JSON=$(aws greengrass get-group --group-id "$GROUP_ID")
LATEST_VERSION_ID=$(echo "$GROUP_JSON" | jq -r '.LatestVersion')
LATEST_VERSION_JSON=$(aws greengrass get-group-version \
    --group-id "$GROUP_ID" \
    --group-version-id "$LATEST_VERSION_ID")
CORE_DEF_VERSION_ARN=$(echo "$LATEST_VERSION_JSON" \
    | jq -r '.Definition.CoreDefinitionVersionArn // empty')

if [ -z "$CORE_DEF_VERSION_ARN" ] || [ "$CORE_DEF_VERSION_ARN" = "null" ]; then
    echo "ERROR: group $GROUP_ID has no core definition. Did you create the group?" >&2
    exit 1
fi
echo "  Core: $CORE_DEF_VERSION_ARN"

# 2. Function definition with the Lambda configured as root in no-container.
#
#    - DefaultConfig NoContainer is REQUIRED
#    - NO MemorySize. Non-containerized functions have no memory limit.
#    - Environment. Variables carries the cred endpoint + role alias down to
#      the handler (os.environ)
UPGRADE_RUN_ID="$(date +%s)-$$"

echo "Creating function definition..."
FUNC_DEF_VERSION_ARN=$(aws greengrass create-function-definition \
    --name "GGv1Upgrade-${PHASE}-$(date +%s)" \
    --initial-version "$(jq -nc \
        --arg id "$FUNCTION_ID" \
        --arg arn "$LAMBDA_ARN" \
        --arg cred_ep "$IOT_CRED_ENDPOINT" \
        --arg role_alias "$TES_ROLE_ALIAS" \
        --arg run_id "$UPGRADE_RUN_ID" \
        --argjson timeout "$TIMEOUT" \
        '{DefaultConfig:{Execution:{IsolationMode:"NoContainer"}},
          Functions:[{
            Id:$id,
            FunctionArn:$arn,
            FunctionConfiguration:{
                Pinned:true,
                Timeout:$timeout,
                EncodingType:"json",
                Environment:{
                    Execution:{IsolationMode:"NoContainer", RunAs:{Uid:0,Gid:0}},
                    Variables:{
                        IOT_CRED_ENDPOINT:$cred_ep,
                        TES_ROLE_ALIAS:$role_alias,
                        UPGRADE_RUN_ID:$run_id
                    }
                }
            }
        }]}')" \
    --query 'LatestVersionArn' --output text)
echo "  Func: $FUNC_DEF_VERSION_ARN"

# 3. Subscription definition (device -> cloud on the phase's reporting topic).
echo "Creating subscription definition..."
SUB_DEF_VERSION_ARN=$(aws greengrass create-subscription-definition \
    --name "GGv1Upgrade-${PHASE}-subs-$(date +%s)" \
    --initial-version "$(jq -nc \
        --arg id "$SUB_ID" \
        --arg src "$LAMBDA_ARN" \
        --arg subj "$TOPIC" \
        '{Subscriptions:[{
            Id:$id,
            Source:$src,
            Subject:$subj,
            Target:"cloud"
        }]}')" \
    --query 'LatestVersionArn' --output text)
echo "  Subs: $SUB_DEF_VERSION_ARN"

# 4. New group version that combines them.
echo "Creating group version..."
GROUP_VERSION_ID=$(aws greengrass create-group-version \
    --group-id "$GROUP_ID" \
    --core-definition-version-arn "$CORE_DEF_VERSION_ARN" \
    --function-definition-version-arn "$FUNC_DEF_VERSION_ARN" \
    --subscription-definition-version-arn "$SUB_DEF_VERSION_ARN" \
    --query 'Version' --output text)
echo "  Group version: $GROUP_VERSION_ID"

# 5. Deploy. For a pinned Lambda this both ships the code AND starts it
echo "Creating deployment..."
DEPLOYMENT_ID=$(aws greengrass create-deployment \
    --group-id "$GROUP_ID" \
    --group-version-id "$GROUP_VERSION_ID" \
    --deployment-type NewDeployment \
    --query 'DeploymentId' --output text)
echo "  Deployment: $DEPLOYMENT_ID"
echo
echo "Done. Subscribe to '$TOPIC' in the IoT MQTT test client to watch progress."
echo "Deployment status: aws greengrass get-deployment-status \\"
echo "    --group-id $GROUP_ID --deployment-id $DEPLOYMENT_ID"
