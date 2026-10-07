import io
import json
import urllib.error
from unittest.mock import Mock

import pytest
import yaml


def container(health, status="Up 1 hour (unhealthy)", state="running"):
    return health.normalize_api_container({
        "Id": "a" * 64, "Names": ["/web"], "State": state, "Status": status,
        "Image": "nginx:1.2.3", "Labels": {"com.docker.compose.project": "media"},
    }, "host-1")


def snapshot(health, monkeypatch, now=1000, previous=None, status="Up (unhealthy)"):
    monkeypatch.setattr(health.time, "time", lambda: now)
    return health.build_snapshot({"hosts": [{"containers": [container(health, status)]}]}, previous)


@pytest.mark.parametrize("state,status,expected", [
    ("running", "Up 1 hour (healthy)", "healthy"),
    ("running", "Up 1 hour (unhealthy)", "unhealthy"),
    ("running", "Up 1 second (starting)", "starting"),
    ("running", "Up 1 hour", "no_healthcheck"),
    ("exited", "Exited (0) 1 minute ago", "exited_success"),
    ("exited", "Exited (137) 1 minute ago", "exited_error"),
    ("restarting", "Restarting (1) 4 seconds ago", "restarting"),
    ("paused", "Up 1 hour (Paused)", "paused"),
    ("dead", "Dead", "dead"),
    ("unexpected", "", "unknown"),
])
def test_container_classification(health, state, status, expected):
    item = container(health, status, state)
    assert item["classification"] == expected
    assert item["name"] == "web"
    assert item["compose"]["project"] == "media"


def test_restart_exit_code_is_not_a_restart_count(health):
    item = container(health, "Restarting (137) 4 seconds ago", "restarting")
    assert item["restart_exit_code"] == 137
    assert item["restart_age"] == "4 seconds ago"
    assert item["restart_loop_candidate"] is True


def test_healthcheck_details_are_bounded(health):
    result = health.normalize_healthcheck_detail({
        "State": {"Health": {"Status": "unhealthy", "FailingStreak": 8,
            "Log": [{"ExitCode": n, "Output": "x" * 3000} for n in range(8)]}},
        "Config": {"Healthcheck": {"Test": ["CMD", "curl", "localhost"], "Retries": 3}},
    })
    assert [entry["exit_code"] for entry in result["log"]] == [3, 4, 5, 6, 7]
    assert all(len(entry["output"]) == 2000 for entry in result["log"])
    assert result["failing_streak"] == 8
    assert result["retries"] == 3


def test_collection_only_inspects_unhealthy_containers(health, health_config, monkeypatch):
    items = [{"Id": str(n) * 64, "Names": [f"/web-{n}"], "State": "running",
              "Status": f"Up ({state})"} for n, state in enumerate(["healthy", "unhealthy"])]
    requests = []

    def response(request, timeout):
        requests.append(request)
        value = items if "/containers/json?" in request.full_url else {
            "State": {"Health": {"Status": "unhealthy", "Log": []}}}
        return io.BytesIO(json.dumps(value).encode())

    monkeypatch.setattr(health.urllib.request, "urlopen", response)
    result = health.check_host(health_config["hosts"][0], True, 10)
    assert result["errors"] == []
    assert len(result["containers"]) == 2
    assert [r.get_method() for r in requests] == ["GET", "GET"]
    assert requests[0].full_url.endswith("/containers/json?all=true")
    assert requests[1].full_url.endswith(f"/containers/{'1' * 64}/json")


def test_unreachable_host_is_reported_as_an_error(health, health_config, monkeypatch):
    monkeypatch.setattr(health.urllib.request, "urlopen", Mock(side_effect=urllib.error.URLError("offline")))
    result = health.check_host(health_config["hosts"][0], True, 10)
    assert result["containers"] == []
    assert result["errors"][0]["host"] == "host-1"
    assert "offline" in result["errors"][0]["error"]


