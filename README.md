# Hermes Skills Library

[![Hermes Agent](https://img.shields.io/badge/Hermes%20Agent-Skills-7C3AED)](https://github.com/NousResearch/hermes-agent)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A curated collection of **reusable, versioned, and production-oriented skills for Hermes Agent**.

This repository is intended to provide a central place for custom Hermes skills covering infrastructure, Docker, monitoring, automation, diagnostics, notifications, and other useful workflows.

> **Note:** This is a community-maintained collection of skills. Each skill may have its own requirements, configuration, and compatibility constraints.

---

## 📚 Skills

| Skill | Description | Latest |
|---|---|---|
| 🐳 **[docker-health](docker-health/v1.0.0/SKILL.md)** | Docker infrastructure monitoring, diagnostics, reporting and alerting | `v1.0.0` |
| 🔄 **[docker-updater](docker-updater/v1.0.0/SKILL.md)** | Docker image update and container maintenance | `v1.0.0` |

More skills will be added over time.

---

## 🐳 Docker Health

**Docker Health** is a comprehensive monitoring and diagnostics skill for Hermes Agent.

It is designed for environments running multiple Docker hosts and provides a unified way to inspect container health and infrastructure problems without giving access to the Docker socket.
You'll need a Docker proxy.

### Features

- 🔍 Monitor multiple Docker hosts
- ❤️ Check Docker container health
- 🚨 Detect unhealthy containers
- 🛑 Detect exited containers
- 🩺 Detect containers without healthchecks
- 🔄 Detect restarting containers
- 📊 Generate infrastructure summaries
- 🖥️ Generate host reports
- 📦 Generate stack reports
- 🐳 Generate container reports
- 📈 Track trends and changes
- 🕐 Review changes observed between monitoring snapshots
- 🔎 Diagnose problematic containers
- 🧪 Audit healthcheck configuration
- 🔕 Acknowledge and silence problems
- 🔔 Send notifications through **ntfy**
- 💾 Maintain monitoring state and history

### Configuration

Configure the Docker proxy endpoints and monitoring options in [config.yaml](docker-health/v1.0.0/config.yaml). The supplied configuration enables ntfy: set `NTFY_ENDPOINT` and `NTFY_TOKEN`, or set `ntfy.enabled: false` to run without notifications. When ntfy is enabled, the script validates these variables for diagnostics and reports too.

See [SKILL.md](docker-health/v1.0.0/SKILL.md) for commands, persisted state, and read-only Docker permissions.

---

## 🔄 Docker Updater

**Docker Updater** is a Docker image update management skill for Hermes Agent, designed for self-hosted infrastructures.

Docker Updater relies on external services to discover available updates and execute the corresponding maintenance actions.

### Dependencies

Docker Updater requires both:

- **[What's Up Docker (WUD)](https://github.com/getwud/wud)** — used to detect available Docker image updates.
- **[Komodo](https://github.com/moghtech/komodo)** — used to manage and apply Docker updates across the infrastructure.

Both services are required for the skill to operate correctly.

### Workflow

The general workflow is:

```text
Docker containers
       │
       ▼
      WUD
       │
       │ detects available updates
       ▼
Docker Updater
       │
       │ validates / orchestrates update
       ▼
    Komodo
       │
       │ performs update
       ▼
Updated containers
```

### Features

- 🔍 Discover available Docker image updates through WUD
- 📋 Present pending updates to Hermes Agent
- 🎯 Select containers that should be updated
- 🔄 Trigger updates through Komodo
- 🧩 Integrate WUD and Komodo into a single Hermes workflow
- 🛡️ Provide controlled update operations rather than directly modifying containers
- 📝 Keep update-related logic and configuration documented within the skill

### Configuration

Docker Updater reads environment variables and JSON state files; it does not use `config.yaml`. Configure `KOMODO_URL`, `KOMODO_API_KEY`, `KOMODO_API_SECRET`, `WUD_URL`, `WUD_USER`, and `WUD_PASSWORD` as described in its [SKILL.md](docker-updater/v1.0.0/SKILL.md).

### Requirements

Before installing Docker Updater, make sure you have:

1. A working **WUD** installation.
2. A working **Komodo** installation.
3. WUD configured to monitor the Docker containers you want to update.
4. Komodo configured with access to the relevant Docker infrastructure.
5. The required WUD and Komodo credentials/API configuration available to Hermes Agent.

---

# 📦 Repository Structure

```text
hermes-skill/
├── docker-health/
│   └── v1.0.0/
│       ├── SKILL.md
│       ├── CHANGELOG.md
│       ├── VERSION
│       ├── config.yaml
│       └── scripts/
│           └── docker-health.py
├── docker-updater/
│   └── v1.0.0/
│       ├── SKILL.md
│       ├── CHANGELOG.md
│       ├── VERSION
│       └── scripts/
│           └── docker-update.py
├── README.md
└── LICENSE
```

Each skill release should ideally be self-contained and include the files required to install and operate that version.

---

# 🚀 Installation

Clone the repository:

```bash
git clone "<repository-url>" hermes-skill
cd hermes-skill
```

Choose the skill and version you want to install.

For example:

```bash
cp -r docker-health/v1.0.0 /path/to/hermes/skills/docker-health
```

Then follow the instructions provided by the skill's `SKILL.md`.

> Installation paths may differ depending on your Hermes Agent deployment.

---

# ⚙️ Configuration

Skills are designed to keep their configuration isolated from the rest of the repository.

Depending on the skill, configuration may include:

- YAML configuration
- Environment variables
- API endpoints
- Authentication credentials
- Docker hosts
- Notification settings
- Runtime options

### 🔐 Never commit secrets

Do **not** commit:

```text
API keys
Access tokens
Passwords
Private keys
Production credentials
Personal information
```

Use environment variables or a dedicated secret-management solution instead.

---

# 🔖 Versioning

Skills are versioned independently using **Semantic Versioning** whenever possible:

```text
MAJOR.MINOR.PATCH
```

For example:

```text
docker-health/
├── v1.0.0/
├── v1.1.0/
└── v1.2.0/
```



Previous stable releases are intentionally preserved.

This allows users to:

- Pin a known-good version
- Reproduce an existing installation
- Roll back after an upgrade
- Compare changes between releases

---

# 📝 Changelog

Significant changes should be documented in a `CHANGELOG.md`.

Example:

```text
v1.0.0
------
- Initial stable release
- Docker host and container monitoring
- Health diagnostics and reporting
- Multi-host support
- Notification support
```

Each skill should maintain its own changelog rather than using a single global changelog.

Initial release notes:

- [Docker Health v1.0.0](docker-health/v1.0.0/CHANGELOG.md)
- [Docker Updater v1.0.0](docker-updater/v1.0.0/CHANGELOG.md)

---

# 🧪 Development and tests

Use Python 3.12 and a development environment at the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

This environment is for repository development. Installed skills continue to use the Hermes Python interpreter documented in their `SKILL.md`.

The [GitHub Actions test workflow](.github/workflows/tests.yml) runs on every push and pull request, and can also be started manually. It uses Python 3.12 on Ubuntu 24.04, installs the pinned development dependencies, and runs the full test suite with line and branch coverage reported in the job logs. CI requires 100% line and branch coverage for both skill scripts. No infrastructure credentials are required.

The tests cover Docker health classification and collection, snapshot persistence and recovery, alert thresholds, silences, ntfy delivery, WUD/Komodo stack matching, version policies, Compose preparation, rollback, locks, and verified deployment guards. They also parse the YAML metadata and validate every documented CLI example against the scripts' argument parsers.

Tests use temporary state files and fake credentials. External service responses are simulated; unexpected HTTP requests or socket connections fail the test. No Docker, WUD, Komodo or ntfy service is required. This suite does not replace integration testing against a real deployment. Additional scenarios exercise persisted incident histories, report and diagnostic CLI output, HTTP retry limits, interactive update confirmation, deployment polling, and runtime health deadlines.

To measure line and branch coverage:

```bash
.venv/bin/python -m pytest --cov=docker-health/v1.0.0/scripts --cov=docker-updater/v1.0.0/scripts --cov-branch --cov-report=term-missing --cov-fail-under=100
```

---

# 🧪 Compatibility

Skills are developed and tested against specific Hermes Agent and infrastructure environments.

Compatibility may depend on:

- Hermes Agent version
- Python version
- Docker Engine version
- Operating system
- Docker API configuration
- Network architecture
- External services
- Authentication configuration

Always check the skill's `SKILL.md` before installing it in production.

---

# 🤝 Contributing

Contributions are welcome.

If you want to add or improve a skill, please follow these guidelines.

### New skills

A new skill should:

1. Have a clear and descriptive name.
2. Include a `SKILL.md`.
3. Document its requirements.
4. Keep secrets outside the repository.
5. Include installation instructions.
6. Include configuration documentation.
7. Use versioned releases.
8. Include a changelog for significant changes.
9. Be tested before submission.

### SKILL.md conventions

Keep each skill's instruction language consistent and organize its sections in the following order (described here in English):

1. Purpose and scope
2. Prerequisites
3. Execution
4. Configuration
5. Workflow
6. Commands
7. State and files
8. Output
9. Security
10. Limitations

Use command-specific subsections as needed. Preserve the Hermes frontmatter fields in this order: `name`, `description`, `version`, `author`, `license`, `platforms`, `metadata`. Keep `version` consistent with the release directory and `VERSION` file, and make each description explain when to select the skill.

Use complete commands with the absolute path to the Hermes Python interpreter, document configuration and side effects from the actual script, and distinguish configuration preparation from deployment. The example installation uses `/opt/hermes/.venv/bin/python3` and `/opt/data/.hermes/skills/<skill-name>`; adapt those absolute paths to the deployment.

### Pull requests

Please keep pull requests focused.

Examples:

```text
feat: add new skill
feat(docker-health): add stack diagnostics
fix(docker-health): handle unreachable host
docs: improve installation instructions
refactor(docker-updater): simplify update workflow
```

---

# 🛡️ Security

If you discover a security vulnerability, please avoid publishing sensitive information in a public issue.

Instead, report the issue privately to the repository maintainer.

Never include credentials, tokens, private keys, or production configuration in issues or pull requests.

---

# 📄 License

This project is licensed under the **MIT License** unless a specific skill states otherwise.

See [`LICENSE`](LICENSE) for details.

---

# 🙏 Credits

This project is built for the **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** ecosystem.

The goal is to provide a growing collection of practical, reusable, and well-documented skills that extend Hermes Agent beyond its default capabilities.

---

## ⭐ Support the Project

If you find these skills useful:

- ⭐ Star the repository
- 🐛 Report bugs
- 💡 Suggest improvements
- 🔧 Submit pull requests
- 📦 Share useful skills with the community

---

**Hermes Skills Library**

*Reusable skills for a more capable Hermes Agent.*
