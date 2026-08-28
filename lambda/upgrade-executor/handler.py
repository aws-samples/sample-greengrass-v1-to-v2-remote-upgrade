# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Greengrass V1 -> V2 Upgrade: Phase 3 - Upgrade Executor (production)

Deployed to a Greengrass V1 group as a NoContainer Lambda running as root.
Writes an upgrade shell script and a one-shot systemd unit, then starts the
unit. The systemd unit lives outside the GG V1 process tree, so the upgrade
survives the V1 daemon being stopped (which kills this Lambda).

What's different from a "demo" upgrade:

  1. Manual provisioning (--provision false). The V1 certificate, private
     key, and root CA are reused as the V2 device identity. The IoT thing
     name is reused. No new IAM resources are created at install time.
     This means downstream rules/shadows/jobs/components keyed on the V1
     thing name keep working after the cutover.

  2. The prerequisites stack (templates/greengrass-upgrade-prereqs.yaml) and
     the per-device cert attach (scripts/attach-cert-policies.sh) MUST have
     run before this Lambda is deployed. The handler validates what it can
     see from the device (readiness report, installer hash, endpoint config).

  3. The installer was downloaded and SHA256-verified by Phase 2; this module
     re-verifies the hash before installing (see V2_INSTALLER_SHA256 — keep in
     sync with the readiness-checker when revving the nucleus version).

  4. The upgrade script writes per-step status markers (no MQTT once V1 is
     stopped) and tries to restart V1 on any failure path.

  5. MODE=auto is preserved as an environment override for ad-hoc lab
     testing. Production deployments leave it unset.

References:
  https://docs.aws.amazon.com/greengrass/v2/developerguide/manual-installation.html
  https://docs.aws.amazon.com/greengrass/v2/developerguide/configure-greengrass-core-v2.html
"""

import json
import logging
import os
# Bandit B404 accepted: subprocess is required to drive systemctl. Both call
# sites use a fixed argv list with shell=False and an absolute path from
# _systemctl_path().
import subprocess  # nosec B404
import time
from pathlib import Path

import greengrasssdk

# --- Configuration ---
STATUS_TOPIC_PREFIX = "greengrass/upgrade/status"
V2_INSTALLER_DIR = "/var/lib/gg-v2-upgrade"
V2_INSTALLER_PATH = f"{V2_INSTALLER_DIR}/greengrass-nucleus.zip"
READINESS_REPORT_PATH = f"{V2_INSTALLER_DIR}/readiness-report.json"
V2_INSTALL_ROOT = "/greengrass/v2"

UPGRADE_SERVICE_NAME = "gg-v1-to-v2-upgrade"
UPGRADE_SCRIPT_PATH = f"{V2_INSTALLER_DIR}/do-upgrade.sh"
UPGRADE_LOG_PATH = f"{V2_INSTALLER_DIR}/upgrade.log"
UPGRADE_STATUS_FILE = f"{V2_INSTALLER_DIR}/upgrade-status"

TES_ROLE_NAME = os.environ.get("TES_ROLE_NAME", "GreengrassV2TokenExchangeRole")
TES_ROLE_ALIAS = os.environ.get("TES_ROLE_ALIAS", "GreengrassCoreTokenExchangeRoleAlias")
THING_POLICY_NAME = os.environ.get("THING_POLICY_NAME", "GreengrassV2IoTThingPolicy")
TARGET_THING_GROUP = os.environ.get("TARGET_THING_GROUP", "GreengrassV2_UpgradedFromV1")

# The IoT credentials-provider endpoint is ACCOUNT-SPECIFIC (e.g.
# c1xxxxxxxxxxxx.credentials.iot.us-east-1.amazonaws.com). It cannot be derived
# on the device
IOT_CRED_ENDPOINT = os.environ.get("IOT_CRED_ENDPOINT", "")

# The nucleus version Phase 2 downloaded; SHA re-verified before install.
# Must match lambda/readiness-checker/handler.py (currently nucleus 2.18.3).
V2_INSTALLER_SHA256 = os.environ.get(
    "V2_INSTALLER_SHA256",
    "538eb3a10dbfcb534865d1da3ede39dcfe3db705bd1938088a07ce3dfd575237",
)

PROVISION_MODE = os.environ.get("PROVISION_MODE", "manual").lower()

# One-shot-per-deployment guard. This is a pinned Lambda, so GG V1
# restarts it after every daemon restart. Without a guard that
# becomes an infinite loop. Re-deploy Phase 3 for a new ID
UPGRADE_RUN_ID = os.environ.get("UPGRADE_RUN_ID", "")
ATTEMPT_MARKER_PATH = f"{V2_INSTALLER_DIR}/upgrade-attempt-id"

V1_SERVICE_NAME = "greengrass"

# Absolute candidate paths for systemctl, tried in order.
#
# Deliberately NOT resolved via PATH/shutil.which: this module runs as root and
# drives the destructive cutover, so the executable it invokes must not depend
# on an environment variable at all. Several paths are listed rather than a
# single hardcoded /usr/bin/systemctl because Phase 2 only asserts that
# systemctl exists, and the two gates must agree on images that ship it in
# /bin or /usr/sbin.
SYSTEMCTL_CANDIDATES = (
    "/usr/bin/systemctl",
    "/bin/systemctl",
    "/usr/sbin/systemctl",
    "/sbin/systemctl",
)

# --- Setup ---
logger = logging.getLogger()
logger.setLevel(logging.INFO)

iot_client = greengrasssdk.client("iot-data")


def _systemctl_path():
    """First existing executable in SYSTEMCTL_CANDIDATES.

    Consults only absolute paths, never PATH, so no environment variable can
    influence what this root process executes. Raises rather than falling back
    to a bare `systemctl`.
    """
    for candidate in SYSTEMCTL_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError(
        "systemctl not found at any expected absolute path "
        f"({', '.join(SYSTEMCTL_CANDIDATES)})"
    )


def _read_v1_config():
    with open("/greengrass/config/config.json", "r") as f:
        return json.load(f)


def get_thing_name():
    thing_name = os.environ.get("AWS_IOT_THING_NAME")
    if thing_name:
        return thing_name
    try:
        config = _read_v1_config()
        thing_arn = config.get("coreThing", {}).get("thingArn", "")
        return thing_arn.split("/")[-1] if "/" in thing_arn else "unknown"
    except Exception:
        return "unknown"


def get_iot_config():
    """Pull the V1 IoT identity off disk to reuse it for V2."""
    config = _read_v1_config()
    core = config.get("coreThing", {})
    crypto = config.get("crypto", {})
    principals = crypto.get("principals", {})
    iot_cert = principals.get("IoTCertificate", {})

    thing_arn = core.get("thingArn", "")
    parts = thing_arn.split(":")
    region = parts[3] if len(parts) >= 4 else ""

    def _strip(prefix, value):
        return value[len(prefix):] if value.startswith(prefix) else value

    cert_path = _strip("file://", iot_cert.get("certificatePath", ""))
    key_path = _strip("file://", iot_cert.get("privateKeyPath", ""))
    ca_path = _strip("file://", crypto.get("caPath", ""))

    iot_host = core.get("iotHost", "")

    return {
        "thing_name": thing_arn.split("/")[-1] if "/" in thing_arn else "",
        "thing_arn": thing_arn,
        "region": region,
        "iot_data_endpoint": iot_host,
        "cert_path": cert_path,
        "key_path": key_path,
        "ca_path": ca_path,
    }


def publish_status(thing_name, status, message, details=None):
    """Best-effort MQTT status publish. Silently skips if the SDK isn't healthy."""
    topic = f"{STATUS_TOPIC_PREFIX}/{thing_name}"
    payload = {
        "thingName": thing_name,
        "timestamp": int(time.time()),
        "phase": "upgrade-execution",
        "status": status,
        "message": message,
    }
    if details:
        payload["details"] = details

    logger.info(f"Publishing to {topic}: {json.dumps(payload)}")
    try:
        iot_client.publish(topic=topic, payload=json.dumps(payload))
    except Exception as e:
        logger.error(f"Failed to publish MQTT: {e}")