def test_docker_logs_decode_multiplexed_frames(health, monkeypatch):
    def frame(stream, data):
        return bytes([stream, 0, 0, 0]) + len(data).to_bytes(4, "big") + data
    response = Mock(return_value=io.BytesIO(frame(1, b"stdout\n") + frame(2, b"stderr\n")))
    monkeypatch.setattr(health.urllib.request, "urlopen", response)
    result = health.docker_remote_logs("http://docker.invalid", "abc", 10)
    assert result["lines"] == ["stdout", "stderr"]
    assert result["truncated"] is False
    request = response.call_args.args[0]
    assert request.get_method() == "GET"
    assert "tail=200" in request.full_url


def test_config_loads_yaml_without_ntfy_credentials(health, health_config, monkeypatch, tmp_path):
    monkeypatch.delenv("NTFY_ENDPOINT")
    monkeypatch.delenv("NTFY_TOKEN")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(health_config))
    monkeypatch.setattr(health, "CONFIG_PATH", path)
    assert health.load_config() == health_config


def test_enabled_ntfy_requires_token_even_for_config_load(health, health_config, monkeypatch, capsys):
    health_config["ntfy"]["enabled"] = True
    monkeypatch.delenv("NTFY_TOKEN")
    with pytest.raises(SystemExit) as error:
        health.validate_config(health_config)
    assert error.value.code == 1
    assert "NTFY_TOKEN" in json.loads(capsys.readouterr().out)["error"]


@pytest.mark.parametrize("change", ["duplicate", "socket", "timeout"])
def test_invalid_config_fails(health, health_config, change):
    if change == "duplicate":
        health_config["hosts"] *= 2
    elif change == "socket":
        health_config["hosts"][0]["mode"] = "local"
    else:
        health_config["options"]["timeout_seconds"] = 0
    with pytest.raises(SystemExit):
        health.validate_config(health_config)


def test_snapshot_preserves_onset_and_records_recovery(health, monkeypatch, tmp_path):
    first = snapshot(health, monkeypatch)
    second = snapshot(health, monkeypatch, 1300, first)
    assert next(iter(second["containers"].values()))["state_since"] == 1000
    recovered = snapshot(health, monkeypatch, 1600, second, "Up (healthy)")
    changes = health.compare_snapshots(second, recovered)
    assert changes["changes"][0]["recovered"] is True
    assert changes["changes"][0]["previous_duration_seconds"] == 600
    path = tmp_path / "snapshot.json"
    health.save_snapshot_from_built(path, recovered)
    assert health.load_snapshot(path) == recovered
    assert not path.with_suffix(".json.tmp").exists()


@pytest.mark.parametrize("elapsed,count", [(1799, 0), (1800, 1), (1801, 1)])
def test_alert_threshold_boundary(health, monkeypatch, elapsed, count):
    first = snapshot(health, monkeypatch)
    current = snapshot(health, monkeypatch, 1000 + elapsed, first)
    _, alerts = health.build_alerts(current, 1800)
    assert len(alerts) == count
    if alerts:
        assert alerts[0]["classification"] == "unhealthy"
        assert alerts[0]["severity"] == "critical"


def test_silence_expires_at_its_deadline(health, health_config, monkeypatch, tmp_path):
    current = snapshot(health, monkeypatch)
    path = tmp_path / "snapshot.json"
    health.save_snapshot_from_built(path, current)
    health.silence_alert(health_config, "host-1", "web", 60, snapshot_path=path)
    notification = {"type": "alert_started", "alert": next(iter(current["containers"].values()))}
    assert health.apply_silences(health_config, [notification], now=1059) == ([], 1)
    assert health.apply_silences(health_config, [notification], now=1060) == ([notification], 0)


def test_cooldown_does_not_suppress_recoveries(health, health_config):
    path = health_config["options"]["ntfy_state_file"]
    health.ntfy_save_state(path, {"last_alert_notification_at": 1000})
    active = {"type": "alert_started"}
    recovery = {"type": "alert_recovered"}
    selected, *_ = health.ntfy_filter_notifications(health_config, [active, recovery], now=1100)
    assert selected == [recovery]
    selected, *_ = health.ntfy_filter_notifications(health_config, [active], now=1900)
    assert selected == [active]


