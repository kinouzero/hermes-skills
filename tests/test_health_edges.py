"""Fault handling, legacy state, and boundary cases for Docker Health."""
import builtins
import copy
import io
import json
import runpy
import sys
import urllib.error
from functools import partial
from pathlib import Path
from unittest.mock import Mock

import pytest

from test_health_reports import incident, run_cli


@pytest.mark.parametrize("config,error", [
    ([], "document YAML"), ({}, "aucun hôte"), ({"hosts": [None]}, "objet"),
    ({"hosts": [{"name": 4}]}, "nom d'hôte"),
    ({"hosts": [{"name": "x", "url": ""}]}, "URL API Docker manquante"),
    ({"hosts": [{"name": "x", "url": "ftp://host"}]}, "URL API Docker invalide"),
])
def test_invalid_host_configuration(health, capsys, config, error):
    with pytest.raises(SystemExit):
        health.validate_config(config)
    assert error in json.loads(capsys.readouterr().out)["error"]


@pytest.mark.parametrize("key,value,error", [
    ("options", [1], "options"), ("ntfy", [1], "ntfy"),
    ("options", {"timeout_seconds": "bad"}, "numériques"),
    ("options", {"max_parallel": 0}, "max_parallel"),
])
def test_invalid_monitor_options(health, health_config, capsys, key, value, error):
    health_config[key] = value
    with pytest.raises(SystemExit):
        health.validate_config(health_config)
    assert error in json.loads(capsys.readouterr().out)["error"]


@pytest.mark.parametrize("case", ["missing", "invalid", "no-yaml"])
def test_config_load_failures(health, monkeypatch, tmp_path, capsys, case):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(health, "CONFIG_PATH", path)
    if case != "missing":
        path.write_text("[invalid yaml")
    if case == "no-yaml":
        monkeypatch.setattr(health, "yaml", None)
    with pytest.raises(SystemExit) as error:
        health.load_config()
    assert error.value.code == 1
    assert "error" in json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("case", ["url", "topic", "valid"])
def test_ntfy_configuration_requirements(health, health_config, monkeypatch, capsys, case):
    health_config["ntfy"]["enabled"] = True
    if case == "url":
        monkeypatch.setenv("NTFY_ENDPOINT", "invalid")
    if case == "topic":
        health_config["ntfy"]["topic"] = ""
    if case == "valid":
        assert health.validate_config(health_config) is None
    else:
        with pytest.raises(SystemExit):
            health.validate_config(health_config)
        assert "ntfy" in json.loads(capsys.readouterr().out)["error"]


def test_import_without_optional_yaml_and_script_entrypoint(health, monkeypatch):
    original = builtins.__import__
    def import_without_yaml(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("optional dependency absent")
        return original(name, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "__import__", import_without_yaml)
        namespace = runpy.run_path(health.__file__)
    assert namespace["yaml"] is None
    monkeypatch.setattr(sys, "argv", [health.__file__, "--help"])
    with pytest.raises(SystemExit) as error:
        runpy.run_path(health.__file__, run_name="__main__")
    assert error.value.code == 0


def test_incomplete_docker_metadata_is_not_invented(health):
    assert health.normalize_name(None) == "unknown"
    assert health.parse_restart_info("restarting", "unexpected") == (None, None)
    assert health.classify("exited", "none", None) == "exited"
    assert health.normalize_healthcheck_detail([]) is None
    assert health.normalize_healthcheck_detail({}) is None
    result = health.normalize_healthcheck_detail({"State": {"Health": {"Status": "unhealthy", "Log": [None, {}]}}})
    assert result["log"] == [{"start": None, "end": None, "exit_code": None, "output": None}]


@pytest.mark.parametrize("operation", ["remote_get", "docker_remote_logs"])
@pytest.mark.parametrize("kind", ["http", "connection", "timeout"])
def test_docker_read_errors_have_context(health, monkeypatch, operation, kind):
    error = {
        "http": urllib.error.HTTPError("http://docker.invalid", 403, "forbidden", {}, io.BytesIO(b"denied")),
        "connection": urllib.error.URLError("offline"), "timeout": TimeoutError("slow"),
    }[kind]
    monkeypatch.setattr(health.urllib.request, "urlopen", Mock(side_effect=error))
    with pytest.raises(RuntimeError) as exc:
        getattr(health, operation)("http://docker.invalid", "/containers/json" if operation == "remote_get" else "abc", 1)
    assert "docker.invalid" in str(exc.value)


@pytest.mark.parametrize("body", [b"not JSON", b"\xff"])
def test_invalid_docker_json(health, monkeypatch, body):
    monkeypatch.setattr(health.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(body))
    with pytest.raises(RuntimeError, match="JSON invalide"):
        health.remote_get("http://docker.invalid", "/containers/json", 1)


def test_logs_are_bounded_and_report_truncation(health, monkeypatch):
    monkeypatch.setattr(health.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b"x" * 100))
    result = health.docker_remote_logs("http://docker.invalid", "abc", 1, max_bytes=20)
    assert result["truncated"] is True
    assert result["lines"] == ["x" * 20]


