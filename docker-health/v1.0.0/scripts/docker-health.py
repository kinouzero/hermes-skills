#!/usr/bin/env python3

import argparse
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

VERSION = "1.0.0"
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
CONFIG_PATH = SKILL_DIR / "config.yaml"

try:
    import yaml
except ImportError:
    yaml = None

PROBLEM_STATES = {"unhealthy", "exited_error", "restarting", "dead", "paused", "unknown"}
HEALTH_RE = re.compile(r"\((healthy|unhealthy|starting)\)\s*$", re.IGNORECASE)
EXIT_CODE_RE = re.compile(r"\bExited\s*\((-?\d+)\)", re.IGNORECASE)
RESTART_STATUS_RE = re.compile(r"^Restarting\s*\((-?\d+)\)(?:\s+(.*?))?\s*$", re.IGNORECASE)
STATUS_AGE_RE = re.compile(r"(\d+(?:\.\d+)?\s+(?:second|seconds|minute|minutes|hour|hours|day|days|week|weeks|month|months|year|years)\s+ago)\s*$", re.IGNORECASE)


def fail(message, code=1):
    print(json.dumps({"error": message}, ensure_ascii=False, indent=2))
    raise SystemExit(code)


def load_config():
    if not CONFIG_PATH.exists():
        fail(f"Configuration introuvable: {CONFIG_PATH}")
    if yaml is None:
        fail("PyYAML est requis pour lire config.yaml")

    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    except Exception as exc:
        fail(f"Impossible de lire la configuration: {exc}")

    validate_config(config)
    return config


def validate_config(config):
    if not isinstance(config, dict):
        fail("Configuration invalide: le document YAML doit être un objet")

    hosts = config.get("hosts")
    if not isinstance(hosts, list) or not hosts:
        fail("Configuration invalide: aucun hôte configuré")

    seen = set()
    for host in hosts:
        if not isinstance(host, dict):
            fail("Configuration invalide: chaque hôte doit être un objet")
        name = host.get("name")
        if not name or not isinstance(name, str):
            fail("Configuration invalide: nom d'hôte manquant")
        if name in seen:
            fail(f"Configuration invalide: hôte dupliqué: {name}")
        seen.add(name)

        mode = host.get("mode", "remote")
        if mode != "remote":
            fail(f"Configuration invalide: l'hôte {name} doit être en mode remote")
        url = host.get("url")
        if not isinstance(url, str) or not url.strip():
            fail(f"Configuration invalide: URL API Docker manquante pour {name}")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            fail(f"Configuration invalide: URL API Docker invalide pour {name}: {url}")

    options = config.get("options") or {}
    if not isinstance(options, dict):
        fail("Configuration invalide: options doit être un objet")

    ntfy = config.get("ntfy") or {}
    if not isinstance(ntfy, dict):
        fail("Configuration invalide: ntfy doit être un objet")
    ntfy_enabled = bool(ntfy.get("enabled", False))
    if ntfy_enabled:
        endpoint = os.environ.get("NTFY_ENDPOINT", "").strip()
        topic = str(ntfy.get("topic") or "").strip()
        if not endpoint.startswith(("http://", "https://")):
            fail("Configuration ntfy invalide: NTFY_ENDPOINT doit contenir une URL HTTP(S)")
        if not topic:
            fail("Configuration ntfy invalide: ntfy.topic est absent")
        if not os.environ.get("NTFY_TOKEN"):
            fail("Configuration ntfy invalide: NTFY_TOKEN est absente")

    try:
        timeout = float(options.get("timeout_seconds", 10))
        max_parallel = int(options.get("max_parallel", len(hosts)))
    except (TypeError, ValueError):
        fail("Configuration invalide: timeout_seconds/max_parallel doivent être numériques")
    if timeout <= 0:
        fail("Configuration invalide: timeout_seconds doit être > 0")
    if max_parallel <= 0:
        fail("Configuration invalide: max_parallel doit être > 0")


def normalize_name(name):
    if not name:
        return "unknown"
    return str(name).lstrip("/")


def parse_health(state, status=""):
    if state != "running":
        return "none"
    match = HEALTH_RE.search(status)
    if match:
        return match.group(1).lower()
    return "no_healthcheck"


def parse_exit_code(state, status):
    if state != "exited":
        return None
    match = EXIT_CODE_RE.search(status)
    return int(match.group(1)) if match else None


def parse_restart_info(state, status):
    """Extract restart-state metadata without inspect.

    Docker's list Status can expose e.g. "Restarting (1) 4 seconds ago".
    The number is the last restart/exit code, not a restart counter.
    """
    if state != "restarting":
        return None, None
    match = RESTART_STATUS_RE.search(status or "")
    if not match:
        return None, None
    exit_code = int(match.group(1))
    age = match.group(2).strip() if match.group(2) else None
    return exit_code, age


def parse_status_age(status):
    """Extract the human-readable age suffix when Docker exposes one."""
    match = STATUS_AGE_RE.search(status or "")
    return match.group(1) if match else None


def classify(state, health, exit_code=None):
    if state == "running":
        if health in {"healthy", "unhealthy", "starting"}:
            return health
        return "no_healthcheck"
    if state == "exited":
        if exit_code == 0:
            return "exited_success"
        if exit_code is not None:
            return "exited_error"
        return "exited"
    if state in {"restarting", "dead", "paused"}:
        return state
    return "unknown"


def compose_metadata(labels):
    return {
        "project": labels.get("com.docker.compose.project"),
        "service": labels.get("com.docker.compose.service"),
        "working_dir": labels.get("com.docker.compose.project.working_dir"),
    }


def normalize_healthcheck_detail(inspect_data):
    """Extract a bounded, JSON-safe summary of Docker healthcheck state."""
    if not isinstance(inspect_data, dict):
        return None

    state = inspect_data.get("State") or {}
    health = state.get("Health") or {}
    config = inspect_data.get("Config") or {}
    healthcheck = config.get("Healthcheck") or {}
    if not health:
        return None

    logs = []
    for entry in (health.get("Log") or [])[-5:]:
        if not isinstance(entry, dict):
            continue
        output = entry.get("Output")
        if output is not None:
            output = str(output)[:2000]
        logs.append({
            "start": entry.get("Start"),
            "end": entry.get("End"),
            "exit_code": entry.get("ExitCode"),
            "output": output,
        })

    return {
        "status": health.get("Status"),
        "failing_streak": health.get("FailingStreak"),
        "log": logs,
        "test": healthcheck.get("Test"),
        "interval": healthcheck.get("Interval"),
        "timeout": healthcheck.get("Timeout"),
        "start_period": healthcheck.get("StartPeriod"),
        "retries": healthcheck.get("Retries"),
    }


def normalize_api_container(item, host, healthcheck_detail=None):
    labels = item.get("Labels") or {}
    state = str(item.get("State") or "unknown").lower()
    status = str(item.get("Status") or "")
    health = parse_health(state, status)
    exit_code = parse_exit_code(state, status)
    restart_exit_code, restart_age = parse_restart_info(state, status)
    status_age = parse_status_age(status)
    classification = classify(state, health, exit_code)

    names = item.get("Names") or []
    name = names[0] if names else item.get("Name")

    return {
        "host": host,
        "name": normalize_name(name),
        "id": str(item.get("Id") or "")[:12],
        "image": item.get("Image"),
        "state": state,
        "health": health,
        "exit_code": exit_code,
        "classification": classification,
        "status": status,
        "status_age": status_age,
        "restart_exit_code": restart_exit_code,
        "restart_age": restart_age,
        "restart_loop_candidate": state == "restarting",
        "compose": compose_metadata(labels),
        "healthcheck": healthcheck_detail,
    }


def remote_get(base_url, path, timeout):
    base_url = base_url.rstrip("/")
    url = f"{base_url}{path}"
    req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code} sur {url}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Connexion impossible à {url}: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise RuntimeError(f"Connexion/timeout impossible à {url}: {exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Réponse JSON invalide sur {url}: {exc}") from exc


def docker_remote_list(base_url, include_stopped, timeout):
    query = "?all=true" if include_stopped else "?all=false"
    return remote_get(base_url, f"/containers/json{query}", timeout)


def docker_remote_inspect(base_url, container_id, timeout):
    """Read-only Docker inspect for a specific container."""
    encoded_id = urllib.parse.quote(str(container_id), safe="")
    return remote_get(base_url, f"/containers/{encoded_id}/json", timeout)