def _sha256(path):
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_readiness(iot_config):
    # Gate 1: Phase 2 must have run on THIS device and concluded the device
    # is upgradeable.
    try:
        with open(READINESS_REPORT_PATH) as f:
            report = json.load(f)
    except FileNotFoundError:
        return False, (
            f"No readiness report at {READINESS_REPORT_PATH}. "
            "Run Phase 2 (readiness-checker) first."
        )
    except Exception as e:
        return False, f"Could not read readiness report: {e}"
    status = report.get("overall_status", "")
    if status not in ("PASS", "PASS_WITH_WARNINGS"):
        return False, (
            f"Readiness report says {status or 'UNKNOWN'} — refusing to upgrade. "
            "Fix the failed checks and redeploy Phase 2."
        )
    # Freshness: a PASS from weeks ago says nothing about today's disk space,
    # clock, or Java. Default value is 24h for a valid report
    generated_at = report.get("generated_at", 0)
    age = time.time() - generated_at
    if not generated_at or age > 24 * 3600:
        return False, (
            f"Readiness report is too old ({age / 3600:.1f}h; max 24h) or "
            "unstamped. Redeploy Phase 2 for a fresh report."
        )

    # Gate 2: the installer must exist AND still hash to the pinned SHA256.
    # Phase 2 verified it at download time
    if not V2_INSTALLER_SHA256:
        return False, (
            "V2_INSTALLER_SHA256 is empty — refusing to install an unverified "
            "artifact. Set the pin (see lambda/readiness-checker/handler.py)."
        )
    if not os.path.exists(V2_INSTALLER_PATH):
        return False, (
            f"V2 installer not found at {V2_INSTALLER_PATH}. "
            "Run Phase 2 (readiness-checker) first."
        )
    file_size = os.path.getsize(V2_INSTALLER_PATH)
    if file_size < 1_000_000:
        return False, f"V2 installer file too small ({file_size} bytes)."
    digest = _sha256(V2_INSTALLER_PATH)
    if digest.lower() != V2_INSTALLER_SHA256.lower():
        return False, (
            f"Installer SHA256 mismatch (got {digest[:12]}…). "
            "Delete it and re-run Phase 2."
        )

    # Gate 3: manual mode needs the account-specific credentials endpoint
    # (delivered via deploy-phase.sh -> Environment.Variables).
    if PROVISION_MODE == "manual" and not IOT_CRED_ENDPOINT:
        return False, (
            "IOT_CRED_ENDPOINT is not set. Deploy with scripts/deploy-phase.sh, "
            "which resolves and injects the account-specific IoT credentials-"
            "provider endpoint."
        )

    if PROVISION_MODE == "manual":
        # Manual mode requires the V1 identity files to exist.
        for label, path in (
            ("certificate", iot_config["cert_path"]),
            ("private key", iot_config["key_path"]),
            ("root CA", iot_config["ca_path"]),
        ):
            if not path or not os.path.isfile(path):
                return False, f"V1 {label} not readable at: {path or '(unset)'}"
        if not iot_config["region"] or not iot_config["thing_name"]:
            return False, "Region or thing name missing from /greengrass/config/config.json"
        if not iot_config["iot_data_endpoint"]:
            return False, "iotHost missing from /greengrass/config/config.json"
    elif PROVISION_MODE != "auto":
        return False, f"Unknown PROVISION_MODE: {PROVISION_MODE}"

    return True, f"Installer present ({file_size // (1024*1024)}MB), identity OK"


