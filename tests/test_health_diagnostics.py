"""Diagnostics remain scoped and useful when historical context is incomplete."""
from unittest.mock import Mock

import pytest


@pytest.fixture
def diagnosis(health, health_config, monkeypatch, tmp_path):
    monkeypatch.setattr(health.time, "time", lambda: 10000)
    snapshot = tmp_path / "snapshot.json"
    events = tmp_path / "custom-events.json"
    current = {"id": "current", "name": "web", "host": "host-1",
               "classification": "healthy", "compose": {"project": "media"}}

    def run(history=(), *, target=None, container=None, **scope):
        health.save_snapshot_from_built(snapshot, {
            "snapshot_version": health.SNAPSHOT_VERSION,
            "containers": {"current": container or current}, "timestamp": 10000})
        health.save_events(events, {"events_version": 1, "events": list(history)})
        return health.build_diagnose(health_config, snapshot, events,
                                    **(target or {"host": "host-1"}), **scope)
    return run


def event(name="web", project="media", **extra):
    return {"type": "problem_started", "timestamp": 9900, "host": "host-1",
            "name": name, "id": "old-id", "compose": {"project": project},
            "classification": "unhealthy", **extra}


def test_diagnosis_follows_recreated_stack_members_and_filters_other_hosts(diagnosis):
    report = diagnosis([event(), event(host="host-2"), event(project="other"),
                        event(timestamp=1)], target={"stack": "media"})
    assert report["recent_events"] == 1
    assert [item["container"] for item in report["related_problems"]] == ["web"]
    assert report["hypotheses"][0]["type"] == "stack_correlation"


def test_container_diagnosis_does_not_list_itself_as_related(diagnosis):
    report = diagnosis([event(), event(name="database")],
                       target={"host": "host-1", "container": "web"})
    assert report["recent_events"] == 1
    assert report["related_problems"] == []
    assert report["hypotheses"] == []


def test_host_diagnosis_correlates_removed_stack(diagnosis):
    report = diagnosis([event(name="old-db", project="removed-stack")])
    assert report["related_problems"][0]["relation"] == "same_host"
    assert report["hypotheses"][0]["type"] == "host_correlation"
    assert report["root_cause_claimed"] is False


def test_stack_diagnosis_does_not_correlate_previous_project_metadata(diagnosis):
    # An event still belongs to the same container by ID, but describes an old project.
    report = diagnosis([event(id="current", project="old-project")], target={"stack": "media"})
    assert report["recent_events"] == 1
    assert report["related_problems"] == []


@pytest.mark.parametrize("healthcheck,logs,confidence,has_log_hypothesis", [
    ({}, {"lines": []}, "medium", False),
    ({}, {"lines": ["  ", "\t"]}, "medium", False),
    ({}, {"error": "access denied"}, "medium", False),
    ({"failing_streak": 3}, {"lines": ["connection refused"]}, "medium", True),
    ({"log": [{"exit_code": 1}]}, {"lines": []}, "high", False),
    ({"log": [{"output": "probe failed"}]}, {"lines": []}, "high", False),
])
def test_diagnosis_handles_partial_healthchecks_and_unusable_logs(
        health, diagnosis, monkeypatch, healthcheck, logs, confidence, has_log_hypothesis):
    reader = Mock(return_value=logs)
    monkeypatch.setattr(health, "collect_container_logs", reader)
    container = {"id": "current", "host": "host-1", "name": "web",
                 "classification": "unhealthy", "healthcheck": healthcheck}
    report = diagnosis(container=container)
    reader.assert_called_once()
    hypotheses = {item["type"]: item for item in report["hypotheses"]}
    assert hypotheses["healthcheck_failure"]["confidence"] == confidence
    assert ("container_logs" in hypotheses) is has_log_hypothesis
    assert report["logs"]["host-1::current"] == logs
    if logs.get("error"):
        assert any("access denied" in item for item in report["evidence"])
    if not healthcheck:
        assert "définition du healthcheck" in hypotheses["healthcheck_failure"]["message"]


def test_trends_ignore_unknown_event_types_without_losing_known_counts(health, health_config, tmp_path, monkeypatch):
    monkeypatch.setattr(health.time, "time", lambda: 10000)
    snapshot = tmp_path / "snapshot.json"
    health.save_snapshot_from_built(snapshot, {"snapshot_version": health.SNAPSHOT_VERSION, "containers": {}})
    health.save_events(snapshot.with_suffix(".events.json"), {
        "events_version": 1, "events": [event(type="future-event"), event()]})
    result = health.build_trends(health_config, snapshot)
    assert result["problem_starts"] == 1
    assert sum(sum(day.values()) for day in result["by_day"].values()) == 1
