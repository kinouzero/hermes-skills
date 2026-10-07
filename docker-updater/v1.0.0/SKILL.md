---
name: docker-updater
description: Prepare, verify, and deploy Docker updates detected by WUD through Komodo. Use to plan updates, prepare a rollback, or explicitly deploy verified stacks.
version: 1.0.0
author: Kinou
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [docker, wud, komodo, updates]
    category: infrastructure
---

# Docker Updater

## Purpose and scope

Prepare updates detected by WUD in Komodo Compose files, verify those files, then deploy the explicitly requested stacks. Preparation and deployment are separate operations, without Docker socket access.

## Prerequisites

- Linux and the Hermes Python environment; the script uses the standard library, including `fcntl` for locking.
- WUD configured to monitor the relevant containers.
- Komodo configured to manage the corresponding stacks and accessible from Hermes.
- The required credentials in the process environment and writable state paths.

## Execution

Use the Python interpreter from the Hermes virtual environment directly:

```bash
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-updater/scripts/docker-update.py --help
```

The examples assume installation in `/opt/data/.hermes/skills/docker-updater`. Adjust the absolute paths to match the installation if necessary. Do not use `uv`, a virtual environment local to the skill, or `python3` without an explicit path.

## Configuration

This skill uses environment variables and JSON files; it does not read a `config.yaml` file.

| Variable | Usage |
|---|---|
| `KOMODO_URL` | Komodo URL; default: `http://komodo:9120` |
| `KOMODO_API_KEY`, `KOMODO_API_SECRET` | Komodo credentials required by all operational commands |
| `WUD_URL` | WUD URL; default: `http://wud:3000` |
| `WUD_USER`, `WUD_PASSWORD` | WUD credentials |

`wud-updates`, `plan`, `status`, `update`, `verify`, `interactive`, `auto`, and `preflight` contact WUD. The initial WUD credential check explicitly covers only `wud-updates`, `plan`, `update`, and `verify`; this difference does not make the other commands independent of WUD.

Run the script directly to validate its prerequisites. Never request or display secret values, or inspect them with `execute_code` or `os.environ` in the agent.

## Workflow

1. Use `preflight`, `status`, or `plan` to check dependencies and review updates in read-only mode.
2. Prepare the requested scope with `update --confirm`: only the stacks' `config.file_contents` configurations are changed on the Komodo side. A digest update prepares a redeployment without modifying the Compose file.
3. Run `verify` with the same filters and version options. This command rereads `GetStack` and checks `config.file_contents`, never `info.deployed_contents`, then stores a SHA-256 fingerprint for each verified stack.
4. Run `deploy --confirm` only when deployment has been explicitly requested. The existence of an update or verified state does not constitute a deployment request.
5. Use `--follow` to track the operation and, when requested, `--verify-runtime` to check the image and state reported by Komodo.

WUD / Komodo resolution prioritizes an exact match, then the name prefixed with the WUD server, and checks that the service exists in the stack. An inferred match requires an explicit, matching host. A missing or ambiguous match must block the affected target.

## Commands

### Planning, preparation, and deployment

```bash
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-updater/scripts/docker-update.py preflight
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-updater/scripts/docker-update.py status --human
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-updater/scripts/docker-update.py plan --stack host-1-media
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-updater/scripts/docker-update.py update --stack host-1-media --dry-run --human
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-updater/scripts/docker-update.py update --stack host-1-media --confirm
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-updater/scripts/docker-update.py verify --stack host-1-media

# Only after an explicit deployment request
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-updater/scripts/docker-update.py deploy --stack host-1-media --confirm --follow
```

Replace the example names with the actual targets. Planning and preparation commands accept `--stack`, `--service`, `--stacks`, and `--services`; lists are comma-separated. `deploy` accepts only stack filters and deploys all services in those stacks.

### Version policy

- PATCH/MINOR: included in preparation.
- MAJOR: skipped unless `--update-major` is used.
- DIGEST: informational by default; use `--include-digest` to prepare a targeted redeployment.
- ERROR/CONFLICT: block `update` unless `--skip-errors` is used. This option skips problematic items; it does not make them deployable.
- Versions listed in `update-blocklist.json` are skipped. References starting with `ghcr.io/...` are normalized for comparison without changing the registry in the Compose file.

WUD problems and informational digests do not block the separate `deploy` command, which relies on verified stack state.

### Digests

A digest update indicates that a mutable tag points to a new image. The `image:` reference in the Compose file remains unchanged.

