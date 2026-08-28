# Appendix — GreenGrass Device  / Greengrass V1 background notes


## What `setup-v1-device.sh` writes, explained

1. **config.json**:
  - `coreThing.thingArn` / `iotHost` / `ggHost` — the device's identity and your account's ATS data endpoints
  - `crypto.principals.IoTCertificate` — file paths to the cert/key that **V2 will reuse**. The readiness check verifies they're readable; the upgrade writes the same paths into the V2 config.
  - `runtime.allowFunctionsToRunAsRoot: "yes"

2. **greengrass.service**:
  - V1 as a `Type=forking` unit. The name must be exactly `greengrass.service`

---

## Manual install

```bash
# 1. sysctl + users
printf 'fs.protected_hardlinks = 1\nfs.protected_symlinks = 1\n' \
    > /etc/sysctl.d/98-greengrass.conf && sysctl --system
adduser --system --no-create-home ggc_user
addgroup --system ggc_group

# 2. Dependencies
apt-get update && apt-get install -y --no-install-recommends \
    python3 unzip ca-certificates curl default-jre-headless

# 2b. Python 3.8 for the GG V1 runtime, installed side-by-side without
# touching the system python3 (3.11)
curl -fSLO https://github.com/astral-sh/python-build-standalone/releases/download/20241002/cpython-3.8.20+20241002-aarch64-unknown-linux-gnu-install_only.tar.gz
sha256sum cpython-3.8.20+20241002-aarch64-unknown-linux-gnu-install_only.tar.gz
# expect: 9d8798f9e79e0fc0f36fcb95bfa28a1023407d51a8ea5944b4da711f1f75f1ed
tar -xzf cpython-3.8.20+20241002-aarch64-unknown-linux-gnu-install_only.tar.gz -C /tmp
rm -rf /opt/greengrass-python3.8 && mv /tmp/python /opt/greengrass-python3.8
ln -sf /opt/greengrass-python3.8/bin/python3.8 /usr/local/bin/python3.8

# 3. GG V1 1.11.6
curl -fSLO https://d1onfpft10uf5o.cloudfront.net/greengrass-core/downloads/1.11.6/greengrass-linux-aarch64-1.11.6.tar.gz
sha256sum greengrass-linux-aarch64-1.11.6.tar.gz
# expect: 92dc496efd787fd70701059271986f596086e6d569a539527b88e6d7d1452d0f
tar -xzf greengrass-linux-aarch64-1.11.6.tar.gz -C /

# 4. identity files from the provisioning bundle
mkdir -p /greengrass/certs /greengrass/config
install -m 644 cert.pem root.ca.pem /greengrass/certs/
install -m 600 private.key          /greengrass/certs/
install -m 600 config.json          /greengrass/config/

# 5. run under systemd 
systemctl daemon-reload && systemctl enable --now greengrass
tail -f /greengrass/ggc/var/log/system/runtime.log
```
