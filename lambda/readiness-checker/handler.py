# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Greengrass V1 → V2 Upgrade: Phase 2 — Readiness Checker

This Lambda function is deployed to a Greengrass V1 group in no-container mode
running as root. It performs pre-upgrade validation and downloads the V2 installer
WITHOUT stopping V1 or making any destructive changes.

HOW IT RUNS: all work happens at module import time.
A pinned (long-lived) GG V1 Lambda executes its module-level code as soon as
the deployment starts it, then stays resident. Re-runs (daemon restart, device
reboot) are harmless: every check is read-only and the installer download is
skipped when the verified file is already present.

Reports results via MQTT to: greengrass/upgrade/readiness/<thingName>
Writes the full report to:   /var/lib/gg-v2-upgrade/readiness-report.json
(Phase 3 refuses to run without a passing report at that path.)

Prerequisites:
- GGC >= 1.7
- allowFunctionsToRunAsRoot: "yes" in config.json
- No-container mode
- UID/GID = 0 (root)

Reference:
- https://docs.aws.amazon.com/greengrass/v2/developerguide/upgrade-v1-core-devices.html
- https://docs.aws.amazon.com/greengrass/v1/developerguide/lambda-group-config.html
"""

import grp
import hashlib
import json
import logging
import os
import platform
import pwd
import re
import shutil
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import greengrasssdk

# --- Configuration ---
READINESS_TOPIC_PREFIX = "greengrass/upgrade/readiness"

# Pin to a specific Greengrass V2 Nucleus release for reproducibility and integrity.
# 2.18.3 is the latest release as of the last update of this sample (Aug 2026).
# To rev: replace both VERSION and SHA256 together. Compute the hash on a
# trusted host:
#   curl -sSLO https://d2s8p88vqu9w66.cloudfront.net/releases/greengrass-2.18.3.zip
#   shasum -a 256 greengrass-2.18.3.zip
V2_NUCLEUS_VERSION = "2.18.3"
V2_INSTALLER_URL = (
    f"https://d2s8p88vqu9w66.cloudfront.net/releases/greengrass-{V2_NUCLEUS_VERSION}.zip"
)
V2_INSTALLER_SHA256 = "538eb3a10dbfcb534865d1da3ede39dcfe3db705bd1938088a07ce3dfd575237"

# /var/lib, NOT /tmp: /tmp is periodically cleaned (systemd-tmpfiles), is
# tmpfs (wiped on reboot) on many distros incl. Debian 13+, and may be
# mounted noexec.
V2_INSTALLER_DIR = "/var/lib/gg-v2-upgrade"
V2_INSTALLER_PATH = f"{V2_INSTALLER_DIR}/greengrass-nucleus.zip"
READINESS_REPORT_PATH = f"{V2_INSTALLER_DIR}/readiness-report.json"
MIN_DISK_SPACE_MB = 1024     # V2 nucleus + JRE + working space.
MIN_FREE_RAM_MB = 256        # JVM + nucleus working set.
MIN_GGC_VERSION = (1, 7, 0)
REQUIRED_JAVA_VERSION = 8

# Conventional JVM install roots. Deliberately not exhaustive: JAVA_HOME and the
# `java` on PATH cover anything unusual (Temurin/Corretto under /opt, SDKMAN,
# Yocto images), so these only help when no launcher is on PATH yet.
JVM_SEARCH_DIRS = ("/usr/lib/jvm", "/usr/java", "/opt/java")

# The V2 nucleus requires glibc >= 2.25. Old V1 field devices — this sample's
# whole audience — are exactly where an older glibc turns up, and the nucleus
# simply won't run below it.
MIN_GLIBC_VERSION = (2, 25)

# Keep in sync with lambda/upgrade-executor/handler.py, which writes
# `runWithDefault.posixUser: "ggc_user:ggc_group"` into the V2 config. If these
# don't exist, components can't start — and that failure would otherwise land
# after V1 is already stopped.
V2_POSIX_USER = "ggc_user"
V2_POSIX_GROUP = "ggc_group"

# Commands the V2 nucleus documents as required, beyond the four this sample's
# own upgrade script invokes. Shell builtins (echo, exit) are omitted because
# shutil.which can't reliably resolve them even when the shell provides them.
V2_DOCUMENTED_COMMANDS = [
    "ps", "sh", "kill", "cp", "chmod", "rm", "ln", "id", "uname", "grep", "sudo",
]

# Mount points inspected for exec permissions. Bandit B108 accepted: these are
# compared as strings against /proc/mounts entries. This sample never writes to
# /tmp — it stages under V2_INSTALLER_DIR precisely to avoid it.
TMP_EXEC_MOUNTS = ("/", "/tmp")  # nosec B108

# Architectures the V2 installer is published for.
SUPPORTED_ARCHS = {"x86_64", "amd64", "aarch64", "arm64", "armv7l", "armv7hl"}

V2_INSTALLER_HOST = "d2s8p88vqu9w66.cloudfront.net"

# Directories a root-owned system binary is expected to live in. Resolving
# through this allowlist keeps a manipulated PATH out of the exec path without
# hardcoding /usr/bin, which would disagree with the PATH-based tool checks
# below and misreport images that ship java outside it.

AWS_REGION_PATTERN = re.compile(
    r"^(?:(?:af|ap|ca|cn|eu|il|me|mx|sa)-[a-z]+|"
    r"us-(?:(?:gov|iso|isob|isoe|isof)-)?[a-z]+)-[1-9][0-9]*$"
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects so an allowlisted URL cannot pivot to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_HTTPS_OPENER = urllib.request.build_opener(_NoRedirectHandler())


# --- Setup ---
logger = logging.getLogger()
logger.setLevel(logging.INFO)

iot_client = greengrasssdk.client("iot-data")


def _validated_region(value, source):
    """Return a conservative AWS region value, logging rejected input."""
    if isinstance(value, str) and AWS_REGION_PATTERN.fullmatch(value):
        return value
    if value:
        logger.warning("Rejected invalid AWS region from %s: %r", source, value)
    return None


def _open_allowlisted_https(request, allowed_host, timeout):
    """Open one exact HTTPS host on its default port without redirects."""
    url = request.full_url if isinstance(request, urllib.request.Request) else request
    parsed = urllib.parse.urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL contains an invalid port") from exc
    if parsed.scheme != "https":
        raise ValueError("Only HTTPS URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL userinfo is not allowed")
    if parsed.hostname != allowed_host:
        raise ValueError(f"URL host is not allowlisted: {parsed.hostname!r}")
    if port not in (None, 443):
        raise ValueError(f"URL port is not allowed: {port}")
    return _HTTPS_OPENER.open(request, timeout=timeout)


def get_thing_name():
    """Get the Greengrass core thing name from the environment or config."""
    # GG V1 sets this environment variable
    thing_name = os.environ.get("AWS_IOT_THING_NAME", None)
    if thing_name:
        return thing_name

    # Fallback: read from config.json
    config_path = "/greengrass/config/config.json"
    try:
        with open(config_path, "r") as f:
            config = json.load(f)
        thing_arn = config.get("coreThing", {}).get("thingArn", "")
        # ARN format: arn:aws:iot:<region>:<account>:thing/<thingName>
        return thing_arn.split("/")[-1] if "/" in thing_arn else "unknown"
    except Exception:
        return "unknown"


def publish_status(thing_name, status, details):
    """Publish readiness status to MQTT topic."""
    topic = f"{READINESS_TOPIC_PREFIX}/{thing_name}"
    payload = {
        "thingName": thing_name,
        "timestamp": int(time.time()),
        "phase": "readiness-check",
        "status": status,  # "PASS", "FAIL", "IN_PROGRESS"
        "details": details,
    }
    logger.info(f"Publishing to {topic}: {json.dumps(payload)}")
    iot_client.publish(topic=topic, payload=json.dumps(payload))


def publish_progress(thing_name, step, done=None, total=None, **extra):
    """Emit a lightweight progress ping while the check set runs.

    The existing subscription subject is `greengrass/upgrade/readiness/#`, 
    so this child topic reaches the cloud with no subscription or IoT-policy change.
    """
    payload = {
        "thingName": thing_name,
        "timestamp": int(time.time()),
        "phase": "readiness-check",
        "status": "IN_PROGRESS",
        "step": step,
    }
    if done is not None and total:
        payload["completed"] = done
        payload["total"] = total
        payload["percent"] = int(done * 100 / total)
    payload.update(extra)
    try:
        iot_client.publish(
            topic=f"{READINESS_TOPIC_PREFIX}/{thing_name}/progress",
            payload=json.dumps(payload),
        )
    except Exception as e:  # noqa: BLE001 - progress is never load-bearing
        logger.warning(f"Progress publish failed (continuing): {e}")


def check_disk_space():
    """Check available disk space on root partition."""
    result = {"check": "disk_space", "passed": False, "message": ""}
    try:
        total, used, free = shutil.disk_usage("/")
        free_mb = free // (1024 * 1024)
        total_mb = total // (1024 * 1024)
        result["free_mb"] = free_mb
        result["total_mb"] = total_mb

        if free_mb >= MIN_DISK_SPACE_MB:
            result["passed"] = True
            result["message"] = f"Sufficient disk space: {free_mb}MB free (need {MIN_DISK_SPACE_MB}MB)"
        else:
            result["message"] = f"Insufficient disk space: {free_mb}MB free (need {MIN_DISK_SPACE_MB}MB)"
    except Exception as e:
        result["message"] = f"Failed to check disk space: {str(e)}"
    return result


def _java_major(home):
    """Major Java version for a JVM home directory, or None.

    Prefers the `release` file: it ships with every JDK/JRE runtime image
    (JEP 220), is present in Debian's openjdk-*-jre-headless packages, and is
    distribution-independent. Falls back to the version embedded in the
    directory name, which covers the common packaging conventions
    (java-17-openjdk-arm64, java-1.8.0-openjdk, temurin-17-jdk, corretto-11).
    """
    try:
        with open(os.path.join(home, "release")) as f:
            for line in f:
                if line.startswith("JAVA_VERSION="):
                    # "17.0.13" -> 17; "1.8.0_402" -> 8
                    match = re.search(r"(?:1\.)?(\d+)", line.split("=", 1)[1].strip().strip('"'))
                    if match:
                        return int(match.group(1))
    except OSError:
        pass
    match = re.search(r"-(?:1\.)?(\d+)", os.path.basename(home.rstrip("/")))
    return int(match.group(1)) if match else None


def _candidate_java_homes():
    """JVM homes to consider, most authoritative first, de-duplicated."""
    homes = []
    env_home = os.environ.get("JAVA_HOME", "").strip()
    if env_home:
        homes.append(env_home)
    launcher = shutil.which("java")
    if launcher:
        # .../bin/java -> the JVM home. realpath follows Debian's alternatives
        # symlink chain and any vendor symlink farm.
        homes.append(os.path.dirname(os.path.dirname(os.path.realpath(launcher))))
    for root in JVM_SEARCH_DIRS:
        try:
            homes.extend(os.path.join(root, e) for e in os.listdir(root))
        except OSError:
            continue
    return [h for h in dict.fromkeys(homes) if os.path.isdir(h)]


def check_java():
    """Check Java 8+ availability without executing the JVM.

    Resolves candidate JVM homes from JAVA_HOME, the `java` on PATH, and the
    conventional install roots, then reads the version from each. Nothing here
    is distribution-specific except the fallback directory-name parse.

    No architecture check: unlike the python3.8 build in setup-v1-device.sh,
    which downloads a prebuilt for a guessed arch, a JVM installed by the
    system package manager always matches the system. check_required_tools
    separately hard-fails when `java` is missing from PATH, which is what the
    V2 installer actually requires.
    """
    result = {"check": "java", "passed": False, "message": ""}
    found = {}
    for home in _candidate_java_homes():
        major = _java_major(home)
        if major:
            found[home] = major

    if not found:
        launcher = shutil.which("java")
        if launcher:
            # A launcher exists but carries no version metadata anywhere. Don't
            # block a device that has a working JVM just because it is unlabelled
            # — the nucleus only needs >= 8, and Java 7 predates every layout
            # likely to appear here.
            result["passed"] = True
            result["warn"] = True
            result["java_bin"] = launcher
            result["message"] = (
                f"java found on PATH at {launcher}, but no version metadata "
                "(no release file, no version in its path). Assuming it satisfies "
                f">= {REQUIRED_JAVA_VERSION}; confirm manually if this device is unusual."
            )
            return result
        result["message"] = "Java not found. Will need to be installed before upgrade."
        result["java_installable"] = check_java_installable()
        return result

    java_home, java_version = max(found.items(), key=lambda kv: kv[1])
    result["java_version"] = java_version
    result["java_home"] = java_home
    result["jvms_found"] = sorted(set(found.values()))
    if java_version >= REQUIRED_JAVA_VERSION:
        result["passed"] = True
        result["message"] = (
            f"Java {java_version} found at {java_home} (need >= {REQUIRED_JAVA_VERSION})."
        )
    else:
        result["message"] = (
            f"Java {java_version} at {java_home} is older than {REQUIRED_JAVA_VERSION}."
        )
        result["java_installable"] = check_java_installable()
    return result


def check_java_installable():
    """Whether Phase 3 could install Java itself.

    Deliberately apt-specific: the Phase 3 script runs
    `apt-get install default-jre-headless`, so reporting "installable" on a
    dnf/apk/zypper system would be wrong.

    Only checks that apt-get exists rather than querying the package cache,
    because a wrong answer here is cheap. Phase 3 installs Java at Step 1,
    BEFORE it stops V1 at Step 3 (the documented "point of no return"), so a
    failed install reports FAILED_apt_install and exits with V1 still running
    and the device intact.
    """
    return shutil.which("apt-get") is not None


def check_ggc_version():
    """Check Greengrass Core software version."""
    result = {"check": "ggc_version", "passed": False, "message": ""}
    min_str = ".".join(str(p) for p in MIN_GGC_VERSION)
    try:
        version_str = ""
        pkg_dir = "/greengrass/ggc/packages"
        versions = []
        try:
            for entry in os.listdir(pkg_dir):
                match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", entry)
                if match and os.path.isdir(os.path.join(pkg_dir, entry)):
                    versions.append(tuple(int(p) for p in match.groups()))
        except OSError as e:
            logger.debug("Could not list %s: %s", pkg_dir, e)
        if versions:
            # V1's own OTA leaves older packages behind, so take the highest
            # rather than whatever readdir happens to return first.
            version_str = ".".join(str(p) for p in max(versions))

        # Fallback: parse RELEASE_NOTES.
        if not version_str:
            release_file = "/greengrass/ggc/core/RELEASE_NOTES"
            if os.path.exists(release_file):
                with open(release_file, "r") as f:
                    content = f.read()
                match = re.search(r"(\d+\.\d+\.\d+)", content)
                if match:
                    version_str = match.group(1)

        if not version_str:
            result["message"] = "Could not determine GGC version"
            return result

        result["ggc_version"] = version_str

        # The package directory yields a bare x.y.z, but RELEASE_NOTES text may
        # carry vendor prefixes like "Greengrass v1.11.6" or release dates, so
        # pull the first x.y.z out of whichever source produced version_str.
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", version_str)
        if not match:
            result["message"] = f"Could not parse GGC version: {version_str}"
            return result

        current = tuple(int(p) for p in match.groups())
        if current >= MIN_GGC_VERSION:
            result["passed"] = True
            result["message"] = f"GGC version {version_str} >= {min_str}"
        else:
            result["message"] = f"GGC version {version_str} < {min_str} (need >= {min_str})"
    except Exception as e:
        result["message"] = f"GGC version check failed: {str(e)}"
    return result


def check_root_lambda_config():
    """Verify allowFunctionsToRunAsRoot is enabled."""
    result = {"check": "root_lambda_config", "passed": False, "message": ""}
    try:
        config_path = "/greengrass/config/config.json"
        with open(config_path, "r") as f:
            config = json.load(f)

        runtime = config.get("runtime", {})
        allow_root = runtime.get("allowFunctionsToRunAsRoot", "no")

        if allow_root.lower() == "yes":
            result["passed"] = True
            result["message"] = "allowFunctionsToRunAsRoot is enabled"
        else:
            result["message"] = (
                "allowFunctionsToRunAsRoot is NOT enabled. "
                "This cannot be changed remotely — physical access required."
            )
    except Exception as e:
        result["message"] = f"Config check failed: {str(e)}"
    return result


def check_running_as_root():
    """Verify this Lambda is actually running as root."""
    result = {"check": "running_as_root", "passed": False, "message": ""}
    uid = os.getuid()
    gid = os.getgid()
    result["uid"] = uid
    result["gid"] = gid

    if uid == 0:
        result["passed"] = True
        result["message"] = f"Running as root (UID={uid}, GID={gid})"
    else:
        result["message"] = f"NOT running as root (UID={uid}, GID={gid}). Lambda must be configured as root."
    return result


def _get_region_from_config():
    """Return a validated region from config/environment, or us-east-1."""
    candidates = []
    try:
        with open("/greengrass/config/config.json", "r") as f:
            config = json.load(f)
        thing_arn = config.get("coreThing", {}).get("thingArn", "")
        parts = thing_arn.split(":")
        if len(parts) >= 4 and parts[3]:
            candidates.append((parts[3], "config thingArn"))
    except Exception as exc:
        logger.warning("Could not read AWS region from config: %s", exc)

    for variable in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        value = os.environ.get(variable)
        if value:
            candidates.append((value, variable))

    for value, source in candidates:
        region = _validated_region(value, source)
        if region:
            return region
    return "us-east-1"


def _v1_cert_paths():
    """cert/key/CA file paths from config.json, or None if not usable. The
    readiness checker runs as root, so it can read the private key."""
    try:
        with open("/greengrass/config/config.json") as f:
            cfg = json.load(f)
    except Exception:
        return None
    crypto = cfg.get("crypto", {})
    iot_cert = crypto.get("principals", {}).get("IoTCertificate", {})
    strip = lambda v: v[len("file://"):] if v.startswith("file://") else v
    cert = strip(iot_cert.get("certificatePath", ""))
    key = strip(iot_cert.get("privateKeyPath", ""))
    ca = strip(crypto.get("caPath", ""))
    if cert and key and os.path.isfile(cert) and os.path.isfile(key):
        return {"cert": cert, "key": key, "ca": ca if os.path.isfile(ca) else None}
    return None


def check_network_connectivity():
    """Check connectivity to required AWS endpoints on 443, region-aware."""
    result = {"check": "network", "passed": False, "message": ""}
    region = _get_region_from_config()

    # A completed TLS handshake confirms the device can reach the endpoint and
    # nothing is intercepting TLS. These three accept an anonymous handshake
    # (they return 4xx without auth, but the handshake itself succeeds).
    endpoints = [
        ("Greengrass V2 installer (CloudFront)", "d2s8p88vqu9w66.cloudfront.net"),
        ("IoT data (control plane)", f"greengrass-ats.iot.{region}.amazonaws.com"),
        ("STS (V2 provisioning)", f"sts.{region}.amazonaws.com"),
    ]

    ok, failed = [], []
    for name, host in endpoints:
        try:
            with socket.create_connection((host, 443), timeout=10) as sock:
                ctx = ssl.create_default_context()
                with ctx.wrap_socket(sock, server_hostname=host):
                    ok.append(name)
        except Exception as e:
            failed.append(f"{name} ({type(e).__name__})")

    # The IoT credentials-provider endpoint is ACCOUNT-SPECIFIC
    # (c1xxxx.credentials.iot.<region>.amazonaws.com); the generic regional
    # hostname does not resolve. deploy-phase.sh injects the real one via the
    # function environment. Crucially, this endpoint requires MUTUAL TLS. 
    # Fall back to a TCP-only reachability check if the identity files somehow 
    # aren't readable.
    cred_endpoint = os.environ.get("IOT_CRED_ENDPOINT", "").strip()
    if not cred_endpoint:
        result["warn"] = True
        result["cred_endpoint_note"] = (
            "IOT_CRED_ENDPOINT not set — credentials-provider reachability "
            "not tested. Deploy with scripts/deploy-phase.sh to inject it."
        )
    else:
        name = "IoT credentials provider"
        certs = _v1_cert_paths()
        try:
            with socket.create_connection((cred_endpoint, 443), timeout=10) as sock:
                if certs:
                    ctx = (ssl.create_default_context(cafile=certs["ca"])
                           if certs["ca"] else ssl.create_default_context())
                    ctx.load_cert_chain(certs["cert"], certs["key"])
                    with ctx.wrap_socket(sock, server_hostname=cred_endpoint):
                        ok.append(name + " (mTLS)")
                else:
                    # Couldn't present a client cert; TCP reachability is the
                    # most we can assert (anonymous TLS would reset here).
                    ok.append(name + " (TCP only — identity files unreadable)")
        except Exception as e:
            failed.append(f"{name} ({type(e).__name__})")

    result["region"] = region
    result["endpoints_ok"] = ok
    result["endpoints_failed"] = failed
    if not failed:
        result["passed"] = True
        result["message"] = f"All {len(ok)} endpoints reachable in {region}"
    else:
        result["message"] = f"Unreachable: {', '.join(failed)}"
    return result


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download_v2_installer(thing_name=None):
    """Download and verify the V2 installer (but don't execute it).

    thing_name is optional and only enables mid-download progress pings;
    the check works identically without it.
    """
    result = {
        "check": "v2_installer_download",
        "passed": False,
        "message": "",
        "url": V2_INSTALLER_URL,
        "version": V2_NUCLEUS_VERSION,
    }
    try:
        os.makedirs(V2_INSTALLER_DIR, exist_ok=True)
        # Owner-only from the start: this dir will hold the installer, the
        # readiness report, and (in Phase 2) the rendered upgrade script.
        # 0o700 is already the minimum for a root-owned directory: it must stay
        # traversable by its owner, so the narrower 0o600 some linters suggest
        # would make it unusable. Non-executable files here use 0o600.
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions, python.lang.security.audit.insecure-file-permissions
        os.chmod(V2_INSTALLER_DIR, 0o700)

        # Idempotency: if a previous run already downloaded and the pinned
        # SHA256 still matches, skip the ~52 MB re-download. This makes
        # daemon restarts / reboots of the pinned Lambda cheap.
        if V2_INSTALLER_SHA256 and os.path.isfile(V2_INSTALLER_PATH):
            if _sha256(V2_INSTALLER_PATH).lower() == V2_INSTALLER_SHA256.lower():
                result["passed"] = True
                result["sha256_verified"] = True
                result["file_size_mb"] = round(os.path.getsize(V2_INSTALLER_PATH) / (1024 * 1024), 1)
                result["message"] = (
                    f"V2 installer {V2_NUCLEUS_VERSION} already present and "
                    f"checksum-verified ({result['file_size_mb']}MB); skipping download."
                )
                return result
            # Stale or corrupted leftover — remove and re-download.
            os.remove(V2_INSTALLER_PATH)

        max_retries = 3
        for attempt in range(max_retries):
            try:
                logger.info(f"Downloading V2 installer (attempt {attempt + 1}/{max_retries})")
                # Chunked download with a socket timeout. urlretrieve has no
                # timeout parameter
                with _open_allowlisted_https(
                    V2_INSTALLER_URL, V2_INSTALLER_HOST, timeout=60
                ) as resp, open(V2_INSTALLER_PATH, "wb") as out:
                    expected = int(resp.headers.get("Content-Length") or 0)
                    chunk_size = 1024 * 1024
                    fetched = 0
                    last_ping = time.monotonic()
                    last_ping_bytes = 0
                    while True:
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        out.write(chunk)
                        fetched += len(chunk)
                        now = time.monotonic()
                        if thing_name and (now - last_ping >= 10) and \
                                (fetched - last_ping_bytes >= 4 * chunk_size):
                            mb = round(fetched / (1024 * 1024), 1)
                            extra = {"downloaded_mb": mb}
                            if expected:
                                extra["percent_downloaded"] = int(fetched * 100 / expected)
                                extra["total_mb"] = round(expected / (1024 * 1024), 1)
                            publish_progress(
                                thing_name, "v2_installer_download",
                                message=f"Downloading V2 installer... {mb} MB",
                                attempt=attempt + 1, **extra)
                            last_ping = now
                            last_ping_bytes = fetched
                break
            except Exception as e:
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    logger.warning(f"Download attempt {attempt + 1} failed: {e}. Retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    raise

        file_size = os.path.getsize(V2_INSTALLER_PATH)
        if file_size <= 1_000_000:
            result["message"] = f"Downloaded file too small ({file_size} bytes), likely corrupted"
            return result

        # Integrity: SHA256 (must be pinned) + structural (Greengrass.jar must be present).
        digest = _sha256(V2_INSTALLER_PATH)
        result["sha256"] = digest
        allow_unverified = os.environ.get("ALLOW_UNVERIFIED_INSTALLER", "").lower() in ("1", "true", "yes")

        if not V2_INSTALLER_SHA256:
            if not allow_unverified:
                result["message"] = (
                    "V2_INSTALLER_SHA256 is empty. Pin a published SHA256 in "
                    "lambda/readiness-checker/handler.py before deploying this. "
                    "To override for testing, set the Lambda env var "
                    "ALLOW_UNVERIFIED_INSTALLER=1 (NOT recommended for production)."
                )
                # Remove the unverified file so Phase 3 cannot pick it up.
                try:
                    os.remove(V2_INSTALLER_PATH)
                except OSError:
                    pass
                return result
            logger.warning(
                "ALLOW_UNVERIFIED_INSTALLER=1 set — skipping SHA256 verification. "
                "Do not use in production."
            )
            result["sha256_verified"] = False
        elif digest.lower() != V2_INSTALLER_SHA256.lower():
            result["message"] = (
                f"SHA256 mismatch: got {digest}, expected {V2_INSTALLER_SHA256}. "
                "Refusing to proceed."
            )
            try:
                os.remove(V2_INSTALLER_PATH)
            except OSError:
                pass
            return result
        else:
            result["sha256_verified"] = True

        try:
            with zipfile.ZipFile(V2_INSTALLER_PATH) as zf:
                names = zf.namelist()
                has_jar = any(n.endswith("/Greengrass.jar") or n == "Greengrass.jar" for n in names)
                if not has_jar:
                    result["message"] = "Greengrass.jar not found inside installer ZIP"
                    return result
        except zipfile.BadZipFile:
            result["message"] = "Installer is not a valid ZIP file"
            return result

        result["passed"] = True
        result["file_size_mb"] = round(file_size / (1024 * 1024), 1)
        sha_note = "verified" if result.get("sha256_verified") else "UNVERIFIED (override active)"
        result["message"] = (
            f"V2 installer {V2_NUCLEUS_VERSION} downloaded ({result['file_size_mb']}MB), "
            f"checksum {sha_note}, archive valid."
        )
    except Exception as e:
        result["message"] = f"Failed to download V2 installer: {str(e)}"
    return result


def get_system_info():
    """Gather general system information for the report."""
    try:
        with open("/proc/uptime") as f:
            uptime_seconds = int(float(f.read().split()[0]))
    except Exception:
        uptime_seconds = -1
    return {
        "hostname": platform.node(),
        "architecture": platform.machine(),
        "os_release": platform.release(),
        "python_version": platform.python_version(),
        "uptime_seconds": uptime_seconds,
    }


def check_architecture():
    """Block early on architectures the V2 installer doesn't support."""
    result = {"check": "architecture", "passed": False, "message": ""}
    arch = platform.machine().lower()
    result["arch"] = arch
    if arch in SUPPORTED_ARCHS:
        result["passed"] = True
        result["message"] = f"Architecture {arch} is supported by Greengrass V2."
    else:
        result["message"] = (
            f"Architecture {arch} is NOT supported by Greengrass V2. "
            "Affected devices need hardware replacement, not a software upgrade."
        )
    return result


def check_glibc():
    """Hard gate: the V2 nucleus requires glibc >= 2.25.

    Undeterminable is treated as a failure, not a pass. A musl/uClibc device
    reports nothing here, and guessing would only surface the problem after V1
    is stopped.
    """
    result = {"check": "glibc", "passed": False, "message": ""}
    min_str = ".".join(str(p) for p in MIN_GLIBC_VERSION)

    version_str = ""
    try:
        version_str = os.confstr("CS_GNU_LIBC_VERSION") or ""
    except (ValueError, OSError, AttributeError):
        version_str = ""
    if not version_str:
        try:
            version_str = " ".join(platform.libc_ver()).strip()
        except Exception:
            version_str = ""
    result["libc"] = version_str

    match = re.search(r"(\d+)\.(\d+)", version_str)
    if not match or "glibc" not in version_str.lower():
        result["message"] = (
            f"Could not confirm glibc (reported: {version_str or 'nothing'}). "
            f"V2 nucleus requires glibc >= {min_str}. If this device uses musl or "
            "uClibc, the nucleus will not run and the device needs a different "
            "image, not a software upgrade."
        )
        return result

    current = (int(match.group(1)), int(match.group(2)))
    result["glibc_version"] = ".".join(str(p) for p in current)
    if current >= MIN_GLIBC_VERSION:
        result["passed"] = True
        result["message"] = f"glibc {result['glibc_version']} >= {min_str}."
    else:
        result["message"] = (
            f"glibc {result['glibc_version']} < {min_str}. The V2 nucleus will not "
            "run on this device; it needs an OS upgrade, not a Greengrass upgrade."
        )
    return result


def check_ggc_user():
    """Hard gate: the V2 config Phase 3 writes runs components as
    ggc_user:ggc_group. Missing either one fails only after V1 has been
    stopped, so it has to be caught here."""
    result = {"check": "ggc_user", "passed": False, "message": ""}
    missing = []
    try:
        pwd.getpwnam(V2_POSIX_USER)
    except KeyError:
        missing.append(f"user '{V2_POSIX_USER}'")
    except Exception as e:
        result["message"] = f"Could not resolve user '{V2_POSIX_USER}': {e}"
        return result
    try:
        grp.getgrnam(V2_POSIX_GROUP)
    except KeyError:
        missing.append(f"group '{V2_POSIX_GROUP}'")
    except Exception as e:
        result["message"] = f"Could not resolve group '{V2_POSIX_GROUP}': {e}"
        return result

    result["missing"] = missing
    if missing:
        result["message"] = (
            f"Missing {', '.join(missing)}. Phase 3 writes "
            f"runWithDefault.posixUser={V2_POSIX_USER}:{V2_POSIX_GROUP} into the V2 "
            "config, so components would fail to start after the cutover. Create "
            f"them first: adduser --system --no-create-home {V2_POSIX_USER} && "
            f"addgroup --system {V2_POSIX_GROUP}"
        )
        return result
    result["passed"] = True
    result["message"] = f"{V2_POSIX_USER}:{V2_POSIX_GROUP} both present."
    return result


def check_tmp_exec():
    """Warn: V2 documents /tmp as needing exec permissions.

    This sample deliberately stages its own artifacts under /var/lib, but the
    nucleus installer still uses /tmp, so a noexec /tmp can break the install.
    """
    result = {"check": "tmp_exec", "passed": True, "message": ""}
    try:
        # /tmp may be its own mount or inherit "/". Prefer the longest match.
        best_mount, best_opts = "", ""
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 4:
                    continue
                mount_point, opts = parts[1], parts[3]
                if mount_point in TMP_EXEC_MOUNTS and len(mount_point) >= len(best_mount):
                    best_mount, best_opts = mount_point, opts
        if not best_mount:
            result["warn"] = True
            result["message"] = "Could not find a mount entry for /tmp or /."
            return result

        result["mount"] = best_mount
        result["options"] = best_opts
        if "noexec" in best_opts.split(","):
            result["warn"] = True
            result["message"] = (
                f"{best_mount} is mounted noexec. V2 documents /tmp as requiring "
                "exec, so the nucleus installer may fail. Remount with exec before "
                "Phase 3."
            )
        else:
            result["message"] = f"{best_mount} is mounted exec."
    except Exception as e:
        result["warn"] = True
        result["message"] = f"Could not read /proc/mounts: {e}"
    return result


def check_init_system():
    """V2's --setup-system-service requires systemd. Detect alternatives."""
    result = {"check": "init_system", "passed": False, "message": ""}
    # systemd writes /run/systemd/system on boot.
    if os.path.isdir("/run/systemd/system"):
        result["passed"] = True
        result["init"] = "systemd"
        result["message"] = "systemd detected."
        return result

    # Sniff for known alternatives so the failure message is actionable.
    if os.path.isfile("/etc/init.d/cron") and not shutil.which("systemctl"):
        result["init"] = "sysvinit"
    elif os.path.isfile("/etc/init/system.conf"):
        result["init"] = "upstart"
    elif os.path.isfile("/sbin/openrc"):
        result["init"] = "openrc"
    elif os.path.isfile("/sbin/procd"):
        result["init"] = "procd (OpenWrt)"
    else:
        result["init"] = "unknown"
    result["message"] = (
        f"Init system is {result['init']}, not systemd. "
        "This sample only supports systemd-based devices. "
        "Only systemd devices are supported by this sample."
    )
    return result


def check_os_family():
    """Warn (not fail) outside Debian/Ubuntu — we apt-get install Java."""
    result = {"check": "os_family", "passed": False, "message": ""}
    info = {}
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    info[k] = v.strip('"')
    except Exception:
        result["message"] = "Could not read /etc/os-release."
        return result

    os_id = info.get("ID", "").lower()
    id_like = info.get("ID_LIKE", "").lower()
    result["os_id"] = os_id
    result["os_pretty"] = info.get("PRETTY_NAME", "")

    debian_family = os_id in ("debian", "ubuntu", "raspbian") or "debian" in id_like
    if debian_family:
        result["passed"] = True
        result["message"] = f"OS family is Debian-compatible ({result['os_pretty']})."
    else:
        # Don't hard-fail; Java may already be installed via another package
        # manager.
        result["passed"] = True
        result["warn"] = True
        result["message"] = (
            f"OS is {result['os_pretty']}, not Debian-family. "
            "If Java is missing, `apt-get install` will fail. "
            "Pre-install a JRE before deploying"
        )
    return result


def check_systemctl_writable():
    """Phase 3 writes a systemd unit."""
    result = {"check": "systemctl_writable", "passed": False, "message": ""}
    if not shutil.which("systemctl"):
        result["message"] = "systemctl not on PATH."
        return result
    if not os.access("/etc/systemd/system", os.W_OK):
        result["message"] = "/etc/systemd/system is not writable (read-only root?)."
        return result
    result["passed"] = True
    result["message"] = "systemctl present, /etc/systemd/system writable."
    return result


def check_required_tools():
    """Tools the upgrade script invokes. Missing any of these = hard fail."""
    result = {"check": "required_tools", "passed": False, "message": ""}
    required = ["unzip", "curl", "find", "tee"]
    missing = [t for t in required if not shutil.which(t)]

    if not shutil.which("java"):
        if not shutil.which("apt-get"):
            missing.append("java (no apt-get to install it)")

    result["missing"] = missing
    if missing:
        result["message"] = f"Missing required tools: {', '.join(missing)}"
        return result

    # V2's own documented command set. Warn rather than hard-fail: this sample's
    # upgrade path doesn't invoke all of them, but the nucleus can once
    # components are deployed.
    v2_missing = [c for c in V2_DOCUMENTED_COMMANDS if not shutil.which(c)]
    result["v2_documented_missing"] = v2_missing
    result["passed"] = True
    if v2_missing:
        result["warn"] = True
        result["message"] = (
            "Tools this upgrade needs are present, but the V2 nucleus also "
            f"documents these and they were not found: {', '.join(v2_missing)}. "
            "Install them before deploying V2 components."
            + (" Note that sudo also needs 'root ALL=(ALL:ALL) ALL' in /etc/sudoers."
               if "sudo" in v2_missing else "")
        )
    else:
        result["message"] = "All required CLI tools available, including V2's documented set."
    return result


def check_memory():
    """JVM + nucleus need RAM headroom V1 didn't."""
    result = {"check": "memory", "passed": False, "message": ""}
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k.strip()] = v.strip()

        # MemAvailable is the right number on modern kernels.
        avail_kb = int(info.get("MemAvailable", "0 kB").split()[0])
        total_kb = int(info.get("MemTotal", "0 kB").split()[0])
        result["mem_total_mb"] = total_kb // 1024
        result["mem_available_mb"] = avail_kb // 1024
        if avail_kb // 1024 >= MIN_FREE_RAM_MB:
            result["passed"] = True
            result["message"] = (
                f"Memory available: {result['mem_available_mb']}MB "
                f"(need {MIN_FREE_RAM_MB}MB)."
            )
        else:
            result["message"] = (
                f"Insufficient memory: {result['mem_available_mb']}MB available "
                f"(need {MIN_FREE_RAM_MB}MB). Reduce running components first."
            )
    except Exception as e:
        result["message"] = f"Failed to read /proc/meminfo: {e}"
    return result


