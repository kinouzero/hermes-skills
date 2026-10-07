"""Notification batching, suppression and readable reports for edge cases."""
import io
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from test_health_reports import incident, run_cli


def notification(kind="alert_started", severity="warning", name="web", **extra):
    return {"type": kind, "severity": severity, "alert": {
        "host": "host-1", "name": name, "classification": "unhealthy",
        "message": "healthcheck failed", "severity": severity,
        "compose": {"project": "media", "service": name}, **extra}}


@pytest.mark.parametrize("kind,label", [("alert_started", "WARNING"), ("alert_changed", "CHANGED"), ("alert_recovered", "RECOVERED")])
def test_notification_text_includes_context(health, kind, label):
    event = notification(kind, duration_seconds=120)
    event["before"] = {"classification": "restarting"}
    title, message = health.ntfy_event_text(event)
    assert label in title
    assert "media" in message and "web" in message and "2m" in message


@pytest.mark.parametrize("events,priority", [
    ([notification(), notification(name="db")], "high"),
    ([notification(severity="critical"), notification("alert_changed")], "urgent"),
    ([notification("alert_recovered", "recovered"), notification("alert_recovered", "recovered", "db")], "default"),
    ([], "default"),
])
def test_batch_priority_and_grouping(health, events, priority):
    title, message, actual = health.ntfy_build_batch(events)
    assert actual == priority
    assert str(len(events)) in message
    for event in events:
        assert event["alert"]["name"] in message
    assert "DOCKER HEALTH" in title


@pytest.mark.parametrize("case,failed,sent", [("token", 1, 0), ("url", 1, 0), ("http", 1, 0), ("recovery", 0, 1), ("cooldown", 0, 0)])
def test_ntfy_failure_and_recovery_delivery(health, health_config, monkeypatch, case, failed, sent):
    health_config["ntfy"]["enabled"] = True
    response = io.BytesIO(b"")
    response.status = 503 if case == "http" else 200
    network = Mock(return_value=response)
    monkeypatch.setattr(health.urllib.request, "urlopen", network)
    if case == "token":
        monkeypatch.delenv("NTFY_TOKEN")
    if case == "url":
        monkeypatch.delenv("NTFY_ENDPOINT")
    event = notification("alert_recovered", "recovered") if case == "recovery" else notification()
    if case == "cooldown":
        health.ntfy_save_state(health_config["options"]["ntfy_state_file"], {"last_alert_notification_at": 1000})
    result = health.send_ntfy(health_config, [event], now=1001)
    assert result["failed"] == failed and result["sent"] == sent
    if case in {"token", "url", "cooldown"}:
        network.assert_not_called()
    if case == "recovery":
        assert not Path(health_config["options"]["ntfy_state_file"]).exists()


def test_monitor_suppresses_acknowledged_alerts_and_reports_transitions(health, incident, tmp_path):
    health.acknowledge_alert(incident.config, "host-1", "web", incident.path)
    separate = tmp_path / "fresh-alerts.json"
    def cycle():
        return health.monitor_once(incident.config, incident.path, incident.events, separate, "0s")
    new = cycle()
    assert all(item["alert"]["name"] != "web" for item in new["notifications"])
    assert "NEW/" in health.human_monitor_cycle(new)
    incident.config["options"]["alert_rules"] = {"unhealthy": "warning"}
    acknowledged_change = cycle()
    assert acknowledged_change["changed_alerts"]
    assert acknowledged_change["notifications"] == []
    health.unacknowledge_alert(incident.config, "host-1", "web")
    incident.config["options"]["alert_rules"]["unhealthy"] = "critical"
    changed = cycle()
    assert changed["notifications"][0]["type"] == "alert_changed"
    assert "CHANGED/" in health.human_monitor_cycle(changed)
    incident.rows[0]["Status"] = "Up (healthy)"
    incident.clock[0] += 60
    recovered = cycle()
    assert recovered["notifications"][0]["type"] == "alert_recovered"
    recovered["ntfy"] = {"enabled": True, "sent": 1}
    assert "RECOVERED" in health.human_monitor_cycle(recovered)
    assert "ntfy: 1" in health.human_monitor_cycle(recovered)


def test_quiet_monitor_outputs_new_notifications_only(health, incident, monkeypatch, capsys, tmp_path):
    result = json.loads(run_cli(health, monkeypatch, capsys, "monitor", "--once", "--quiet", "--snapshot", incident.path,
                               "--alert-state", tmp_path / "new-alerts.json"))
    assert len(result["notifications"]) == 4


