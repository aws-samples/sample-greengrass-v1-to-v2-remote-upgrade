# Contributing

Thanks for your interest in contributing. The most useful contributions to this sample are:

1. **Validated platform variants.** If you successfully ran the upgrade on a different OS / hardware combination, open a PR adding it to the tested-scope table with a note on what you had to adjust. We'd rather grow the matrix slowly with real validation than add untested promises.
2. **Reproducer for a failure.** If Phase 2 failed on your fleet in a way the readiness check didn't catch, open an issue with the readiness payload, the upgrade log (`/tmp/gg-v2-upgrade/upgrade.log`), and the systemd journal (`journalctl -u gg-v1-to-v2-upgrade`).
3. **Hardening:** scoped IAM/IoT policies, proxy support, init-system fallbacks.

## Reporting bugs / security issues

For security issues, do **not** open a public issue. See [`SECURITY.md`](SECURITY.md) (or follow the AWS Vulnerability Reporting policy at <https://aws.amazon.com/security/vulnerability-reporting/>).

## Pull request process

1. Fork the repo and create a feature branch.
2. Run the validation commands from `README.md` (`python3 -m py_compile`, `shellcheck scripts/*.sh`, `bash -n` on rendered scripts) before pushing.
3. If you change `lambda/upgrade-executor/handler.py:render_upgrade_script`, verify both `manual` and `auto` modes still render and shellcheck cleanly.
5. Open the PR with a clear description of *what failure mode this addresses* or *what new platform this supports*.

## Code of conduct

This project follows the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct).

## License

By contributing, you agree your contributions will be licensed under the MIT-0 License (see `LICENSE`).