def test_logs_without_container_or_host_metadata_are_reported(health, health_config):
    for item in [{}, {"id": "abc", "host": "missing"}]:
        result = health.collect_container_logs(health_config, item)
        assert result["lines"] == [] and result["error"]


@pytest.mark.parametrize("case", ["invalid-list", "no-id", "inspect-fails"])
def test_partial_docker_collection(health, health_config, monkeypatch, case):
    rows = {} if case == "invalid-list" else [{"Id": "abc" if case != "no-id" else None,
                                               "State": "running", "Status": "Up (unhealthy)"}]
    calls = Mock(side_effect=[io.BytesIO(json.dumps(rows).encode()), OSError("inspect unavailable")])
    monkeypatch.setattr(health.urllib.request, "urlopen", calls)
    result = health.check_host(health_config["hosts"][0], False, 1)
    if case == "invalid-list":
        assert result["errors"] and result["containers"] == []
    else:
        assert result["containers"][0]["classification"] == "unhealthy"
        if case == "inspect-fails":
            assert "inspect unavailable" in result["containers"][0]["healthcheck"]["error"]
        else:
            assert calls.call_count == 1


@pytest.mark.parametrize("function,key,version", [
    ("load_snapshot", "containers", "snapshot_version"), ("load_events", "events", "events_version"),
    ("load_alert_state", "alerts", "version"), ("load_ack_state", "acks", "version"),
    ("load_silence_state", "silences", "version"),
])
@pytest.mark.parametrize("case", ["invalid-json", "wrong-version", "wrong-container"])
def test_invalid_persisted_state_is_rejected(health, tmp_path, capsys, function, key, version, case):
    path = tmp_path / "state.json"
    value = {version: 999, key: [] if key == "events" else {}} if case == "wrong-version" else {version: 1, key: None}
    path.write_text("not json" if case == "invalid-json" else json.dumps(value))
    with pytest.raises(SystemExit) as error:
        getattr(health, function)(path)
    assert error.value.code == 1
    assert str(path) in json.loads(capsys.readouterr().out)["error"]


def test_missing_snapshot_is_an_error(health, tmp_path):
    with pytest.raises(SystemExit):
        health.load_snapshot(tmp_path / "absent.json")


@pytest.mark.parametrize("function", ["save_snapshot", "save_snapshot_from_built", "save_events", "save_alert_state"])
def test_storage_failure_does_not_destroy_previous_state(health, tmp_path, monkeypatch, function, capsys):
    path = tmp_path / "state.json"
    path.write_text('{"previous": true}')
    monkeypatch.setattr(Path, "replace", Mock(side_effect=OSError("disk full")))
    with pytest.raises(SystemExit):
        getattr(health, function)(path, {})
    assert path.read_text() == '{"previous": true}'
    assert "disk full" in json.loads(capsys.readouterr().out)["error"]


@pytest.mark.parametrize("value,seconds", [(None, 0), ("1.5h", 5400), ("2w", 1209600), ("30", 30)])
def test_duration_units(health, value, seconds):
    assert health.parse_duration(value) == seconds


@pytest.mark.parametrize("value", ["", "-2h", "1 year"])
def test_invalid_duration(health, value):
    with pytest.raises(ValueError):
        health.parse_duration(value)


def test_alerts_require_observed_onset(health):
    snapshot = {"timestamp": 1000, "containers": {
        "unknown-onset": {"classification": "unhealthy", "state_since": None},
        "recent": {"classification": "unhealthy", "state_since": 999},
    }}
    assert health.persistent_problems(snapshot, 30) == []
    assert health.build_alerts(snapshot, 30)[1] == []


@pytest.mark.parametrize("classification,severity", [("dead", "critical"), ("unknown", "warning"), ("other", "warning"), ("exited_error", "warning")])
def test_alert_profiles_have_actionable_guidance(health, classification, severity):
    profile = health.alert_profile({"classification": classification}, 60)
    assert profile[0] == severity
    assert profile[2] and profile[3]


