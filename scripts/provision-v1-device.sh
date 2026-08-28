#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Create the cloud-side resources for one demo Greengrass V1 core device and
# emit a bundle directory to copy to that device.
#
# Usage:
#   scripts/provision-v1-device.sh <thing-name> [group-name]
# Example:
#   scripts/provision-v1-device.sh gg-v1-demo-Core gg-v1-demo
#
# Creates (all idempotent — safe to re-run):
#   - Greengrass V1 service role association for the account (if missing)
#   - IoT thing <thing-name>
#   - X.509 certificate + keys -> local/device-bundle/<thing-name>/
#   - A deterministic, least-privilege per-device IoT policy attached to the cert
#   - GG V1 core definition + group <group-name>
#   - GGIPDetector system function + an initial group deployment (queued;
#     applies when the device first connects)
#   - config.json prefilled for the device -> device-bundle/<thing-name>/
#

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "usage: $0 <thing-name> [group-name]" >&2
    exit 1
fi

THING="$1"
GROUP_NAME="${2:-gg-v1-demo}"
LEGACY_V1_POLICY_NAME="GGv1UpgradeDemoDevicePolicy"

command -v jq >/dev/null || { echo "jq is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
V1_POLICY_NAME=$(printf '%s' "$THING" | python3 -c \
    'import hashlib, sys; print("GGv1UpgradeDemoDevicePolicy-" + hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:32])')
REGION="${AWS_REGION:-$(aws configure get region)}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLE_DIR="${REPO_ROOT}/device-bundle/${THING}"
mkdir -p "$BUNDLE_DIR"
chmod 700 "$BUNDLE_DIR"

echo "Region: $REGION   Account: $ACCOUNT_ID"

# ----------------------------------------------------------------------------
# 0. Greengrass V1 SERVICE ROLE: The V1 cloud service assumes this role to do
#    work on your behalf during deployments
# ----------------------------------------------------------------------------
if aws greengrass get-service-role-for-account >/dev/null 2>&1; then
    echo "GG V1 service role already associated:"
    aws greengrass get-service-role-for-account --query 'RoleArn' --output text
else
    echo "Associating GG V1 service role (Greengrass_ServiceRole)..."
    if ! aws iam get-role --role-name Greengrass_ServiceRole >/dev/null 2>&1; then
        aws iam create-role --role-name Greengrass_ServiceRole \
            --assume-role-policy-document '{
              "Version":"2012-10-17",
              "Statement":[{"Effect":"Allow",
                "Principal":{"Service":"greengrass.amazonaws.com"},
                "Action":"sts:AssumeRole"}]}' >/dev/null
        aws iam attach-role-policy --role-name Greengrass_ServiceRole \
            --policy-arn arn:aws:iam::aws:policy/service-role/AWSGreengrassResourceAccessRolePolicy
        sleep 10  # IAM propagation
    fi
    aws greengrass associate-service-role-to-account \
        --role-arn "arn:aws:iam::${ACCOUNT_ID}:role/Greengrass_ServiceRole" >/dev/null
    echo "  Associated."
fi

# ----------------------------------------------------------------------------
# 1. IoT thing: the device's cloud identity.
# ----------------------------------------------------------------------------
echo "Creating IoT thing ${THING}..."
THING_ARN=$(aws iot describe-thing --thing-name "$THING" \
    --query 'thingArn' --output text 2>/dev/null) || \
THING_ARN=$(aws iot create-thing --thing-name "$THING" \
    --query 'thingArn' --output text)
echo "  $THING_ARN"

# ----------------------------------------------------------------------------
# 2. Certificate + private key.
# ----------------------------------------------------------------------------
CERT_ARN=""
if [ -f "${BUNDLE_DIR}/cert-arn" ]; then
    CACHED_ARN=$(cat "${BUNDLE_DIR}/cert-arn")
    CACHED_ID="${CACHED_ARN##*/}"
    if [ ! -f "${BUNDLE_DIR}/cert.pem" ] || [ ! -f "${BUNDLE_DIR}/private.key" ]; then
        echo "Cached cert ARN present but key material missing from bundle — minting a new certificate."
    elif aws iot describe-certificate --certificate-id "$CACHED_ID" >/dev/null 2>&1; then
        CERT_ARN="$CACHED_ARN"
        echo "Reusing certificate from previous run: $CERT_ARN"
    else
        echo "Cached certificate no longer exists in AWS (torn down?) — minting a new one."
    fi