def docker_remote_logs(base_url, container_id, timeout, tail=200, max_bytes=50000):
    """Read a bounded tail of container logs through the read-only Docker API."""
    encoded_id = urllib.parse.quote(str(container_id), safe="")
    query = urllib.parse.urlencode({"stdout": "true", "stderr": "true", "timestamps": "true", "tail": str(max(1, int(tail)))})
    url = f"{base_url.rstrip('/')}/containers/{encoded_id}/logs?{query}"
    req = urllib.request.Request(url, method="GET", headers={"Accept": "text/plain"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code} sur {url}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Connexion impossible à {url}: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise RuntimeError(f"Connexion/timeout impossible à {url}: {exc}") from exc
    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[-max_bytes:]
    chunks = []
    pos = 0
    framed = False
    while pos + 8 <= len(raw):
        stream_type = raw[pos]
        size = int.from_bytes(raw[pos + 4:pos + 8], "big")
        if stream_type not in (0, 1, 2) or pos + 8 + size > len(raw):
            break
        framed = True
        chunks.append(raw[pos + 8:pos + 8 + size])
        pos += 8 + size
    payload = b"".join(chunks) if framed and pos == len(raw) else raw
    lines = payload.decode("utf-8", errors="replace").splitlines()
    return {"tail": int(tail), "line_count": len(lines), "truncated": truncated, "lines": lines}


def host_config_by_name(config, host):
    for item in config.get("hosts") or []:
        if item.get("name") == host:
            return item
    return None


def collect_container_logs(config, container, tail=200, max_bytes=50000):
    """Fetch bounded logs only when diagnose/container-report explicitly requests them."""
    host = container.get("host")
    container_id = container.get("id")
    if not host or not container_id:
        return {"error": "Hôte ou identifiant du container absent du snapshot.", "lines": []}
    host_cfg = host_config_by_name(config, host)
    if not host_cfg:
        return {"error": f"Configuration Docker introuvable pour l'hôte {host}.", "lines": []}
    try:
        timeout = float((config.get("options") or {}).get("timeout_seconds", 10))
        return docker_remote_logs(host_cfg["url"], container_id, timeout, tail=tail, max_bytes=max_bytes)
    except Exception as exc:
        return {"error": str(exc), "tail": int(tail), "line_count": 0, "truncated": False, "lines": []}


def check_host(host_cfg, include_stopped, timeout):
    host = host_cfg["name"]
    url = host_cfg["url"]
    try:
        items = docker_remote_list(url, include_stopped, timeout)
        if not isinstance(items, list):
            raise RuntimeError("Réponse Docker inattendue: une liste de containers était attendue")

        containers = []
        for item in items:
            container = normalize_api_container(item, host)
            if container.get("classification") == "unhealthy":
                full_id = item.get("Id")
                if full_id:
                    try:
                        inspect_data = docker_remote_inspect(url, full_id, timeout)
                        container["healthcheck"] = normalize_healthcheck_detail(inspect_data)
                    except Exception as exc:
                        container["healthcheck"] = {"error": str(exc)}
            containers.append(container)

        return {"host": host, "mode": "remote", "url": url, "containers": containers, "errors": []}
    except Exception as exc:
        return {"host": host, "mode": "remote", "url": url, "containers": [], "errors": [{"host": host, "error": str(exc)}]}


def count_containers(containers):
    counts = {}
    for container in containers:
        classification = container["classification"]
        counts[classification] = counts.get(classification, 0) + 1
    return counts


def group_by_stack(containers):
    groups = {}
    for c in containers:
        compose = c.get("compose") or {}
        project = compose.get("project") or None
        service = compose.get("service") or None
        host = c.get("host") or "unknown"
        key = (host, project)
        g = groups.setdefault(key, {"host": host, "project": project, "containers": 0, "services": set(), "problems": 0, "counts": {}})
        g["containers"] += 1
        if service: g["services"].add(service)
        cls = c.get("classification")
        g["counts"][cls] = g["counts"].get(cls, 0) + 1
        if cls in PROBLEM_STATES: g["problems"] += 1
    result=[]
    for g in groups.values():
        g["services"] = sorted(g["services"])
        result.append(g)
    result.sort(key=lambda x:(-x["problems"], x["host"], x["project"] or ""))
    return result

def stack_problems(containers):
    groups = {}
    for c in containers:
        if c.get("classification") not in PROBLEM_STATES: continue
        compose=c.get("compose") or {}
        project=compose.get("project") or None
        service=compose.get("service") or None
        host=c.get("host") or "unknown"
        key=(host,project)
        g=groups.setdefault(key,{"host":host,"project":project,"problems":0,"containers":[]})
        g["problems"] += 1
        g["containers"].append({"name":c.get("name"),"service":service,"classification":c.get("classification")})
    result=list(groups.values())
    result.sort(key=lambda x:(-x["problems"],x["host"],x["project"] or ""))
    return result



def healthcheck_audit(containers):
    """Audit healthcheck coverage using only fields available from /containers/json.

    Docker's container-list endpoint does not expose the configured Healthcheck
    definition for stopped containers, so coverage is computed on running
    containers for which healthcheck presence is observable from Status.
    """
    running = [c for c in containers if c.get("state") == "running"]
    without_hc = [c for c in running if c.get("classification") == "no_healthcheck"]
    with_hc = [c for c in running if c.get("classification") in {"healthy", "unhealthy", "starting"}]
    other_running = [c for c in running if c not in without_hc and c not in with_hc]

    def pct(part, total):
        return round((part / total) * 100, 2) if total else 0.0

    def host_rows(items):
        rows = {}
        for c in items:
            host = c.get("host") or "unknown"
            row = rows.setdefault(host, {"host": host, "running": 0, "with_healthcheck": 0, "without_healthcheck": 0, "coverage_percent": 0.0, "containers": []})
            row["running"] += 1
            if c.get("classification") == "no_healthcheck":
                row["without_healthcheck"] += 1
            elif c.get("classification") in {"healthy", "unhealthy", "starting"}:
                row["with_healthcheck"] += 1
            row["containers"].append({"name": c.get("name"), "service": (c.get("compose") or {}).get("service"), "project": (c.get("compose") or {}).get("project"), "classification": c.get("classification")})
        for row in rows.values():
            row["coverage_percent"] = pct(row["with_healthcheck"], row["with_healthcheck"] + row["without_healthcheck"])
            row["containers"].sort(key=lambda x: x["name"] or "")
        return sorted(rows.values(), key=lambda x: x["host"])

    stack_map = {}
    for c in running:
        compose = c.get("compose") or {}
        host = c.get("host") or "unknown"
        project = compose.get("project") or None
        key = (host, project)
        row = stack_map.setdefault(key, {"host": host, "project": project, "running": 0, "with_healthcheck": 0, "without_healthcheck": 0, "coverage_percent": 0.0, "containers": []})
        row["running"] += 1
        if c.get("classification") == "no_healthcheck":
            row["without_healthcheck"] += 1
        elif c.get("classification") in {"healthy", "unhealthy", "starting"}:
            row["with_healthcheck"] += 1
        row["containers"].append({"name": c.get("name"), "service": compose.get("service"), "classification": c.get("classification")})
    for row in stack_map.values():
        row["coverage_percent"] = pct(row["with_healthcheck"], row["with_healthcheck"] + row["without_healthcheck"])
        row["containers"].sort(key=lambda x: x["name"] or "")

    return {
        "version": VERSION,
        "scope": "running_containers",
        "total_containers": len(containers),
        "running": len(running),
        "with_healthcheck": len(with_hc),
        "without_healthcheck": len(without_hc),
        "not_determinable": len(other_running) + (len(containers) - len(running)),
        "coverage_percent": pct(len(with_hc), len(with_hc) + len(without_hc)),
        "by_host": host_rows(running),
        "by_stack": sorted(stack_map.values(), key=lambda x: (-x["without_healthcheck"], x["host"], x["project"] or "")),
        "containers": [
            {"host": c.get("host"), "name": c.get("name"), "project": (c.get("compose") or {}).get("project"), "service": (c.get("compose") or {}).get("service"), "classification": c.get("classification")}
            for c in without_hc
        ],
        "limitations": [
            "L'API GET /containers/json ne permet pas de déterminer de façon fiable la configuration Healthcheck des containers arrêtés.",
            "La couverture est donc calculée sur les containers en cours d'exécution et observables via leur statut.",
            "Un container sans healthcheck n'est pas considéré comme en panne."
        ]
    }


def human_healthcheck_audit(data):
    lines = [
        "Docker Health — Healthcheck Audit",
        "=================================",
        f"Containers: {data['total_containers']} | Running: {data['running']}",
        f"Avec healthcheck: {data['with_healthcheck']}",
        f"Sans healthcheck: {data['without_healthcheck']}",
        f"Non déterminable: {data['not_determinable']}",
        f"Couverture observable: {data['coverage_percent']:.2f}%",
        "",
        "Par hôte:"
    ]
    for row in data["by_host"]:
        lines.append(f"• {row['host']} — {row['with_healthcheck']} avec / {row['without_healthcheck']} sans — {row['coverage_percent']:.2f}%")
    lines.extend(["", "Par stack:"])
    for row in data["by_stack"]:
        name = row["project"] or "sans-compose"
        lines.append(f"• {row['host']}/{name} — {row['with_healthcheck']} avec / {row['without_healthcheck']} sans — {row['coverage_percent']:.2f}%")
        for c in row["containers"]:
            if c["classification"] == "no_healthcheck":
                lines.append(f"  - {c['name']} [{c['service'] or 'sans-service'}]")
    if not data["containers"]:
        lines.extend(["", "Aucun container sans healthcheck détecté."])
    return "\n".join(lines)

def summarize(host_results):
    counts = {}
    problems = 0
    exited_success = 0
    exited_error = 0
    no_healthcheck = 0
    restarting = 0
    containers_total = 0
    errors_total = 0

    for host in host_results:
        errors_total += len(host["errors"])
        for container in host["containers"]:
            containers_total += 1
            classification = container["classification"]
            counts[classification] = counts.get(classification, 0) + 1
            if classification in PROBLEM_STATES:
                problems += 1
            if classification == "exited_success":
                exited_success += 1
            if classification == "exited_error":
                exited_error += 1
            if classification == "no_healthcheck":
                no_healthcheck += 1
            if container["restart_loop_candidate"]:
                restarting += 1

    return {
        "version": VERSION,
        "hosts": len(host_results),
        "hosts_ok": sum(1 for h in host_results if not h["errors"]),
        "hosts_with_errors": sum(1 for h in host_results if h["errors"]),
        "containers": containers_total,
        "counts": counts,
        "problems": problems,
        "restarting": restarting,
        "exited_success": exited_success,
        "exited_error": exited_error,
        "without_healthcheck": no_healthcheck,
        "errors": errors_total,
        "compose_stacks": len(group_by_stack([c for h in host_results for c in h["containers"]])), 
        "compose_managed_containers": sum(1 for h in host_results for c in h["containers"] if (c.get("compose") or {}).get("project")),
    }


def summarize_host(host_result):
    counts = count_containers(host_result["containers"])
    restarting = sum(1 for c in host_result["containers"] if c["restart_loop_candidate"])
    return {
        "host": host_result["host"],
        "url": host_result["url"],
        "ok": not bool(host_result["errors"]),
        "containers": len(host_result["containers"]),
        "counts": counts,
        "problems": sum(counts.get(state, 0) for state in PROBLEM_STATES),
        "restarting": restarting,
        "errors": host_result["errors"],
    }


def run_check(config, selected_host=None):
    options = config.get("options") or {}
    timeout = float(options.get("timeout_seconds", 10))
    include_stopped = bool(options.get("include_stopped", True))
    hosts = config["hosts"]
    if selected_host:
        hosts = [h for h in hosts if h["name"] == selected_host]
        if not hosts:
            fail(f"Hôte inconnu: {selected_host}")

    max_parallel = min(int(options.get("max_parallel", len(hosts))), len(hosts))
    with ThreadPoolExecutor(max_workers=max_parallel) as executor:
        results = list(executor.map(lambda h: check_host(h, include_stopped, timeout), hosts))
    return {"summary": summarize(results), "hosts": results}


def human_summary(data):
    summary = data["summary"]
    counts = summary["counts"]
    return "\n".join([
        "Docker Health Summary",
        "====================",
        f"Hosts: {summary['hosts']} | OK: {summary['hosts_ok']} | Erreurs: {summary['hosts_with_errors']}",
        f"Containers: {summary['containers']}",
        f"Healthy: {counts.get('healthy', 0)}",
        f"Unhealthy: {counts.get('unhealthy', 0)}",
        f"Starting: {counts.get('starting', 0)}",
        f"No healthcheck: {summary['without_healthcheck']}",
        f"Exited avec succès: {summary['exited_success']}",
        f"Exited en erreur: {summary['exited_error']}",
        f"Redémarrages en cours: {summary['restarting']}",
        f"Problems: {summary['problems']}",
    ])


def human_host_summary(data):
    lines = ["Docker Health — Host Summary", "============================"]
    for host in data["host_summaries"]:
        counts = host["counts"]
        state = "OK" if host["ok"] else "ERROR"
        lines.extend([
            "",
            f"{host['host']} — {state}",
            f"  Containers: {host['containers']}",
            f"  Healthy: {counts.get('healthy', 0)}",
            f"  Unhealthy: {counts.get('unhealthy', 0)}",
            f"  Starting: {counts.get('starting', 0)}",
            f"  No healthcheck: {counts.get('no_healthcheck', 0)}",
            f"  Exited succès: {counts.get('exited_success', 0)}",
            f"  Exited erreur: {counts.get('exited_error', 0)}",
            f"  Redémarrages en cours: {host['restarting']}",
            f"  Problems: {host['problems']}",
        ])
        for error in host["errors"]:
            lines.append(f"  ERROR: {error['error']}")
    return "\n".join(lines)


def human(data, mode):
    if mode == "summary":
        return human_summary(data)
    if mode == "host-summary":
        return human_host_summary(data)

    summary = data["summary"]
    lines = [
        "Docker Health",
        "=============",
        f"Hosts: {summary['hosts']} | Containers: {summary['containers']}",
        f"Healthy: {summary['counts'].get('healthy', 0)}",
        f"Unhealthy: {summary['counts'].get('unhealthy', 0)}",
        f"Starting: {summary['counts'].get('starting', 0)}",
        f"No healthcheck: {summary['without_healthcheck']}",
        f"Problems: {summary['problems']}",
        f"Exited avec succès: {summary['exited_success']}",
        f"Exited en erreur: {summary['exited_error']}",
        f"Redémarrages en cours: {summary['restarting']}",
    ]
    if summary["errors"]:
        lines.append(f"Host/API errors: {summary['errors']}")

    for host in data["hosts"]:
        lines.extend(("", host["host"], "-" * len(host["host"])))
        selected = host["containers"]
        if mode == "problems":
            selected = [c for c in selected if c["classification"] in PROBLEM_STATES]
        elif mode == "no-healthcheck":
            selected = [c for c in selected if c["classification"] == "no_healthcheck"]

        for container in selected:
            classification = container["classification"]
            symbol = "✓" if classification == "healthy" else "⚠" if classification in {"starting", "no_healthcheck"} else "✗"
            detail = container.get("status") or container["state"]
            if classification == "restarting":
                age = container.get("restart_age")
                code = container.get("restart_exit_code")
                if code is not None:
                    detail += f" | exit={code}"
                if age:
                    detail += f" | depuis {age}"
            elif classification in {"exited_error", "exited_success", "exited"} and container.get("status_age"):
                detail += f" | depuis {container['status_age']}"
            elif classification == "unhealthy":
                hc = container.get("healthcheck") or {}
                if hc.get("failing_streak") is not None:
                    detail += f" | échecs={hc['failing_streak']}"
                logs = hc.get("log") or []
                if logs:
                    last = logs[-1] or {}
                    if last.get("exit_code") is not None:
                        detail += f" | exit={last['exit_code']}"
                    output = " ".join(str(last.get("output") or "").split())
                    if output:
                        detail += f" | {output[:500]}"
                elif hc.get("error"):
                    detail += f" | inspect: {hc['error']}"
            lines.append(f"{symbol} {container['name']} — {classification} — {detail}")

        for error in host["errors"]:
            lines.append(f"✗ ERROR — {error['error']}")
    return "\n".join(lines)



SNAPSHOT_VERSION = 2

def container_identity(container):
    return container.get("id") or f"{container.get('host')}::{container.get('name')}"


def build_snapshot(data, previous=None):
    """Build a compact snapshot and preserve state start timestamps."""
    now = int(time.time())
    previous_containers = (previous or {}).get("containers", {})
    containers = {}
    for host in data.get("hosts", []):
        for container in host.get("containers", []):
            identity = container_identity(container)
            classification = container.get("classification")
            restart_candidate = container.get("restart_loop_candidate", False)
            old = previous_containers.get(identity) or {}
            old_classification = old.get("classification")
            old_restart = old.get("restart_loop_candidate", False)
            state_since = old.get("state_since") if old_classification == classification else now
            restart_since = old.get("restart_since") if old_restart == restart_candidate else now
            if not restart_candidate:
                restart_since = None
            containers[identity] = {
                "host": container.get("host"),
                "name": container.get("name"),
                "id": container.get("id"),
                "classification": classification,
                "state": container.get("state"),
                "health": container.get("health"),
                "exit_code": container.get("exit_code"),
                "status": container.get("status"),
                "status_age": container.get("status_age"),
                "restart_age": container.get("restart_age"),
                "restart_exit_code": container.get("restart_exit_code"),
                "restart_loop_candidate": restart_candidate,
                "compose": container.get("compose") or {},
                "healthcheck": container.get("healthcheck"),
                "state_since": state_since,
                "restart_since": restart_since,
            }
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "skill_version": VERSION,
        "timestamp": now,
        "summary": data.get("summary", {}),
        "containers": containers,
    }

def load_snapshot(path):
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as f:
            snapshot = json.load(f)
    except FileNotFoundError:
        fail(f"Snapshot introuvable: {path}")
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"Snapshot invalide: {path}: {exc}")
    if not isinstance(snapshot, dict) or snapshot.get("snapshot_version") not in {1, SNAPSHOT_VERSION}:
        fail(f"Version de snapshot non supportée: {path}")
    if not isinstance(snapshot.get("containers"), dict):
        fail(f"Snapshot invalide: containers doit être un objet: {path}")
    return snapshot


def save_snapshot(path, data, previous=None):
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        with temp.open("w", encoding="utf-8") as f:
            json.dump(build_snapshot(data, previous=previous), f, ensure_ascii=False, indent=2)
            f.write("\n")
        temp.replace(path)
    except OSError as exc:
        fail(f"Impossible d'enregistrer le snapshot {path}: {exc}")


def format_duration(seconds):
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}j")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts[:2])


