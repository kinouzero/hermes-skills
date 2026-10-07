---
name: docker-health
description: Monitor and diagnose Docker containers across multiple hosts through a read-only API. Use for health checks, reports, and ntfy alerts.
version: 1.0.0
author: Kinou
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [docker, health, monitoring, infrastructure, containers, multi-host, ntfy, diagnostics]
    category: infrastructure
---

# Docker Health

## Purpose and scope

Monitor remote Docker containers, persist their state, and explain observed problems. Docker access is read-only; the skill writes local state files and can send ntfy notifications.

## Prerequisites

- Linux and the Hermes Python environment, with `PyYAML`.
- A Docker HTTP(S) proxy for each host, accessible without a Docker socket and allowing the reads described in the Security section.
- The [config.yaml](config.yaml) file with the names and URLs of the hosts to monitor.
- A writable data directory for snapshots and history.

## Execution

Use the Python interpreter from the Hermes virtual environment directly:

```bash
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-health/scripts/docker-health.py --help
```

The examples assume installation in `/opt/data/.hermes/skills/docker-health`. Adjust the absolute paths to match the installation if necessary. Do not use `uv`, a virtual environment local to the skill, or `python3` without an explicit path.

## Configuration

The script reads `config.yaml` from the skill root. Replace the example hosts and keep `mode: remote`. Options include the network timeout, parallelism, whether to include stopped containers, the alert threshold, and the monitoring interval.

Configure notifications in the same file:

```yaml
ntfy:
  enabled: true
  topic: docker-health
  timeout_seconds: 10
  cooldown_seconds: 900
```

When `ntfy.enabled` is `true`, the script requires `NTFY_ENDPOINT` (an HTTP(S) URL), `NTFY_TOKEN`, and a nonempty topic as soon as it loads the configuration, including for diagnostics and reports. To run without ntfy, set `ntfy.enabled: false`. The supplied configuration enables ntfy by default.

Secrets are read from the process environment; do not display their values.

## Workflow

1. For a one-time check, use `check`, `summary`, `problems`, or `healthcheck-audit`: these commands query Docker.
2. To persist state and track changes, use `monitor`. `--once` runs a single cycle; otherwise, collection runs periodically. This mode can send notifications when ntfy is enabled.
3. For reports and historical diagnostics, use an existing snapshot. These commands do not automatically refresh Docker state; mention the snapshot's age if it limits the analysis.
4. For a specific problem, use `diagnose` with a host, stack, or container. Distinguish observations, correlations, and possible causes to investigate: temporal correlation does not prove a root cause.

## Commands

### Checks and monitoring

```bash
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-health/scripts/docker-health.py check --human
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-health/scripts/docker-health.py problems --host host-1 --human
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-health/scripts/docker-health.py healthcheck-audit --human
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-health/scripts/docker-health.py monitor --once
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-health/scripts/docker-health.py monitor --interval 300
```

Monitoring collects data from all configured hosts. The `summary`, `host-summary`, `stack-summary`, `stack-problems`, `no-healthcheck`, and `agent` commands also query the current state.

### Reports and diagnostics

```bash
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-health/scripts/docker-health.py report --human
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-health/scripts/docker-health.py host-report --host host-1 --human
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-health/scripts/docker-health.py stack-report --stack monitoring --human
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-health/scripts/docker-health.py container-report --host host-1 --container nginx --human
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-health/scripts/docker-health.py trends --since 7d --human
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-health/scripts/docker-health.py diagnose --host host-1 --container nginx --human
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-health/scripts/docker-health.py diagnose --host host-1 --stack monitoring --human
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-health/scripts/docker-health.py diagnose --host host-1 --human
```

Replace `host-1`, `monitoring`, and `nginx` with the actual targets. Specify the host for a container report to avoid duplicate names; `diagnose --container` requires `--host`.

`container-report` and `diagnose` read healthcheck data from the snapshot and fetch a log excerpt for the targeted containers with problems. The other reports above read persisted state without querying Docker.

### Acknowledgements and silences

```bash
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-health/scripts/docker-health.py ack --host host-1 --container nginx --reason "Incident being handled"
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-health/scripts/docker-health.py silence --host host-1 --container nginx --duration 2h --reason "Maintenance"
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-health/scripts/docker-health.py notification-history --limit 20 --human
```

Use `acknowledged` and `silences` to inspect these states, and `unack` and `unsilence` with the same target to remove them. They affect local alerts without modifying Docker.

## State and files

| Default file | Purpose |
|---|---|
| `/opt/data/.hermes/data/docker-health.json` | Container snapshot and healthcheck details |
| `/opt/data/.hermes/data/docker-health.events.json` | Changes observed between collections |
| `/opt/data/.hermes/data/docker-health.alerts.json` | Alert state |
| `/opt/data/.hermes/data/docker-health.acks.json` | Acknowledgements |
| `/opt/data/.hermes/data/docker-health.silences.json` | Temporary silences |
| `/opt/data/.hermes/data/docker-health.ntfy.json` | ntfy cooldown state |
| `/opt/data/.hermes/data/docker-health.ntfy-history.json` | History of sent notifications |

The `snapshot`, `changes`, `persistent-problems`, `events`, `alerts`, and `stats` commands accept explicit paths; see their `--help` for required arguments. Event history comes from snapshots, not a subscription to the Docker event stream.

## Output

JSON is the default output for Hermes. Add `--human` for readable output. Present the affected hosts and containers, observed states, and collection errors; do not interpret an unreachable host as healthy.

## Security

- Use only Docker `GET` requests, with no Docker socket access, restarts, stops, or deployments.
- Make one `/containers/json` request per host per collection, using `all=true` or `all=false` according to `include_stopped`.
- For each `unhealthy` container, perform at most one `/containers/{id}/json` read per collection. Keep the status, `FailingStreak`, healthcheck definition, and its five latest executions; truncate each output to 2,000 characters.
- Reserve `/containers/{id}/logs` for `diagnose` and `container-report`, for their targeted containers with problems. The request asks for the last 200 lines, and the read is bounded to approximately 50 KB.
- Do not request Docker administration permissions for this skill.

## Limitations

- A failure that starts and ends between two collections may never be observed.
- A healthcheck describes the test failure reported by Docker; it does not prove the root cause of an application problem.
- General logs are not collected during monitoring or standard checks.
- Reports and trends depend on the availability, freshness, and retention period of local data.
