# Security policy

If you discover a potential security issue in this project, **please do not open a public issue or pull request**.

Report it via the AWS Vulnerability Reporting page:

<https://aws.amazon.com/security/vulnerability-reporting/>

Or email `aws-security@amazon.com` directly. Please include:

- A description of the issue and how to reproduce it.
- The affected file(s) / commit(s) / version(s).
- Any proof-of-concept code or sample payload (clearly labelled).

We'll acknowledge receipt within a few business days.

## Scope

This sample is **for experimentation and reference**, not a production-ready tool. The standard AWS security responsibility model applies: customers running this in their own accounts are responsible for the security of:

- IAM roles and IoT policies created (the defaults are minimum-viable, not least-privilege)
- The on-device V1 certificate and private key (this sample reuses them as-is).
- The Lambda functions deployed to V1 groups. They run **as root in no-container mode** by design

In-scope for vulnerability reports:

- Code-execution or privilege-escalation paths in the rendered upgrade shell script that aren't already discussed
- Identity-leakage paths (e.g., V1 cert/key materially exposed beyond what's documented).
- Any way to bypass the Phase 1 readiness gate or the SHA256 verification on the V2 installer.
- Default IoT/IAM policies that grant strictly more permissions than V2 nucleus actually needs.

Out of scope (please don't report these):

- The fact that the sample runs Lambdas as root — that's required for the use case.
- The fact that `Resource: "*"` is used in some default IAM/IoT policies — these are documented as needing scope-down before production rollout.
- Issues in the cloned `greengrass-agent-context-pack/` directory (it's a separate upstream project).

## Disclosure

We follow coordinated disclosure: we'll work with you on a fix and a public-disclosure timeline. Please give us a reasonable window before posting publicly.