def save_snapshot_from_built(path, snapshot):
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        with temp.open("w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
            f.write("\n")
        temp.replace(path)
    except OSError as exc:
        fail(f"Impossible d'enregistrer le snapshot {path}: {exc}")


def compare_snapshots(previous, current):
    old = previous.get("containers", {})
    new = current.get("containers", {})
    changes = []
    now = int(current.get("timestamp") or time.time())

    for identity in sorted(set(old) | set(new)):
        before = old.get(identity)
        after = new.get(identity)
        if before is None:
            changes.append({"type": "added", "container": after})
            continue
        if after is None:
            changes.append({"type": "removed", "container": before})
            continue

        before_class = before.get("classification")
        after_class = after.get("classification")
        if before_class != after_class:
            state_since = after.get("state_since") or now
            previous_since = before.get("state_since") or previous.get("timestamp") or now
            previous_duration = max(0, now - previous_since)
            changes.append({
                "type": "state_changed",
                "host": after.get("host"),
                "name": after.get("name"),
                "id": after.get("id"),
                "from": before_class,
                "to": after_class,
                "since": state_since,
                "duration_seconds": max(0, now - state_since),
                "previous_since": previous_since,
                "previous_duration_seconds": previous_duration,
                "recovered": before_class in PROBLEM_STATES and after_class not in PROBLEM_STATES,
                "before": before,
                "after": after,
            })
            continue

        if before.get("restart_loop_candidate") != after.get("restart_loop_candidate"):
            restart_since = after.get("restart_since") or now
            changes.append({
                "type": "restart_state_changed",
                "host": after.get("host"),
                "name": after.get("name"),
                "id": after.get("id"),
                "from": before.get("restart_loop_candidate"),
                "to": after.get("restart_loop_candidate"),
                "since": restart_since,
                "duration_seconds": max(0, now - restart_since),
            })

    active_states = {}
    for identity, container in new.items():
        since = container.get("state_since")
        if since is not None:
            active_states[identity] = {
                "host": container.get("host"),
                "name": container.get("name"),
                "classification": container.get("classification"),
                "since": since,
                "duration_seconds": max(0, now - since),
            }

    return {
        "version": VERSION,
        "previous_timestamp": previous.get("timestamp"),
        "current_timestamp": current.get("timestamp"),
        "changes": changes,
        "changes_count": len(changes),
        "state_changes": sum(1 for c in changes if c["type"] == "state_changed"),
        "added": sum(1 for c in changes if c["type"] == "added"),
        "removed": sum(1 for c in changes if c["type"] == "removed"),
        "restart_state_changes": sum(1 for c in changes if c["type"] == "restart_state_changed"),
        "active_states": active_states,
    }

def load_events(path):
    path = Path(path)
    if not path.exists():
        return {"events_version": 1, "events": []}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"Historique d'événements invalide: {path}: {exc}")
    if not isinstance(data, dict) or data.get("events_version") != 1:
        fail(f"Version d'historique non supportée: {path}")
    if not isinstance(data.get("events"), list):
        fail(f"Historique d'événements invalide: events doit être une liste: {path}")
    return data


def save_events(path, data):
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        with temp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        temp.replace(path)
    except OSError as exc:
        fail(f"Impossible d'enregistrer l'historique {path}: {exc}")


def events_from_changes(changes_data):
    events = []
    timestamp = changes_data.get("current_timestamp") or int(time.time())
    for change in changes_data.get("changes", []):
        ctype = change.get("type")
        if ctype == "state_changed":
            if change.get("recovered"):
                event_type = "recovered"
                duration = change.get("previous_duration_seconds", 0)
            elif change.get("to") in PROBLEM_STATES:
                event_type = "problem_started"
                duration = 0
            else:
                event_type = "state_changed"
                duration = 0
            events.append({
                "timestamp": timestamp,
                "type": event_type,
                "host": change.get("host"),
                "name": change.get("name"),
                "id": change.get("id"),
                "from": change.get("from"),
                "to": change.get("to"),
                "classification": change.get("to"),
                "compose": (change.get("after") or {}).get("compose") or {},
                "duration_seconds": duration,
            })
        elif ctype == "restart_state_changed":
            event_type = "problem_started" if change.get("to") else "recovered"
            events.append({
                "timestamp": timestamp,
                "type": event_type,
                "host": change.get("host"),
                "name": change.get("name"),
                "id": change.get("id"),
                "from": "restarting" if change.get("from") else None,
                "to": "restarting" if change.get("to") else None,
                "duration_seconds": change.get("duration_seconds", 0),
                "restart_state_change": True,
            })
        elif ctype in {"added", "removed"}:
            c = change.get("container", {})
            events.append({
                "timestamp": timestamp,
                "type": ctype,
                "host": c.get("host"),
                "name": c.get("name"),
                "id": c.get("id"),
                "classification": c.get("classification"),
                "compose": c.get("compose") or {},
            })
    return events


def append_events(path, changes_data, max_events=500):
    history = load_events(path)
    history["events"].extend(events_from_changes(changes_data))
    history["events"] = history["events"][-max_events:]
    history["updated_at"] = changes_data.get("current_timestamp") or int(time.time())
    save_events(path, history)
    return history


def human_events(data):
    lines = [
        "Docker Health — Events",
        "======================",
        f"Événements: {len(data['events'])}",
    ]
    for event in reversed(data["events"]):
        duration = event.get("duration_seconds")
        suffix = f" | durée {format_duration(duration)}" if duration is not None else ""
        transition = ""
        if event.get("from") is not None or event.get("to") is not None:
            transition = f" | {event.get('from')} → {event.get('to')}"
        lines.append(f"• {event.get('type')} — {event.get('host')}/{event.get('name')}{transition}{suffix}")
    if not data["events"]:
        lines.append("Aucun événement enregistré.")
    return "\n".join(lines)


def parse_duration(value):
    """Parse a simple duration such as 30s, 15m, 2h or 1d."""
    if value is None:
        return 0
    text = str(value).strip().lower()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(s|m|h|d|w)?", text)
    if not match:
        raise ValueError(f"Durée invalide: {value} (exemples: 30m, 2h, 1d)")
    number = float(match.group(1))
    unit = match.group(2) or "s"
    factor = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return int(number * factor)


def persistent_problems(data, min_duration_seconds):
    now = int(data.get("timestamp") or time.time())
    result = []
    for item in data.get("containers", {}).values():
        if item.get("classification") not in PROBLEM_STATES:
            continue
        since = item.get("state_since")
        if since is None:
            continue
        duration = max(0, now - since)
        if duration >= min_duration_seconds:
            result.append({
                "host": item.get("host"),
                "name": item.get("name"),
                "classification": item.get("classification"),
                "since": since,
                "duration_seconds": duration,
            })
    result.sort(key=lambda x: (-x["duration_seconds"], x.get("host") or "", x.get("name") or ""))
    return result


def human_persistent(data):
    lines = [
        "Docker Health — Persistent Problems",
        "===================================",
        f"Seuil: {format_duration(data['min_duration_seconds'])}",
        f"Containers concernés: {len(data['containers'])}",
    ]
    for item in data["containers"]:
        lines.append(
            f"• {item['host']}/{item['name']}: {item['classification']} | depuis {format_duration(item['duration_seconds'])}"
        )
    if not data["containers"]:
        lines.append("Aucun problème persistant au-delà du seuil.")
    return "\n".join(lines)


def human_changes(data):
    lines = [
        "Docker Health — Changes",
        "=======================",
        f"Changes: {data['changes_count']}",
        f"State changes: {data['state_changes']}",
        f"Added: {data['added']} | Removed: {data['removed']} | Restart changes: {data['restart_state_changes']}",
    ]
    for change in data["changes"]:
        if change["type"] == "state_changed":
            if change.get("recovered"):
                lines.append(f"• {change['host']}/{change['name']}: {change['from']} → {change['to']} | problème pendant {format_duration(change.get('previous_duration_seconds', 0))}")
            else:
                lines.append(f"• {change['host']}/{change['name']}: {change['from']} → {change['to']} | nouvel état depuis {format_duration(change.get('duration_seconds', 0))}")
        elif change["type"] == "restart_state_changed":
            lines.append(f"• {change['host']}/{change['name']}: restart_loop_candidate {change['from']} → {change['to']} | depuis {format_duration(change.get('duration_seconds', 0))}")
        elif change["type"] == "added":
            c = change["container"]
            lines.append(f"• + {c.get('host')}/{c.get('name')}: {c.get('classification')}")
        elif change["type"] == "removed":
            c = change["container"]
            lines.append(f"• - {c.get('host')}/{c.get('name')}: {c.get('classification')}")

    active_problems = [
        item for item in data.get("active_states", {}).values()
        if item.get("classification") in PROBLEM_STATES
    ]
    if active_problems:
        lines.extend(["", "Problèmes actuellement actifs:"])
        for item in sorted(active_problems, key=lambda x: (x.get("host") or "", x.get("name") or "")):
            lines.append(
                f"• {item.get('host')}/{item.get('name')}: {item.get('classification')} | depuis {format_duration(item.get('duration_seconds', 0))}"
            )
    return "\n".join(lines)


def alert_identity(alert):
    return f"{alert.get('host')}::{alert.get('id') or alert.get('name')}"


def load_alert_state(path):
    path = Path(path)
    if not path.exists():
        return {"version": 1, "alerts": {}}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"État des alertes invalide: {path}: {exc}")
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("alerts"), dict):
        fail(f"État des alertes non supporté: {path}")
    return data


def save_alert_state(path, data):
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        with temp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        temp.replace(path)
    except OSError as exc:
        fail(f"Impossible d'enregistrer l'état des alertes {path}: {exc}")


def alert_diff(previous_state, alerts):
    old = previous_state.get("alerts", {})
    current = {alert_identity(a): a for a in alerts}
    new_alerts = [a for key, a in current.items() if key not in old]
    resolved = [a for key, a in old.items() if key not in current]
    changed = []
    for key, alert in current.items():
        before = old.get(key)
        if before and any(
            before.get(field) != alert.get(field)
            for field in ("classification", "severity", "confidence")
        ):
            changed.append({"before": before, "after": alert})
    return new_alerts, resolved, changed, current


def alert_profile(item, duration):
    """Return contextual severity and guidance without extra Docker calls.

    The goal is to separate states that usually require immediate attention
    from states that are useful signals but can legitimately be transient.
    """
    classification = item.get("classification")
    exit_code = item.get("exit_code")
    healthcheck = item.get("healthcheck") or {}

    profiles = {
        "unhealthy": ("critical", "high", "Healthcheck en échec"),
        "restarting": ("critical", "high", "Container en redémarrage"),
        "dead": ("critical", "high", "Container marqué dead par Docker"),
        "exited_error": ("warning", "medium", "Container arrêté avec un code de sortie non nul"),
        "paused": ("warning", "medium", "Container actuellement en pause"),
        "unknown": ("warning", "low", "État Docker impossible à classer"),
    }
    severity, confidence, reason = profiles.get(
        classification, ("warning", "low", "État problématique")
    )

    if classification == "exited_error":
        if exit_code is not None:
            reason = f"Arrêt avec code de sortie {exit_code}"
        action = "Vérifier les logs si cet arrêt est inattendu ou récurrent."
    elif classification == "unhealthy":
        streak = healthcheck.get("failing_streak")
        logs = healthcheck.get("log") or []
        last = logs[-1] if logs else {}
        last_exit = last.get("exit_code")
        output = " ".join(str(last.get("output") or "").split())
        if streak is not None:
            reason += f" ({streak} échec(s) consécutif(s)"
            if last_exit is not None:
                reason += f", code {last_exit}"
            reason += ")"
        if output:
            reason += f" : {output[:500]}"
        action = "Vérifier la cause indiquée par la dernière exécution du healthcheck et le service concerné."
    elif classification == "restarting":
        action = "Vérifier la cause du redémarrage et les logs du container."
    elif classification == "dead":
        action = "Vérifier le container et envisager une intervention."
    elif classification == "paused":
        action = "Vérifier si la mise en pause est volontaire."
    else:
        action = "Vérifier l'état du container."

    return severity, confidence, reason, action


DEFAULT_ALERT_RULES = {
    "unhealthy": "critical",
    "restarting": "critical",
    "dead": "critical",
    "exited_error": "warning",
    "paused": "warning",
    "unknown": "warning",
}
VALID_ALERT_LEVELS = {"critical", "warning", "ignore"}

def normalize_alert_rules(config):
    rules = dict(DEFAULT_ALERT_RULES)
    configured = ((config or {}).get("options") or {}).get("alert_rules") or {}
    for classification, level in configured.items():
        if classification in rules and level in VALID_ALERT_LEVELS:
            rules[classification] = level
    return rules

def apply_alert_rules(alerts, rules):
    filtered = []
    for alert in alerts:
        classification = alert.get("classification")
        level = rules.get(classification, alert.get("severity", "warning"))
        if level == "ignore":
            continue
        item = dict(alert)
        item["severity"] = level
        filtered.append(item)
    filtered.sort(key=lambda x: (0 if x.get("severity") == "critical" else 1, x.get("host", ""), x.get("name", "")))
    return filtered

def build_alerts(snapshot, threshold):
    now = int(snapshot.get("timestamp") or time.time())
    alerts = []
    for item in snapshot.get("containers", {}).values():
        classification = item.get("classification")
        if classification not in PROBLEM_STATES:
            continue
        since = item.get("state_since")
        if since is None:
            continue
        duration = max(0, now - since)
        if duration < threshold:
            continue

        severity, confidence, reason, action = alert_profile(item, duration)
        exit_code = item.get("exit_code")
        message = f"{reason} depuis {format_duration(duration)}"
        if exit_code is not None and classification == "exited_error":
            message += f" (code {exit_code})"

        alerts.append({
            "type": "persistent_problem",
            "severity": severity,
            "confidence": confidence,
            "host": item.get("host"),
            "name": item.get("name"),
            "id": item.get("id"),
            "classification": classification,
            "since": since,
            "duration_seconds": duration,
            "exit_code": exit_code,
            "reason": reason,
            "recommended_action": action,
            "message": message,
        })
    alerts.sort(
        key=lambda x: (
            0 if x["severity"] == "critical" else 1,
            -x["duration_seconds"],
            x.get("host") or "",
            x.get("name") or "",
        )
    )
    return now, alerts