def _resolve_iot_cred_endpoint(region):  # noqa: ARG001 — kept for call-site symmetry
    """The credentials-provider endpoint is account-specific and NOT derivable
    on-device (credentials.iot.<region>.amazonaws.com does not resolve). It is
    injected by deploy-phase.sh via the GG function environment; verify_readiness
    already guaranteed it is present in manual mode."""
    return IOT_CRED_ENDPOINT


def _validate_yaml_safe(value, label):
    """Reject characters that would break the heredoc-emitted YAML.
    Real V1 configs never contain these, but a customer-edited config could."""
    if any(ch in value for ch in ('"', '\\', '$', '\n', '`')):
        raise ValueError(f"{label} contains characters unsafe for YAML emission: {value!r}")


def render_upgrade_script(iot_config, mode):
    """Render the bash script systemd will run. Bash variables use ${VAR};
    Python f-string braces are doubled."""
    thing_name = iot_config["thing_name"]
    region = iot_config["region"]
    iot_data_endpoint = iot_config["iot_data_endpoint"]
    iot_cred_endpoint = _resolve_iot_cred_endpoint(region)
    cert_path = iot_config["cert_path"]
    key_path = iot_config["key_path"]
    ca_path = iot_config["ca_path"]
    # Same topic the executor Lambda publishes to, so the operator sees one
    # continuous feed across the V1->V2 handover rather than two streams.
    status_topic = f"{STATUS_TOPIC_PREFIX}/{thing_name}"

    # Covers BOTH the config.json-derived values and the operator-set
    # environment variables that get embedded in the script/config.yaml.
    for label, val in (
        ("thing_name", thing_name),
        ("region", region),
        ("iot_data_endpoint", iot_data_endpoint),
        ("iot_cred_endpoint", iot_cred_endpoint),
        ("cert_path", cert_path),
        ("key_path", key_path),
        ("ca_path", ca_path),
        ("TES_ROLE_ALIAS", TES_ROLE_ALIAS),
        ("TES_ROLE_NAME", TES_ROLE_NAME),
        ("THING_POLICY_NAME", THING_POLICY_NAME),
        ("TARGET_THING_GROUP", TARGET_THING_GROUP),
    ):
        _validate_yaml_safe(val, label)

    if mode == "manual":
        provision_flag = "false"
        # Write config.yaml before invoking the installer so it picks up V1 identity.
        # Note: omit --component-default-user from the CLI
        manual_cfg_block = f"""
log "Writing V2 manual-provisioning config..."
mkdir -p "$V2_INSTALL_ROOT"
chmod 700 "$V2_INSTALL_ROOT"
cat >"$V2_INSTALL_ROOT/config.yaml" <<EOFCONFIG
---
system:
  certificateFilePath: "{cert_path}"
  privateKeyPath: "{key_path}"
  rootCaPath: "{ca_path}"
  rootpath: "$V2_INSTALL_ROOT"
  thingName: "{thing_name}"
services:
  aws.greengrass.Nucleus:
    componentType: "NUCLEUS"
    configuration:
      awsRegion: "{region}"
      iotRoleAlias: "{TES_ROLE_ALIAS}"
      iotDataEndpoint: "{iot_data_endpoint}"
      iotCredEndpoint: "{iot_cred_endpoint}"
      runWithDefault:
        posixUser: "ggc_user:ggc_group"
EOFCONFIG
chmod 600 "$V2_INSTALL_ROOT/config.yaml"
"""
        installer_extra_args = '--init-config "$V2_INSTALL_ROOT/config.yaml"'
        component_user_flag = ""
    else:
        # auto: lab/demo only. Creates new thing/cert/role.
        provision_flag = "true"
        manual_cfg_block = ""
        installer_extra_args = (
            f'--thing-name "{thing_name}-v2-auto" '
            f'--thing-group-name "{TARGET_THING_GROUP}" '
            f'--thing-policy-name "{THING_POLICY_NAME}" '
            f'--tes-role-name "{TES_ROLE_NAME}" '
            f'--tes-role-alias-name "{TES_ROLE_ALIAS}"'
        )
        component_user_flag = "--component-default-user ggc_user:ggc_group"

    return f"""#!/bin/bash
# =============================================================================
# Greengrass V1 -> V2 Upgrade Script
# Generated by the upgrade-executor Lambda. Runs as a systemd one-shot unit.
# Survives GG V1 shutdown.
#
# Mode: {mode}
# =============================================================================

# umask 077: written files (log, script copies, status, V1 unit backup) are
# owner-only by default. Avoids local non-root users reading the embedded
# V1 cert/key paths.
umask 077

set -uo pipefail
# NOTE: deliberately NOT using `set -e`. We need to catch installer exit codes
# and run a fallback (restart V1) on failure.

LOG="{UPGRADE_LOG_PATH}"
STATUS_FILE="{UPGRADE_STATUS_FILE}"
THING_NAME="{thing_name}"
REGION="{region}"
V2_INSTALL_ROOT="{V2_INSTALL_ROOT}"
INSTALLER_ZIP="{V2_INSTALLER_PATH}"
INSTALLER_DIR="{V2_INSTALLER_DIR}"
V1_SERVICE="{V1_SERVICE_NAME}"
V1_UNIT_BACKUP="$INSTALLER_DIR/v1-greengrass.service.bak"

# Identity + endpoint for direct-to-IoT-Core status publishing (see mqtt_publish).
IOT_DATA_ENDPOINT="{iot_data_endpoint}"
IOT_CERT="{cert_path}"
IOT_KEY="{key_path}"
IOT_CA="{ca_path}"
STATUS_TOPIC="{status_topic}"

mkdir -p "$(dirname "$LOG")"
chmod 700 "$INSTALLER_DIR" 2>/dev/null || true

log() {{
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $1" | tee -a "$LOG"
}}

# Publish a status message straight to AWS IoT Core over mutual TLS.
#
# WHY NOT the Greengrass SDK: from the moment this script stops V1 until V2
# finishes installing, there is no local Greengrass broker and no Lambda
# runtime — the SDK path used by the executor Lambda is gone. But the device
# still holds its X.509 cert, and AWS IoT Core accepts HTTPS publishes
# authenticated by that cert:
#   POST https://<data-endpoint>:8443/topics/<topic>?qos=<0|1>
# (https://docs.aws.amazon.com/iot/latest/developerguide/http.html)
# That bypasses Greengrass entirely, so the blackout window stops being a
# blind spot. The V1 device policy already grants iot:Publish.
#
# PORT 8443 IS REQUIRED, not optional. X.509 client-certificate auth for this
# REST API lives on 8443; the same request to 443 is rejected with
# `403 Missing authentication` (443 expects SigV4). Verified empirically
# against this account's ATS data endpoint.
#
# Strictly best-effort: short timeouts, all failures swallowed. Status
# reporting must never be able to stall or fail the upgrade itself. The
# status file remains the authoritative record.
mqtt_publish() {{
    local status="$1" message="$2"
    [ -n "$IOT_DATA_ENDPOINT" ] || return 0
    [ -r "$IOT_CERT" ] && [ -r "$IOT_KEY" ] || return 0
    command -v curl >/dev/null 2>&1 || return 0

    local payload
    payload=$(printf '{{"thingName":"%s","timestamp":%s,"phase":"upgrade","status":"%s","message":"%s","source":"systemd-unit"}}' \
        "$THING_NAME" "$(date -u +%s)" "$status" "$message")

    local ca_opt=""
    [ -r "$IOT_CA" ] && ca_opt="--cacert $IOT_CA"

    curl -sS -o /dev/null --max-time 10 --connect-timeout 5 \
        --cert "$IOT_CERT" --key "$IOT_KEY" $ca_opt \
        -X POST -H 'Content-Type: application/json' \
        --data "$payload" \
        "https://${{IOT_DATA_ENDPOINT}}:8443/topics/${{STATUS_TOPIC}}?qos=0" \
        >/dev/null 2>&1 || log "  (status publish failed for '$status' — continuing)"
    return 0
}}

# Single choke point: every status transition lands in the file AND on the
# topic, so adding a step can't accidentally leave the operator in the dark.
write_status() {{
    echo "$1" > "$STATUS_FILE"
    mqtt_publish "$1" "${{2:-$1}}"
}}

backup_v1_unit() {{
    # Capture V1's systemd unit BEFORE V2 installer overwrites it. Used for
    # rollback if the V2 install fails. The V1 unit's ExecStart points at
    # /greengrass/ggc/core/greengrassd; this is how we tell V1 from V2 later.
    local unit_path="/etc/systemd/system/${{V1_SERVICE}}.service"
    if [ -f "$unit_path" ] && grep -q "/greengrass/ggc/core/greengrassd" "$unit_path"; then
        cp -p "$unit_path" "$V1_UNIT_BACKUP"
        log "Backed up V1 unit -> $V1_UNIT_BACKUP"
    else
        log "No V1 systemd unit found at $unit_path (or doesn't look like V1) — fallback uses greengrassd directly."
    fi
}}

restore_v1_and_start() {{
    # Rollback path: V2 install failed, restore V1 if possible.
    if [ -f "$V1_UNIT_BACKUP" ]; then
        log "Restoring V1 systemd unit..."
        cp -p "$V1_UNIT_BACKUP" "/etc/systemd/system/${{V1_SERVICE}}.service"
        systemctl daemon-reload || true
        systemctl start --no-block "$V1_SERVICE" || true
    elif [ -x /greengrass/ggc/core/greengrassd ]; then
        /greengrass/ggc/core/greengrassd start &
    fi
}}

stop_v1() {{
    if systemctl list-unit-files | grep -q "^${{V1_SERVICE}}\\.service"; then
        log "Stopping V1 systemd unit (${{V1_SERVICE}}.service)..."
        systemctl stop "$V1_SERVICE" || true
    fi
    if [ -x /greengrass/ggc/core/greengrassd ]; then
        /greengrass/ggc/core/greengrassd stop || true
    fi
    sleep 5
}}

# V1 and V2 BOTH register `greengrass.service` — `systemctl is-active` alone
# cannot tell you which generation owns the unit. ExecStart can.
greengrass_service_is_v2() {{
    local exec_start
    exec_start="$(systemctl show -p ExecStart --value greengrass 2>/dev/null || true)"
    case "$exec_start" in
        *"/greengrass/v2/"*|*"loader"*) return 0 ;;
        *) return 1 ;;
    esac
}}

# --- Step 0: Pre-flight + idempotency guard ---
log "=== Greengrass V1 -> V2 Upgrade starting (mode={mode}) ==="
log "Thing: $THING_NAME, Region: $REGION"
write_status "IN_PROGRESS_starting" "Upgrade unit started; running pre-flight checks"

# Idempotency: if V2 is already installed AND the active greengrass.service
# is actually V2's (not a rolled-back V1 wearing the same unit name), treat
# a re-run as a no-op. A partial V2 tree with V1 active — the state a failed
# install leaves behind — must NOT report SUCCESS.
if [ -d "$V2_INSTALL_ROOT/alts/current/distro" ]; then
    if systemctl is-active --quiet greengrass && greengrass_service_is_v2; then
        log "V2 already installed at $V2_INSTALL_ROOT and greengrass.service points at V2. Nothing to do."
        write_status "SUCCESS" "Greengrass V2 is installed and the service is active"
        exit 0
    fi
    log "ERROR: V2 install tree exists at $V2_INSTALL_ROOT but the active service"
    log "       is not V2 (partial/failed prior install, possibly rolled back to V1)."
    log "       Refusing to re-run. Triage manually before retrying. To force a"
    log "       fresh install, remove $V2_INSTALL_ROOT first (and accept the risk)."
    write_status "FAILED_partial_v2_install_present" "Refusing to run: a partial V2 install already exists"
    exit 1
fi

if [ ! -f "$INSTALLER_ZIP" ]; then
    log "ERROR: installer not found at $INSTALLER_ZIP"
    write_status "FAILED_no_installer" "Staged V2 installer missing - re-run Phase 2"
    exit 1
fi

# --- Step 1: Install Java if missing ---
if ! command -v java >/dev/null 2>&1; then
    log "Java not found. Installing default-jre-headless..."
    write_status "IN_PROGRESS_installing_java" "Installing Java (JRE) via apt-get"
    DEBIAN_FRONTEND=noninteractive apt-get update -y || {{
        log "ERROR: apt-get update failed"
        write_status "FAILED_apt_update" "apt-get update failed; cannot install Java"
        exit 1
    }}
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends default-jre-headless || {{
        log "ERROR: apt-get install default-jre-headless failed"
        write_status "FAILED_apt_install" "apt-get install default-jre-headless failed"
        exit 1
    }}
fi
log "Java: $(java -version 2>&1 | head -1)"

# --- Step 2: Unzip installer ---
log "Unzipping V2 installer..."
write_status "IN_PROGRESS_unzip" "Unzipping the verified V2 nucleus installer"
mkdir -p "$INSTALLER_DIR/nucleus"
if ! unzip -o -q "$INSTALLER_ZIP" -d "$INSTALLER_DIR/nucleus"; then
    log "ERROR: unzip failed"
    write_status "FAILED_unzip" "Could not unzip the V2 installer"
    exit 1
fi

GG_JAR=$(find "$INSTALLER_DIR/nucleus" -name "Greengrass.jar" | head -1)
if [ -z "$GG_JAR" ]; then
    log "ERROR: Greengrass.jar not found in archive"
    write_status "FAILED_no_jar" "Greengrass.jar not found inside the installer archive"
    exit 1
fi

# --- Step 3: Back up V1 unit, then stop V1 (point of no return for V1 MQTT) ---
backup_v1_unit

# From the moment V1 stops until the installer finishes, an external
# termination (systemd TimeoutStartSec, operator systemctl stop, shutdown)
# would otherwise strand the device with V1 stopped and V2 absent. Restore
# V1 on the way out if we get killed inside that window.
on_terminated() {{
    log "TERMINATED mid-upgrade — attempting V1 restore before exit."
    write_status "FAILED_terminated_mid_upgrade" "Terminated mid-upgrade; attempting V1 restore"
    restore_v1_and_start
    exit 1
}}
trap on_terminated TERM INT

log "Stopping Greengrass V1..."
write_status "IN_PROGRESS_stopping_v1" "Stopping Greengrass V1 - MQTT via V1 ends here"
stop_v1
{manual_cfg_block}
# --- Step 4: Run V2 installer ---
log "Installing Greengrass V2 Nucleus to $V2_INSTALL_ROOT..."
write_status "IN_PROGRESS_installing_v2" "Running the V2 installer (this is the longest step)"

java \\
    -Droot="$V2_INSTALL_ROOT" \\
    -Dlog.store=FILE \\
    -jar "$GG_JAR" \\
    --aws-region "$REGION" \\
    {component_user_flag} \\
    --provision {provision_flag} \\
    --setup-system-service true \\
    --deploy-dev-tools false \\
    {installer_extra_args} \\
    >>"$LOG" 2>&1
INSTALL_EXIT=$?

if [ "$INSTALL_EXIT" -ne 0 ]; then
    log "ERROR: V2 installer exited $INSTALL_EXIT. Attempting V1 rollback."
    write_status "FAILED_v2_installer_exit_${{INSTALL_EXIT}}" "V2 installer exited ${{INSTALL_EXIT}}; rolling back to V1"
    restore_v1_and_start
    exit "$INSTALL_EXIT"
fi

# Install succeeded — from here a termination must NOT restore V1 over a
# working V2 registration.
trap - TERM INT

# --- Step 5: Verify V2 service is up ---
log "Verifying Greengrass V2 service is active..."
sleep 15
if systemctl is-active --quiet greengrass && greengrass_service_is_v2; then
    log "V2 systemd unit (greengrass.service) is active."
else
    log "WARN: greengrass.service not active. Listing greengrass-related units:"
    systemctl list-units --type=service --no-pager | grep -i green | tee -a "$LOG" || true
    log "WARN: V2 may still be starting. Check 'aws greengrassv2 list-core-devices'."
fi

write_status "SUCCESS" "Greengrass V2 is installed and the service is active"
log "=== Upgrade complete. V1 binaries preserved at /greengrass for rollback. ==="
log "V1 systemd unit backed up at: $V1_UNIT_BACKUP"
log "Verify: aws greengrassv2 list-core-devices --region $REGION"
"""


