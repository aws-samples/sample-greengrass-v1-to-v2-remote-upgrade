#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#  Per-device cloud step.
#
#   * The device keeps its existing V1 certificate.
#
#   * IoT policies are JSON permission documents that get attached to a
#     certificate. A certificate can have up to 10 policies attached.
#
#   * Attaches the two policies created by templates/greengrass-upgrade-prereqs.yaml
#     to your device's existing V1 certificate.
#
#
#  Usage:
#   scripts/attach-cert-policies.sh <V1_CERT_ARN>
#   scripts/attach-cert-policies.sh <V1_THING_NAME>

set -euo pipefail

THING_POLICY_NAME="${THING_POLICY_NAME:-GreengrassV2IoTThingPolicy}"
TES_CERT_POLICY_NAME="${TES_CERT_POLICY_NAME:-GreengrassTESCertificatePolicy}"

if [ $# -lt 1 ]; then
    echo "usage: $0 <V1_CERT_ARN_or_THING_NAME>" >&2
    exit 1
fi
ARG="$1"

# Accept either a cert ARN directly or a thing name (we'll resolve it).
if [[ "$ARG" == arn:aws:iot:* ]]; then
    CERT_ARN="$ARG"
else
    # shellcheck disable=SC2016  # JMESPath, not shell expansion.
    CERT_ARN=$(aws iot list-thing-principals --thing-name "$ARG" \
        --query 'principals[?contains(@,`cert/`)]|[0]' --output text)
    if [ "$CERT_ARN" = "None" ] || [ -z "$CERT_ARN" ]; then
        echo "ERROR: no certificate found attached to thing $ARG" >&2
        exit 1
    fi
fi
echo "Target certificate: $CERT_ARN"

EXISTING_POLICIES=$(aws iot list-attached-policies --target "$CERT_ARN" \
    --query 'policies[].policyName' --output text 2>/dev/null || echo "")
EXISTING_COUNT=$(echo "$EXISTING_POLICIES" | wc -w | tr -d ' ')

NEW_NEEDED=0
for p in "$THING_POLICY_NAME" "$TES_CERT_POLICY_NAME"; do
    if ! echo "$EXISTING_POLICIES" | tr '\t' '\n' | grep -qx "$p"; then
        NEW_NEEDED=$((NEW_NEEDED + 1))
    fi
done

if [ $((EXISTING_COUNT + NEW_NEEDED)) -gt 10 ]; then
    echo "ERROR: cert $CERT_ARN already has $EXISTING_COUNT policies." >&2
    echo "       Adding $NEW_NEEDED more would exceed the IoT limit of 10." >&2
    echo "       Existing: $EXISTING_POLICIES" >&2
    echo "       Detach unused policies before re-running this script." >&2
    exit 1
fi

attach() {
    local policy="$1"
    if echo "$EXISTING_POLICIES" | tr '\t' '\n' | grep -qx "$policy"; then
        echo "  [skip] $policy already attached"
    else
        echo "  [attach] $policy"
        aws iot attach-policy --policy-name "$policy" --target "$CERT_ARN"
    fi
}

attach "$THING_POLICY_NAME"
attach "$TES_CERT_POLICY_NAME"
echo "Done. Cert $CERT_ARN now has $((EXISTING_COUNT + NEW_NEEDED)) attached policies."