def stats_from_history(snapshot, history):
    """Build recurrence and duration statistics from the snapshot + event history."""
    events = history.get("events", []) if isinstance(history, dict) else []
    by_container = {}
    by_host = {}

    def key_for(event):
        return f"{event.get('host')}::{event.get('id') or event.get('name')}"

    for event in events:
        if event.get("type") not in {"problem_started", "recovered"}:
            continue
        key = key_for(event)
        item = by_container.setdefault(key, {
            "host": event.get("host"), "name": event.get("name"), "id": event.get("id"),
            "problem_starts": 0, "recoveries": 0, "total_problem_duration_seconds": 0,
            "last_problem_started": None, "last_recovered": None,
        })
        host = by_host.setdefault(event.get("host"), {"host": event.get("host"), "problem_starts": 0, "recoveries": 0, "total_problem_duration_seconds": 0})
        if event.get("type") == "problem_started":
            item["problem_starts"] += 1
            host["problem_starts"] += 1
            item["last_problem_started"] = event.get("timestamp")
        else:
            duration = int(event.get("duration_seconds") or 0)
            item["recoveries"] += 1
            item["total_problem_duration_seconds"] += duration
            item["last_recovered"] = event.get("timestamp")
            host["recoveries"] += 1
            host["total_problem_duration_seconds"] += duration

    active = snapshot.get("containers", {}) if isinstance(snapshot, dict) else {}
    active_by_key = {}
    for item in active.values():
        if item.get("classification") in PROBLEM_STATES:
            key = f"{item.get('host')}::{item.get('id') or item.get('name')}"
            active_by_key[key] = item
            current_duration = max(0, int(snapshot.get("timestamp") or time.time()) - int(item.get("state_since") or snapshot.get("timestamp") or time.time()))
            stats = by_container.setdefault(key, {
                "host": item.get("host"), "name": item.get("name"), "id": item.get("id"),
                "problem_starts": 0, "recoveries": 0, "total_problem_duration_seconds": 0,
                "last_problem_started": item.get("state_since"), "last_recovered": None,
            })
            stats["active_problem"] = True
            stats["current_classification"] = item.get("classification")
            stats["current_problem_duration_seconds"] = current_duration
            if not stats.get("last_problem_started"):
                stats["last_problem_started"] = item.get("state_since")

    for item in by_container.values():
        item.setdefault("active_problem", False)
        item.setdefault("current_classification", None)
        item.setdefault("current_problem_duration_seconds", 0)
        item["average_problem_duration_seconds"] = (
            item["total_problem_duration_seconds"] / item["recoveries"] if item["recoveries"] else 0
        )
        item["recurrence"] = "recurrent" if item["problem_starts"] >= 2 else ("active" if item["active_problem"] else "observed_once")

    containers = list(by_container.values())
    containers.sort(key=lambda x: (-x["problem_starts"], -x["total_problem_duration_seconds"], x.get("host") or "", x.get("name") or ""))
    return {
        "version": VERSION,
        "snapshot": snapshot.get("timestamp"),
        "events_considered": len(events),
        "problem_starts": sum(x["problem_starts"] for x in containers),
        "recoveries": sum(x["recoveries"] for x in containers),
        "active_problems": sum(1 for x in containers if x["active_problem"]),
        "total_observed_problem_duration_seconds": sum(x["total_problem_duration_seconds"] for x in containers),
        "containers_with_history": len(containers),
        "recurrent_containers": sum(1 for x in containers if x["recurrence"] == "recurrent"),
        "by_host": sorted(by_host.values(), key=lambda x: x.get("host") or ""),
        "containers": containers,
    }


def human_stats(data):
    lines = [
        "Docker Health — Statistics",
        "==========================",
        f"Événements analysés: {data['events_considered']}",
        f"Débuts de problèmes: {data['problem_starts']} | Rétablissements: {data['recoveries']}",
        f"Problèmes actuellement actifs: {data['active_problems']}",
        f"Durée cumulée observée: {format_duration(data['total_observed_problem_duration_seconds'])}",
        f"Containers avec historique: {data['containers_with_history']} | Récurrents: {data['recurrent_containers']}",
        "",
        "Containers avec incidents:",
    ]
    if not data["containers"]:
        lines.append("• Aucun historique de problème disponible.")
    else:
        for item in data["containers"]:
            lines.append(
                f"• {item.get('host')}/{item.get('name')} — {item['problem_starts']} début(s), "
                f"{item['recoveries']} rétablissement(s), cumul {format_duration(item['total_problem_duration_seconds'])}, "
                f"{item['recurrence']}"
            )
    return "\n".join(lines)


def build_agent_report(config, host=None, previous_path=None, min_duration="30m"):
    data = run_check(config, host)
    containers = [c for h in data["hosts"] for c in h["containers"]]
    summary = data["summary"]
    problems = [c for c in containers if c.get("classification") in PROBLEM_STATES]
    stacks = stack_problems(containers)
    audit = healthcheck_audit(containers)
    threshold = parse_duration(min_duration)
    previous = load_snapshot(previous_path) if previous_path else None
    snapshot = build_snapshot(data, previous=previous)
    now, alerts = build_alerts(snapshot, threshold)
    alerts = apply_alert_rules(alerts, normalize_alert_rules(config))
    changes = compare_snapshots(previous, snapshot) if previous is not None else None
    return {
        "version": VERSION,
        "timestamp": now,
        "mode": "agent",
        "summary": summary,
        "problems": problems,
        "problem_count": len(problems),
        "stacks_with_problems": stacks,
        "healthcheck_audit": {
            "running": audit["running"],
            "with_healthcheck": audit["with_healthcheck"],
            "without_healthcheck": audit["without_healthcheck"],
            "coverage_percent": audit["coverage_percent"],
        },
        "alerts": alerts,
        "alert_count": len(alerts),
        "changes": changes,
        "previous_snapshot": previous_path,
    }

def human_agent_report(data):
    s=data["summary"]
    lines=[
        "Docker Health — Agent Report",
        "=============================",
        f"{s.get('containers', 0)} containers sur {s.get('hosts', 0)} hôte(s).",
        f"{s.get('healthy', 0)} healthy | {s.get('problems', 0)} problème(s) | {s.get('without_healthcheck', 0)} sans healthcheck.",
    ]
    if data["alerts"]:
        lines.append(
            f"Alertes persistantes: {sum(1 for a in data['alerts'] if a['severity']=='critical')} critiques, "
            f"{sum(1 for a in data['alerts'] if a['severity']=='warning')} warnings."
        )
    if data["problems"]:
        lines.append("Problèmes:")
        for c in data["problems"][:20]:
            compose=c.get("compose") or {}
            stack=compose.get("project") or "sans-compose"
            service=compose.get("service") or "sans-service"
            line = f"• {c.get('host')}/{stack}/{service} — {c.get('name')} — {c.get('classification')}"
            if c.get("classification") == "unhealthy":
                hc = c.get("healthcheck") or {}
                logs = hc.get("log") or []
                last = logs[-1] if logs else {}
                output = " ".join(str(last.get("output") or "").split())
                details = []
                if hc.get("failing_streak") is not None:
                    details.append(f"échecs={hc['failing_streak']}")
                if last.get("exit_code") is not None:
                    details.append(f"exit={last['exit_code']}")
                if output:
                    details.append(output[:400])
                if details:
                    line += " — " + " | ".join(details)
            lines.append(line)
        if len(data["problems"])>20:
            lines.append(f"• … {len(data['problems'])-20} autre(s)")
    else:
        lines.append("Aucun container actuellement classé comme problématique.")
    if data["stacks_with_problems"]:
        lines.append("Stacks concernées:")
        for st in data["stacks_with_problems"][:10]:
            lines.append(f"• {st['host']}/{st['project'] or 'sans-compose'} — {st['problems']} problème(s)")
    if data["changes"] is not None:
        ch=data["changes"]
        lines.append(f"Changements depuis le snapshot: {ch['changes_count']} ({ch['state_changes']} états, {ch['added']} ajoutés, {ch['removed']} supprimés).")
    return "\n".join(lines)


DEFAULT_REPORT_WINDOW = 86400

def _report_identity(item):
    return f"{item.get('host')}::{item.get('id') or item.get('name')}"

def build_report(config, snapshot_path="/opt/data/.hermes/data/docker-health.json", events_path=None, window="24h", host=None):
    """Build an operator report entirely from persisted state; never queries Docker."""
    snapshot = load_snapshot(snapshot_path)
    now = int(time.time())
    window_seconds = parse_duration(window)
    cutoff = now - window_seconds
    if events_path is None:
        events_path = str(Path(snapshot_path).with_suffix(".events.json"))
    history = load_events(events_path)
    events = history.get("events", [])
    if host:
        events = [e for e in events if e.get("host") == host]

    containers = list(snapshot.get("containers", {}).values())
    if host:
        containers = [c for c in containers if c.get("host") == host]
    active = [c for c in containers if c.get("classification") in PROBLEM_STATES]

    recent = [e for e in events if int(e.get("timestamp") or 0) >= cutoff]
    new_problems = [e for e in recent if e.get("type") == "problem_started"]
    resolved_problems = [e for e in recent if e.get("type") == "recovered"]

    ack_state = load_ack_state(ack_state_path(config))
    acked = []
    for item in active:
        ack = ack_state.get("acks", {}).get(_report_identity(item))
        if ack and ack.get("classification") == item.get("classification"):
            acked.append((item, ack))

    silence_state = cleanup_expired_silences(config, now=now)
    active_silences = []
    silences = silence_state.get("silences", {})
    for item in active:
        identity = _report_identity(item)
        name_identity = silence_identity(item.get("host"), None, item.get("name"))
        silence = silences.get(identity) or silences.get(name_identity)
        if silence and int(silence.get("until", 0)) > now:
            active_silences.append((item, silence))

    by_host = {}
    for item in active:
        h = item.get("host") or "unknown-host"
        by_host[h] = by_host.get(h, 0) + 1
    by_host = dict(sorted(by_host.items()))

    by_stack = {}
    stack_available = any("compose" in c for c in containers)
    for item in active:
        compose = item.get("compose") or {}
        project = compose.get("project") or "sans-compose"
        key = f"{item.get('host')}/{project}"
        by_stack[key] = by_stack.get(key, 0) + 1
    by_stack = dict(sorted(by_stack.items(), key=lambda x: (-x[1], x[0])))

    by_classification = {}
    for item in active:
        cls = item.get("classification") or "unknown"
        by_classification[cls] = by_classification.get(cls, 0) + 1
    by_classification = dict(sorted(by_classification.items(), key=lambda x: (-x[1], x[0])))

    stats = stats_from_history(snapshot, {"events": events})
    recurring = [x for x in stats["containers"] if x.get("recurrence") == "recurrent"]
    recurring = recurring[:10]

    running = sum(1 for c in containers if c.get("classification") in {"healthy", "starting", "no_healthcheck", "running", "unhealthy", "restarting", "dead", "paused", "unknown"})
    with_healthcheck = sum(1 for c in containers if c.get("health") in {"healthy", "unhealthy", "starting"})
    without_healthcheck = sum(1 for c in containers if c.get("classification") == "no_healthcheck")
    coverage_base = with_healthcheck + without_healthcheck
    coverage = round((with_healthcheck / coverage_base) * 100, 1) if coverage_base else 0.0

    return {
        "version": VERSION,
        "mode": "report",
        "timestamp": now,
        "window_seconds": window_seconds,
        "snapshot": str(snapshot_path),
        "events_file": str(events_path),
        "active_problems": len(active),
        "new_problems": len(new_problems),
        "resolved_problems": len(resolved_problems),
        "acknowledged": len(acked),
        "silenced": len(active_silences),
        "healthcheck_coverage": coverage,
        "healthcheck": {
            "running": running,
            "with_healthcheck": with_healthcheck,
            "without_healthcheck": without_healthcheck,
        },
        "by_host": by_host,
        "by_stack": by_stack,
        "by_classification": by_classification,
        "top_recurring": recurring,
        "stack_context_available": stack_available,
        "events_in_window": len(recent),
    }

def human_report(data):
    lines = [
        "Docker Health — Report",
        "======================",
        f"Problèmes actifs: {data['active_problems']}",
        f"Nouveaux problèmes ({format_duration(data['window_seconds'])}): {data['new_problems']}",
        f"Problèmes résolus ({format_duration(data['window_seconds'])}): {data['resolved_problems']}",
        f"Acquittés: {data['acknowledged']}",
        f"Silencés: {data['silenced']}",
        "",
        f"Healthchecks: {data['healthcheck_coverage']:.1f}% ({data['healthcheck']['with_healthcheck']} avec / {data['healthcheck']['without_healthcheck']} sans)",
        "",
        "Hôtes:",
    ]
    if data["by_host"]:
        for host, count in data["by_host"].items():
            lines.append(f"• {host} → {count} problème(s)")
    else:
        lines.append("• Tous les hôtes sont OK")
    lines.append("")
    lines.append("Stacks concernées:")
    if data["by_stack"]:
        for stack, count in list(data["by_stack"].items())[:10]:
            lines.append(f"• {stack} → {count} problème(s)")
    elif data.get("stack_context_available"):
        lines.append("• Aucune")
    else:
        lines.append("• Indisponible dans l'ancien snapshot")
    lines.append("")
    lines.append("Classifications:")
    if data["by_classification"]:
        for cls, count in data["by_classification"].items():
            lines.append(f"• {cls} → {count}")
    else:
        lines.append("• Aucune")
    if data["top_recurring"]:
        lines.append("")
        lines.append("Containers récurrents:")
        for item in data["top_recurring"]:
            lines.append(f"• {item.get('host')}/{item.get('name')} → {item.get('problem_starts', 0)} incident(s)")
    lines.append("")
    lines.append(f"Événements sur la période: {data['events_in_window']}")
    return "\n".join(lines)


def _report_events(snapshot_path, events_path=None):
    if events_path is None:
        events_path = str(Path(snapshot_path).with_suffix(".events.json"))
    return load_events(events_path), events_path


def build_host_report(config, snapshot_path=None, host=None, window="24h"):
    """Detailed host report from persisted state only; never queries Docker."""
    snapshot_path = snapshot_path or DEFAULT_SNAPSHOT_PATH
    if not host:
        fail("--host est requis pour host-report")
    snapshot = load_snapshot(snapshot_path)
    containers = [c for c in snapshot.get("containers", {}).values() if c.get("host") == host]
    if not containers:
        fail(f"Aucun container trouvé pour l'hôte {host} dans {snapshot_path}")
    events, events_path = _report_events(snapshot_path)
    cutoff = int(time.time()) - parse_duration(window)
    host_events = [e for e in events.get("events", []) if e.get("host") == host and int(e.get("timestamp") or 0) >= cutoff]
    active = [c for c in containers if c.get("classification") in PROBLEM_STATES]
    by_class = {}
    for c in containers:
        cls = c.get("classification") or "unknown"
        by_class[cls] = by_class.get(cls, 0) + 1
    stacks = {}
    for c in active:
        project = (c.get("compose") or {}).get("project") or "sans-compose"
        stacks[project] = stacks.get(project, 0) + 1
    return {
        "version": VERSION, "mode": "host-report", "host": host,
        "timestamp": int(time.time()), "window_seconds": parse_duration(window),
        "snapshot": str(snapshot_path), "events_file": events_path,
        "containers": len(containers), "active_problems": len(active),
        "by_classification": dict(sorted(by_class.items(), key=lambda x: (-x[1], x[0]))),
        "by_stack": dict(sorted(stacks.items(), key=lambda x: (-x[1], x[0]))),
        "problems": sorted(active, key=lambda x: (x.get("classification") or "", x.get("name") or "")),
        "events_in_window": len(host_events),
    }