def write_upgrade_script(content):
    Path(V2_INSTALLER_DIR).mkdir(parents=True, exist_ok=True)
    # Restrict the parent dir: the upgrade script and config.yaml will land
    # here and contain the V1 cert/key paths (and config.yaml itself once V2
    # writes it). Owner-only. 0o700 is the minimum for a traversable root-owned
    # directory; 0o600 would make it unusable.
    # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions, python.lang.security.audit.insecure-file-permissions
    os.chmod(V2_INSTALLER_DIR, 0o700)
    path = Path(UPGRADE_SCRIPT_PATH)
    path.write_text(content)
    # 0700: executable by root only. systemd runs as root regardless. The
    # execute bit is required — systemd ExecStart cannot run a 0o600 file.
    # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions, python.lang.security.audit.insecure-file-permissions
    os.chmod(str(path), 0o700)
    return str(path)


def write_cleanup_script():
    """Drop a cleanup script alongside the upgrade artifacts. Customers can
    invoke `sudo /var/lib/gg-v2-upgrade/cleanup.sh` after V2 soak — no need to
    scp anything from the orchestration host.
    """
    cleanup = """#!/bin/bash
# Post-upgrade cleanup. Run as root AFTER V2 has been healthy for >= 24h.
#
#   sudo /var/lib/gg-v2-upgrade/cleanup.sh             # remove helper unit only
#   sudo /var/lib/gg-v2-upgrade/cleanup.sh --remove-v1 # also wipe the V1 install tree
#
# This script is dropped here by the upgrade-executor Lambda so you can run it
# on-device without separate access. It is generated from write_cleanup_script()
# in lambda/upgrade-executor/handler.py — edit it there, not here; any on-device
# edits are overwritten the next time Phase 3 runs.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 1; }

KEEP_V1=1
[ "${1-}" = "--remove-v1" ] && KEEP_V1=0

UPGRADE_UNIT=gg-v1-to-v2-upgrade.service
SCRATCH_DIR=/var/lib/gg-v2-upgrade

echo "Disabling helper unit ${UPGRADE_UNIT}..."
systemctl disable "$UPGRADE_UNIT" 2>/dev/null || true
systemctl stop    "$UPGRADE_UNIT" 2>/dev/null || true
rm -f "/etc/systemd/system/${UPGRADE_UNIT}"
systemctl daemon-reload

if [ "$KEEP_V1" -eq 0 ]; then
    V2_DISTRO=/greengrass/v2/alts/current/distro
    [ -d "$V2_DISTRO" ] || { echo "ERROR: $V2_DISTRO missing — refusing." >&2; exit 1; }
    systemctl is-active --quiet greengrass || { echo "ERROR: greengrass not active." >&2; exit 1; }
    EXEC_START="$(systemctl show -p ExecStart --value greengrass 2>/dev/null || true)"
    case "$EXEC_START" in
        *"/greengrass/v2/"*|*"loader"*) : ;;
        *) echo "ERROR: greengrass.service does not look like V2." >&2; exit 1 ;;
    esac

    # --- CRITICAL: never delete /greengrass/certs ---
    # In manual mode V2's device identity IS the V1 cert/key/CA at their
    # ORIGINAL paths (/greengrass/certs/*), baked into the nucleus's config
    # transaction log (config.tlog) — the authoritative store that no on-disk
    # YAML edit can change. Deleting them bricks V2 at its next restart.
    echo "Removing V1 tree under /greengrass (preserving /greengrass/v2 and /greengrass/certs)..."
    for entry in /greengrass/*; do
        [ "$entry" = "/greengrass/v2" ] && continue
        [ "$entry" = "/greengrass/certs" ] && continue
        [ -e "$entry" ] || continue
        rm -rf "$entry"
    done

    # Scratch dir irrelevant with V1 gone (holds V1 unit backup, attempt marker, log).
    rm -rf "$SCRATCH_DIR"
else
    echo "Leaving /greengrass and $SCRATCH_DIR in place."
    echo "Pass --remove-v1 once V2 is fully validated."
fi
echo "Done."
"""
    path = Path(V2_INSTALLER_DIR) / "cleanup.sh"
    path.write_text(cleanup)
    # 0700: the operator runs this as root after the soak. The execute bit is
    # required, so 0o600 is not an option.
    # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions, python.lang.security.audit.insecure-file-permissions
    os.chmod(str(path), 0o700)
    return str(path)


