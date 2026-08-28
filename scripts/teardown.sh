#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Teardown for the Greengrass V1 -> V2 upgrade lab.
#
# SCOPE — read this before running against a multi-device account:
#   Per-device: this thing, its certificates, its per-device IoT policy, and
#   its local device-bundle directory. Certificate discovery is limited to the
#   selected thing and its local cache, so it never deletes a certificate
#   belonging to another device via the shared legacy policy.
#   SHARED (deleted anyway): the named V1 group, the GGv1Upgrade-* function and
#   subscription definitions, the three GGv1-Upgrade-* Lambda functions, and the
#   prerequisites CloudFormation stack. These are lab-wide singletons, so
#   tearing down one device removes the upgrade plumbing every device in this
#   lab shares. Re-run the provisioning + build steps before upgrading another.
# Safe to re-run: every step is best-effort and skips resources that are gone.
#
# Options (env):
#   THING, GROUP_NAME, STACK_NAME, AWS_REGION   override the defaults
#   ASSUME_YES=1                                skip the confirmation prompt
#   GG_HOST=user@host                           also SSH in and remove the
#                                               on-device V1/V2 install

set -uo pipefail

THING="${1:-${THING:-gg-v1-demo-Core}}"
GROUP_NAME="${2:-${GROUP_NAME:-gg-v1-demo}}"
STACK_NAME="${STACK_NAME:-gg-v1v2-upgrade-prereqs}"
LEGACY_V1_POLICY_NAME="GGv1UpgradeDemoDevicePolicy"
THING_GROUP="GreengrassV2_UpgradedFromV1"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

command -v aws >/dev/null || { echo "aws CLI is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
V1_POLICY_NAME=$(printf '%s' "$THING" | python3 -c \
    'import hashlib, sys; print("GGv1UpgradeDemoDevicePolicy-" + hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:32])')
REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null)}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) \
    || { echo "Not authenticated to AWS. Run 'aws sts get-caller-identity' first." >&2; exit 1; }

echo "Teardown target:"
echo "  Account: ${ACCOUNT_ID}   Region: ${REGION:-<default>}"
echo "  Thing:   ${THING}"
echo "  Group:   ${GROUP_NAME}"
echo "  Stack:   ${STACK_NAME}"
echo
if [ "${ASSUME_YES:-0}" != "1" ]; then
    printf "This permanently deletes the above lab resources. Continue? [y/N] "
    read -r reply
    case "$reply" in y|Y|yes|YES) ;; *) echo "Aborted."; exit 0 ;; esac
fi

warn() { echo "  (skip) $*"; }

# Delete one certificate by ARN: detach its policies + things, deactivate,
# then force-delete. Used for every cert this lab may have left behind.
delete_cert() {
    local cert_arn="$1" cert_id="${1##*/}"
    [ -n "$cert_arn" ] && [ "$cert_arn" != "None" ] || return 0
    echo "  Removing certificate ${cert_id}"
    for p in $(aws iot list-attached-policies --target "$cert_arn" \
                 --query 'policies[].policyName' --output text 2>/dev/null); do
        aws iot detach-policy --policy-name "$p" --target "$cert_arn" 2>/dev/null \
            || warn "detach policy $p"
    done
    for t in $(aws iot list-principal-things --principal "$cert_arn" \
                 --query 'things[]' --output text 2>/dev/null); do
        aws iot detach-thing-principal --thing-name "$t" --principal "$cert_arn" 2>/dev/null \
            || warn "detach thing $t"
    done
    aws iot update-certificate --certificate-id "$cert_id" --new-status INACTIVE 2>/dev/null \
        || warn "deactivate cert"
    aws iot delete-certificate --certificate-id "$cert_id" --force-delete 2>/dev/null \
        || warn "delete cert"
}