def check_v1_identity_files():
    """Manual provisioning reuses the V1 cert/key/CA. They must be readable."""
    result = {"check": "v1_identity_files", "passed": False, "message": ""}
    try:
        with open("/greengrass/config/config.json") as f:
            cfg = json.load(f)
    except Exception as e:
        result["message"] = f"Could not read /greengrass/config/config.json: {e}"
        return result

    crypto = cfg.get("crypto", {})
    iot_cert = crypto.get("principals", {}).get("IoTCertificate", {})

    def _strip(p, v):
        return v[len(p):] if v.startswith(p) else v

    paths = {
        "certificate": _strip("file://", iot_cert.get("certificatePath", "")),
        "private_key": _strip("file://", iot_cert.get("privateKeyPath", "")),
        "root_ca": _strip("file://", crypto.get("caPath", "")),
    }
    result["paths"] = paths
    missing = [k for k, p in paths.items() if not p or not os.path.isfile(p)]
    if missing:
        result["message"] = f"Missing V1 identity files: {', '.join(missing)}"
        return result
    result["passed"] = True
    result["message"] = "V1 cert, key, and root CA all readable."
    return result


def check_clock_skew():
    """TLS handshake to AWS fails if the device clock drifts >5min."""
    result = {"check": "clock_skew", "passed": False, "message": ""}
    region = _get_region_from_config()
    host = f"sts.{region}.amazonaws.com"
    try:
        # The HTTP Date header from a TLS-terminated AWS endpoint is our reference.
        with _open_allowlisted_https(
            f"https://{host}/", host, timeout=15
        ) as resp:
            server_date = resp.headers.get("Date")
        if not server_date:
            # 4xx responses still carry Date; treat as success below if we got one.
            result["message"] = "No Date header in response."
            return result
    except urllib.error.HTTPError as e:
        server_date = e.headers.get("Date") if e.headers else None
        if not server_date:
            result["message"] = f"HTTP error and no Date header: {e}"
            return result
    except Exception as e:
        result["message"] = f"Could not reach {host}: {e}"
        return result

    try:
        from email.utils import parsedate_to_datetime

        server_ts = parsedate_to_datetime(server_date).timestamp()
        local_ts = time.time()
        skew = abs(local_ts - server_ts)
        result["skew_seconds"] = round(skew, 1)
        if skew < 60:
            result["passed"] = True
            result["message"] = f"Clock skew {skew:.1f}s (within tolerance)."
        elif skew < 300:
            result["passed"] = True
            result["warn"] = True
            result["message"] = f"Clock skew {skew:.1f}s (acceptable; consider chrony/ntpd)."
        else:
            result["message"] = (
                f"Clock skew {skew:.1f}s exceeds 5 minutes. "
                "TLS handshakes to AWS will fail. Sync time before upgrading."
            )
    except Exception as e:
        result["message"] = f"Failed to parse server date: {e}"
    return result