def test_monitor_cycle_persists_problem_then_recovery(health, health_config, monkeypatch, tmp_path):
    current = [container(health)]
    monkeypatch.setattr(health, "run_check", lambda config: {"hosts": [{"containers": current}]})
    clock = [1000]
    monkeypatch.setattr(health.time, "time", lambda: clock[0])
    paths = [tmp_path / filename for filename in ("snapshot.json", "events.json", "alerts.json")]
    first = health.monitor_once(health_config, *paths, "30m")
    assert first["active_alerts"] == []
    clock[0] = 2800
    second = health.monitor_once(health_config, *paths, "30m")
    assert second["notifications"][0]["type"] == "alert_started"
    assert second["ntfy"]["skipped"] == 1
    health.acknowledge_alert(health_config, "host-1", "web", snapshot_path=paths[0])
    clock[0] = 3100
    current[:] = [container(health, "Up (healthy)")]
    third = health.monitor_once(health_config, *paths, "30m")
    assert third["notifications"][0]["type"] == "alert_recovered"
    assert health.load_ack_state(health_config["options"]["ack_state_file"])["acks"] == {}
    assert health.load_events(paths[1])["events"][-1]["type"] == "recovered"


def test_container_report_fetches_logs_only_for_problem(health, health_config, monkeypatch, tmp_path):
    path = tmp_path / "snapshot.json"
    logs = Mock(return_value={"lines": ["connection refused"]})
    monkeypatch.setattr(health, "collect_container_logs", logs)
    for status, expected in [("Up (healthy)", None), ("Up (unhealthy)", logs.return_value)]:
        health.save_snapshot_from_built(path, snapshot(health, monkeypatch, status=status))
        result = health.build_container_report(health_config, path, "web", "host-1")
        assert result["logs"] == expected
    logs.assert_called_once()


@pytest.mark.parametrize("http_fails", [False, True])
def test_ntfy_records_only_successful_notifications(health, health_config, monkeypatch, http_fails):
    health_config["ntfy"]["enabled"] = True
    notification = {"type": "alert_started", "severity": "critical", "alert": {
        "host": "host-1", "name": "web", "id": "abc", "classification": "unhealthy",
        "message": "Healthcheck failed", "severity": "critical"}}
    requests = []
    class Response(io.BytesIO):
        status = 200
    def send(request, timeout):
        requests.append(request)
        if http_fails:
            raise urllib.error.URLError("unavailable")
        return Response(b"")
    monkeypatch.setattr(health.urllib.request, "urlopen", send)
    result = health.send_ntfy(health_config, [notification], now=1000)
    assert requests[0].get_method() == "POST"
    assert requests[0].full_url == "https://ntfy.invalid/tests"
    assert result["sent"] == int(not http_fails)
    assert result["failed"] == int(http_fails)
    state = health.ntfy_load_state(health_config["options"]["ntfy_state_file"])
    if http_fails:
        assert not state.get("last_alert_notification_at")
    else:
        assert state["last_alert_notification_at"] == 1000
        assert len(health.notification_history(health_config)) == 1


@pytest.mark.parametrize("command,key", [
    ("check", "hosts"), ("problems", "hosts"), ("no-healthcheck", "hosts"),
    ("summary", "summary"), ("host-summary", "host_summaries"),
])
def test_read_cli_returns_json(health, health_config, monkeypatch, tmp_path, capsys, command, key):
    import sys
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(health_config))
    monkeypatch.setattr(health, "CONFIG_PATH", path)
    monkeypatch.setattr(sys, "argv", ["docker-health.py", command])
    monkeypatch.setattr(health.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b"[]"))
    health.main()
    result = json.loads(capsys.readouterr().out)
    assert key in result
    if key == "hosts":
        assert result["hosts"][0]["host"] == "host-1"
        assert result["hosts"][0]["errors"] == []