# Delete a policy after confirmation, including its non-default versions. IoT
# requires old versions to be removed before the policy itself can be deleted.
delete_policy() {
    local policy_name="$1"
    if ! aws iot get-policy --policy-name "$policy_name" >/dev/null 2>&1; then
        warn "no policy ${policy_name}"
        return 0
    fi
    # JMESPath boolean literals use backticks; no shell expansion is intended.
    # shellcheck disable=SC2016
    while IFS= read -r version_id; do
        [ -n "$version_id" ] || continue
        aws iot delete-policy-version --policy-name "$policy_name" \
            --policy-version-id "$version_id" 2>/dev/null \
            || warn "delete policy ${policy_name} version ${version_id}"
    done < <(
        aws iot list-policy-versions --policy-name "$policy_name" \
            --query 'policyVersions[?isDefaultVersion==`false`].versionId' \
            --output text 2>/dev/null | tr '\t' '\n'
    )
    aws iot delete-policy --policy-name "$policy_name" 2>/dev/null \
        || warn "delete policy ${policy_name}"
}

# ---------------------------------------------------------------------------
echo "[1/7] V2 core device registration..."
aws greengrassv2 delete-core-device --core-device-thing-name "$THING" 2>/dev/null \
    || warn "no V2 core device"

# ---------------------------------------------------------------------------
echo "[2/7] V1 group + definitions..."
GROUP_ID=$(aws greengrass list-groups \
    --query "Groups[?Name=='${GROUP_NAME}'].Id | [0]" --output text 2>/dev/null)
if [ -n "$GROUP_ID" ] && [ "$GROUP_ID" != "None" ]; then
    aws greengrass reset-deployments --group-id "$GROUP_ID" --force 2>/dev/null \
        || warn "reset deployments"
    aws greengrass delete-group --group-id "$GROUP_ID" 2>/dev/null || warn "delete group"
else
    warn "no group named ${GROUP_NAME}"
fi
for D in $(aws greengrass list-core-definitions \
             --query "Definitions[?starts_with(Name,'${GROUP_NAME}')].Id" --output text 2>/dev/null); do
    aws greengrass delete-core-definition --core-definition-id "$D" 2>/dev/null || true
done
for D in $(aws greengrass list-function-definitions \
             --query "Definitions[?starts_with(Name,'GGv1Upgrade-') || starts_with(Name,'${GROUP_NAME}')].Id" \
             --output text 2>/dev/null); do
    aws greengrass delete-function-definition --function-definition-id "$D" 2>/dev/null || true
done
for D in $(aws greengrass list-subscription-definitions \
             --query "Definitions[?starts_with(Name,'GGv1Upgrade-')].Id" --output text 2>/dev/null); do
    aws greengrass delete-subscription-definition --subscription-definition-id "$D" 2>/dev/null || true
done

# ---------------------------------------------------------------------------
echo "[3/7] Upgrade Lambda functions..."
for F in GGv1-Upgrade-SmokeTest GGv1-Upgrade-ReadinessChecker GGv1-Upgrade-Executor; do
    aws lambda delete-function --function-name "$F" 2>/dev/null || warn "no lambda $F"
done

# ---------------------------------------------------------------------------
echo "[4/7] Device identity (thing, selected-device certificates, policies)..."
# Collect only certificates attributable to this selected device:
#   a) principals currently attached to the thing
#   b) the ARN cached in this thing's local device bundle
# Never enumerate the targets of the legacy shared policy: they can belong to
# other devices and must not be deleted by this teardown.
CERTS=()
while read -r arn; do [ -n "$arn" ] && CERTS+=("$arn"); done < <(
    aws iot list-thing-principals --thing-name "$THING" \
        --query 'principals[]' --output text 2>/dev/null | tr '\t' '\n'
    [ -f "${REPO_ROOT}/device-bundle/${THING}/cert-arn" ] \
        && cat "${REPO_ROOT}/device-bundle/${THING}/cert-arn"
)
# Detach the thing from the V2 thing group before deleting anything else.
aws iot remove-thing-from-thing-group \
    --thing-group-name "$THING_GROUP" --thing-name "$THING" 2>/dev/null || true
# Dedupe and delete each selected-device certificate.
if [ "${#CERTS[@]}" -gt 0 ]; then
    while IFS= read -r CERT_ARN; do
        [ -n "$CERT_ARN" ] && delete_cert "$CERT_ARN"
    done < <(printf '%s\n' "${CERTS[@]}" | sort -u)
fi
aws iot delete-thing --thing-name "$THING" 2>/dev/null || warn "no thing ${THING}"

delete_policy "$V1_POLICY_NAME"