def check_proxy_env():
    """Surface HTTPS_PROXY etc. so operators know they're in scope."""
    result = {"check": "proxy_env", "passed": True, "message": ""}
    proxies = {
        k: os.environ.get(k, "")
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")
        if os.environ.get(k)
    }
    result["proxies"] = proxies
    if proxies:
        result["warn"] = True
        result["message"] = (
            f"Proxy env detected: {list(proxies.keys())}. "
            "The rendered V2 config does not pass proxies through; add networkProxy to it if needed."
        )
    else:
        result["message"] = "No proxy env vars set."
    return result


def check_v1_components():
    """Inventory deployed V1 Lambdas/connectors. The customer's V1 *workload*
    is NOT migrated by this upgrade; that's a separate component-rewrite project.
    """
    result = {"check": "v1_components", "passed": True, "message": ""}
    try:
        # GG V1 deployment artifacts under /greengrass/ggc/deployment/lambda/
        lambda_dir = "/greengrass/ggc/deployment/lambda"
        if os.path.isdir(lambda_dir):
            entries = sorted(os.listdir(lambda_dir))
            result["lambda_count"] = len(entries)
            result["lambdas"] = entries[:20]  # cap for payload size
        else:
            result["lambda_count"] = 0
            result["lambdas"] = []

        connector_file = "/greengrass/ggc/deployment/group/group.json"
        connector_count = 0
        if os.path.isfile(connector_file):
            with open(connector_file) as f:
                gj = json.load(f)
            connector_count = len(gj.get("connectors", []) or [])
        result["connector_count"] = connector_count

        if result["lambda_count"] > 0 or connector_count > 0:
            result["warn"] = True
            result["message"] = (
                f"WARNING: Found {result['lambda_count']} V1 Lambdas and "
                f"{connector_count} V1 connectors deployed on this device. "
                "**Phase 3 replaces the runtime, not your workload.** "
                "After upgrade, your V1 Lambdas/connectors STOP RUNNING "
                "until you migrate them to V2 generic components. Plan that "
                "migration as a separate project before deploying"
            )
        else:
            result["message"] = "No V1 Lambdas or connectors deployed."
    except Exception as e:
        result["message"] = f"Could not inventory V1 workload: {e}"
    return result