fi

if [ -z "$CERT_ARN" ]; then
    echo "Creating certificate + key pair..."
    CERT_JSON=$(aws iot create-keys-and-certificate --set-as-active --output json)
    CERT_ARN=$(echo "$CERT_JSON" | jq -r '.certificateArn')
    echo "$CERT_JSON" | jq -r '.certificatePem'            > "${BUNDLE_DIR}/cert.pem"
    echo "$CERT_JSON" | jq -r '.keyPair.PrivateKey'        > "${BUNDLE_DIR}/private.key"
    echo "$CERT_JSON" | jq -r '.keyPair.PublicKey'         > "${BUNDLE_DIR}/public.key"
    echo "$CERT_ARN"                                       > "${BUNDLE_DIR}/cert-arn"
    chmod 600 "${BUNDLE_DIR}/private.key"
    echo "  $CERT_ARN"
fi

# Amazon root CA (public artifact, pinned URL from the AWS docs).
if [ ! -f "${BUNDLE_DIR}/root.ca.pem" ]; then
    curl -fsS -o "${BUNDLE_DIR}/root.ca.pem" \
        https://www.amazontrust.com/repository/AmazonRootCA1.pem
fi

# ----------------------------------------------------------------------------
# 3. Core definition + V1 group. The core definition binds thing+cert as the
#    group's core. The group ID is required to scope the device policy.
# ----------------------------------------------------------------------------
echo "Creating core definition + group ${GROUP_NAME}..."
GROUP_ID=$(aws greengrass list-groups \
    --query "Groups[?Name=='${GROUP_NAME}'].Id | [0]" --output text)
if [ "$GROUP_ID" = "None" ] || [ -z "$GROUP_ID" ]; then
    CORE_DEF_VERSION_ARN=$(aws greengrass create-core-definition \
        --name "${GROUP_NAME}-core" \
        --initial-version "$(jq -nc \
            --arg thing "$THING_ARN" --arg cert "$CERT_ARN" \
            '{Cores:[{Id:"core-1",ThingArn:$thing,CertificateArn:$cert,SyncShadow:false}]}')" \
        --query 'LatestVersionArn' --output text)
    GROUP_ID=$(aws greengrass create-group --name "$GROUP_NAME" \
        --query 'Id' --output text)
    aws greengrass create-group-version --group-id "$GROUP_ID" \
        --core-definition-version-arn "$CORE_DEF_VERSION_ARN" >/dev/null
else
    echo "  Group ${GROUP_NAME} already exists."
    # Convergence check: if we minted a NEW certificate above (because the old
    # one was deleted), the existing group's core definition still binds the
    # dead cert and the device would never connect. Re-point it when it drifts.
    CUR_CORE_ARN=$(aws greengrass get-group-version --group-id "$GROUP_ID" \
        --group-version-id "$(aws greengrass get-group --group-id "$GROUP_ID" \
            --query 'LatestVersion' --output text)" \
        --query 'Definition.CoreDefinitionVersionArn' --output text 2>/dev/null)
    CUR_CERT_ARN=""
    if [ -n "$CUR_CORE_ARN" ] && [ "$CUR_CORE_ARN" != "None" ]; then
        CORE_DEF_ID="$(echo "$CUR_CORE_ARN" | awk -F'/' '{print $(NF-2)}')"
        CORE_DEF_VER_ID="${CUR_CORE_ARN##*/}"
        CUR_CERT_ARN=$(aws greengrass get-core-definition-version \
            --core-definition-id "$CORE_DEF_ID" \
            --core-definition-version-id "$CORE_DEF_VER_ID" \
            --query 'Definition.Cores[0].CertificateArn' --output text 2>/dev/null)
    fi
    if [ -n "$CUR_CERT_ARN" ] && [ "$CUR_CERT_ARN" != "None" ] && [ "$CUR_CERT_ARN" != "$CERT_ARN" ]; then
        echo "  Group core is bound to a different certificate — rebinding to ${CERT_ARN}"
        NEW_CORE_ARN=$(aws greengrass create-core-definition \
            --name "${GROUP_NAME}-core-$(date +%s)" \
            --initial-version "$(jq -nc \
                --arg thing "$THING_ARN" --arg cert "$CERT_ARN" \
                '{Cores:[{Id:"core-1",ThingArn:$thing,CertificateArn:$cert,SyncShadow:false}]}')" \
            --query 'LatestVersionArn' --output text)
        aws greengrass create-group-version --group-id "$GROUP_ID" \
            --core-definition-version-arn "$NEW_CORE_ARN" >/dev/null
    fi
