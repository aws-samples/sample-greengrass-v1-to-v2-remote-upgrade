#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Build deployable ZIPs for both Lambdas. Run from the repository root or
# anywhere — the script resolves its own location.
#
# Output:
#   build/smoke-test.zip
#   build/readiness-checker.zip
#   build/upgrade-executor.zip
#
# greengrasssdk is bundled into each ZIP. The GG V1 docs are explicit:
# "Include greengrasssdk in the Lambda function deployment package that
# contains your function code." The core does NOT provide the SDK to Python
# functions unless you pip-install it on the device yourself (a size-
# constrained-device escape hatch AWS recommends against).
# https://docs.aws.amazon.com/greengrass/v1/developerguide/lambda-functions.html

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${REPO_ROOT}/build"
SDK_DIR="${BUILD_DIR}/vendor"
mkdir -p "$BUILD_DIR"

# Fetch greengrasssdk once into build/vendor/. 1.6.1 is the final release and
# the version GGC 1.11.x is documented against.
if [ ! -d "${SDK_DIR}/greengrasssdk" ]; then
    echo "Vendoring greengrasssdk into ${SDK_DIR}..."
    python3 -m pip install --quiet --target "$SDK_DIR" "greengrasssdk==1.6.1"
fi

build() {
    local name="$1"
    local src="${REPO_ROOT}/lambda/${name}"
    local out="${BUILD_DIR}/${name}.zip"
    local stage="${BUILD_DIR}/stage-${name}"

    if [ ! -f "${src}/handler.py" ]; then
        echo "ERROR: missing ${src}/handler.py" >&2
        exit 1
    fi

    rm -rf "$stage" "$out"
    mkdir -p "$stage"
    cp "${src}/handler.py" "$stage/"
    cp -R "${SDK_DIR}/greengrasssdk" "$stage/"
    # ZIP contents must sit at the archive root (handler.py, greengrasssdk/).
    (cd "$stage" && zip -q -r "$out" handler.py greengrasssdk)
    rm -rf "$stage"
    echo "Built  $out  ($(wc -c <"$out") bytes)"
}

build smoke-test
build readiness-checker
build upgrade-executor
echo "Done. Use create-or-update-lambda.sh to publish to AWS Lambda."
