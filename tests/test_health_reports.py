"""Exercise reporting and CLI workflows against a persisted incident history."""
import io
import json
import sys
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import Mock

import pytest
import yaml


@pytest.fixture
def incident(health, health_config, monkeypatch, tmp_path):
    clock = [100000]
    monkeypatch.setattr(health.time, "time", lambda: clock[0])
    health_config["hosts"] += [
        {"name": "host-2", "url": "http://secondary.invalid", "mode": "remote"},
        {"name": "offline", "url": "http://offline.invalid", "mode": "remote"},
    ]
    health_config["options"]["max_parallel"] = 1
    def raw(name, state, status, project="media"):
        return {"Id": name.ljust(64, "0"), "Names": [f"/{name}"], "State": state,
                "Status": status, "Image": "example:1.0",
                "Labels": {"com.docker.compose.project": project,
                           "com.docker.compose.service": name} if project else {}}
    rows = [raw("web", "running", "Up (healthy)"),
            raw("worker", "running", "Up (healthy)"),
            raw("job", "exited", "Exited (7) 1 hour ago"),
            raw("legacy", "running", "Up 1 hour", None),
            raw("batch", "exited", "Exited (0) 1 hour ago"),
            raw("paused", "paused", "Up 1 hour (Paused)")]
    secondary = raw("web", "running", "Up (healthy)")
    secondary["Id"] = "secondary-web".ljust(64, "0")
    requests = []
    def api(request, timeout):
        requests.append(request)
        url = urlparse(request.full_url)
        assert request.get_method() == "GET"
        if url.hostname == "offline.invalid":
            raise OSError("host unavailable")
        if url.path.endswith("/logs"):
            return io.BytesIO(b"database connection refused\nretrying\n")
        if url.path == "/containers/json":
            value = [secondary] if url.hostname == "secondary.invalid" else rows
        else:
            value = {"State": {"Health": {"Status": "unhealthy", "FailingStreak": 5,
                     "Log": [{"ExitCode": 1, "Output": "connection refused"}]}},
                     "Config": {"Healthcheck": {"Test": ["CMD", "curl", "localhost"], "Retries": 3}}}
        return io.BytesIO(json.dumps(value).encode())
    monkeypatch.setattr(health.urllib.request, "urlopen", api)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(health_config))
    monkeypatch.setattr(health, "CONFIG_PATH", config_path)
    path = tmp_path / "snapshot.json"
    events = path.with_suffix(".events.json")
    alerts = path.with_suffix(".alerts.json")
    for now, status in [(100000, "healthy"), (100060, "unhealthy"),
                        (100120, "healthy"), (100180, "unhealthy"), (102000, "unhealthy")]:
        clock[0] = now
        rows[0]["Status"] = f"Up ({status})"
        if now >= 100060:
            rows[1].update(State="restarting", Status="Restarting (137) 5 seconds ago")
        health.monitor_once(health_config, path, events, alerts, "30m")
    requests.clear()
    return SimpleNamespace(config=health_config, path=path, events=events, alerts=alerts,
                           clock=clock, rows=rows, requests=requests)


def run_cli(health, monkeypatch, capsys, *args):
    monkeypatch.setattr(sys, "argv", ["docker-health.py", *map(str, args)])
    health.main()
    return capsys.readouterr().out


def test_reports_respect_host_and_time_window(health, incident):
    result = health.build_report(incident.config, incident.path, window="1h", host="host-1")
    assert result["active_problems"] == 4
    assert result["new_problems"] == 3  # Two web incidents and one restart loop.
    assert result["resolved_problems"] == 1
    assert result["by_host"] == {"host-1": 4}
    assert result["top_recurring"][0]["name"] == "web"
    assert result["healthcheck_coverage"] == 50.0
    recent = health.build_report(incident.config, incident.path, window="10m")
    assert recent["new_problems"] == 0
    assert recent["resolved_problems"] == 0
    assert incident.requests == []  # Reports use persisted data, including history.


def test_stack_report_separates_identical_names_on_different_hosts(health, incident):
    result = health.build_stack_report(incident.config, incident.path, "media", "host-2")
    assert result["containers"] == 1
    assert result["active_problems"] == 0
    assert result["events_in_window"] == 0
    assert result["services"]["web"]["classifications"] == {"healthy": 1}
    host = health.build_host_report(incident.config, incident.path, "host-1")
    assert host["containers"] == 6
    assert host["by_stack"] == {"media": 4}
    assert incident.requests == []


def test_container_report_accounts_for_recurrence_and_duration(health, incident):
    result = health.build_container_report(incident.config, incident.path, "web", "host-1")
    assert result["recurring"] is True
    assert result["problem_starts"] == 2
    assert result["recoveries"] == 1
    assert result["observed_problem_duration_seconds"] == 60 + 1820
    assert result["logs"]["lines"] == ["database connection refused", "retrying"]
    assert len(incident.requests) == 1
    assert "/web000000000/logs?" in incident.requests[0].full_url


