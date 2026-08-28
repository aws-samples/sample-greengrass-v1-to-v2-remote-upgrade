# Why upgrading Greengrass V1 → V2 is hard

## 1. V2 is a rewrite, not a new version

AWS IoT Greengrass V2 is not "V1 with a bigger version number." It is a different runtime with a different cloud API, a different on-device layout, and a different deployment and identity model:

| | Greengrass V1 | Greengrass V2 |
|---|---|---|
| Unit of work | Lambda functions in a **group** | **Components** deployed to **thing groups** |
| Cloud API | `greengrass:*` (groups, definitions, versions) | `greengrassv2:*` (components, deployments, core devices) |
| On-device root | `/greengrass/` | `/greengrass/v2/` |
| Config | `config.json` | `config.yaml` (backed by a transaction log) |
| AWS credentials | group role via `greengrass:AssumeRoleForGroup` | X.509 → token exchange (IoT credentials provider) |

Because the cloud services are not backward compatible, there is no "in-place" version bump. The device software has to be replaced.

## 2. The problem

From [Migrate from AWS IoT Greengrass V1](https://docs.aws.amazon.com/greengrass/v2/developerguide/move-from-v1.html):

> *"AWS IoT Greengrass V1 over-the-air (OTA) updates can't upgrade core devices from V1 to V2."*

V1's own OTA mechanism can patch a V1 core to a newer **1.x** release, but it has no ability to install V2.

## 3. How to upgrade

| Approach | When it fits |
|---|---|
| **Push a V2 install script via SSM or SSH** | If you have a shell or SSM agent on the device, this is the simplest and best-supported. |
| **Replace the hardware / re-provision from scratch** | If the devices are physically reachable, or too constrained for V2, recalling and updating the device is preferred. |
| **Remote upgrade via a root Lambda** | If the first two options are not possible, you'll need to do a remote upgrade. However, with **no** shell/SSM, this can be tricky. We'll explore in the [main guide](../README.md) what is possible remotely and the device your requirements must have |

A **Lambda function** deployed to the Greengrass group must be configured to run as root in no-container mode. That combination has the same power as a root shell. It exists only if the device was provisioned with `allowFunctionsToRunAsRoot: "yes"`

The runbook uses that Lambda primitive to drive the upgrade but this immediately creates three sub-problems.

### 3a. The upgrade can't run *inside* the Lambda

Installing V2 requires stopping V1. But stopping V1 kills every process V1 manages, including the Lambda doing the work. A script running directly under the Lambda would die mid-install. This would leave the device with V1 stopped and V2 absent.

**The fix:** the Lambda doesn't perform the upgrade. It writes a one-shot **systemd** unit and starts it. systemd lives outside Greengrass's process tree, so it survives V1's death and carries the install to completion. We use the Lambda to *schedule* and *report*.

### 3b. Losing the device's identity would break everything downstream

If the upgrade created a brand-new V2 thing and certificate, every cloud-side reference keyed on the old identity such as IoT rules, device shadows, jobs, fleet-index queries, would silently stop matching.

**The fix:** *manual provisioning* (`--provision false`). V2 reuses the V1 thing name, certificate, and private key. The only change is cloud-side: two extra IoT policies are attached to the existing certificate so it's allowed to do the V2-specific things V1 never needed (token exchange, V2 data-plane APIs). No new credentials ever touch the device.

### 3c. It's a one-way door

Because you can't count on reaching the device afterward, the upgrade has to carry its own safety net: verify readiness *before* touching anything, back up V1 so a failed install can roll back, and leave breadcrumbs (status files, logs) that survive reboots. The runbook splits this into a **Phase 1** (smoke test), read-only **Phase 2** (readiness), and a destructive **Phase 3** (upgrade) with a human decision between them.

## 4. What the upgrade does NOT fix: your workload

Moving the runtime does not move your application. V1 Lambda functions do not run on V2. These functions must be re-authored or imported as V2 components, which use a different SDK and packaging model ([import V1 Lambdas as V2 components](https://docs.aws.amazon.com/greengrass/v2/developerguide/run-lambda-functions.html)).

Planning the component migration is a separate project, this guide is about the challenges of upgrading from V1 to V2 and application migration is out of scope.

## 5. Some pre-requisites

`allowFunctionsToRunAsRoot: "yes"` must already be set in the device's V1 `config.json`.

- It makes the root lambda primitive and allows us to schedule system changes
- This was introduced in GGC 1.7 (2018).

If a fleet was never provisioned with it, there is **no remote upgrade path** for those devices. Any devices without it require physical access or an out-of-band management agent.

## 6. The deadline

AWS ends support for Greengrass V1 on **October 7, 2026** ([V1 maintenance policy](https://docs.aws.amazon.com/greengrass/v1/developerguide/maintenance-policy.html)). After that the V1 console, APIs, and device connectivity stop working. Security patches for V1 already ended in June 2023.