if aws iot get-policy --policy-name "$LEGACY_V1_POLICY_NAME" >/dev/null 2>&1; then
    if ! LEGACY_TARGET_COUNT=$(aws iot list-targets-for-policy \
        --policy-name "$LEGACY_V1_POLICY_NAME" --query 'length(targets)' \
        --output text 2>/dev/null); then
        echo "  Retaining legacy shared policy ${LEGACY_V1_POLICY_NAME}: could not verify its targets."
    elif [ "$LEGACY_TARGET_COUNT" = "0" ]; then
        echo "  Legacy shared policy has no remaining targets; deleting it."
        delete_policy "$LEGACY_V1_POLICY_NAME"
    elif [[ "$LEGACY_TARGET_COUNT" =~ ^[0-9]+$ ]]; then
        echo "  Retaining legacy shared policy ${LEGACY_V1_POLICY_NAME}: ${LEGACY_TARGET_COUNT} target(s) remain."
    else
        printf '  Retaining legacy shared policy %s: unexpected target count %q.\n' \
            "$LEGACY_V1_POLICY_NAME" "$LEGACY_TARGET_COUNT"
    fi
else
    warn "no legacy policy ${LEGACY_V1_POLICY_NAME}"
fi

# ---------------------------------------------------------------------------
echo "[5/7] Prerequisites CloudFormation stack..."
if aws cloudformation describe-stacks --stack-name "$STACK_NAME" >/dev/null 2>&1; then
    aws cloudformation delete-stack --stack-name "$STACK_NAME" 2>/dev/null || warn "delete stack"
    echo "  waiting for stack deletion..."
    aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" 2>/dev/null \
        || warn "stack delete wait"
else
    warn "no stack ${STACK_NAME}"
fi

# ---------------------------------------------------------------------------
echo "[6/7] Local files + caches..."
# device-bundle/<thing>/ holds the cert-arn cache that made re-provisioning fail
# after a cloud-only teardown; removing it clears that cache. Only THIS device's
# bundle is deleted — the sibling directories hold other devices' private keys.
rm -rf "${REPO_ROOT}/build" 2>/dev/null || true
rm -rf "${REPO_ROOT}/device-bundle/${THING}" 2>/dev/null || true
rmdir "${REPO_ROOT}/device-bundle" 2>/dev/null || true
echo "  removed build/, device-bundle/${THING}/"
if [ -d "${REPO_ROOT}/device-bundle" ]; then
    echo "  kept other device bundles under device-bundle/:"
    find "${REPO_ROOT}/device-bundle" -mindepth 1 -maxdepth 1 -exec basename {} \; \
        2>/dev/null | sed 's/^/    /'
fi

# ---------------------------------------------------------------------------
echo "[7/7] On-device cleanup..."
if [ -n "${GG_HOST:-}" ]; then
    echo "  cleaning ${GG_HOST} over SSH..."
    # reset-failed clears the lingering "failed" unit state systemd keeps in
    # memory after the unit file is deleted from under a running service —
    # otherwise `systemctl is-active greengrass` still reports failed.
    # The /tmp entries are this lab's staging artifacts; normally removed at
    # the end of Step 3b, swept here in case that was skipped.
    ssh "$GG_HOST" "sudo systemctl stop greengrass 2>/dev/null; \
        sudo systemctl disable greengrass 2>/dev/null; \
        sudo rm -f /etc/systemd/system/greengrass.service; \
        sudo systemctl daemon-reload; \
        sudo systemctl reset-failed greengrass 2>/dev/null; \
        sudo rm -rf /greengrass /var/lib/gg-v2-upgrade; \
        sudo rm -rf '/tmp/${THING}' /tmp/setup-v1-device.sh" \
        || warn "SSH cleanup failed — clean the device by hand or reflash"
else
    echo "  (no GG_HOST set) On the device, run — or just reflash the SD card:"
    echo "    sudo systemctl stop greengrass; sudo systemctl disable greengrass"
    echo "    sudo rm -f /etc/systemd/system/greengrass.service; sudo systemctl daemon-reload"
    echo "    sudo systemctl reset-failed greengrass"
    echo "    sudo rm -rf /greengrass /var/lib/gg-v2-upgrade"
    echo "    sudo rm -rf /tmp/${THING} /tmp/setup-v1-device.sh"
fi

echo
echo "Teardown complete."