def build_stack_report(config, snapshot_path=None, stack=None, host=None, window="24h"):
    """Detailed Compose stack report from persisted state only; never queries Docker."""
    snapshot_path = snapshot_path or DEFAULT_SNAPSHOT_PATH
    if not stack:
        fail("--stack est requis pour stack-report")
    snapshot = load_snapshot(snapshot_path)
    containers = []
    for c in snapshot.get("containers", {}).values():
        compose = c.get("compose") or {}
        project = compose.get("project") or "sans-compose"
        if project == stack and (host is None or c.get("host") == host):
            containers.append(c)
    if not containers:
        fail(f"Aucun container trouvé pour la stack {stack}")
    events, events_path = _report_events(snapshot_path)
    cutoff = int(time.time()) - parse_duration(window)
    identities = {_report_identity(c) for c in containers}
    stack_events = [e for e in events.get("events", []) if _report_identity(e) in identities and int(e.get("timestamp") or 0) >= cutoff]
    active = [c for c in containers if c.get("classification") in PROBLEM_STATES]
    services = {}
    for c in containers:
        service = (c.get("compose") or {}).get("service") or "sans-service"
        item = services.setdefault(service, {"containers": 0, "problems": 0, "classifications": {}})
        item["containers"] += 1
        cls = c.get("classification") or "unknown"
        item["classifications"][cls] = item["classifications"].get(cls, 0) + 1
        if cls in PROBLEM_STATES:
            item["problems"] += 1
    return {
        "version": VERSION, "mode": "stack-report", "stack": stack,
        "host": host, "timestamp": int(time.time()), "window_seconds": parse_duration(window),
        "snapshot": str(snapshot_path), "events_file": events_path,
        "containers": len(containers), "active_problems": len(active),
        "services": dict(sorted(services.items())),
        "problems": sorted(active, key=lambda x: ((x.get("compose") or {}).get("service") or "", x.get("name") or "")),
        "events_in_window": len(stack_events),
    }


def build_container_report(config, snapshot_path=None, container=None, host=None, window="7d"):
    """Detailed container history; fetches bounded Docker logs only for problem containers."""
    snapshot_path = snapshot_path or DEFAULT_SNAPSHOT_PATH
    if not container:
        fail("--container est requis pour container-report")
    snapshot = load_snapshot(snapshot_path)
    matches = [c for c in snapshot.get("containers", {}).values() if c.get("name") == container and (host is None or c.get("host") == host)]
    if not matches:
        fail(f"Container introuvable: {host + '/' if host else ''}{container}")
    current = matches[0]
    identity = _report_identity(current)
    events, events_path = _report_events(snapshot_path)
    cutoff = int(time.time()) - parse_duration(window)
    history = [e for e in events.get("events", []) if _report_identity(e) == identity and int(e.get("timestamp") or 0) >= cutoff]
    starts = [e for e in history if e.get("type") == "problem_started"]
    recoveries = [e for e in history if e.get("type") == "recovered"]
    total_duration = sum(int(e.get("duration_seconds") or 0) for e in recoveries)
    if current.get("classification") in PROBLEM_STATES and current.get("state_since"):
        total_duration += max(0, int(time.time()) - int(current["state_since"]))
    return {
        "version": VERSION, "mode": "container-report", "host": current.get("host"),
        "container": current.get("name"), "id": current.get("id"), "timestamp": int(time.time()),
        "window_seconds": parse_duration(window), "snapshot": str(snapshot_path), "events_file": events_path,
        "current": current, "problem_starts": len(starts), "recoveries": len(recoveries),
        "observed_problem_duration_seconds": total_duration,
        "recurring": len(starts) >= 2,
        "events": history,
        "logs": collect_container_logs(config, current) if current.get("classification") in PROBLEM_STATES else None,
    }


def build_trends(config, snapshot_path=None, window="7d", host=None):
    """Aggregate persisted problem events by day; never queries Docker."""
    snapshot_path = snapshot_path or DEFAULT_SNAPSHOT_PATH
    snapshot = load_snapshot(snapshot_path)
    events, events_path = _report_events(snapshot_path)
    window_seconds = parse_duration(window)
    now = int(time.time())
    cutoff = now - window_seconds
    selected = [e for e in events.get("events", []) if int(e.get("timestamp") or 0) >= cutoff and (host is None or e.get("host") == host)]
    days = {}
    for e in selected:
        day = time.strftime("%Y-%m-%d", time.localtime(int(e.get("timestamp") or now)))
        d = days.setdefault(day, {"problem_started": 0, "recovered": 0, "state_changed": 0, "added": 0, "removed": 0})
        typ = e.get("type")
        if typ in d:
            d[typ] += 1
    starts = [e for e in selected if e.get("type") == "problem_started"]
    recoveries = [e for e in selected if e.get("type") == "recovered"]
    hosts = {}
    for e in starts:
        h = e.get("host") or "unknown-host"
        hosts[h] = hosts.get(h, 0) + 1
    return {
        "version": VERSION, "mode": "trends", "host": host, "timestamp": now,
        "window_seconds": window_seconds, "snapshot": str(snapshot_path), "events_file": events_path,
        "problem_starts": len(starts), "recoveries": len(recoveries),
        "net_change": len(starts) - len(recoveries),
        "by_day": dict(sorted(days.items())),
        "problem_starts_by_host": dict(sorted(hosts.items(), key=lambda x: (-x[1], x[0]))),
    }


def human_host_report(data):
    lines=["Docker Health — Host Report", "============================", f"Hôte: {data['host']}", f"Containers: {data['containers']} | Problèmes: {data['active_problems']}", ""]
    lines.append("Stacks:")
    for k,v in data["by_stack"].items(): lines.append(f"• {k} → {v} problème(s)")
    if not data["by_stack"]: lines.append("• Aucune")
    lines.append("\nProblèmes:")
    if data["problems"]:
        for c in data["problems"]: lines.append(f"• {c.get('name')} — {c.get('classification')}")
    else: lines.append("• Aucun")
    lines.append(f"\nÉvénements sur la période: {data['events_in_window']}")
    return "\n".join(lines)


def human_stack_report(data):
    title=f"Docker Health — Stack Report\n============================\nStack: {data['stack']}"
    if data.get("host"): title += f"\nHôte: {data['host']}"
    lines=[title, f"Containers: {data['containers']} | Problèmes: {data['active_problems']}", "", "Services:"]
    for service,item in data["services"].items(): lines.append(f"• {service} → {item['containers']} container(s), {item['problems']} problème(s)")
    lines.append("\nProblèmes:")
    if data["problems"]:
        for c in data["problems"]: lines.append(f"• {c.get('name')} — {c.get('classification')}")
    else: lines.append("• Aucun")
    lines.append(f"\nÉvénements sur la période: {data['events_in_window']}")
    return "\n".join(lines)


def human_container_report(data):
    c=data["current"]
    compose=c.get("compose") or {}
    lines=["Docker Health — Container Report", "================================", f"Container: {data['host']}/{data['container']}", f"État: {c.get('classification')}", f"Stack: {compose.get('project') or 'sans-compose'} / {compose.get('service') or 'sans-service'}", f"Incidents: {data['problem_starts']} | Rétablissements: {data['recoveries']}", f"Durée cumulée observée: {format_duration(data['observed_problem_duration_seconds'])}", f"Récurrent: {'oui' if data['recurring'] else 'non'}", ""]
    if data["events"]:
        lines.append("Historique:")
        for e in data["events"][-20:]: lines.append(f"• {e.get('type')} — {time.strftime('%Y-%m-%d %H:%M', time.localtime(int(e.get('timestamp') or 0)))}")
    else: lines.append("Aucun événement sur la période.")
    logs = data.get("logs")
    if logs is not None:
        lines.append("\nLogs récents:")
        if logs.get("error"):
            lines.append(f"• Impossible de lire les logs: {logs['error']}")
        elif logs.get("lines"):
            for line in logs["lines"][-80:]:
                lines.append(f"  {line}")
            if logs.get("truncated"):
                lines.append("• Logs tronqués à 50 Ko.")
        else:
            lines.append("• Aucun log récent.")
    return "\n".join(lines)


def _diagnostic_identity(c):
    return _report_identity(c)


def _event_container_name(event):
    alert = event.get("alert") or event.get("after") or event.get("before") or {}
    return alert.get("name") or event.get("name") or event.get("container")


def _event_compose(event):
    alert = event.get("alert") or event.get("after") or event.get("before") or {}
    return alert.get("compose") or event.get("compose") or {}


def build_diagnose(config, snapshot_path=None, events_path=None, host=None, container=None, stack=None, window="30m"):
    """Correlate persisted observations and read bounded Docker logs for active problems."""
    snapshot_path = snapshot_path or DEFAULT_SNAPSHOT_PATH
    if not host and not container and not stack:
        fail("diagnose nécessite --host, --container ou --stack")
    if container and not host:
        fail("--container nécessite --host")
    snapshot = load_snapshot(snapshot_path)
    events, events_path = _report_events(snapshot_path, events_path)
    window_seconds = parse_duration(window)
    now = int(time.time())
    cutoff = now - window_seconds
    containers = list(snapshot.get("containers", {}).values())

    target = []
    for c in containers:
        compose = c.get("compose") or {}
        project = compose.get("project") or "sans-compose"
        if host and c.get("host") != host:
            continue
        if container and c.get("name") != container:
            continue
        if stack and project != stack:
            continue
        target.append(c)
    if not target:
        label = container or stack or host
        fail(f"Aucune cible trouvée dans le snapshot: {label}")

    target_ids = {_diagnostic_identity(c) for c in target}
    target_hosts = {c.get("host") for c in target}
    target_projects = {(c.get("compose") or {}).get("project") or "sans-compose" for c in target}

    recent = []
    for event in events.get("events", []):
        ts = int(event.get("timestamp") or 0)
        if ts < cutoff:
            continue
        event_host = event.get("host")
        if event_host not in target_hosts:
            continue
        identity = _report_identity(event.get("alert") or event.get("after") or event.get("before") or event)
        compose = _event_compose(event)
        project = compose.get("project") or "sans-compose"
        name = _event_container_name(event)
        matches_target = identity in target_ids
        if not matches_target and stack and project in target_projects:
            matches_target = True
        if not matches_target and container and name == container and event_host == host:
            matches_target = True
        if matches_target or (host and not container and not stack and event_host == host):
            recent.append(event)

    problem_events = [e for e in recent if e.get("type") == "problem_started"]
    state_events = [e for e in recent if e.get("type") in {"state_changed", "problem_started", "recovered"}]

    related = []
    for event in problem_events:
        name = _event_container_name(event)
        compose = _event_compose(event)
        project = compose.get("project") or "sans-compose"
        ts = int(event.get("timestamp") or 0)
        if container and name == container:
            continue
        same_host = event.get("host") in target_hosts
        same_stack = project in target_projects
        if same_host and (same_stack or not stack):
            related.append({
                "host": event.get("host"), "container": name,
                "stack": project, "service": compose.get("service") or "sans-service",
                "classification": (event.get("alert") or event.get("after") or {}).get("classification") or event.get("classification"),
                "timestamp": ts, "delta_seconds": max(0, now - ts),
                "relation": "same_stack" if same_stack else "same_host",
            })

    evidence = []
    hypotheses = []
    if len(target) > 1 and any(c.get("classification") in PROBLEM_STATES for c in target):
        evidence.append("Plusieurs containers de la cible sont actuellement problématiques.")
    if related:
        same_stack_count = sum(1 for x in related if x["relation"] == "same_stack")
        if same_stack_count:
            evidence.append(f"{same_stack_count} autre(s) problème(s) ont été détectés dans la même stack pendant la fenêtre.")
            hypotheses.append({"type": "stack_correlation", "confidence": "medium", "message": "Plusieurs problèmes temporellement proches dans la même stack ; vérifier les changements communs de cette stack."})
        else:
            evidence.append(f"{len(related)} autre(s) problème(s) ont été détectés sur le même hôte pendant la fenêtre.")
            hypotheses.append({"type": "host_correlation", "confidence": "low", "message": "Plusieurs problèmes temporellement proches sur le même hôte ; vérifier les ressources ou événements communs."})

    additions = [e for e in recent if e.get("type") == "added"]
    removals = [e for e in recent if e.get("type") == "removed"]
    if additions:
        evidence.append(f"{len(additions)} ajout(s) de container observé(s) dans la fenêtre.")
        hypotheses.append({"type": "recent_container_change", "confidence": "low", "message": "Un changement de cycle de vie de container précède ou accompagne la période ; vérifier les déploiements/recréations."})
    if removals:
        evidence.append(f"{len(removals)} suppression(s) de container observée(s) dans la fenêtre.")

    active = [c for c in target if c.get("classification") in PROBLEM_STATES]
    diagnostic_logs = {}
    if active:
        for c in active:
            diagnostic_logs[_diagnostic_identity(c)] = collect_container_logs(config, c)
            if c.get("classification") == "unhealthy":
                hc = c.get("healthcheck") or {}
                streak = hc.get("failing_streak")
                logs = hc.get("log") or []
                last = logs[-1] if logs else {}
                last_exit = last.get("exit_code")
                output = " ".join(str(last.get("output") or "").split())
                details = []
                if streak is not None:
                    details.append(f"{streak} échec(s) consécutif(s)")
                if last_exit is not None:
                    details.append(f"code {last_exit}")
                if output:
                    details.append(f"dernier résultat: {output[:500]}")
                if details:
                    message = f"Le healthcheck de {c.get('name')} est en échec — " + "; ".join(details) + "."
                    confidence = "high" if last_exit is not None or output else "medium"
                else:
                    message = f"Le healthcheck de {c.get('name')} est en échec ; consulter la définition du healthcheck."
                    confidence = "medium"
                log_data = diagnostic_logs.get(_diagnostic_identity(c)) or {}
                log_lines = log_data.get("lines") or []
                if log_lines:
                    evidence.append(f"{len(log_lines)} ligne(s) de logs récentes récupérées pour {c.get('name')}.")
                    tail_text = " | ".join(x.strip() for x in log_lines[-12:] if x.strip())
                    if tail_text:
                        hypotheses.append({"type": "container_logs", "confidence": "medium", "message": f"Logs récents de {c.get('name')} : {tail_text[:1500]}"})
                elif log_data.get("error"):
                    evidence.append(f"Les logs de {c.get('name')} n'ont pas pu être récupérés : {log_data['error']}")
                hypotheses.append({"type": "healthcheck_failure", "confidence": confidence, "message": message})
            elif c.get("classification") == "restarting":
                hypotheses.append({"type": "restart_loop", "confidence": "medium", "message": f"{c.get('name')} est actuellement en redémarrage ; vérifier le code de sortie et les logs du service."})
            elif c.get("classification") == "exited_error":
                hypotheses.append({"type": "exit_error", "confidence": "medium", "message": f"{c.get('name')} est arrêté avec une erreur ; vérifier le code de sortie et les logs."})

    for c in active:
        log_data = diagnostic_logs.get(_diagnostic_identity(c)) or {}
        log_lines = log_data.get("lines") or []
        if log_lines:
            tail_text = " | ".join(x.strip() for x in log_lines[-8:] if x.strip())
            if tail_text:
                evidence.append(f"Logs récents de {c.get('name')} : {tail_text[:1200]}")
        elif log_data.get("error"):
            evidence.append(f"Logs de {c.get('name')} indisponibles : {log_data['error']}")

    if not hypotheses:
        evidence.append("Aucune corrélation significative n'est visible dans les données persistées sur cette fenêtre.")

    return {
        "version": VERSION, "mode": "diagnose", "timestamp": now,
        "window_seconds": window_seconds, "snapshot": str(snapshot_path), "events_file": events_path,
        "target": {"host": host, "container": container, "stack": stack},
        "containers": len(target), "active_problems": len(active),
        "recent_events": len(recent), "related_problems": related[:20],
        "evidence": evidence, "hypotheses": hypotheses,
        "logs": diagnostic_logs,
        "root_cause_claimed": False,
        "limitations": [
            "Les corrélations sont basées sur les snapshots et événements connus de Hermes.",
            "Pour les containers problématiques ciblés, un nombre limité de logs Docker récents est lu via l'API Docker en lecture seule ; les logs complets ne sont pas récupérés.",
            "Les logs sont des éléments de preuve à vérifier et ne démontrent pas automatiquement une cause racine.",
            "Une corrélation temporelle ne constitue pas une preuve de causalité.",
        ],
    }