def test_empty_history_and_reports_are_readable(health, incident):
    empty = {"containers": {}, "timestamp": 102000, "snapshot_version": 2}
    health.save_snapshot_from_built(incident.path, empty)
    health.save_events(incident.events, {"events_version": 1, "events": []})
    report = health.build_report(incident.config, incident.path)
    assert "ancien snapshot" in health.human_report(report)
    empty["containers"]["web"] = {"host": "host-1", "name": "web", "classification": "healthy", "compose": {}}
    health.save_snapshot_from_built(incident.path, empty)
    report = health.build_report(incident.config, incident.path)
    assert "Aucune" in health.human_report(report)
    assert "Aucun" in health.human_host_report(health.build_host_report(incident.config, incident.path, "host-1"))
    assert "Aucun" in health.human_stack_report(health.build_stack_report(incident.config, incident.path, "sans-compose"))
    container = health.build_container_report(incident.config, incident.path, "web", "host-1")
    assert "Aucun événement" in health.human_container_report(container)
    assert "Aucun" in health.human_stats(health.stats_from_history(empty, {}))
    assert "Aucun" in health.human_events({"events": []})
    assert "Aucun" in health.human_persistent({"containers": [], "min_duration_seconds": 60})
    assert "Docker Health" in health.human_trends(health.build_trends(incident.config, incident.path))
    diagnose = health.build_diagnose(incident.config, incident.path, host="host-1")
    assert "Docker Health" in health.human_diagnose(diagnose)


@pytest.mark.parametrize("logs,fragment", [
    ({"error": "denied"}, "denied"), ({"lines": []}, "Aucun log"),
    ({"lines": ["tail"], "truncated": True}, "tronqués"),
])
def test_container_report_renders_log_failures_and_truncation(health, incident, logs, fragment):
    report = health.build_container_report(incident.config, incident.path, "web", "host-2")
    report["logs"] = logs
    assert fragment in health.human_container_report(report)


def test_healthcheck_audit_can_represent_unknown_running_status(health):
    data = health.healthcheck_audit([{"host": "h", "name": "web", "state": "running", "classification": "unknown"}])
    assert data["not_determinable"] == 1
    assert data["coverage_percent"] == 0
    assert "Aucun container sans healthcheck" in health.human_healthcheck_audit(data)


def test_human_checks_handle_missing_healthcheck_details(health, incident):
    data = health.run_check(incident.config, "host-2")
    c = data["hosts"][0]["containers"][0]
    for details, expected in [({"error": "inspect unavailable"}, "inspect unavailable"), ({}, "unhealthy"),
                              ({"log": [{"exit_code": None, "output": ""}]}, "unhealthy")]:
        c.update(classification="unhealthy", healthcheck=details)
        assert expected in health.human(data, "check")
    c.update(classification="restarting", restart_age=None, restart_exit_code=None)
    assert "restarting" in health.human(data, "check")


def test_agent_report_empty_and_many_problems(health, incident):
    healthy = health.build_agent_report(incident.config, host="host-2")
    assert "Aucun container" in health.human_agent_report(healthy)
    report = health.build_agent_report(incident.config, host="host-1", previous_path=incident.path)
    web = next(item for item in report["problems"] if item["name"] == "web")
    web["healthcheck"] = {}
    report["problems"] = [web] * 21
    assert "1 autre(s)" in health.human_agent_report(report)
    web["healthcheck"] = {"failing_streak": 2}
    assert "échecs=2" in health.human_agent_report(report)


def test_changes_render_lifecycle_and_unknown_events(health):
    old = {"timestamp": 1, "containers": {"old": {"name": "old", "host": "h", "classification": "healthy"},
            "same": {"name": "same", "host": "h", "classification": "healthy"}}}
    new = {"timestamp": 2, "containers": {"new": {"name": "new", "host": "h", "classification": "healthy"},
            "same": {"name": "same", "host": "h", "classification": "unhealthy"}}}
    changes = health.compare_snapshots(old, new)
    changes["changes"].append({"type": "unknown"})
    rendered = health.human_changes(changes)
    assert "+ h/new" in rendered and "- h/old" in rendered and "nouvel état" in rendered
    assert "Aucun" not in health.human_events({"events": [{"type": "added", "name": "new"}]})