def check_bandwidth():
    """Lightweight bandwidth probe: HEAD the installer + measure 1 MB pull."""
    result = {"check": "bandwidth", "passed": True, "message": ""}
    try:
        # 1 MB pull as a quick estimate. Don't be precise; just flag obvious problems.
        start = time.time()
        req = urllib.request.Request(V2_INSTALLER_URL, headers={"Range": "bytes=0-1048575"})
        with _open_allowlisted_https(
            req, V2_INSTALLER_HOST, timeout=30
        ) as resp:
            data = resp.read()
        dur = max(time.time() - start, 0.001)
        kbps = (len(data) * 8 / 1000) / dur
        result["sample_kbps"] = round(kbps)
        result["sample_bytes"] = len(data)

        # ~52 MB installer at this rate (V2.18.3; rev when bumping V2_NUCLEUS_VERSION).
        installer_mb = 52
        eta_seconds = (installer_mb * 1024 * 8) / max(kbps, 1)
        result["installer_mb_estimate"] = installer_mb
        result["eta_full_download_seconds"] = round(eta_seconds)

        if kbps < 200:
            result["warn"] = True
            result["message"] = (
                f"~{result['sample_kbps']} kbps observed. Full installer "
                f"(~{installer_mb}MB) ETA {eta_seconds/60:.1f} min. For cellular/"
                "satellite fleets, mirror the nucleus ZIP to your own S3 bucket, "
                "grant the token-exchange role s3:GetObject on it, and repoint "
                "V2_INSTALLER_URL — the SHA256 pin still applies."
            )
        else:
            result["message"] = (
                f"~{result['sample_kbps']} kbps observed; full download "
                f"ETA ~{eta_seconds/60:.1f} min."
            )
    except Exception as e:
        result["warn"] = True
        result["message"] = f"Bandwidth probe failed: {e} (proceeding regardless)"
    return result


