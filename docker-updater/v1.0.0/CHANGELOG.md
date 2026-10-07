# Changelog

## v1.0.0

Initial release of Docker Updater for Hermes Agent.

### Added

- Discover Docker image updates through WUD and manage Compose configurations through Komodo without Docker socket access.
- Check service availability and review pending updates with `preflight`, `status`, and `plan`.
- Resolve WUD containers to Komodo stacks, with host checks and rejection of ambiguous matches.
- Filter updates by stack and service, preview changes with dry-run, and require confirmation before applying updates.
- Prepare PATCH/MINOR updates, optionally include MAJOR updates, and respect a version blocklist.
- Prepare digest redeployments without changing Compose image references, then pull the affected services before deploying their stack.
- Keep preparation, verification, and explicit deployment separate, with SHA-256 checks that block deployment if the Compose configuration changes after verification.
- Deploy selected verified stacks directly through `DeployStack`, with optional operation tracking and runtime image/state checks.
- Retain failed or pending stacks in verification state while clearing deployments confirmed as successful.
- Record update history and prepare rollbacks without deploying automatically.
- Support interactive preparation and automatic PATCH/MINOR preparation followed by verification, without automatic deployment.
- Protect update, rollback, and deployment operations with concurrency locks.
- Configure credentials through environment variables and persist history, blocklists, and verification state in JSON files.
- Provide JSON output for Hermes and human-readable status and dry-run summaries.

See [SKILL.md](SKILL.md) for requirements, commands, configuration, and limitations.
