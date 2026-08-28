#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
#   sudo /tmp/setup-v1-device.sh /tmp/<thing-name>
#
# What it does (idempotent):
#   1. OS prep: sysctl hardening flags GG V1 requires, ggc_user/ggc_group
#   2. Installs deps: python3, unzip, curl, Java (for V2 later)
#   3. Installs Python 3.8 for the GG V1 Lambda runtime (system
#      python3 is left untouched)
#   4. Downloads GG V1 1.11.6 (SHA256-pinned per arch), installs to /greengrass
#   5. Installs certs + config.json from the bundle
#   6. Registers and starts greengrass.service (systemd)

set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo $0 <bundle-dir>" >&2; exit 1; }
BUNDLE="${1:?usage: sudo $0 /tmp/<thing-name>  (bundle dir from provision-v1-device.sh)}"
# Each bundle file must exist EITHER in the bundle OR already installed from a
# previous run
declare -A INSTALLED=(
    [cert.pem]=/greengrass/certs/cert.pem
    [private.key]=/greengrass/certs/private.key
    [root.ca.pem]=/greengrass/certs/root.ca.pem
    [config.json]=/greengrass/config/config.json
)
for f in cert.pem private.key root.ca.pem config.json; do
    if [ ! -f "${BUNDLE}/${f}" ] && [ ! -f "${INSTALLED[$f]}" ]; then
        echo "Missing ${BUNDLE}/${f} (and not already installed) — copy the full bundle." >&2
        exit 1
    fi
done

GG_V1_VERSION=1.11.6
# SHA256 of the official 1.11.6 tarballs (each verified against a fresh
# download; the URL pattern and arch list come from the V1 docs).
SHA_aarch64=92dc496efd787fd70701059271986f596086e6d569a539527b88e6d7d1452d0f
SHA_armv7l=79425145dca285a5ce129b868e2680ada38e5b1aa2871519be14a75cff13d636
# Derive the userland (as opposed to kernel) architecture
detect_userland_arch() {
    local deb=""
    command -v dpkg >/dev/null 2>&1 && deb="$(dpkg --print-architecture 2>/dev/null)"
    case "$deb" in
        armhf)  echo armv7l;  return ;;
        arm64)  echo aarch64; return ;;
        amd64)  echo x86_64;  return ;;
    esac
    # No dpkg: combine kernel arch with userland word size.
    local bits; bits="$(getconf LONG_BIT 2>/dev/null || echo 64)"
    case "$(uname -m)" in
        aarch64|arm64)   [ "$bits" = "32" ] && echo armv7l || echo aarch64 ;;
        armv7l|armv7*)   echo armv7l ;;
        x86_64|amd64)    [ "$bits" = "32" ] && echo i686 || echo x86_64 ;;
        *)               echo "$(uname -m)" ;;
    esac
}
USERLAND_ARCH="$(detect_userland_arch)"

# EI_CLASS byte of an ELF header: 1 = 32-bit, 2 = 64-bit. Used to detect a
# previously installed binary built for the wrong userland.
elf_bits() {
    [ -r "$1" ] || return 1
    case "$(od -An -tu1 -j4 -N1 "$1" 2>/dev/null | tr -d ' ')" in
        1) echo 32 ;;
        2) echo 64 ;;
        *) return 1 ;;
    esac
}

case "$USERLAND_ARCH" in
    armv7l) EXPECTED_ELF_BITS=32 ;;
    i686)   EXPECTED_ELF_BITS=32 ;;
    *)      EXPECTED_ELF_BITS=64 ;;
esac

echo "Detected userland arch: ${USERLAND_ARCH} (${EXPECTED_ELF_BITS}-bit); kernel reports $(uname -m)"

echo "=== [1/6] OS prep ==="
# Hardlink/symlink protection: greengrassd refuses to start without these.
tee /etc/sysctl.d/98-greengrass.conf >/dev/null <<'EOF'
fs.protected_hardlinks = 1
fs.protected_symlinks = 1
EOF
sysctl --system >/dev/null

# GG V1 requires this user/group to exist even when Lambdas run as root.
id ggc_user >/dev/null 2>&1  || adduser --system --no-create-home ggc_user
getent group ggc_group >/dev/null || addgroup --system ggc_group

echo "=== [2/6] Dependencies ==="
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
# default-jre-headless is for Greengrass V2 later 
apt-get install -y --no-install-recommends \
    python3 unzip ca-certificates curl default-jre-headless

echo "=== [3/6]Python 3.8 runtime ==="
# GG V1 launches python3.8-runtime Lambdas by executing a binary
# named `python3.8` on PATH.
PY38_VERSION=3.8.20
PBS_TAG=20241002
PY38_PREFIX=/opt/greengrass-python3.8
PY38_BIN="${PY38_PREFIX}/bin/python3.8"

# Source tarball, used when no prebuilt exists for this userland (e.g. armhf).
# SHA-256 verified against a fresh download from python.org.
PY38_SRC_URL="https://www.python.org/ftp/python/${PY38_VERSION}/Python-${PY38_VERSION}.tgz"
PY38_SRC_SHA=9f2d5962c2583e67ef75924cd56d0c1af78bf45ec57035cf8a2cc09f74f4bf78

