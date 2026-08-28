# Greengrass V1 → V2 remote upgrade

V1 and V2 are different runtimes with incompatible cloud APIs, and V1's own over-the-air update *cannot* install V2. This guide is to give a remediation path in the instance the device is physically unreachable.

> AWS has a **hard deadline to update: October 7, 2026.** The V1 console, APIs, deployments, and even V1 device connectivity will not work after that date ([V1 maintenance policy](https://docs.aws.amazon.com/greengrass/v1/developerguide/maintenance-policy.html)). Security patches for V1 already ended in June 2023.

> **IMPORTANT:** GreenGrass V1 is not fully supported in new AWS accounts. To follow along with this guide, you will need an account that is whitelisted for creating AWS GreenGrass V1 Groups

---

## Options for upgrading V1 → V2

| Approach | When it fits |
|---|---|
| **Push a V2 install script via SSM or SSH** | If you have a shell or SSM agent on the device, this is the simplest and best-supported. |
| **Replace the hardware / re-provision from scratch** | If the devices are physically reachable, or too constrained for V2, recalling and updating the device is preferred. |
| **Remote upgrade via a root Lambda** | If the first two options are not possible, you'll need to do a remote upgrade. However, with **no** shell/SSM, this can be tricky. We'll explore in this guide what is possible remotely and the device your requirements must have |


> **IMPORTANT** Your OS and Hardware will likely be different than exactly what this lab includes a guide for. Run each safety test thoroughly in a controlled environment prior to testing the upgrade on a field device

### Design Points:

- **systemd does the install, not the Lambda.** Stopping V1 kills every Lambda V1 manages, including the one driving the upgrade. The Lambda *schedules* the work with systemd (which survives V1's removal) and reports back.
- **The device keeps its identity.** V2 installs with *manual provisioning*, reusing the V1 thing name, certificate, and key. Only cloud-side policies change. IoT rules, shadows, and jobs keyed on the thing name keep working, and no new credentials touch the device.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Flash and boot the GreenGrass device](#2-flash-and-boot-the-greengrass-device)
3. [Install Greengrass V1](#3-install-greengrass-v1)
4. [Validate the device's AWS connection](#4-validate-the-devices-aws-connection)
5. [Upgrade V1 → V2 remotely](#5-upgrade-v1--v2-remotely)
6. [Validate the upgrade](#6-validate-the-upgrade)
7. [Soak and clean up](#7-soak-and-clean-up)
8. [Teardown](#8-teardown)

---

## Hardware used in this guide

We are working with the following components

| Dimension | Value |
|---|---|
| Hardware | Raspberry Pi 4 Model B (4 GB), `aarch64` |
| OS | Raspberry Pi OS **(Legacy) Lite 64-bit:** Bookworm / Debian 12 |
| Greengrass V1 | 1.11.6 (final V1 release) |
| Greengrass V2 nucleus | 2.18.3 - the SHA-256 is pinned in both readiness-checker and upgrade-executor and must be changed together.|
| Java | OpenJDK 17 (`default-jre-headless`) |
| Region | us-east-1 |

---

## 1. Prerequisites

This guide has two machines: your **workstation** and the **GreenGrass device**. The whole point of this, is that everything reaching the GreenGrass device during the upgrade process does not require a direct connection.

### Field device (remote V1 → V2 upgrade)

| Requirement | Why it's non-negotiable | Fixable remotely? |
|---|---|---|
| **Allow functions to run as root** in `config.json` | Multiple processes require root to run during the setup/upgrade/teardown workflow | **No:** set only by editing `config.json` on the device |
| **Lambda runs as root (UID/GID 0) in NoContainer mode** | In container mode the function runs in an isolated namespace with no direct filesystem access. The upgrade needs arbitrary host paths *and* `systemctl`, which requires the host PID namespace and systemd's socket. Only a non-containerized root process has both. | **No:** the mode itself is set per deployment. |
| **An init supervisor that outlives V1** | The installer runs as a detached systemd unit so it survives V1 (its parent) being stopped mid-upgrade. Without a supervisor outside the V1 process tree, stopping V1 kills the upgrade halfway. | **No:** a property of the OS image. |
| **A V2-supported CPU architecture + OS:** `armv7l` (32-bit), `aarch64`, or `x86_64`; Linux kernel 4.4+ | AWS publishes the nucleus and a compatible JRE only for these targets. | **No:** needs a hardware or OS swap. |
| **A working Greengrass deployment channel:** MQTT and HTTPS egress | The device is notified of a deployment over **MQTT** and reports status back the same way, but the group definition and Lambda code are downloaded over HTTPS | **No:** If it's down, the device is already unreachable. |
| **Greengrass V1 core ≥ 1.7** | Run-as-root and NoContainer execution modes were both [introduced in GGC 1.7](https://docs.aws.amazon.com/greengrass/v1/developerguide/lambda-group-config.html) | **Yes:** V1's own OTA can bump a 1.x core up to 1.11.6. |
| **Disk + RAM headroom, and egress to the V2 endpoints** | The staged installer (~52 MB), the extracted nucleus, and the JVM working set all need room beyond V1's footprint; the device must also reach the installer CDN and the IoT data/credentials endpoints. | **Yes:** a Lambda can free space or stop components |
| **A JVM (Java 8+), installed or installable** | The V2 nucleus is a Java application and exits immediately if no compatible `java` is on `PATH`. | **Yes:** if the device has working package-repo egress. |
| **Python 3.8** | 3.8 is V1's hard ceiling. V1's [supported-runtime table](https://docs.aws.amazon.com/greengrass/v1/developerguide/lambda-functions.html) ends at Python 3.8. | **Yes:** available via function install |


### Workstation:

| Requirement | Verify |
|---|---|
| AWS Account Permissions for AWS IoT Core, AWS IoT Greengrass (V1 and V2), IAM, CloudFormation, and Lambda | verify account permissions are set in the console |
| AWS CLI v2, authenticated to your sandbox | `aws sts get-caller-identity` |
| `jq`, `zip`, `unzip`, `curl`, `python3`, `git`, `pip` | `jq --version` etc. |

> **Prefer an assistant?** You can run an AI agent on the device that already knows Greengrass, to help with setup and debugging. See [`docs/agent-assisted-setup.md`](docs/agent-assisted-setup.md).

---

## 2. Flash and boot the GreenGrass device

1. Open Raspberry Pi Imager. Choose your Pi model, then under *Raspberry Pi OS (other)* pick **Raspberry Pi OS (Legacy) Lite, 64-bit** (Bookworm)
2. Click **Edit settings** on the customization prompt:
   - hostname: gg-v1-demo
   - username/password: pick your own
   - configure Wi-Fi if not using Ethernet
   - Services tab → enable SSH
3. Write the card, boot the Pi, and confirm you can reach it:

```bash
ssh <your-user>@gg-v1-demo.local
```

> **Wait for the clock.** The Pi has no battery-backed clock; its time is wrong until NTP syncs, and TLS to AWS fails with a wrong clock. On the Pi, run `timedatectl` and wait for `System clock synchronized: yes` (usually under a minute) before installing V1.

---

## 3. Install Greengrass V1

**Set your default region, in every terminal you use. This guide will fall back to us-east-1 automatically**

```bash
export AWS_REGION=us-east-1
```

### 3a. Create the V1 cloud identity

```bash
./scripts/provision-v1-device.sh gg-v1-demo-Core gg-v1-demo
```

The script creates:

- The account's **Greengrass V1 service role** association
- IoT **thing** `gg-v1-demo-Core`
- An X.509 **certificate + private key**, saved to `device-bundle/gg-v1-demo-Core/` on your workstation
- A demo-scoped **IoT policy** attached to the cert
- The Greengrass V1 **group** `gg-v1-demo` with the thing as its core,
- The **`GGIPDetector`** system function plus an **initial group deployment**. The deployment is queued and applies automatically when the device first connects in Step 3b
- a prefilled **`config.json`** 



Capture the two values the rest of the lab uses:

```bash
export THING=gg-v1-demo-Core
export GROUP_ID=$(aws greengrass list-groups \
    --query "Groups[?Name=='gg-v1-demo'].Id | [0]" --output text)
echo "GROUP_ID=$GROUP_ID  THING=$THING"
```

### 3b. Install V1 on the device

Copy the identity bundle and the device script over, then run it on the device:

```bash
scp -r device-bundle/gg-v1-demo-Core scripts/setup-v1-device.sh \
    <your-user>@gg-v1-demo.local:/tmp/

# If you used a different device hostname, replace it in the commands
ssh <your-user>@gg-v1-demo.local

# on the GreenGrass device:
sudo /tmp/setup-v1-device.sh /tmp/gg-v1-demo-Core
```

The device setup script ([read it first](scripts/setup-v1-device.sh)) prepares the OS, installs dependencies, installs Python 3.8 for the V1 runtime (side-by-side, without touching the system Python), downloads Greengrass 1.11.6 with a pinned SHA-256, installs your identity files, and starts V1 as the systemd service `greengrass.service`.

The script ends with `greengrass.service: ACTIVE`. See [`docs/appendix-device-setup.md`](docs/appendix-device-setup.md).

Once you see `ACTIVE`, remove the staged files from the device. Their working copies now live under `/greengrass`.

```bash
# on the GreenGrass device:
sudo rm -rf /tmp/gg-v1-demo-Core /tmp/setup-v1-device.sh
exit
```

This is the **last time you touch the device to build it**. From here the lab treats the GreenGrass device as unreachable. Every remaining step drives it through the Greengrass deployment channel via MQTT, and SSH is used only to observe logs or debug.

---

## 4. Validate the device's AWS connection

Confirm the V1 core actually reached your account before going further. Give it a minute or two after `greengrass.service` goes active 

```bash
aws greengrass get-connectivity-info --thing-name "$THING"
```

Seeing the Device's IP address(es) means the core connected, took its first deployment, and reported in. You can also open the AWS IoT console → **Greengrass (classic V1) → Groups → gg-v1-demo** and confirm the core shows as connected. If it still 404s after a couple of minutes, see [the connectivity check returns 404](#the-connectivity-check-returns-404) in the troubleshooting index.

At this point you have a working V1 device linked to your account. From here, we'll upgrade it to V2 via MQTT

---

## 5. Upgrade V1 → V2 remotely

The remote upgrade uses three safety layers which we'll deploy as individual lambdas to run on the test device:

```
 SMOKE TEST   proves the pipeline: a deployment reaches the device, runs as
 (Phase 1)    root, sees the host, and can publish MQTT back

 READINESS    21 read-only checks + downloads/verifies the V2 installer, and
 (Phase 2)    writes a PASS/FAIL report the upgrade later enforces

 UPGRADE      systemd (outside the V1 process tree) stops V1, 
 (Phase 3)    installs V2 reusing the SAME thing name + certificate,
              or restores V1 if the install fails
```

### 5a. Deploy the V2 cloud prerequisites (CloudFormation)

```bash
aws cloudformation deploy \
    --stack-name gg-v1v2-upgrade-prereqs \
    --template-file templates/greengrass-upgrade-prereqs.yaml \
    --capabilities CAPABILITY_NAMED_IAM
```

> The template's `CoreDeviceThingName` parameter defaults to `gg-v1-demo-Core`. If you used a different thing name in Step 3, redeploy with `--parameter-overrides CoreDeviceThingName=<your-name>`.

| Resource | What it does |
|---|---|
| IAM role `GreengrassV2TokenExchangeRole` | How a V2 device calls AWS: it trades its X.509 cert for short-lived STS credentials for this role |
| IoT role alias `GreengrassCoreTokenExchangeRoleAlias` | An AWS IoT resource that points at `GreengrassV2TokenExchangeRole`. When the device trades its certificate for credentials, it names *this alias* rather than the IAM role |
| IoT policy `GreengrassV2IoTThingPolicy` | The V2 data-plane permissions (MQTT topics, deployment APIs) the V1 certificate doesn't have |
| IoT policy `GreengrassTESCertificatePolicy` | One permission: `iot:AssumeRoleWithCertificate` on the role alias |
| Thing group `GreengrassV2_UpgradedFromV1` | Where upgraded devices go; V2 deployments target V2 thing groups |
| IAM role `GGv1UpgradeLambdaExecutionRole` | Permissionless placeholder required for `lambda:CreateFunction` |

Grab the Lambda role ARN for the build step:

```bash
export LAMBDA_ROLE_ARN=$(aws cloudformation describe-stacks \
    --stack-name gg-v1v2-upgrade-prereqs \
    --query "Stacks[0].Outputs[?OutputKey=='LambdaExecutionRoleArn'].OutputValue" \
    --output text)
echo "$LAMBDA_ROLE_ARN"
```

### 5b. Authorize the device certificate for V2

Attach the two V2 policies from the stack to the device's existing certificate.

```bash
./scripts/attach-cert-policies.sh "$THING"
```

This is what lets the device keep its identity across the upgrade: the same certificate gains permission to do the V2-specific things it couldn't before.

### 5c. Build and publish the upgrade Lambdas

```bash
#bundle lambdas into a package with greengrasssdk
./scripts/package-lambdas.sh

#publish each function to the cloud using the latest version
./scripts/create-or-update-lambda.sh smoke-test        "$LAMBDA_ROLE_ARN"
./scripts/create-or-update-lambda.sh readiness-checker "$LAMBDA_ROLE_ARN"
./scripts/create-or-update-lambda.sh upgrade-executor  "$LAMBDA_ROLE_ARN"
```

### 5d. Phase 1: Smoke test

Open the AWS IoT console → **MQTT test client** → subscribe to `greengrass/upgrade/#`, then run the following script:

```bash
./scripts/deploy-phase.sh smoke-test
```

This script publishes one JSON message to greengrass/upgrade/smoketest/<thingName> and if that message shows uid=0 and systemd=True, the device is ready for Phase 2.

Within a minute you should see on the topic `greengrass/upgrade/smoketest/gg-v1-demo-Core`:
```json
{ 
    "thingName": "gg-v1-demo-Core",
    "timestamp": 1785865408,
    "phase": "smoke-test",
    "whoami": "root", 
    "uid": 0, 
    "gid": 0, 
    "systemd_visible": true, 
    "host_fs_visible": true, 
     "python": "3.8.20" 
}
```

> Note: There is only one published message during the smoke test run. If you aren't sure you subscribed to the topic in time, just rerun the test 

This proves the whole chain: 
- Deployments reach the device via HTTPS
- The Lambda runs **as root**
- **NoContainer** mode exposes the real host (systemd, real filesystem)
- module-level code fires on deployment
- MQTT reporting works. 

> If `uid` isn't 0 or nothing arrives, stop. See [the smoke test is silent or not root](#the-smoke-test-is-silent-or-not-root) in the troubleshooting index.

Now that we know the necessary networking pieces are in place, let's validate the device configuration meets the requisite standards

### 5e. Phase 2: readiness check

Deploy the readiness checker:

```bash
./scripts/deploy-phase.sh readiness-checker
```
This phase runs the longest so subscribe to the `greengrass/upgrade/#` topic to see updates as it goes.

On `greengrass/upgrade/readiness/gg-v1-demo-Core` you'll get a JSON report of 21 checks, then it downloads the V2 installer (~52 MB), verifies its pinned SHA-256, and stores it at `/var/lib/gg-v2-upgrade/` on the device. The report should pass, or pass with warnings as you will have some v1 lambdas on the device, but you should get one of the following:

- `PASS`
- `PASS_WITH_WARNINGS`
- `FAIL`

Make sure to read the full report and make note of any warnings or failures, then run the script again once you've made any changes. Phase 3 relies on this report to run.

> **Reports expire after 24 hours.** Phase 3 rejects an older readiness report, if the latest report is more than 24h old, redeploy Phase 2

### 5f. Phase 3: the upgrade

```bash
./scripts/deploy-phase.sh upgrade-executor
```

What happens, in order (visible on `greengrass/upgrade/status/gg-v1-demo-Core`):

1. The executor Lambda re-verifies everything (readiness report PASS, installer hash still matches, config injected), renders the upgrade script, registers the one-shot systemd unit `gg-v1-to-v2-upgrade.service`, starts it detached, and reports `UPGRADING`.
2. The unit backs up V1's systemd unit file, stops V1, writes the V2 config, and runs the V2 installer.
3. **If the installer fails**, the unit restores V1's systemd unit and restarts V1. The device comes back as it was, and the status file says why. A guard prevents the restarted Lambda from re-firing the upgrade.
4. On success it reports `SUCCESS. Greengrass V2 is installed and the service is active.

> **How reporting survives the V1 shutdown.** Once V1 stops there is no local Greengrass broker and no Lambda runtime, so the SDK the executor used is gone. But the device still holds its X.509 certificate, and AWS IoT Core accepts [HTTPS publishes authenticated by that certificate](https://docs.aws.amazon.com/iot/latest/developerguide/http.html) such as `POST https://<data-endpoint>/topics/<topic>`. It works during the V2 install too, because HTTPS publishing has no `clientId` to collide with V2's own MQTT session.

---

## 6. Validate the upgrade

From your workstation:

```bash
aws greengrassv2 get-core-device --core-device-thing-name "$THING" \
    --query '{thing:coreDeviceThingName,status:status,version:coreVersion}'
```

Expect `status: HEALTHY` and `version: 2.18.3` within ~5 minutes (`UNHEALTHY` in the first minute is normal. V2 registered but hasn't reported its first health sample yet). If it doesn't appear, see [V2 doesn't appear after the upgrade](#v2-doesnt-appear-after-the-upgrade) in the troubleshooting index.

Then prove it survives a reboot. Both V1 and V2 use the systemd unit name `greengrass.service`, so a reboot is the cheapest test that the right one owns it:

```bash
# on the device
sudo reboot
# after it's back (wait 2-3 minutes), from your workstation:
aws greengrassv2 get-core-device --core-device-thing-name "$THING"
```
> The greengrass device in AWS may report as healthy even when the device itself has been turned off. Check the timestamp to ensure an up to date healthy check has been run.

Once verified, add the device to the upgraded-devices thing group. This is how V2 deployments will target it (V2 deploys to thing groups, not V1-style Greengrass groups):

```bash
aws iot add-thing-to-thing-group \
    --thing-group-name GreengrassV2_UpgradedFromV1 --thing-name "$THING"
```

---

## 7. Soak and clean up

The V1 binaries are deliberately left at `/greengrass/` as a rollback path. Give V2 24–48 hours (or as long as you're comfortable), then remove the upgrade helper and, when you're sure, the V1 tree:

```bash
# On your device:
sudo /var/lib/gg-v2-upgrade/cleanup.sh              # helper unit only; keeps V1 fallback
sudo /var/lib/gg-v2-upgrade/cleanup.sh --remove-v1  # also deletes V1 (3 safety checks first)
```

> `--remove-v1` deletes the V1 software tree but deliberately preserves `/greengrass/certs`. With manual provisioning those files ARE V2's device identity, referenced at their original paths by the nucleus's config store.

**Need out during the soak?** While the V1 tree still exists, you can return to V1. See [Rolling back to V1](#rolling-back-to-v1) in the troubleshooting index.

---

## 8. Teardown

Use the following command to clean up your local workstation resources that were created throughout this guide

```bash
./scripts/teardown.sh
```

Wipe the GreenGrass device over SSH
```bash
GG_HOST=<your-user>@gg-v1-demo.local ./scripts/teardown.sh 
```

> The V1 service role (`Greengrass_ServiceRole`) and its association are intentionally left: they're account-level, may predate this guide, and other Greengrass use in the account may rely on them. The script prints how to remove it by hand if you're certain.

Verify:

```bash
aws greengrass list-groups --query 'Groups[].Name'
aws greengrassv2 list-core-devices --query 'coreDevices[].coreDeviceThingName'
aws iot list-certificates --query 'certificates[].certificateId'
aws cloudformation describe-stacks --stack-name gg-v1v2-upgrade-prereqs 2>&1 | grep -q 'does not exist' && echo "stack: gone"
```

---

## Troubleshooting index

### The connectivity check returns 404

- `aws greengrass get-connectivity-info` 404s even though `greengrass.service` is active on the GreenGrass device. Check the group's deployment first. Connectivity info is reported by the `GGIPDetector` system function, which only reaches the device via a deployment:

```bash
aws greengrass list-deployments --group-id "$GROUP_ID"
aws greengrass get-deployment-status --group-id "$GROUP_ID" \
    --deployment-id <id-from-list>
```

- **No deployments listed:** Step 3a was run with an older version of the provision script. Re-run it to add the IP detector and deploy.
- **The deployment succeeded but the device never connected:** The usual causes are clock skew (recheck `timedatectl` on the device) or the cert/config not matching. Check `/greengrass/ggc/var/log/system/runtime.log` on the GreenGrass device.

### The smoke test is silent or not root

- **Nothing arrives:** Check the `deploy-phase.sh` output and the deployment status (`aws greengrass get-deployment-status` with the printed deployment ID), then `/greengrass/ggc/var/log/system/runtime.log` on the device.
- **`uid` isn't 0:** The device's `config.json` is missing `allowFunctionsToRunAsRoot: "yes"`. That flag can only be set by editing the file on the device and restarting the daemon

### Phase 2 refuses to run

- **"No readiness report":** Phase 2 never ran on this device. Deploy it ([Step 5e](#5e-phase-2-readiness-check)).
- **Report older than 24 hours:** Disk, clock, and Java facts go stale. Redeploy Phase 2 (read-only; it skips the installer re-download).
- **Report status is `FAIL`:** The report names each failed check; fix and redeploy Phase 2. The gate can't be skipped.
- **Installer hash mismatch:** The staged installer changed or was corrupted since Phase 2. Re-run Phase 2 to re-stage and re-verify it.

### V2 doesn't appear after the upgrade

Triage over SSH:

```bash
cat /var/lib/gg-v2-upgrade/upgrade-status    # SUCCESS / IN_PROGRESS_<step> / FAILED_<reason>
sudo journalctl -u gg-v1-to-v2-upgrade -n 200 --no-pager
sudo tail -n 100 /greengrass/v2/logs/greengrass.log
```

The status file names the step that failed; the journal shows the upgrade unit's own output; the V2 log covers everything after the installer handed off. If the installer failed, the unit restores V1 automatically ([Step 5f](#5f-phase-3-the-upgrade), item 4) and the status file will say why.

### Rolling back to V1

While the V1 tree still exists, you can return the device to V1:

```bash
sudo systemctl stop greengrass
sudo cp /var/lib/gg-v2-upgrade/v1-greengrass.service.bak /etc/systemd/system/greengrass.service
sudo systemctl daemon-reload && sudo systemctl start greengrass
```

The V1 group must still exist in the cloud for the restarted core to be usable.

---

## Optional reading

Background that isn't needed to run the lab but explains the choices behind it:

- [`docs/why-v1-to-v2-is-hard.md`](docs/why-v1-to-v2-is-hard.md): Why there's no built-in path and why the approach looks the way it does.
- [`docs/agent-assisted-setup.md`](docs/agent-assisted-setup.md): Running an on-device AI agent to help with setup and debugging.

---

## Repository layout

```
.
├── README.md                          ← this guide
├── templates/
│   └── greengrass-upgrade-prereqs.yaml← Step 5a: static V2 cloud prerequisites (CFT)
├── scripts/
│   ├── attach-cert-policies.sh        ← Step 5b: authorize the V1 cert for V2
│   ├── create-or-update-lambda.sh     ← Step 5c: publish a Lambda version, print ARN
│   ├── deploy-phase.sh                ← Steps 5d–5f: deploy smoke-test / phase 2 / phase 3
│   ├── package-lambdas.sh             ← Step 5c: build Lambda zips (bundles greengrasssdk)
│   ├── provision-v1-device.sh         ← Step 3a: V1 cloud identity + group + first deployment (workstation)
│   ├── setup-v1-device.sh             ← Step 3b: V1 install (runs on the GreenGrass device)
│   └── teardown.sh                    ← Step 8: one-command cloud + local teardown
├── lambda/
│   ├── smoke-test/handler.py          ← Step 5d: pipeline proof (root? host? MQTT?)
│   ├── readiness-checker/handler.py   ← Step 5e: 21 checks + installer download/verify
│   └── upgrade-executor/handler.py    ← Step 5f: writes systemd one-shot, steps aside
└── docs/
    ├── agent-assisted-setup.md        ← optional on-device AI agent
    ├── appendix-device-setup.md       ← by-hand V1 install & config details
    └── why-v1-to-v2-is-hard.md        ← why the upgrade needs this approach
```

## References

1. [Migrate from AWS IoT Greengrass V1](https://docs.aws.amazon.com/greengrass/v2/developerguide/move-from-v1.html)
2. [Upgrade V1 core devices to V2](https://docs.aws.amazon.com/greengrass/v2/developerguide/upgrade-v1-core-devices.html)
3. [V1 maintenance / end-of-support policy](https://docs.aws.amazon.com/greengrass/v1/developerguide/maintenance-policy.html)
4. [Manual installation of the GG V2 nucleus](https://docs.aws.amazon.com/greengrass/v2/developerguide/manual-installation.html)
5. [Minimal IoT policy for V2 core devices](https://docs.aws.amazon.com/greengrass/v2/developerguide/device-auth.html)
6. [Running Lambda functions as root on GG V1](https://docs.aws.amazon.com/greengrass/v1/developerguide/lambda-group-config.html)
7. [Import V1 Lambdas as V2 components](https://docs.aws.amazon.com/greengrass/v2/developerguide/run-lambda-functions.html)