fi
echo "  GROUP_ID=$GROUP_ID"

# ----------------------------------------------------------------------------
# 4. Per-device V1 IoT policy. The group-scoped deployment ARNs are not known
#    until the group exists. Policy updates are convergent and preserve old
#    versions for operator review rather than deleting them automatically.
# ----------------------------------------------------------------------------
PARTITION="${THING_ARN#arn:}"
PARTITION="${PARTITION%%:*}"
IOT_ARN_PREFIX="arn:${PARTITION}:iot:${REGION}:${ACCOUNT_ID}"
GROUP_DEPLOYMENTS_ARN="arn:${PARTITION}:greengrass:${REGION}:${ACCOUNT_ID}:/greengrass/groups/${GROUP_ID}/deployments/*"

# Greengrass V1 does not support resource-level scoping for
# AssumeRoleForGroup or CreateCertificate, so only those two actions use "*".
#
# Thing-scoped resources use a bare "<thing>*" wildcard with NO separator.
# Do not "tighten" this to "<thing>/*", and do not split it into a "<thing>" +
# "<thing>-*" pair. Both variants have been tried on a real device and fail:
#
#   "<thing>/*" only  -> V1's own system components use SUFFIXED thing names for
#                        their shadows (<core>-gda device agent, <core>-gcf
#                        connector function). The core subscribes to
#                        $aws/things/<core>-gda/shadow/get/accepted, is denied,
#                        and the broker closes the connection with EOF in a
#                        reconnect loop. No deployment is ever applied.
#   exact + "<thing>-*" -> correct semantically, but doubling every thing-scoped
#                        resource pushes the document past AWS IoT's 2048-byte
#                        HARD limit once the group UUID is included twice
#                        (measured: 2071 bytes). CreatePolicyVersion fails with
#                        InvalidRequestException.
#
# "<thing>*" covers both the core's own topics and the -gda/-gcf system things in
# a single resource (~1726 bytes, ~320 to spare). It also matches a hypothetical
# sibling thing sharing this name as a prefix, which is the documented V1 minimal
# policy's own tradeoff and still far tighter than the Resource:"*" this replaced.
# https://docs.aws.amazon.com/greengrass/v1/developerguide/device-auth.html
# Six statements, ~1.1 KB of the 2048-byte budget. Kept deliberately small:
#  - Deployment ARNs use one "deployments/*" resource for all three deployment
#    actions. IoT policy wildcards span "/", so it also covers the
#    ".../deployments/<id>/cores/<url-encoded-core-arn>" resource that
#    UpdateCoreDeploymentStatus is evaluated against. Spelling that ARN out
#    separately cost 296 bytes for a single action.
#  - No iot:*ThingShadow actions. Those govern the shadow REST API; this lab
#    sets SyncShadow:false and has no client devices. The core's own shadow
#    traffic is MQTT, authorized by the topic/topicfilter statements below.
#    Add them back if you enable shadow sync or attach client devices.
DESIRED_POLICY_JSON=$(jq -nc \
    --arg connect "${IOT_ARN_PREFIX}:client/${THING}*" \
    --arg thingTopic "${IOT_ARN_PREFIX}:topic/\$aws/things/${THING}*" \
    --arg thingFilter "${IOT_ARN_PREFIX}:topicfilter/\$aws/things/${THING}*" \
    --arg thingWildcard "${THING_ARN}*" \
    --arg upgradeTopic "${IOT_ARN_PREFIX}:topic/greengrass/upgrade/*" \
    --arg deployments "$GROUP_DEPLOYMENTS_ARN" \
    '{
      Version:"2012-10-17",
      Statement:[
        {Sid:"Connect",Effect:"Allow",Action:"iot:Connect",Resource:$connect},
        {Sid:"Pub",Effect:"Allow",Action:["iot:Publish","iot:Receive"],Resource:[$thingTopic,$upgradeTopic]},
        {Sid:"Sub",Effect:"Allow",Action:"iot:Subscribe",Resource:$thingFilter},
        {Sid:"GGCore",Effect:"Allow",Action:["greengrass:AssumeRoleForGroup","greengrass:CreateCertificate"],Resource:"*"},
        {Sid:"Deploy",Effect:"Allow",Action:["greengrass:GetDeployment","greengrass:GetDeploymentArtifacts","greengrass:UpdateCoreDeploymentStatus"],Resource:$deployments},
        {Sid:"Conn",Effect:"Allow",Action:["greengrass:GetConnectivityInfo","greengrass:UpdateConnectivityInfo"],Resource:$thingWildcard}
      ]
    }')