def test_healthcheck_guidance_without_exit_code(health):
    profile = health.alert_profile({"classification": "unhealthy", "healthcheck": {"failing_streak": 2}}, 60)
    assert "2" in profile[2]
    assert "code" not in profile[2]


def test_alert_rules_ignore_invalid_entries_and_apply_overrides(health):
    rules = health.normalize_alert_rules({"options": {"alert_rules": {
        "unhealthy": "ignore", "exited_error": "critical", "unknown": "invalid", "custom": "critical"}}})
    assert rules["unknown"] == "warning" and "custom" not in rules
    result = health.apply_alert_rules([
        {"classification": "unhealthy"}, {"classification": "exited_error", "severity": "warning"}], rules)
    assert len(result) == 1 and result[0]["severity"] == "critical"


def test_restart_metadata_changes_and_legacy_snapshots(health):
    previous = {"timestamp": 100, "containers": {"abc": {"name": "web", "host": "h", "classification": "unknown", "restart_loop_candidate": False}}}
    current = copy.deepcopy(previous)
    current["timestamp"] = 200
    current["containers"]["abc"]["restart_loop_candidate"] = True
    diff = health.compare_snapshots(previous, current)
    assert diff["restart_state_changes"] == 1
    assert diff["active_states"] == {}
    assert health.events_from_changes(diff)[0]["type"] == "problem_started"
    reverse = health.compare_snapshots(current, previous)
    assert health.events_from_changes(reverse)[0]["type"] == "recovered"
    assert "web" in health.human_changes(diff)
    assert health.events_from_changes({"changes": [{"type": "unsupported"}]}) == []
    state = health.events_from_changes({"changes": [{"type": "state_changed", "to": "healthy", "from": "starting"}]})
    assert state[0]["type"] == "state_changed"


def test_stats_accept_legacy_onset_and_non_problem_events(health):
    state = {"timestamp": 100, "containers": {"web": {"host": "h", "name": "web", "classification": "unhealthy"}}}
    events = {"events": [{"type": "added"}, {"host": "h", "name": "web", "type": "recovered", "timestamp": 50}]}
    result = health.stats_from_history(state, events)
    assert result["recoveries"] == 1 and result["problem_starts"] == 0
    assert result["containers"][0]["active_problem"] is True


def test_selected_host_and_unknown_host(health, incident):
    result = health.run_check(incident.config, "host-2")
    assert result["summary"]["hosts"] == 1
    assert result["hosts"][0]["host"] == "host-2"
    with pytest.raises(SystemExit):
        health.run_check(incident.config, "absent")


@pytest.mark.parametrize("function,kwargs", [
    ("build_host_report", {}), ("build_host_report", {"host": "absent"}),
    ("build_stack_report", {}), ("build_stack_report", {"stack": "absent"}),
    ("build_container_report", {}), ("build_container_report", {"container": "absent"}),
])
def test_reports_reject_missing_target(health, incident, function, kwargs):
    with pytest.raises(SystemExit):
        getattr(health, function)(incident.config, incident.path, **kwargs)


@pytest.mark.parametrize("operation", ["acknowledge_alert", "silence_alert"])
def test_cannot_ack_or_silence_missing_problem(health, incident, operation):
    kwargs = {"duration": 60} if operation == "silence_alert" else {}
    with pytest.raises(SystemExit):
        getattr(health, operation)(incident.config, "host-1", "absent", snapshot_path=incident.path, **kwargs)


def test_silence_requires_positive_duration(health, incident):
    with pytest.raises(SystemExit):
        health.silence_alert(incident.config, "host-1", "web", 0, snapshot_path=incident.path)


def test_acknowledgement_is_invalidated_by_new_classification(health, incident):
    health.acknowledge_alert(incident.config, "host-1", "web", incident.path)
    alert = {"host": "host-1", "name": "web", "id": "web000000000", "classification": "unhealthy"}
    assert health.apply_acknowledgements(incident.config, [alert])[0]["acknowledged"] is True
    alert["classification"] = "dead"
    assert health.apply_acknowledgements(incident.config, [alert])[0]["acknowledged"] is False
    assert health.unacknowledge_alert(incident.config, "other", "web") is False
    assert health.list_acknowledged(incident.config, incident.path.parent / "missing")[0]["active"] is False
    health.silence_alert(incident.config, "host-1", "web", 60, snapshot_path=incident.path)
    assert health.unsilence_alert(incident.config, "other", "web") is False