def run_readiness_check():
    """
    Run all checks and publish a comprehensive readiness report.

    Called from module level below — a pinned GG V1 Lambda executes its
    module top-level code when the deployment starts it, which is the only
    execution trigger this design uses.

    Hard-fail checks (any failure -> overall_status FAIL): the device cannot
    safely proceed to Phase 3.

    Soft / warn checks (warn=True): the device CAN proceed but the operator
    should know.
    """
    thing_name = get_thing_name()
    logger.info(f"Starting readiness check for {thing_name}")

    publish_status(thing_name, "IN_PROGRESS", {"message": "Readiness check starting..."})

    # Ordered (label, callable) pairs rather than a list of already-evaluated
    # results: the loop below publishes a progress ping as each one finishes,
    # so a long run (bandwidth probe + ~52 MB download) isn't silent for
    # minutes. Cheap checks come first so failures surface fast.
    CHECK_SEQUENCE = [
        ("running_as_root", check_running_as_root),
        ("root_lambda_config", check_root_lambda_config),
        ("architecture", check_architecture),
        ("glibc", check_glibc),
        ("ggc_user", check_ggc_user),
        ("init_system", check_init_system),
        ("systemctl_writable", check_systemctl_writable),
        ("tmp_exec", check_tmp_exec),
        ("required_tools", check_required_tools),
        ("os_family", check_os_family),
        ("ggc_version", check_ggc_version),
        ("disk_space", check_disk_space),
        ("memory", check_memory),
        ("java", check_java),
        ("v1_identity_files", check_v1_identity_files),
        ("clock_skew", check_clock_skew),
        ("proxy_env", check_proxy_env),
        ("network", check_network_connectivity),
        ("bandwidth", check_bandwidth),
        ("v1_components", check_v1_components),
        ("v2_installer_download", lambda: download_v2_installer(thing_name)),
    ]

    checks = []
    total_steps = len(CHECK_SEQUENCE)
    for idx, (label, fn) in enumerate(CHECK_SEQUENCE, start=1):
        publish_progress(thing_name, label, done=idx - 1, total=total_steps,
                         message=f"Running {label} ({idx}/{total_steps})...")
        try:
            outcome = fn()
        except Exception as e:  # a crashing check must not lose the whole report
            logger.exception(f"Check {label} raised")
            outcome = {"check": label, "passed": False,
                       "message": f"Check raised an exception: {e}"}
        checks.append(outcome)
        publish_progress(thing_name, label, done=idx, total=total_steps,
                         passed=bool(outcome.get("passed")),
                         message=outcome.get("message", "")[:200])

    # Hard-fail set: anything in this list failing blocks Phase 3.
    # required_tools, memory, and the conditional java/clock checks are added
    # below after we look at their individual outcomes.
    HARD_FAIL_CHECKS = {
        "running_as_root",
        "root_lambda_config",
        "architecture",
        "glibc",              # nucleus won't run below 2.25
        "ggc_user",           # posixUser in the V2 config Phase 3 writes
        "init_system",
        "systemctl_writable",
        "required_tools",     # missing curl/unzip/etc -> Phase 3 bash will fail
        "ggc_version",
        "disk_space",
        "memory",             # JVM won't start without RAM headroom
        "v1_identity_files",
        "network",
        "v2_installer_download",
    }

    by_name = {c["check"]: c for c in checks}
    hard_failures = [n for n in HARD_FAIL_CHECKS if not by_name.get(n, {}).get("passed", False)]
    warn_checks = [c for c in checks if c.get("warn") and c.get("passed")]

    java_check = by_name.get("java", {})
    java_installable = java_check.get("java_installable", False)
    java_blocks = (not java_check.get("passed")) and not java_installable
    if java_blocks:
        hard_failures.append("java")

    # clock_skew: hard-fail when the check failed AND we have evidence of
    # actual large drift, OR when the check failed for an unknown reason
    # (no skew_seconds at all, e.g., network error to STS during the probe).
    # The latter previously slipped through silently and devices with broken
    # TLS could still proceed.
    clock_check = by_name.get("clock_skew", {})
    if not clock_check.get("passed", False):
        skew = clock_check.get("skew_seconds")
        if skew is None or skew >= 300:
            hard_failures.append("clock_skew")

    all_passed = all(c["passed"] for c in checks)
    if hard_failures:
        overall_status = "FAIL"
    elif warn_checks or not all_passed:
        overall_status = "PASS_WITH_WARNINGS"
    else:
        overall_status = "PASS"

    # Build final report. generated_at lets Phase 3 reject stale reports.
    report = {
        "overall_status": overall_status,
        "all_checks_passed": all_passed,
        "generated_at": int(time.time()),
        "system_info": get_system_info(),
        "checks": checks,
        "recommendations": [],
    }

    # Add recommendations
    if not java_check["passed"] and java_installable:
        report["recommendations"].append(
            "Java not found but installable. The upgrade executor will install it automatically."
        )
    if not java_check["passed"] and not java_installable:
        report["recommendations"].append(
            "Java not found and cannot be auto-installed. Manual intervention required."
        )

    failed_checks = [c for c in checks if not c["passed"]]
    if failed_checks:
        report["recommendations"].append(
            f"Failed checks: {', '.join(c['check'] for c in failed_checks)}"
        )

    if overall_status == "PASS":
        report["recommendations"].append(
            "Device is ready for Phase 3 (upgrade execution). Deploy the upgrade-executor Lambda."
        )

    # Persist the report on-device. Phase 3 (upgrade-executor) reads this file
    # and refuses to run unless overall_status is PASS / PASS_WITH_WARNINGS
    try:
        os.makedirs(V2_INSTALLER_DIR, exist_ok=True)
        with open(READINESS_REPORT_PATH, "w") as f:
            json.dump(report, f, indent=2)
        os.chmod(READINESS_REPORT_PATH, 0o600)
    except Exception as e:
        logger.error(f"Could not write {READINESS_REPORT_PATH}: {e}")

    # Publish final report
    publish_status(thing_name, overall_status, report)
    logger.info(f"Readiness check complete: {overall_status}")

    return {"statusCode": 200, "body": report}


def function_handler(event, context):
    """No-op shim. GG V1 requires a handler symbol, but this Lambda does all
    its work at module import (see below)."""
    return run_readiness_check()

try:
    run_readiness_check()
except Exception as _e:  # noqa: BLE001 — last-resort catch, report then idle
    logger.exception("Readiness check crashed at module level")
    try:
        publish_status(
            get_thing_name(),
            "FAIL",
            {"message": f"Readiness check crashed: {type(_e).__name__}. See runtime.log on device."},
        )
    except Exception:  # nosec B110
        # Deliberate: the crash itself was already logged via logger.exception
        # above, and this is the nested fallback for the failure *report* also
        # failing. Raising here would crash-loop the pinned Lambda; the absent
        # MQTT report is the operator's failure signal.
        pass