def human_diagnose(data):
    target = data["target"]
    label = target.get("container") or target.get("stack") or target.get("host") or "cible"
    lines = [
        "Docker Health — Diagnostic assisté",
        "=================================",
        f"Cible: {label}",
        f"Fenêtre: {format_duration(data['window_seconds'])}",
        f"Problèmes actifs: {data['active_problems']}",
        "",
        "Observations:",
    ]
    for item in data["evidence"]:
        lines.append(f"• {item}")
    lines.append("")
    lines.append("Pistes à vérifier:")
    if data["hypotheses"]:
        for h in data["hypotheses"]:
            lines.append(f"• [{h['confidence'].upper()}] {h['message']}")
    else:
        lines.append("• Aucune piste significative à partir des données disponibles.")
    lines.append("")
    lines.append("Important: corrélation observée ≠ cause racine démontrée.")
    return "\n".join(lines)

def human_trends(data):
    lines=["Docker Health — Trends", "======================", f"Période: {format_duration(data['window_seconds'])}", f"Nouveaux incidents: {data['problem_starts']} | Rétablissements: {data['recoveries']} | Solde: {data['net_change']}", "", "Par jour:"]
    for day,vals in data["by_day"].items(): lines.append(f"• {day} → +{vals['problem_started']} / -{vals['recovered']}")
    if data["problem_starts_by_host"]:
        lines.append("\nNouveaux incidents par hôte:")
        for host,count in data["problem_starts_by_host"].items(): lines.append(f"• {host} → {count}")
    return "\n".join(lines)

DEFAULT_SNAPSHOT_PATH = "/opt/data/.hermes/data/docker-health.json"
DEFAULT_ALERT_STATE_PATH = "/opt/data/.hermes/data/docker-health.alerts.json"
DEFAULT_MONITOR_INTERVAL = 300
DEFAULT_NTFY_COOLDOWN = 900
DEFAULT_NTFY_STATE_PATH = "/opt/data/.hermes/data/docker-health.ntfy.json"
DEFAULT_NTFY_HISTORY_PATH = "/opt/data/.hermes/data/docker-health.ntfy-history.json"
DEFAULT_NTFY_HISTORY_LIMIT = 500
DEFAULT_ACK_STATE_PATH = "/opt/data/.hermes/data/docker-health.acks.json"
DEFAULT_SILENCE_STATE_PATH = "/opt/data/.hermes/data/docker-health.silences.json"


def correlate_alerts(alerts):
    """Group active alerts by host/Compose stack and expose service-level context."""
    groups = {}
    for alert in alerts:
        compose = alert.get("compose") or {}
        host = alert.get("host") or "unknown-host"
        project = compose.get("project") or "sans-compose"
        key = (host, project)
        group = groups.setdefault(key, {"host": host, "project": project, "alert_count": 0, "services": {}, "classifications": {}, "containers": []})
        group["alert_count"] += 1
        service = compose.get("service") or "sans-service"
        group["services"][service] = group["services"].get(service, 0) + 1
        classification = alert.get("classification") or "unknown"
        group["classifications"][classification] = group["classifications"].get(classification, 0) + 1
        group["containers"].append({"name": alert.get("name"), "service": service, "classification": classification, "severity": alert.get("severity")})
    result = list(groups.values())
    for group in result:
        group["services"] = [{"name": name, "alerts": count} for name, count in sorted(group["services"].items())]
        group["containers"].sort(key=lambda x: (x.get("service") or "", x.get("name") or ""))
    result.sort(key=lambda x: (-x["alert_count"], x["host"], x["project"]))
    return result



def ack_state_path(config):
    options = config.get("options") or {}
    return str(options.get("ack_state_file") or DEFAULT_ACK_STATE_PATH)


def load_ack_state(path):
    target = Path(path)
    if not target.exists():
        return {"version": 1, "acks": {}}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        fail(f"État des acquittements invalide: {target}: {exc}")
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("acks"), dict):
        fail(f"État des acquittements non supporté: {target}")
    return data


def save_ack_state(path, data):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)


def acknowledge_alert(config, host, container, snapshot_path=DEFAULT_SNAPSHOT_PATH, reason=""):
    snapshot = load_snapshot(Path(snapshot_path))
    current = snapshot.get("containers", {})
    matches = [x for x in current.values() if x.get("host") == host and x.get("name") == container and x.get("classification") in PROBLEM_STATES]
    if not matches:
        fail(f"Aucune alerte active trouvée pour {host}/{container} dans {snapshot_path}")
    alert = matches[0]
    identity = alert_identity(alert)
    path = ack_state_path(config)
    state = load_ack_state(path)
    now = int(time.time())
    state["acks"][identity] = {
        "host": host,
        "container": container,
        "container_id": alert.get("id"),
        "classification": alert.get("classification"),
        "timestamp": now,
        "reason": reason,
    }
    save_ack_state(path, state)
    return state["acks"][identity]


def unacknowledge_alert(config, host, container):
    path = ack_state_path(config)
    state = load_ack_state(path)
    removed = False
    for key, value in list(state["acks"].items()):
        if value.get("host") == host and value.get("container") == container:
            del state["acks"][key]
            removed = True
    if removed:
        save_ack_state(path, state)
    return removed


def list_acknowledged(config, snapshot_path=DEFAULT_SNAPSHOT_PATH):
    state = load_ack_state(ack_state_path(config))
    snapshot = load_snapshot(Path(snapshot_path)) if Path(snapshot_path).exists() else {"containers": {}}
    current = {alert_identity(x): x for x in snapshot.get("containers", {}).values()}
    result = []
    for identity, ack in state.get("acks", {}).items():
        item = dict(ack)
        item["identity"] = identity
        item["active"] = identity in current and current[identity].get("classification") in PROBLEM_STATES
        if identity in current:
            item["current_classification"] = current[identity].get("classification")
        result.append(item)
    result.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    return result


def apply_acknowledgements(config, alerts):
    state = load_ack_state(ack_state_path(config))
    acks = state.get("acks", {})
    result = []
    for alert in alerts:
        item = dict(alert)
        identity = alert_identity(alert)
        ack = acks.get(identity)
        item["acknowledged"] = bool(ack)
        if ack:
            item["acknowledged_at"] = ack.get("timestamp")
            item["acknowledged_reason"] = ack.get("reason", "")
            if ack.get("classification") != alert.get("classification"):
                item["acknowledged"] = False
        result.append(item)
    return result



def silence_state_path(config):
    options = config.get("options") or {}
    return str(options.get("silence_state_file") or DEFAULT_SILENCE_STATE_PATH)


def load_silence_state(path):
    target = Path(path)
    if not target.exists():
        return {"version": 1, "silences": {}}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        fail(f"État des silences invalide: {target}: {exc}")
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("silences"), dict):
        fail(f"État des silences non supporté: {target}")
    return data


def save_silence_state(path, data):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)


def cleanup_expired_silences(config, state=None, now=None):
    path = silence_state_path(config)
    state = state or load_silence_state(path)
    now = int(time.time() if now is None else now)
    changed = False
    for key, value in list(state.get("silences", {}).items()):
        if int(value.get("until", 0)) <= now:
            del state["silences"][key]
            changed = True
    if changed:
        save_silence_state(path, state)
    return state


def silence_identity(host, container_id=None, container_name=None):
    return f"{host}::{container_id or container_name}"


def silence_alert(config, host, container, duration, reason="", snapshot_path=DEFAULT_SNAPSHOT_PATH):
    if int(duration) <= 0:
        fail("La durée du silence doit être supérieure à 0 seconde")
    snapshot = load_snapshot(Path(snapshot_path))
    matches = [x for x in snapshot.get("containers", {}).values() if x.get("host") == host and x.get("name") == container and x.get("classification") in PROBLEM_STATES]
    if not matches:
        fail(f"Aucun problème actif trouvé pour {host}/{container} dans {snapshot_path}")
    alert = matches[0]
    now = int(time.time())
    entry = {
        "host": host,
        "container": container,
        "container_id": alert.get("id"),
        "timestamp": now,
        "until": now + int(duration),
        "reason": reason,
        "classification": alert.get("classification"),
    }
    path = silence_state_path(config)
    state = cleanup_expired_silences(config, now=now)
    state["silences"][silence_identity(host, alert.get("id"), container)] = entry
    save_silence_state(path, state)
    return entry


def unsilence_alert(config, host, container):
    path = silence_state_path(config)
    state = cleanup_expired_silences(config)
    removed = False
    for key, value in list(state.get("silences", {}).items()):
        if value.get("host") == host and value.get("container") == container:
            del state["silences"][key]
            removed = True
    if removed:
        save_silence_state(path, state)
    return removed


def list_silences(config):
    state = cleanup_expired_silences(config)
    now = int(time.time())
    result = []
    for identity, item in state.get("silences", {}).items():
        entry = dict(item)
        entry["identity"] = identity
        entry["remaining_seconds"] = max(0, int(entry.get("until", 0)) - now)
        result.append(entry)
    result.sort(key=lambda x: x.get("until", 0))
    return result


def apply_silences(config, notifications, now=None):
    if not notifications:
        return [], 0
    state = cleanup_expired_silences(config, now=now)
    silences = state.get("silences", {})
    selected = []
    suppressed = 0
    for item in notifications:
        alert = item.get("alert") or item.get("after") or {}
        identity = alert_identity(alert)
        name_identity = silence_identity(alert.get("host"), None, alert.get("name"))
        silence = silences.get(identity) or silences.get(name_identity)
        if silence:
            suppressed += 1
            continue
        selected.append(item)
    return selected, suppressed


def ntfy_config(config):
    options = config.get("options") or {}
    ntfy = config.get("ntfy") or {}
    return {
        "enabled": bool(ntfy.get("enabled", False)),
        "url": os.environ.get("NTFY_ENDPOINT", "").rstrip("/"),
        "topic": str(ntfy.get("topic") or "").strip(),
        "token_env": "NTFY_TOKEN",
        "timeout_seconds": int(ntfy.get("timeout_seconds") or options.get("timeout_seconds") or 10),
        "cooldown_seconds": int(ntfy.get("cooldown_seconds", DEFAULT_NTFY_COOLDOWN)),
        "state_file": str(options.get("ntfy_state_file") or DEFAULT_NTFY_STATE_PATH),
        "history_file": str(options.get("ntfy_history_file") or DEFAULT_NTFY_HISTORY_PATH),
        "history_limit": int(ntfy.get("history_limit", DEFAULT_NTFY_HISTORY_LIMIT)),
    }


def ntfy_priority(severity):
    return {
        "critical": "urgent",
        "warning": "high",
        "recovered": "default",
        "batch": "urgent",
    }.get(severity, "default")