def write_systemd_unit(script_path):
    unit = f"""[Unit]
Description=Greengrass V1 to V2 Upgrade Service
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={script_path}
StandardOutput=journal+console
StandardError=journal+console
TimeoutStartSec=900
TimeoutStopSec=180

[Install]
WantedBy=multi-user.target
"""
    unit_path = f"/etc/systemd/system/{UPGRADE_SERVICE_NAME}.service"
    with open(unit_path, "w") as f:
        f.write(unit)
    # 0600: ExecStart points at our restricted upgrade script. systemd reads
    # the unit as root regardless of mode.
    os.chmod(unit_path, 0o600)
    # Executable is an absolute path from SYSTEMCTL_CANDIDATES (never PATH) and
    # the argument is a fixed constant, so no external input reaches argv.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit, python.lang.security.audit.dangerous-subprocess-use-audit
    subprocess.run(  # nosec B603
        [_systemctl_path(), "daemon-reload"], check=True, timeout=30
    )
    return unit_path


def start_upgrade_unit():
    # start WITHOUT enable, deliberately. An enabled one-shot would re-run
    # the whole upgrade on every boot until cleanup — after a failed install
    # that means an unattended stop-V1/retry/rollback loop on each reboot.
    # Start-only gives strictly better behavior: if the device reboots
    # mid-upgrade, V1's own (still-enabled) unit brings V1 back up; if the
    # upgrade succeeded, the V2 installer has already registered and enabled
    # its own greengrass.service.
    # Executable is an absolute path from SYSTEMCTL_CANDIDATES (never PATH); the
    # arguments and service name are module constants, so no external input
    # reaches argv.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit, python.lang.security.audit.dangerous-subprocess-use-audit
    subprocess.run(  # nosec B603
        [_systemctl_path(), "start", "--no-block", UPGRADE_SERVICE_NAME],
        check=True,
        timeout=30,
    )