install_python38_from_source() {
    echo "  no prebuilt CPython ${PY38_VERSION} exists for ${USERLAND_ARCH}; building from source."

    echo "  installing build dependencies..."
    apt-get install -y --no-install-recommends \
        build-essential zlib1g-dev libssl-dev libffi-dev \
        libbz2-dev libreadline-dev libsqlite3-dev uuid-dev

    local src_tgz="/tmp/Python-${PY38_VERSION}.tgz"
    local build_dir="/tmp/python38-build"

    echo "  downloading ${PY38_SRC_URL}..."
    curl -fSL -o "$src_tgz" "$PY38_SRC_URL"
    local actual
    actual="$(sha256sum "$src_tgz" | awk '{print $1}')"
    if [ "$actual" != "$PY38_SRC_SHA" ]; then
        echo "SHA256 mismatch on Python source (got $actual) — refusing to build." >&2
        rm -f "$src_tgz"
        exit 1
    fi
    tar -tzf "$src_tgz" >/dev/null   # corrupt-archive guard

    rm -rf "$build_dir" && mkdir -p "$build_dir"
    tar -xzf "$src_tgz" -C "$build_dir" --strip-components=1

    echo "  configuring..."
    ( cd "$build_dir" && ./configure --prefix="$PY38_PREFIX" --with-ensurepip=install >/dev/null )
    echo "  compiling with $(nproc) jobs"
    ( cd "$build_dir" && make -j"$(nproc)" >/dev/null )
    echo "  installing to ${PY38_PREFIX}..."
    ( cd "$build_dir" && make altinstall >/dev/null )

    rm -rf "$build_dir" "$src_tgz"

    [ -x "$PY38_BIN" ] || { echo "Build finished but ${PY38_BIN} is missing." >&2; exit 1; }
    echo "  built $("$PY38_BIN" --version 2>&1)"
}

if [ -x "$PY38_BIN" ] && "$PY38_BIN" --version 2>&1 | grep -q "Python ${PY38_VERSION}"; then
    echo " Python ${PY38_VERSION} already installed at ${PY38_PREFIX}"
elif [ "$USERLAND_ARCH" = "armv7l" ]; then
    # No prebuilt exists for 32-bit ARM — go straight to source.
    install_python38_from_source
else
    case "$USERLAND_ARCH" in
        aarch64)
            PBS_ASSET="cpython-${PY38_VERSION}+${PBS_TAG}-aarch64-unknown-linux-gnu-install_only.tar.gz"
            PBS_SHA=9d8798f9e79e0fc0f36fcb95bfa28a1023407d51a8ea5944b4da711f1f75f1ed
            ;;
        *)
            echo "  no pinned prebuilt for ${USERLAND_ARCH}; falling back to a source build."
            install_python38_from_source
            PBS_ASSET=""
            ;;
    esac
fi

# Prebuilt path only — skipped when a source build already produced the binary.
if [ -n "${PBS_ASSET:-}" ]; then
    PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${PBS_ASSET}"
    echo "  downloading CPython ${PY38_VERSION} (python-build-standalone ${PBS_TAG})..."
    cd /tmp
    curl -fSL -o "$PBS_ASSET" "$PBS_URL"
    ACTUAL_SHA="$(sha256sum "$PBS_ASSET" | awk '{print $1}')"
    if [ "$ACTUAL_SHA" != "$PBS_SHA" ]; then
        echo "SHA256 mismatch for ${PBS_ASSET} (got $ACTUAL_SHA) — refusing to install." >&2
        exit 1
    fi
    tar -tzf "$PBS_ASSET" >/dev/null    # corrupt-archive guard
    rm -rf /tmp/pbs-python38 && mkdir -p /tmp/pbs-python38
    tar -xzf "$PBS_ASSET" -C /tmp/pbs-python38   # extracts a top-level python/
    rm -rf "$PY38_PREFIX"
    mv /tmp/pbs-python38/python "$PY38_PREFIX"
    rm -rf /tmp/pbs-python38 "$PBS_ASSET"
    echo "  installed to ${PY38_PREFIX}"
fi

# Expose ONLY python3.8 on PATH
ln -sf "$PY38_BIN" /usr/local/bin/python3.8

# Prove it actually RUNS before moving on. Previously this was a bare
# `echo "... $(python3.8 --version)"`: a wrong-architecture binary printed
# "cannot execute: required file not found" to stderr, the echo still
# succeeded, `set -e` never tripped, and setup continued to register a
# service that could not possibly start. Fail here instead, where the cause
# is obvious.
PY38_ELF_BITS="$(elf_bits "$PY38_BIN" || echo unknown)"
if ! PY38_REPORTED="$(/usr/local/bin/python3.8 --version 2>&1)"; then
    echo "ERROR: ${PY38_BIN} exists but cannot execute on this system." >&2
    echo "       Binary is ${PY38_ELF_BITS}-bit; this userland (${USERLAND_ARCH}) needs ${EXPECTED_ELF_BITS}-bit." >&2
    echo "       Delete ${PY38_PREFIX} and re-run to rebuild for the correct architecture." >&2
    exit 1