DESIRED_POLICY_CANONICAL=$(printf '%s' "$DESIRED_POLICY_JSON" | jq -S -c .)

echo "Ensuring per-device IoT policy ${V1_POLICY_NAME}..."
if ! aws iot get-policy --policy-name "$V1_POLICY_NAME" >/dev/null 2>&1; then
    aws iot create-policy --policy-name "$V1_POLICY_NAME" \
        --policy-document "$DESIRED_POLICY_JSON" >/dev/null
    echo "  Created per-device policy."
else
    CURRENT_POLICY_JSON=$(aws iot get-policy --policy-name "$V1_POLICY_NAME" \
        --query 'policyDocument' --output text)
    CURRENT_POLICY_CANONICAL=$(printf '%s' "$CURRENT_POLICY_JSON" | jq -S -c .)
    if [ "$CURRENT_POLICY_CANONICAL" = "$DESIRED_POLICY_CANONICAL" ]; then
        echo "  Default policy version already matches."
    else
        VERSION_COUNT=$(aws iot list-policy-versions --policy-name "$V1_POLICY_NAME" \
            --query 'length(policyVersions)' --output text)
        if [ "$VERSION_COUNT" -ge 5 ]; then
            echo "Policy ${V1_POLICY_NAME} differs from the desired policy but already has 5 versions." >&2
            echo "Review and delete an unused non-default version manually, then re-run provisioning." >&2
            exit 1
        fi
        aws iot create-policy-version --policy-name "$V1_POLICY_NAME" \
            --policy-document "$DESIRED_POLICY_JSON" --set-as-default >/dev/null
        echo "  Created a new default policy version."
    fi
fi

aws iot attach-policy --policy-name "$V1_POLICY_NAME" --target "$CERT_ARN"
aws iot attach-thing-principal --thing-name "$THING" --principal "$CERT_ARN"
if aws iot detach-policy --policy-name "$LEGACY_V1_POLICY_NAME" \
        --target "$CERT_ARN" >/dev/null 2>&1; then
    echo "  Detached legacy shared policy from this certificate."
else
    echo "  Legacy shared policy was not attached to this certificate."
fi

# ----------------------------------------------------------------------------
# 5. GGIPDetector + initial deployment. Two things the old console wizard did
#    silently that a CLI-built group must do explicitly:
#
#    - GGIPDetector is the AWS-provided system function that uploads the
#      core's IP addresses.
#    - A group has to be DEPLOYED at least once for any of it (including the
#      IP detector) to reach the device.
# ----------------------------------------------------------------------------
LATEST_VERSION_ID=$(aws greengrass get-group --group-id "$GROUP_ID" \
    --query 'LatestVersion' --output text)