def test_trends_and_stats_count_incidents_without_double_counting(health, incident):
    trends = health.build_trends(incident.config, incident.path, host="host-1")
    assert trends["problem_starts"] == 3
    assert trends["recoveries"] == 1
    assert trends["net_change"] == 2
    assert sum(day["problem_started"] for day in trends["by_day"].values()) == 3
    stats = health.stats_from_history(health.load_snapshot(incident.path), health.load_events(incident.events))
    web = next(row for row in stats["containers"] if row["name"] == "web")
    assert web["total_problem_duration_seconds"] == 60
    assert web["current_problem_duration_seconds"] == 1820
    assert web["average_problem_duration_seconds"] == 60
    assert stats["recurrent_containers"] == 1
    assert incident.requests == []


def test_diagnose_distinguishes_evidence_from_root_cause(health, incident):
    result = health.build_diagnose(incident.config, incident.path, host="host-1", stack="media", window="1h")
    assert result["root_cause_claimed"] is False
    assert result["active_problems"] == 4
    types = {item["type"] for item in result["hypotheses"]}
    assert {"healthcheck_failure", "container_logs", "restart_loop", "exit_error", "stack_correlation"} <= types
    assert any("connection refused" in item["message"] for item in result["hypotheses"])
    assert len(incident.requests) == 4
    assert all("/logs?" in request.full_url for request in incident.requests)
    assert all(item["host"] == "host-1" for item in result["related_problems"])
    assert {item["container"] for item in result["related_problems"]} == {"web", "worker"}
    assert {item["classification"] for item in result["related_problems"]} == {"unhealthy", "restarting"}


def test_diagnose_healthy_target_does_not_fetch_logs(health, incident):
    result = health.build_diagnose(incident.config, incident.path, host="host-2", container="web")
    assert result["active_problems"] == 0
    assert result["hypotheses"] == []
    assert result["logs"] == {}
    assert incident.requests == []


def test_diagnose_retains_healthcheck_evidence_if_logs_fail(health, incident, monkeypatch):
    monkeypatch.setattr(health.urllib.request, "urlopen", Mock(side_effect=OSError("logs unavailable")))
    result = health.build_diagnose(incident.config, incident.path, host="host-1", container="web")
    assert result["active_problems"] == 1
    assert any(item["type"] == "healthcheck_failure" for item in result["hypotheses"])
    assert any("logs unavailable" in entry for entry in result["evidence"])
    assert result["root_cause_claimed"] is False


@pytest.mark.parametrize("kwargs", [{}, {"container": "web"}, {"host": "unknown"},
                                    {"host": "host-1", "stack": "unknown"}])
def test_diagnose_rejects_missing_or_unknown_target(health, incident, kwargs):
    with pytest.raises(SystemExit) as error:
        health.build_diagnose(incident.config, incident.path, **kwargs)
    assert error.value.code == 1
    assert incident.requests == []


@pytest.mark.parametrize("command,extra,expected", [
    ("report", [], {"active_problems": 4, "resolved_problems": 1}),
    ("host-report", ["--host", "host-1"], {"containers": 6, "active_problems": 4}),
    ("stack-report", ["--stack", "media", "--host", "host-1"], {"containers": 5, "active_problems": 4}),
    ("container-report", ["--container", "web", "--host", "host-1"], {"problem_starts": 2, "recurring": True}),
    ("trends", [], {"problem_starts": 3, "recoveries": 1}),
    ("diagnose", ["--host", "host-1", "--since", "1h"], {"active_problems": 4, "root_cause_claimed": False}),
])
@pytest.mark.parametrize("human", [False, True], ids=["json", "human"])
def test_report_cli(health, incident, monkeypatch, capsys, command, extra, expected, human):
    output = run_cli(health, monkeypatch, capsys, command, "--snapshot", incident.path, *extra,
                     *(["--human"] if human else []))
    if human:
        assert "Docker Health" in output
        assert "host-1" in output or command == "container-report"
        if command in {"container-report", "diagnose"}:
            assert "connection refused" in output
    else:
        result = json.loads(output)
        for key, value in expected.items():
            assert result[key] == value


@pytest.mark.parametrize("command,expected", [
    ("summary", "Problems: 4"), ("host-summary", "offline — ERROR"),
    ("check", "connection refused"), ("problems", "worker"),
    ("no-healthcheck", "legacy"), ("stack-summary", "host-1/media"),
    ("stack-problems", "job"), ("healthcheck-audit", "66.67%"),
])
def test_live_human_cli_reports_real_counts(health, incident, monkeypatch, capsys, command, expected):
    output = run_cli(health, monkeypatch, capsys, command, "--human")
    assert expected in output
    if command == "no-healthcheck":
        assert "web —" not in output


def test_agent_cli_combines_alerts_healthchecks_and_changes(health, incident, monkeypatch, capsys):
    output = run_cli(health, monkeypatch, capsys, "agent", "--previous", incident.path)
    result = json.loads(output)
    assert result["problem_count"] == 4
    assert result["alert_count"] == 4
    assert result["healthcheck_audit"]["coverage_percent"] == 66.67
    assert result["summary"]["hosts_with_errors"] == 1
    assert result["changes"]["changes_count"] == 0
    human = run_cli(health, monkeypatch, capsys, "agent", "--previous", incident.path, "--human")
    assert "connection refused" in human
    assert "4 problème(s)" in human


