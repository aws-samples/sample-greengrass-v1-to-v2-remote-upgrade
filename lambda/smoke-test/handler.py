# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Greengrass V1 -> V2 Upgrade: Phase 1 - Smoke Test

Deployed to the V1 group before either real phase. Proves, end to end, every
mechanism the upgrade depends on:

  - the deployment pipeline reaches the device,
  - the Lambda runs as root (allowFunctionsToRunAsRoot + RunAs 0/0),
  - NoContainer mode gives it the real host (systemctl is visible),
  - module-level code fires on deployment (the trigger the phases use),
  - MQTT publishing to the cloud works (the reporting channel).

Publishes one JSON message to greengrass/upgrade/smoketest/<thingName>.
If that message shows uid=0 and systemd=True, the device is ready for Phase 2.
"""

import json
import os
import platform
import pwd
import shutil
import time

import greengrasssdk

iot_client = greengrasssdk.client("iot-data")


def get_thing_name():
    return os.environ.get("AWS_IOT_THING_NAME", "unknown")


def run_smoke_test():
    thing_name = get_thing_name()
    try:
        whoami = pwd.getpwuid(os.getuid()).pw_name
    except Exception as e:
        whoami = f"error: {e}"

    payload = {
        "thingName": thing_name,
        "timestamp": int(time.time()),
        "phase": "smoke-test",
        "whoami": whoami,
        "uid": os.getuid(),
        "gid": os.getgid(),
        "systemd_visible": shutil.which("systemctl") is not None
        and os.path.isdir("/run/systemd/system"),
        "host_fs_visible": os.path.isfile("/greengrass/config/config.json"),
        "python": platform.python_version(),
    }
    iot_client.publish(
        topic=f"greengrass/upgrade/smoketest/{thing_name}",
        payload=json.dumps(payload),
    )
    return payload


def function_handler(event, context):
    """No-op shim; work happens at module import, same as the real phases."""
    return run_smoke_test()


try:
    run_smoke_test()
except Exception:  # nosec B110
    # Never crash-loop the pinned container; the absence of the MQTT message
    # is itself the failure signal for this step.
    pass