FUNC_DEF_ARN=$(aws greengrass get-group-version --group-id "$GROUP_ID" \
    --group-version-id "$LATEST_VERSION_ID" \
    --query 'Definition.FunctionDefinitionVersionArn' --output text)
if [ "$FUNC_DEF_ARN" = "None" ] || [ -z "$FUNC_DEF_ARN" ]; then
    echo "Adding GGIPDetector system function to the group..."
    CORE_DEF_ARN=$(aws greengrass get-group-version --group-id "$GROUP_ID" \
        --group-version-id "$LATEST_VERSION_ID" \
        --query 'Definition.CoreDefinitionVersionArn' --output text)
    FUNC_DEF_ARN=$(aws greengrass create-function-definition \
        --name "${GROUP_NAME}-ipdetector" \
        --initial-version '{
          "DefaultConfig": {"Execution": {"IsolationMode": "NoContainer"}},
          "Functions": [{
            "Id": "ip-detector",
            "FunctionArn": "arn:aws:lambda:::function:GGIPDetector:1",
            "FunctionConfiguration": {"Pinned": true, "Timeout": 3}
          }]
        }' --query 'LatestVersionArn' --output text)
    LATEST_VERSION_ID=$(aws greengrass create-group-version \
        --group-id "$GROUP_ID" \
        --core-definition-version-arn "$CORE_DEF_ARN" \
        --function-definition-version-arn "$FUNC_DEF_ARN" \
        --query 'Version' --output text)
fi

echo "Creating initial deployment (applies when the device comes online)..."
DEPLOYMENT_ID=$(aws greengrass create-deployment --group-id "$GROUP_ID" \
    --group-version-id "$LATEST_VERSION_ID" \
    --deployment-type NewDeployment \
    --query 'DeploymentId' --output text)
echo "  DEPLOYMENT_ID=$DEPLOYMENT_ID"

# ----------------------------------------------------------------------------
# 6. Prefilled config.json for the device.
# ----------------------------------------------------------------------------
IOT_HOST=$(aws iot describe-endpoint --endpoint-type iot:Data-ATS \
    --query 'endpointAddress' --output text)

cat > "${BUNDLE_DIR}/config.json" <<EOF
{
  "coreThing": {
    "caPath":   "root.ca.pem",
    "certPath": "cert.pem",
    "keyPath":  "private.key",
    "thingArn": "${THING_ARN}",
    "iotHost":  "${IOT_HOST}",
    "ggHost":   "greengrass-ats.iot.${REGION}.amazonaws.com",
    "keepAlive": 600
  },
  "runtime": {
    "cgroup": { "useSystemd": "yes" },
    "allowFunctionsToRunAsRoot": "yes"
  },
  "managedRespawn": false,
  "crypto": {
    "principals": {
      "SecretsManager": {
        "privateKeyPath": "file:///greengrass/certs/private.key"
      },
      "IoTCertificate": {
        "privateKeyPath":  "file:///greengrass/certs/private.key",
        "certificatePath": "file:///greengrass/certs/cert.pem"
      }
    },
    "caPath": "file:///greengrass/certs/root.ca.pem"
  }
}
EOF

echo
echo "=============================================================="
echo "Done. Cloud side is ready."
echo
echo "  export GROUP_ID=${GROUP_ID}"
echo "  export THING=${THING}"
echo
echo "Copy the bundle + device setup script to the GreenGrass device (adjust user/host):"
echo
echo "  scp -r ${BUNDLE_DIR} scripts/setup-v1-device.sh <user>@gg-v1-demo.local:/tmp/"
echo
echo "Then on the device:  sudo /tmp/setup-v1-device.sh /tmp/${THING}"
echo
echo "The initial deployment applies once the device connects; a minute or"
echo "two later the Step 4 check passes:"
echo
echo "  aws greengrass get-connectivity-info --thing-name ${THING}"
echo "=============================================================="