@pytest.mark.parametrize("human", [False, True])
def test_history_cli(health, incident, monkeypatch, capsys, human):
    output = run_cli(health, monkeypatch, capsys, "stats", "--file", incident.path, *(["--human"] if human else []))
    if human:
        assert "recurrent" in output and "web" in output
    else:
        assert json.loads(output)["recurrent_containers"] == 1
    output = run_cli(health, monkeypatch, capsys, "events", "--file", incident.events, "--limit", 1,
                     *(["--human"] if human else []))
    if human:
        assert "web" in output
    else:
        assert len(json.loads(output)["events"]) == 1
    output = run_cli(health, monkeypatch, capsys, "persistent-problems", "--file", incident.path,
                     *(["--human"] if human else []))
    if human:
        assert "worker" in output
    else:
        assert len(json.loads(output)["containers"]) == 4


@pytest.mark.parametrize("human", [False, True])
def test_snapshot_and_changes_cli_advances_history(health, incident, monkeypatch, capsys, tmp_path, human):
    path = tmp_path / "manual.json"
    human_arg = ["--human"] if human else []
    output = run_cli(health, monkeypatch, capsys, "snapshot", "--file", path, *human_arg)
    assert str(path) in output
    assert len(health.load_snapshot(path)["containers"]) == 7
    incident.clock[0] += 60
    incident.rows[0]["Status"] = "Up (healthy)"
    output = run_cli(health, monkeypatch, capsys, "changes", "--file", path, "--previous", path, *human_arg)
    if human:
        assert "web" in output
    else:
        assert json.loads(output)["changes"][0]["recovered"] is True
    current = health.load_snapshot(path)
    assert next(c for c in current["containers"].values() if c["host"] == "host-1" and c["name"] == "web")["classification"] == "healthy"
    assert health.load_events(path.with_suffix(".events.json"))["events"][-1]["type"] == "recovered"


@pytest.mark.parametrize("mode", ["json", "human", "quiet"])
def test_monitor_once_cli_persists_without_looping(health, incident, monkeypatch, capsys, mode):
    args = {"json": [], "human": ["--human"], "quiet": ["--quiet"]}[mode]
    output = run_cli(health, monkeypatch, capsys, "monitor", "--once", "--snapshot", incident.path,
                     "--alert-state", incident.alerts, *args)
    if mode == "quiet":
        assert output == ""  # No new alert since the previous collection.
    elif mode == "human":
        assert "Docker Health" in output
    else:
        assert json.loads(output)["summary"]["problems"] == 4
    assert health.load_snapshot(incident.path)["timestamp"] == incident.clock[0]


def test_acknowledgements_and_silences_appear_in_reports_and_can_be_removed(health, incident):
    health.acknowledge_alert(incident.config, "host-1", "web", incident.path, "Investigating")
    health.silence_alert(incident.config, "host-1", "worker", 3600, "Maintenance", incident.path)
    report = health.build_report(incident.config, incident.path)
    assert report["acknowledged"] == 1
    assert report["silenced"] == 1
    assert health.list_acknowledged(incident.config, incident.path)[0]["active"] is True
    assert health.list_silences(incident.config)[0]["remaining_seconds"] == 3600
    assert health.unacknowledge_alert(incident.config, "host-1", "web") is True
    assert health.unacknowledge_alert(incident.config, "host-1", "web") is False
    assert health.unsilence_alert(incident.config, "host-1", "worker") is True
    assert health.unsilence_alert(incident.config, "host-1", "worker") is False
    assert health.list_silences(incident.config) == []
    assert health.list_acknowledged(incident.config, incident.path) == []


def test_lifecycle_events_keep_context_for_diagnosis(health, incident):
    incident.clock[0] += 60
    removed = incident.rows.pop(0)
    added = dict(removed, Id="replacement".ljust(64, "0"), Names=["/replacement"])
    incident.rows.append(added)
    health.monitor_once(incident.config, incident.path, incident.events, incident.alerts, "30m")
    history = health.load_events(incident.events)["events"]
    lifecycle = [event for event in history if event["type"] in {"added", "removed"}]
    assert {(event["type"], event["name"]) for event in lifecycle} == {
        ("added", "replacement"), ("removed", "web")}
    assert all(event["compose"]["project"] == "media" for event in lifecycle)
    result = health.build_diagnose(incident.config, incident.path, host="host-1", stack="media", window="1h")
    assert any(item["type"] == "recent_container_change" for item in result["hypotheses"])
    assert result["root_cause_claimed"] is False


def test_legacy_events_remain_readable_without_compose_context(health, incident):
    history = health.load_events(incident.events)
    for event in history["events"]:
        event.pop("compose", None)
        event.pop("classification", None)
    health.save_events(incident.events, history)
    result = health.build_diagnose(incident.config, incident.path, host="host-1", window="1h")
    assert result["recent_events"] == 4
    assert {item["container"] for item in result["related_problems"]} == {"web", "worker"}
    assert result["root_cause_claimed"] is False