@pytest.mark.parametrize("human", [False, True])
@pytest.mark.parametrize("reason", [[], ["--reason", "maintenance"]])
def test_ack_silence_and_notification_history_cli(health, incident, monkeypatch, capsys, human, reason):
    # Route commands with a fixed default snapshot into this test's data directory.
    for name in ("acknowledge_alert", "silence_alert"):
        monkeypatch.setattr(health, name, partial(getattr(health, name), snapshot_path=incident.path))
    mode = ["--human"] if human else []
    for command in ("acknowledged", "silences", "notification-history"):
        extra = ["--snapshot", incident.path] if command == "acknowledged" else []
        output = run_cli(health, monkeypatch, capsys, command, *extra, *mode)
        assert output.strip()
    for command in ("ack", "silence"):
        extra = ["--duration", "1h"] if command == "silence" else []
        output = run_cli(health, monkeypatch, capsys, command, "--host", "host-1", "--container", "web", *reason, *extra, *mode)
        assert "web" in output
    for command in ("acknowledged", "silences"):
        extra = ["--snapshot", incident.path] if command == "acknowledged" else []
        assert "web" in run_cli(health, monkeypatch, capsys, command, *extra, *mode)
    for command in ("unack", "unsilence"):
        assert "web" in run_cli(health, monkeypatch, capsys, command, "--host", "host-1", "--container", "web", *mode)
    cfg = health.ntfy_config(incident.config)
    health.ntfy_history_record(cfg, [{"alert": {"host": "host-1", "name": "web"}}], "test", "urgent", 102000)
    assert "test" in run_cli(health, monkeypatch, capsys, "notification-history", *mode)
    # An older history entry may have no per-container detail.
    health.ntfy_save_history(cfg["history_file"], [{"title": "legacy"}], 5)
    assert "legacy" in run_cli(health, monkeypatch, capsys, "notification-history", *mode)


@pytest.mark.parametrize("command,extra", [
    ("diagnose", ["--host", "host-1", "--since", "bad"]),
    ("host-report", ["--host", "host-1", "--since", "bad"]),
    ("agent", ["--min-duration", "bad"]),
    ("monitor", ["--once", "--min-duration", "bad"]),
    ("monitor", ["--once", "--interval", "-1"]),
    ("alerts", ["--min-duration", "bad"]),
    ("persistent-problems", ["--min-duration", "bad"]),
])
def test_cli_invalid_duration_or_interval(health, incident, monkeypatch, capsys, command, extra):
    file_args = ["--file", incident.path] if command in {"alerts", "persistent-problems"} else []
    if command in {"diagnose", "host-report", "monitor"}:
        file_args = ["--snapshot", incident.path]
    with pytest.raises(SystemExit):
        run_cli(health, monkeypatch, capsys, command, *file_args, *extra)


@pytest.mark.parametrize("human", [False, True])
def test_alerts_cli_tracks_new_changed_recovered_and_unchanged(health, incident, monkeypatch, capsys, tmp_path, human):
    mode = ["--human"] if human else []
    state = tmp_path / "separate-alerts.json"
    def collect():
        return run_cli(health, monkeypatch, capsys, "alerts", "--file", incident.path, "--state-file", state, *mode)
    first = collect()
    assert "web" in first
    second = collect()
    if human:
        assert "Aucun nouvel" in second
    else:
        assert json.loads(second)["notification_count"] == 0
    snapshot = health.load_snapshot(incident.path)
    snapshot["containers"]["web000000000"]["classification"] = "dead"
    del snapshot["containers"]["worker000000"]
    health.save_snapshot_from_built(incident.path, snapshot)
    result = collect()
    if human:
        assert "CHANGED" in result and "RECOVERED" in result
    else:
        result = json.loads(result)
        assert len(result["changed_alerts"]) == 1 and len(result["resolved_alerts"]) == 1


def test_continuous_monitor_stops_on_interrupt(health, incident, monkeypatch, capsys):
    monkeypatch.setattr(health.time, "sleep", Mock(side_effect=KeyboardInterrupt))
    output = run_cli(health, monkeypatch, capsys, "monitor", "--snapshot", incident.path, "--alert-state", incident.alerts)
    assert json.loads(output)["summary"]["containers"] == 7


def test_snapshot_overwrite_and_stack_json_cli(health, incident, monkeypatch, capsys):
    result = json.loads(run_cli(health, monkeypatch, capsys, "snapshot", "--file", incident.path))
    assert result["containers"] == 7
    for command in ("stack-summary", "stack-problems", "healthcheck-audit"):
        result = json.loads(run_cli(health, monkeypatch, capsys, command))
        assert result.get("stacks") or result.get("running")
