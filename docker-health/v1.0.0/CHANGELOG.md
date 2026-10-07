# Changelog

## v1.0.0

Initial release of Docker Health for Hermes Agent.

### Added

- Monitor multiple Docker hosts through read-only HTTP(S) proxies without Docker socket access.
- Classify container health and lifecycle states, report collection failures, and audit missing healthchecks.
- Run one-time checks or periodic monitoring with persistent snapshots, observed changes, and recovery history.
- Generate infrastructure, host, stack, and container reports, plus historical trends.
- Diagnose problems using persisted healthcheck details and bounded log excerpts for affected containers.
- Detect persistent problems using configurable alert thresholds and severity rules.
- Acknowledge alerts and temporarily silence notifications.
- Send optional ntfy notifications with batching, cooldown handling, and delivery history.
- Configure hosts and monitoring options through YAML, with notification credentials supplied through environment variables.
- Provide JSON output for Hermes and optional human-readable output.

See [SKILL.md](SKILL.md) for requirements, commands, configuration, and limitations.
