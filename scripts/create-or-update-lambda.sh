#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Create (or update) one of the Lambda functions in AWS and publish a version.
# Prints the published version ARN — you'll feed that into deploy-phase*.sh.
#
# Usage:
#   scripts/create-or-update-lambda.sh smoke-test        <ROLE_ARN>
#   scripts/create-or-update-lambda.sh readiness-checker <ROLE_ARN>
#   scripts/create-or-update-lambda.sh upgrade-executor  <ROLE_ARN>
#
# ROLE_ARN: an IAM role that the AWS Lambda service can assume.

set -euo pipefail

if [ $# -lt 2 ]; then
    echo "usage: $0 <smoke-test|readiness-checker|upgrade-executor> <ROLE_ARN>" >&2
    exit 1
fi

NAME="$1"
ROLE_ARN="$2"
case "$NAME" in
    smoke-test)        FN="GGv1-Upgrade-SmokeTest" ;;
    readiness-checker) FN="GGv1-Upgrade-ReadinessChecker" ;;
    upgrade-executor)  FN="GGv1-Upgrade-Executor" ;;
    *) echo "Unknown lambda: $NAME" >&2; exit 1 ;;
esac

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ZIP="${REPO_ROOT}/build/${NAME}.zip"
[ -f "$ZIP" ] || { echo "Missing $ZIP — run scripts/package-lambdas.sh first." >&2; exit 1; }

# Required runtime:
RUNTIME=python3.8

if aws lambda get-function --function-name "$FN" >/dev/null 2>&1; then
    echo "Updating $FN..." >&2
    PUBLISH_OUTPUT=$(aws lambda update-function-code \
        --function-name "$FN" \
        --zip-file "fileb://$ZIP" \
        --publish)
else
    echo "Creating $FN..." >&2
    PUBLISH_OUTPUT=$(aws lambda create-function \
        --function-name "$FN" \
        --runtime "$RUNTIME" \
        --role "$ROLE_ARN" \
        --handler handler.function_handler \
        --zip-file "fileb://$ZIP" \
        --timeout 600 \
        --memory-size 256 \
        --publish)
fi

# Take the version ARN directly from the publish response. 
VERSION_ARN=$(echo "$PUBLISH_OUTPUT" | jq -r '.FunctionArn')
if [ -z "$VERSION_ARN" ] || [ "$VERSION_ARN" = "null" ]; then
    echo "ERROR: could not extract published FunctionArn." >&2
    echo "$PUBLISH_OUTPUT" >&2
    exit 1
fi
echo "$VERSION_ARN"