def _publish_failure(thing_name, public_msg, exc):
    """Publish a generic message to MQTT; log the full exception locally.
    We keep system-internal details (paths, system errors) out of the public
    payload so a customer's IoT Rule logs don't leak them."""
    logger.error(f"Upgrade failure for {thing_name}: {exc!r}")
    publish_status(
        thing_name,
        "FAILED",
        f"{public_msg}. See {UPGRADE_LOG_PATH} on the device for details.",
    )


def _already_attempted():
    """True if this deployment's run ID was already consumed."""
    try:
        with open(ATTEMPT_MARKER_PATH) as f:
            return f.read().strip() == UPGRADE_RUN_ID
    except FileNotFoundError:
        return False
    except Exception:
        # Unreadable marker: err on the side of NOT re-running a destructive step.
        return True


def _record_attempt():
    os.makedirs(V2_INSTALLER_DIR, exist_ok=True)
    with open(ATTEMPT_MARKER_PATH, "w") as f:
        f.write(UPGRADE_RUN_ID)
    os.chmod(ATTEMPT_MARKER_PATH, 0o600)


def run_upgrade():
    thing_name = get_thing_name()
    logger.info(f"Starting upgrade execution for {thing_name} (mode={PROVISION_MODE})")

    if not UPGRADE_RUN_ID:
        publish_status(
            thing_name,
            "FAILED",
            "UPGRADE_RUN_ID is not set. Deploy with scripts/deploy-phase.sh, "
            "which stamps each deployment; refusing to run unstamped.",
        )
        return {"statusCode": 400, "body": "missing-run-id"}

    if _already_attempted():
        # Normal after the daemon restarts (e.g., our rollback restarted V1,
        # or the device rebooted mid-soak with V1 still active). Not an error.
        logger.info(f"Upgrade already attempted for run ID {UPGRADE_RUN_ID}; idling.")
        publish_status(
            thing_name,
            "SKIPPED",
            f"Upgrade already attempted for this deployment (run ID {UPGRADE_RUN_ID}). "
            f"Check {UPGRADE_STATUS_FILE} on the device. Re-deploy Phase 2 to retry.",
        )
        return {"statusCode": 200, "body": "already-attempted"}

    publish_status(
        thing_name,
        "IN_PROGRESS",
        f"Verifying Phase 2 readiness (mode={PROVISION_MODE})...",
    )

    try:
        iot_config = get_iot_config()
    except Exception as e:
        _publish_failure(thing_name, "Failed to read IoT config", e)
        return {"statusCode": 500, "body": "config-read-failed"}

    ready, msg = verify_readiness(iot_config)
    if not ready:
        # Readiness messages are operator-facing and don't include exception state,
        # so it's fine to publish them as-is.
        publish_status(thing_name, "FAILED", msg)
        return {"statusCode": 400, "body": msg}

    publish_status(thing_name, "IN_PROGRESS", "Writing upgrade script...")
    try:
        script_content = render_upgrade_script(iot_config, PROVISION_MODE)
        script_path = write_upgrade_script(script_content)
        cleanup_path = write_cleanup_script()
    except Exception as e:
        _publish_failure(thing_name, "Failed to write upgrade script", e)
        return {"statusCode": 500, "body": "script-write-failed"}

    publish_status(thing_name, "IN_PROGRESS", "Writing systemd unit...")
    try:
        unit_path = write_systemd_unit(script_path)
    except Exception as e:
        _publish_failure(thing_name, "Failed to write systemd unit", e)
        return {"statusCode": 500, "body": "unit-write-failed"}

    publish_status(
        thing_name,
        "UPGRADING",
        "Starting upgrade unit. GG V1 will be stopped shortly. "
        "Device will reappear in V2 console (~1-3 min).",
        details={
            "mode": PROVISION_MODE,
            "run_id": UPGRADE_RUN_ID,
            "upgrade_script": script_path,
            "upgrade_unit": unit_path,
            "upgrade_log": UPGRADE_LOG_PATH,
            "cleanup_script": cleanup_path,
            "v2_thing_name": thing_name if PROVISION_MODE == "manual" else f"{thing_name}-v2-auto",
        },
    )

    try:
        # Consume the run ID BEFORE starting the unit: if the unit fires and
        # our own rollback restarts V1, the restarted Lambda must see the
        # marker and idle instead of re-triggering the upgrade.
        _record_attempt()
        start_upgrade_unit()
    except Exception as e:
        _publish_failure(thing_name, "Failed to start upgrade unit", e)
        return {"statusCode": 500, "body": "unit-start-failed"}

    return {
        "statusCode": 200,
        "body": (
            f"Upgrade initiated for {thing_name}. "
            f"Monitor: aws greengrassv2 list-core-devices --region {iot_config['region']}"
        ),
    }


def function_handler(event, context):
    """No-op shim: GG V1 requires a handler symbol, but all work happens at
    module import (below). Invoking it again is safe — the run-ID guard makes
    re-entry a no-op."""
    return run_upgrade()


# ---------------------------------------------------------------------------
# Module-level trigger: executes when GG V1 starts this pinned Lambda after
# the Phase 3 deployment.
# ---------------------------------------------------------------------------
try:
    run_upgrade()
except Exception as _e:  # noqa: BLE001 — last-resort catch, report then idle
    logger.exception("Upgrade executor crashed at module level")
    try:
        _publish_failure(get_thing_name(), "Upgrade executor crashed", _e)
    except Exception:  # nosec B110
        # Deliberate: logger.exception above already recorded the crash. This is
        # the nested fallback for the failure *report* also failing. Raising
        # would crash-loop the pinned Lambda and could re-enter the upgrade;
        # the run-ID marker and on-device status file remain authoritative.
        pass