```bash
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-updater/scripts/docker-update.py plan --stack host-1-media --include-digest
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-updater/scripts/docker-update.py update --stack host-1-media --include-digest --confirm
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-updater/scripts/docker-update.py verify --stack host-1-media --include-digest

# Only after an explicit deployment request
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-updater/scripts/docker-update.py deploy --stack host-1-media --confirm --follow
```

The script checks that the image and tag match, rejects images pinned with `@sha256:...`, and records `compose_modified: false` in history. During deployment, it calls `PullStack` only for services marked `digest_redeploy`, waits for the pull to finish, then calls `DeployStack` for their stack. If the pull fails or cannot be tracked, the stack is not deployed.

### History and rollback

```bash
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-updater/scripts/docker-update.py history --stack host-1-games --service romm --limit 20
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-updater/scripts/docker-update.py rollback-info
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-updater/scripts/docker-update.py rollback --stack host-1-games --service romm --confirm
```

Rollback modifies the Compose file without deploying. Then check the configuration with `verify`; deploy only if explicitly requested and verification permits the stack.

### Automatic or interactive preparation

```bash
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-updater/scripts/docker-update.py interactive
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-updater/scripts/docker-update.py auto --stacks host-1-games,host-1-media --lock-timeout 30
```

`interactive` offers READY items and modifies Compose files after selection and confirmation in the terminal. It does not deploy or automatically run `verify`.

`auto` modifies Compose files and then verifies them, without a `--confirm` option. Use it only for a request for automatic preparation: PATCH/MINOR only, MAJOR excluded, errors and digests skipped, blocklist respected, `deployment: NOT_RUN`. It never deploys.

### Deployment tracking

```bash
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-updater/scripts/docker-update.py deploy --stacks host-1-games,host-1-media --confirm --follow --interval 2 --timeout 3600
/opt/hermes/.venv/bin/python3 /opt/data/.hermes/skills/docker-updater/scripts/docker-update.py deploy --stack host-1-media --confirm --follow --verify-runtime --health-timeout 120 --health-interval 5
```

Without a filter, `deploy` targets all stacks present in `.verify-state.json`. Use a filter when the request covers only specific stacks. Verifications from multiple preparation runs are retained to allow deployment together.

## State and files

| Default file | Purpose | Path configuration |
|---|---|---|
| `/opt/data/.hermes/skills/docker-updater/update-blocklist.json` | Versions to skip | `DOCKER_UPDATER_BLOCKLIST` |
| `/opt/data/.hermes/skills/docker-updater/update-history.json` | Preparation and rollback history | `DOCKER_UPDATER_HISTORY` |
| `/opt/data/.hermes/skills/docker-updater/.lock` | Concurrency lock | `DOCKER_UPDATER_LOCK` |
| `/opt/data/.hermes/skills/docker-updater/.verify-state.json` | Fingerprints and verified actions per stack | The `/opt/data` prefix is replaced by `HERMES_WRITE_SAFE_ROOT` |

Blocklist format:

```json
{"blocked": ["rommapp/romm:5.4.0", "immich:1.143.2"]}
```

`update`, `rollback`, and `deploy` use a `flock` lock. `--lock-timeout N` waits up to N seconds; `auto` also passes this option to preparation.

## Output

JSON is the default output for Hermes, except for interactions and deployment tracking. Use `status --human` or `update --dry-run --human` for readable output. The global `--verbose` and `--debug` options precede the subcommand and write to stderr; do not expose secrets.

Present available updates, prepared configurations, verifications, and deployments actually performed separately. Successful preparation does not mean the containers have been updated.

## Security

- Never use `/var/run/docker.sock`, `docker pull`, `docker compose pull`, `docker compose up/down`, or `docker update` for an update managed by this skill.
- Deploy through `DeployStack`, never through `RunProcedure` or a global procedure.
- Deploy only verified stacks: missing state or a Compose file changed since `verify` blocks deployment. The fingerprint is checked immediately before the operation.
- Never bypass a lock, an ambiguous match, or a failed verification. Correct the cause, then rerun the necessary preparation or verification.
- An explicit deployment request remains necessary, including after `auto`, `interactive`, a rollback, or digest preparation.

## Limitations

- Detection depends on WUD and the mapping between its containers and Komodo stacks.
- `verify` validates the prepared configuration; it does not check running container state.
- `--verify-runtime` checks the expected image reference and a Running/Healthy state through Komodo. It retries until the requested timeout but stops at the first successful check: it does not guarantee a continuous stability window or a match of the actual image digest.