def ntfy_event_text(item):
    event_type = item.get("type")
    alert = item.get("alert") or item.get("after") or {}
    host = alert.get("host") or "unknown-host"
    name = alert.get("name") or "unknown-container"
    classification = alert.get("classification") or "unknown"
    compose = alert.get("compose") or {}
    project = compose.get("project") or "sans-compose"
    service = compose.get("service") or "sans-service"

    if event_type == "alert_started":
        title = f"[CRITICAL] Docker / {host} / {name}" if item.get("severity") == "critical" else f"[WARNING] Docker / {host} / {name}"
        message = alert.get("message") or f"Alerte Docker: {classification}"
    elif event_type == "alert_changed":
        title = f"[CHANGED] Docker / {host} / {name}"
        before = (item.get("before") or {}).get("classification") or "unknown"
        message = f"État changé: {before} → {classification}"
    else:
        title = f"[RECOVERED] Docker / {host} / {name}"
        message = "Problème résolu"

    lines = [message, f"Hôte: {host}", f"Container: {name}", f"Stack: {project}", f"Service: {service}"]
    lines.append(f"État: {classification}")
    duration = alert.get("duration_seconds")
    if duration is not None:
        lines.append(f"Durée: {format_duration(duration)}")
    return title, "\n".join(lines)


def ntfy_load_state(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def ntfy_save_state(path, data):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)


def ntfy_filter_notifications(config, notifications, now=None):
    """Group one cycle and suppress repeated non-recovery notifications during cooldown."""
    cfg = ntfy_config(config)
    now = int(now or time.time())
    state_path = Path(cfg["state_file"])
    state = ntfy_load_state(state_path)
    last_sent = int(state.get("last_alert_notification_at") or 0)

    recoveries = [n for n in notifications if n.get("type") == "alert_recovered"]
    active = [n for n in notifications if n.get("type") in {"alert_started", "alert_changed"}]

    if not active:
        selected = recoveries
    elif cfg["cooldown_seconds"] > 0 and last_sent and now - last_sent < cfg["cooldown_seconds"]:
        selected = recoveries
    else:
        selected = active + recoveries

    return selected, state, state_path, now


def ntfy_build_batch(items):
    if len(items) == 1:
        title, message = ntfy_event_text(items[0])
        return title, message, ntfy_priority(items[0].get("severity"))

    critical = sum(1 for i in items if i.get("severity") == "critical")
    recovered = sum(1 for i in items if i.get("type") == "alert_recovered")
    priority = "urgent" if critical else ("default" if recovered == len(items) else "high")
    started = sum(1 for i in items if i.get("type") == "alert_started")
    changed = sum(1 for i in items if i.get("type") == "alert_changed")

    groups = {}
    for item in items:
        alert = item.get("alert") or item.get("after") or {}
        compose = alert.get("compose") or {}
        key = (alert.get("host") or "unknown-host", compose.get("project") or "sans-compose")
        groups.setdefault(key, []).append(item)

    parts = []
    if started: parts.append(f"{started} nouvelle(s)")
    if changed: parts.append(f"{changed} changement(s)")
    if recovered: parts.append(f"{recovered} récupération(s)")
    lines = [f"{len(items)} événement(s) Docker — " + ", ".join(parts)]
    for (host, project), group_items in sorted(groups.items()):
        lines.append("")
        lines.append(f"STACK {host}/{project} — {len(group_items)} problème(s)")
        for item in group_items:
            alert = item.get("alert") or item.get("after") or {}
            compose = alert.get("compose") or {}
            service = compose.get("service") or "sans-service"
            name = alert.get("name") or "unknown-container"
            classification = alert.get("classification") or "unknown"
            severity = item.get("severity") or "recovered"
            lines.append(f"• {service}/{name} — {classification} [{severity}]")
    return "[DOCKER HEALTH] " + ", ".join(parts), "\n".join(lines), priority


def ntfy_load_history(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError, TypeError):
        return []


def ntfy_save_history(path, history, limit):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    history = history[-max(1, int(limit)):]
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)


def ntfy_history_record(cfg, selected, title, priority, now):
    history = ntfy_load_history(cfg["history_file"])
    alerts = []
    for item in selected:
        alert = item.get("alert") or item.get("after") or {}
        compose = alert.get("compose") or {}
        alerts.append({
            "type": item.get("type"),
            "severity": item.get("severity"),
            "host": alert.get("host"),
            "container": alert.get("name"),
            "classification": alert.get("classification"),
            "stack": compose.get("project") or "sans-compose",
            "service": compose.get("service") or "sans-service",
        })
    entry = {
        "timestamp": int(now),
        "title": title,
        "priority": priority,
        "count": len(selected),
        "alerts": alerts,
    }
    history.append(entry)
    ntfy_save_history(cfg["history_file"], history, cfg["history_limit"])


def format_ntfy_history_entry(entry):
    ts = entry.get("timestamp")
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "unknown-time"
    return {**entry, "datetime": when}


def notification_history(config, limit=50):
    cfg = ntfy_config(config)
    history = ntfy_load_history(cfg["history_file"])
    return [format_ntfy_history_entry(x) for x in reversed(history[-max(1, int(limit)):])]


def send_ntfy(config, notifications, now=None):
    cfg = ntfy_config(config)
    result = {
        "enabled": cfg["enabled"],
        "sent": 0,
        "failed": 0,
        "skipped": 0,
        "suppressed": 0,
        "batched": 0,
        "errors": [],
    }
    if not notifications:
        return result
    if not cfg["enabled"]:
        result["skipped"] = len(notifications)
        return result

    selected, state, state_path, now = ntfy_filter_notifications(config, notifications, now=now)
    result["suppressed"] = len(notifications) - len(selected)
    if not selected:
        return result

    token = os.environ.get(cfg["token_env"])
    if not token:
        result["failed"] = 1
        result["errors"].append(f"Variable d'environnement ntfy absente: {cfg['token_env']}")
        return result
    if not cfg["url"] or not cfg["topic"]:
        result["failed"] = 1
        result["errors"].append("Configuration ntfy incomplète: endpoint ou topic absent")
        return result

    endpoint = f"{cfg['url']}/{urllib.parse.quote(cfg['topic'], safe='')}"
    title, message, priority = ntfy_build_batch(selected)
    headers = {
        "Authorization": f"Bearer {token}",
        "Title": title,
        "Priority": priority,
        "Tags": "docker_health",
        "Content-Type": "text/plain; charset=utf-8",
    }
    request = urllib.request.Request(endpoint, data=message.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=cfg["timeout_seconds"]) as response:
            if 200 <= response.status < 300:
                result["sent"] = 1
                result["batched"] = len(selected)
                if any(i.get("type") in {"alert_started", "alert_changed"} for i in selected):
                    state["last_alert_notification_at"] = now
                    ntfy_save_state(state_path, state)
                ntfy_history_record(cfg, selected, title, priority, now)
            else:
                result["failed"] = 1
                result["errors"].append(f"ntfy HTTP {response.status}")
    except Exception as exc:
        result["failed"] = 1
        result["errors"].append(str(exc))
    return result

def monitor_once(config, snapshot_path, events_path, alert_state_path, min_duration):
    """Run one monitoring cycle and persist state/history atomically."""
    previous = None
    snapshot_file = Path(snapshot_path)
    if snapshot_file.exists():
        previous = load_snapshot(snapshot_file)

    data = run_check(config)
    current = build_snapshot(data, previous=previous)

    changes_data = {
        "version": VERSION,
        "timestamp": int(current.get("timestamp") or time.time()),
        "previous_snapshot": str(snapshot_file) if previous else None,
        "changes_count": 0,
        "added": 0,
        "removed": 0,
        "state_changes": 0,
        "restart_state_changes": 0,
        "changes": [],
    }
    if previous is not None:
        changes_data = compare_snapshots(previous, current)
        changes_data["version"] = VERSION
        changes_data["previous_snapshot"] = str(snapshot_file)
        append_events(Path(events_path), changes_data)

    save_snapshot_from_built(snapshot_file, current)

    threshold = parse_duration(min_duration)
    now, alerts = build_alerts(current, threshold)
    alerts = apply_alert_rules(alerts, normalize_alert_rules(config))
    state_path = Path(alert_state_path)
    previous_state = load_alert_state(state_path)
    new_alerts, resolved_alerts, changed_alerts, current_state = alert_diff(previous_state, alerts)
    save_alert_state(state_path, {"version": 1, "timestamp": now, "alerts": current_state})

    new_alerts = apply_acknowledgements(config, new_alerts)
    changed_alerts = [{"before": item["before"], "after": apply_acknowledgements(config, [item["after"]])[0]} for item in changed_alerts]
    notifications = []
    for alert in new_alerts:
        if not alert.get("acknowledged"):
            notifications.append({"type": "alert_started", "severity": alert["severity"], "alert": alert})
    for item in changed_alerts:
        if not item["after"].get("acknowledged"):
            notifications.append({"type": "alert_changed", "severity": item["after"]["severity"], "before": item["before"], "alert": item["after"]})
    ack_path = ack_state_path(config)
    ack_state = load_ack_state(ack_path)
    for alert in resolved_alerts:
        ack_state.get("acks", {}).pop(alert_identity(alert), None)
        notifications.append({"type": "alert_recovered", "severity": "recovered", "alert": alert})
    save_ack_state(ack_path, ack_state)

    notifications, silenced_count = apply_silences(config, notifications, now=now)
    ntfy_result = send_ntfy(config, notifications, now=now)
    correlation = correlate_alerts(alerts)

    return {
        "version": VERSION,
        "timestamp": now,
        "snapshot": str(snapshot_file),
        "events_file": str(events_path),
        "alert_state_file": str(state_path),
        "summary": current.get("summary", {}),
        "changes": changes_data,
        "active_alerts": alerts,
        "correlation": correlation,
        "new_alerts": new_alerts,
        "changed_alerts": changed_alerts,
        "resolved_alerts": resolved_alerts,
        "notification_count": len(notifications),
        "silenced_notification_count": silenced_count,
        "notifications": notifications,
        "ntfy": ntfy_result,
    }


def human_monitor_cycle(result):
    lines = [
        "Docker Health — Monitor",
        "=======================",
        f"Containers: {result['summary'].get('containers', 0)} | Problèmes: {result['summary'].get('problems', 0)}",
        f"Alertes actives: {len(result['active_alerts'])} | Notifications: {result['notification_count']}",
    ]
    changes = result.get("changes") or {}
    if changes.get("changes_count"):
        lines.append(
            f"Changements: {changes['changes_count']} "
            f"({changes.get('state_changes', 0)} états, {changes.get('added', 0)} ajoutés, {changes.get('removed', 0)} supprimés)."
        )
    for item in result["notifications"]:
        if item["type"] == "alert_started":
            a = item["alert"]
            lines.append(f"• [NEW/{a['severity'].upper()}] {a['host']}/{a['name']} — {a['message']}")
        elif item["type"] == "alert_changed":
            a = item["alert"]
            b = item["before"]
            lines.append(f"• [CHANGED/{a['severity'].upper()}] {a['host']}/{a['name']} — {b.get('classification')} → {a.get('classification')}")
        else:
            a = item["alert"]
            lines.append(f"• [RECOVERED] {a.get('host')}/{a.get('name')} — problème résolu")
    if not result["notifications"]:
        lines.append("Aucune nouvelle notification.")
    ntfy = result.get("ntfy") or {}
    if ntfy.get("enabled"):
        lines.append(f"ntfy: {ntfy.get('sent', 0)} envoyée(s), {ntfy.get('failed', 0)} échec(s).")
    return "\n".join(lines)