fi
echo "  /usr/local/bin/python3.8 -> ${PY38_REPORTED} (${PY38_ELF_BITS}-bit)"

echo "=== [4/6] Greengrass V1 ${GG_V1_VERSION} ==="
# Presence alone is NOT proof of a usable install.
GG_TREE_OK=0
if [ -x /greengrass/ggc/core/greengrassd ]; then
    INSTALLED_BITS="$(elf_bits "/greengrass/ggc/packages/${GG_V1_VERSION}/bin/daemon" || echo unknown)"
    if [ "$INSTALLED_BITS" = "$EXPECTED_ELF_BITS" ]; then
        echo "  /greengrass already installed (${INSTALLED_BITS}-bit, matches userland); skipping download."
        GG_TREE_OK=1
    elif [ "$INSTALLED_BITS" = "unknown" ]; then
        echo "  /greengrass present but daemon arch unreadable; reinstalling to be safe."
        rm -rf /greengrass/ggc
    else
        echo "  /greengrass present but is ${INSTALLED_BITS}-bit while this userland is ${EXPECTED_ELF_BITS}-bit."
        echo "  That install can never start here — replacing it (certs/config are preserved)."
        rm -rf /greengrass/ggc
    fi
fi

if [ "$GG_TREE_OK" -eq 0 ]; then
    case "$USERLAND_ARCH" in
        aarch64) GG_ARCH=aarch64; EXPECTED_SHA="$SHA_aarch64" ;;
        armv7l)  GG_ARCH=armv7l;  EXPECTED_SHA="$SHA_armv7l"  ;;
        # x86-64 tarballs exist but are not SHA-pinned here. Compute + pin a
        # hash yourself before extending.
        *) echo "Untested/unpinned userland arch for this runbook: ${USERLAND_ARCH}" >&2; exit 1 ;;
    esac
    ARCHIVE="greengrass-linux-${GG_ARCH}-${GG_V1_VERSION}.tar.gz"
    cd /tmp
    curl -fSL -o "$ARCHIVE" \
        "https://d1onfpft10uf5o.cloudfront.net/greengrass-core/downloads/${GG_V1_VERSION}/${ARCHIVE}"
    ACTUAL_SHA="$(sha256sum "$ARCHIVE" | awk '{print $1}')"
    if [ "$ACTUAL_SHA" != "$EXPECTED_SHA" ]; then
        echo "SHA256 mismatch (got $ACTUAL_SHA) — refusing to extract." >&2
        exit 1
    fi
    tar -tzf "$ARCHIVE" >/dev/null   # corrupt-archive guard
    tar -xzf "$ARCHIVE" -C /
    rm -f "$ARCHIVE"
fi

echo "=== [5/6] Identity + config ==="
mkdir -p /greengrass/certs /greengrass/config
# Install from the bundle when present
install_if_present() {
    local mode="$1" src="$2" dest="$3"
    if [ -f "$src" ]; then
        install -m "$mode" "$src" "$dest"
    else
        echo "  $(basename "$dest"): keeping previously installed copy."
    fi
}
install_if_present 644 "${BUNDLE}/cert.pem"    /greengrass/certs/cert.pem
install_if_present 600 "${BUNDLE}/private.key" /greengrass/certs/private.key
install_if_present 644 "${BUNDLE}/root.ca.pem" /greengrass/certs/root.ca.pem
install_if_present 600 "${BUNDLE}/config.json" /greengrass/config/config.json

echo "=== [6/6] systemd service ==="
# The unit name MUST be `greengrass.service`
tee /etc/systemd/system/greengrass.service >/dev/null <<'EOF'
[Unit]
Description=Greengrass V1 Daemon
After=network-online.target time-sync.target
Wants=network-online.target

[Service]
Type=forking
PIDFile=/run/greengrassd.pid
ExecStart=/greengrass/ggc/core/greengrassd start
ExecStop=/greengrass/ggc/core/greengrassd stop
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now greengrass

sleep 5
if systemctl --no-pager --quiet is-active greengrass; then
    echo "greengrass.service: ACTIVE"
else
    echo "greengrass.service FAILED — see below"
    tail -n 40 /greengrass/ggc/var/log/system/runtime.log 2>/dev/null || true
    tail -n 20 /greengrass/ggc/var/log/crash.log 2>/dev/null || true
    exit 1
fi

echo
echo "=============================================================="
echo "Greengrass V1 is running. Quick checks:"
echo "  sudo tail -f /greengrass/ggc/var/log/system/runtime.log"
echo "Cloud check (from your laptop; allow a minute or two for the queued"
echo "initial deployment to apply first):"
echo "  aws greengrass get-connectivity-info --thing-name <thing-name>"
echo "Next: return to the README — deploy the smoke-test / Phase 1."
echo "=============================================================="

# Securely remove the staged private key copy now that it's installed.
if [ -f "${BUNDLE}/private.key" ]; then
    shred -u "${BUNDLE}/private.key" 2>/dev/null || rm -f "${BUNDLE}/private.key"
fi