def main():
    parser = argparse.ArgumentParser(description="Check Docker container health across Hermes hosts")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("check", "problems", "no-healthcheck", "summary", "host-summary", "snapshot", "changes", "persistent-problems", "events", "alerts", "stats", "stack-summary", "stack-problems", "healthcheck-audit", "notification-history", "ack", "unack", "acknowledged", "silence", "unsilence", "silences", "agent", "monitor", "report", "host-report", "stack-report", "container-report", "trends", "diagnose"):
        command_parser = sub.add_parser(command)
        command_parser.add_argument("--host", help="Nom d'un hôte uniquement")
        if command == "stack-report":
            command_parser.add_argument("--stack", required=True, help="Nom du projet Compose")
        if command == "container-report":
            command_parser.add_argument("--container", required=True, help="Nom du container")
        if command == "diagnose":
            command_parser.add_argument("--container", help="Nom du container cible")
            command_parser.add_argument("--stack", help="Nom du projet Compose cible")
            command_parser.add_argument("--snapshot", default=DEFAULT_SNAPSHOT_PATH, help="Snapshot persistant")
            command_parser.add_argument("--events-file", help="Historique des événements; par défaut: <snapshot>.events.json")
            command_parser.add_argument("--since", default="30m", help="Fenêtre de corrélation, ex. 15m, 30m, 2h")
        if command in {"host-report", "stack-report", "container-report", "trends"}:
            command_parser.add_argument("--snapshot", default=DEFAULT_SNAPSHOT_PATH, help="Snapshot persistant")
            command_parser.add_argument("--since", default=("7d" if command in {"container-report", "trends"} else "24h"), help="Fenêtre, ex. 24h, 7d")
        if command == "notification-history":
            command_parser.add_argument("--limit", type=int, default=50, help="Nombre d'envois à afficher")
        if command in {"ack", "unack"}:
            command_parser.add_argument("--container", required=True, help="Nom du container")
            command_parser.add_argument("--reason", default="", help="Motif de l'acquittement")
        if command == "acknowledged":
            command_parser.add_argument("--snapshot", default=DEFAULT_SNAPSHOT_PATH, help="Snapshot courant")
        if command in {"silence", "unsilence"}:
            command_parser.add_argument("--container", required=True, help="Nom du container")
            command_parser.add_argument("--reason", default="", help="Motif du silence")
        if command == "silence":
            command_parser.add_argument("--duration", required=True, help="Durée, ex. 30m, 2h, 1d")
        command_parser.add_argument("--human", action="store_true", help="Sortie lisible")
        if command in {"snapshot", "changes"}:
            command_parser.add_argument("--file", required=True, help="Fichier snapshot JSON")
        if command == "changes":
            command_parser.add_argument("--previous", required=True, help="Ancien snapshot JSON")
        if command == "persistent-problems":
            command_parser.add_argument("--file", required=True, help="Snapshot JSON courant")
            command_parser.add_argument("--min-duration", default="30m", help="Seuil de durée, ex. 30m, 2h, 1d")
        if command == "events":
            command_parser.add_argument("--file", required=True, help="Historique des événements JSON")
            command_parser.add_argument("--limit", type=int, default=50, help="Nombre maximum d'événements à afficher")
        if command == "alerts":
            command_parser.add_argument("--file", required=True, help="Snapshot JSON courant")
            command_parser.add_argument("--min-duration", default="30m", help="Seuil de durée, ex. 30m, 2h, 1d")
            command_parser.add_argument("--state-file", help="État persistant des alertes; par défaut: <snapshot>.alerts.json")
        if command == "stats":
            command_parser.add_argument("--file", required=True, help="Snapshot JSON courant")
            command_parser.add_argument("--events-file", help="Historique des événements; par défaut: <snapshot>.events.json")
        if command == "agent":
            command_parser.add_argument("--previous", help="Snapshot précédent JSON pour calculer les changements")
            command_parser.add_argument("--min-duration", default="30m", help="Seuil des alertes persistantes")
        if command == "report":
            command_parser.add_argument("--snapshot", default=DEFAULT_SNAPSHOT_PATH, help="Snapshot courant")
            command_parser.add_argument("--events-file", help="Historique des événements; par défaut: <snapshot>.events.json")
            command_parser.add_argument("--window", default="24h", help="Fenêtre des nouveaux/résolus, ex. 6h, 24h, 7d")
        if command == "monitor":
            command_parser.add_argument("--snapshot", default=DEFAULT_SNAPSHOT_PATH, help="Snapshot persistant")
            command_parser.add_argument("--events-file", help="Historique des événements; par défaut: <snapshot>.events.json")
            command_parser.add_argument("--alert-state", default=DEFAULT_ALERT_STATE_PATH, help="État persistant des alertes")
            command_parser.add_argument("--min-duration", default=None, help="Seuil des alertes persistantes; par défaut config options.alert_min_duration ou 30m")
            command_parser.add_argument("--interval", type=int, default=None, help="Intervalle entre contrôles en secondes")
            command_parser.add_argument("--once", action="store_true", help="Effectuer un seul contrôle")
            command_parser.add_argument("--quiet", action="store_true", help="Afficher uniquement les notifications")

    args = parser.parse_args()
    config = load_config()

    if args.command == "report":
        events_file = args.events_file or str(Path(args.snapshot).with_suffix(".events.json"))
        result = build_report(config, args.snapshot, events_file, args.window, args.host)
        if args.human:
            print(human_report(result))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "ack":
        result = acknowledge_alert(config, args.host, args.container, reason=args.reason)
        if args.human:
            print(f"Alerte acquittée: {args.host}/{args.container}")
            if args.reason:
                print(f"Motif: {args.reason}")
        else:
            print(json.dumps({"version": VERSION, "acknowledged": result}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "unack":
        removed = unacknowledge_alert(config, args.host, args.container)
        if args.human:
            print(f"Acquittement {'supprimé' if removed else 'introuvable'}: {args.host}/{args.container}")
        else:
            print(json.dumps({"version": VERSION, "removed": removed, "host": args.host, "container": args.container}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "acknowledged":
        result = list_acknowledged(config, args.snapshot)
        if args.human:
            if not result:
                print("Aucun acquittement.")
            else:
                for item in result:
                    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item.get("timestamp", 0)))
                    state = "actif" if item.get("active") else "résolu/inactif"
                    print(f"{when} | {state} | {item.get('host')}/{item.get('container')} | {item.get('classification')} | {item.get('reason', '')}")
        else:
            print(json.dumps({"version": VERSION, "acknowledged": result}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "silence":
        duration = parse_duration(args.duration)
        result = silence_alert(config, args.host, args.container, duration, reason=args.reason)
        if args.human:
            until = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(result["until"]))
            print(f"Silence activé: {args.host}/{args.container} jusqu'à {until}")
            if args.reason:
                print(f"Motif: {args.reason}")
        else:
            print(json.dumps({"version": VERSION, "silence": result}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "unsilence":
        removed = unsilence_alert(config, args.host, args.container)
        if args.human:
            print(f"Silence {'supprimé' if removed else 'introuvable'}: {args.host}/{args.container}")
        else:
            print(json.dumps({"version": VERSION, "removed": removed, "host": args.host, "container": args.container}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "silences":
        result = list_silences(config)
        if args.human:
            if not result:
                print("Aucun silence actif.")
            else:
                for item in result:
                    until = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item.get("until", 0)))
                    print(f"jusqu'à {until} | {item.get('host')}/{item.get('container')} | {format_duration(item.get('remaining_seconds', 0))} restant | {item.get('reason', '')}")
        else:
            print(json.dumps({"version": VERSION, "silences": result}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "notification-history":
        limit = getattr(args, "limit", 50)
        history = notification_history(config, limit=limit)
        if getattr(args, "human", False):
            if not history:
                print("Aucun historique ntfy.")
            else:
                for entry in history:
                    print(f"{entry['datetime']} | {entry.get('priority', 'default')} | {entry.get('count', 0)} événement(s) | {entry.get('title', '')}")
                    for alert in entry.get("alerts", []):
                        print(f"  • {alert.get('host')}/{alert.get('container')} — {alert.get('classification')} [{alert.get('severity')}] — {alert.get('stack')}/{alert.get('service')}")
        else:
            print(json.dumps({"version": VERSION, "history": history}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "diagnose":
        try:
            result = build_diagnose(config, args.snapshot, args.events_file, args.host, args.container, args.stack, args.since)
        except ValueError as exc:
            fail(str(exc))
        if args.human:
            print(human_diagnose(result))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command in {"host-report", "stack-report", "container-report", "trends"}:
        try:
            if args.command == "host-report":
                result = build_host_report(config, args.snapshot, args.host, args.since)
                output = human_host_report(result) if args.human else json.dumps(result, ensure_ascii=False, indent=2)
            elif args.command == "stack-report":
                result = build_stack_report(config, args.snapshot, args.stack, args.host, args.since)
                output = human_stack_report(result) if args.human else json.dumps(result, ensure_ascii=False, indent=2)
            elif args.command == "container-report":
                result = build_container_report(config, args.snapshot, args.container, args.host, args.since)
                output = human_container_report(result) if args.human else json.dumps(result, ensure_ascii=False, indent=2)
            else:
                result = build_trends(config, args.snapshot, args.since, args.host)
                output = human_trends(result) if args.human else json.dumps(result, ensure_ascii=False, indent=2)
        except ValueError as exc:
            fail(str(exc))
        print(output)
        return

    if args.command == "monitor":
        options = config.get("options") or {}
        min_duration = args.min_duration or options.get("alert_min_duration") or "30m"
        interval = args.interval or int(options.get("monitor_interval_seconds") or DEFAULT_MONITOR_INTERVAL)
        if interval < 1:
            fail("--interval doit être supérieur ou égal à 1 seconde")
        events_file = args.events_file or str(Path(args.snapshot).with_suffix(".events.json"))
        try:
            parse_duration(min_duration)
        except ValueError as exc:
            fail(str(exc))

        while True:
            result = monitor_once(config, args.snapshot, events_file, args.alert_state, min_duration)
            if args.quiet:
                output = result["notifications"]
                if output:
                    print(json.dumps({"version": VERSION, "notifications": output}, ensure_ascii=False, indent=2), flush=True)
            elif args.human:
                print(human_monitor_cycle(result), flush=True)
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)

            if args.once:
                break
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                break
        return

    if args.command == "agent":
        try:
            result = build_agent_report(config, args.host, args.previous, args.min_duration)
        except ValueError as exc:
            fail(str(exc))
        if args.human:
            print(human_agent_report(result))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.command == "healthcheck-audit":
        data = run_check(config, args.host)
        containers = [c for h in data["hosts"] for c in h["containers"]]
        result = healthcheck_audit(containers)
        result["hosts_checked"] = len(data["hosts"])
        result["host_errors"] = [e for h in data["hosts"] for e in h["errors"]]
        if args.human:
            print(human_healthcheck_audit(result))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.command == "stats":
        snapshot = load_snapshot(args.file)
        events_path = Path(args.events_file) if args.events_file else Path(args.file).with_suffix(".events.json")
        history = load_events(str(events_path))
        data = stats_from_history(snapshot, history)
        data["events_file"] = str(events_path)
        if args.human:
            print(human_stats(data))
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    if args.command in {"stack-summary", "stack-problems"}:
        data = run_check(config, args.host)
        containers = [c for h in data["hosts"] for c in h["containers"]]
        if args.command == "stack-summary":
            result = {"version": VERSION, "stacks": group_by_stack(containers)}
            if args.human:
                lines=["Docker Health — Stacks", "======================"]
                for st in result["stacks"]:
                    name=st["project"] or "sans-compose"
                    lines.append(f"• {st['host']}/{name} — {st['containers']} containers, {st['problems']} problème(s)")
                    if st["services"]: lines.append(f"  Services: {', '.join(st['services'])}")
                print("\n".join(lines))
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            result = {"version": VERSION, "stacks": stack_problems(containers)}
            if args.human:
                lines=["Docker Health — Stack Problems", "=============================="]
                for st in result["stacks"]:
                    name=st["project"] or "sans-compose"
                    lines.append(f"• {st['host']}/{name} — {st['problems']} problème(s)")
                    for c in st["containers"]:
                        lines.append(f"  - {c['name']} [{c['service'] or 'sans-service'}] — {c['classification']}")
                print("\n".join(lines))
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.command == "alerts":
        snapshot = load_snapshot(args.file)
        try:
            threshold = parse_duration(args.min_duration)
        except ValueError as exc:
            fail(str(exc))
        now, alerts = build_alerts(snapshot, threshold)
        alerts = apply_alert_rules(alerts, normalize_alert_rules(config))
        state_path = Path(args.state_file) if args.state_file else Path(args.file).with_suffix(".alerts.json")
        previous_state = load_alert_state(state_path)
        new_alerts, resolved_alerts, changed_alerts, current = alert_diff(previous_state, alerts)
        save_alert_state(state_path, {"version": 1, "updated_at": now, "alerts": current})
        result = {
            "version": VERSION,
            "timestamp": now,
            "snapshot": args.file,
            "state_file": str(state_path),
            "min_duration_seconds": threshold,
            "active_alerts": alerts,
            "new_alerts": new_alerts,
            "resolved_alerts": resolved_alerts,
            "changed_alerts": changed_alerts,
            "notification_count": len(new_alerts) + len(changed_alerts) + len(resolved_alerts),
            "alert_count": len(alerts),
            "critical": sum(1 for a in alerts if a["severity"] == "critical"),
            "warning": sum(1 for a in alerts if a["severity"] == "warning"),
            "alert_rules": normalize_alert_rules(config),
        }
        if args.human:
            lines = ["Docker Health — Alerts", "======================", f"Seuil: {format_duration(threshold)}", f"Actives: {len(alerts)} | Nouvelles: {len(new_alerts)} | Rétablies: {len(resolved_alerts)} | Modifiées: {len(changed_alerts)}"]
            for a in new_alerts:
                lines.append(f"• [NEW/{a['severity'].upper()}] {a['host']}/{a['name']} — {a['message']}")
            for item in changed_alerts:
                a = item["after"]
                lines.append(f"• [CHANGED/{a['severity'].upper()}] {a['host']}/{a['name']} — {item['before'].get('classification')} → {a.get('classification')}")
            for a in resolved_alerts:
                lines.append(f"• [RECOVERED] {a.get('host')}/{a.get('name')} — problème résolu")
            if not new_alerts and not changed_alerts and not resolved_alerts:
                lines.append("Aucun nouvel événement d'alerte.")
            print("\n".join(lines))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.command == "events":
        history = load_events(args.file)
        limit = max(0, args.limit)
        events = history.get("events", [])[-limit:] if limit else []
        data = {"version": VERSION, "events_version": history.get("events_version", 1), "file": args.file, "events": events}
        if args.human:
            print(human_events(data))
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    if args.command == "persistent-problems":
        snapshot = load_snapshot(args.file)
        try:
            threshold = parse_duration(args.min_duration)
        except ValueError as exc:
            fail(str(exc))
        items = persistent_problems(snapshot, threshold)
        data = {"version": VERSION, "snapshot": args.file, "min_duration_seconds": threshold, "containers": items, "timestamp": int(snapshot.get("timestamp") or time.time())}
        if args.human:
            print(human_persistent(data))
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    data = run_check(config, args.host)

    if args.command == "problems":
        for host in data["hosts"]:
            host["containers"] = [c for c in host["containers"] if c["classification"] in PROBLEM_STATES]
    elif args.command == "no-healthcheck":
        for host in data["hosts"]:
            host["containers"] = [c for c in host["containers"] if c["classification"] == "no_healthcheck"]
    elif args.command == "summary":
        data = {"summary": data["summary"]}
    elif args.command == "host-summary":
        data = {"version": VERSION, "host_summaries": [summarize_host(h) for h in data["hosts"]]}

    if args.command == "snapshot":
        previous_snapshot = None
        snapshot_path = Path(args.file)
        if snapshot_path.exists():
            previous_snapshot = load_snapshot(snapshot_path)
        save_snapshot(args.file, data, previous=previous_snapshot)
        data = {"version": VERSION, "snapshot": args.file, "timestamp": int(time.time()), "containers": data["summary"]["containers"]}
    elif args.command == "changes":
        previous_snapshot = load_snapshot(args.previous)
        current_snapshot = build_snapshot(data, previous=previous_snapshot)
        changes_data = compare_snapshots(previous_snapshot, current_snapshot)
        events_path = Path(args.previous).with_suffix(".events.json")
        append_events(events_path, changes_data)
        changes_data["events_file"] = str(events_path)
        # Advance the same snapshot file atomically so repeated `changes` calls
        # measure the duration from the latest observed state.
        save_snapshot_from_built(args.previous, current_snapshot)
        data = changes_data

    if args.human:
        if args.command == "changes":
            print(human_changes(data))
        elif args.command == "snapshot":
            print(f"Snapshot enregistré : {args.file}")
        else:
            print(human(data, args.command))
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
