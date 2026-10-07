"""Test update workflows through simulated HTTP responses, not mocked plans."""
import base64
import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def world(updater, stack_factory, monkeypatch):
    stack = stack_factory()
    state = SimpleNamespace(
        stack=stack, requests=[], polls=[], success=True,
        containers=[{"name": "web", "service": "web", "watcher": "host-1", "stack": "media",
                     "updateAvailable": True, "image": {"name": "nginx", "tag": {"value": "1.2.3"}},
                     "result": {"tag": "1.2.4"},
                     "updateKind": {"kind": "tag", "semverDiff": "patch"}}],
    )
    def api(request, timeout):
        payload = json.loads(request.data) if request.data else None
        state.requests.append((request, payload))
        if request.full_url.startswith(updater.WUD_URL):
            assert request.get_method() == "GET"
            assert request.get_header("Authorization") == "Basic " + base64.b64encode(b"test-user:test-password").decode()
            value = state.containers
        else:
            assert request.get_method() == "POST"
            assert request.get_header("X-api-key") == "test-key"
            assert request.get_header("X-api-secret") == "test-secret"
            operation = payload["type"]
            if operation == "ListStacks":
                value = [stack]
            elif operation == "GetStack":
                assert payload["params"]["stack"] == "s1"
                value = stack
            elif operation == "UpdateStack":
                assert request.full_url.endswith("/write")
                stack["config"].update(payload["params"]["config"])
                value = {"ok": True}
            elif operation in {"PullStack", "DeployStack"}:
                assert request.full_url.endswith("/execute")
                if operation == "DeployStack":
                    stack["info"]["services"][0].update(
                        image=updater.find_service_image(stack["config"]["file_contents"], "web"), state="Running")
                value = {"_id": {"$oid": "operation-1"}}
            elif operation == "GetUpdate":
                assert payload["params"]["id"] == "operation-1"
                value = state.polls.pop(0) if state.polls else {
                    "status": "Complete", "success": state.success, "start_ts": 1000, "end_ts": 2500,
                    "logs": [{"stage": "deploy", "success": state.success, "stdout": "service started"}]}
            else:
                pytest.fail(f"Unexpected Komodo operation: {operation}")
        return io.BytesIO(json.dumps(value).encode())
    monkeypatch.setattr(urllib.request, "urlopen", api)
    return state


def run_cli(updater, monkeypatch, capsys, *args):
    monkeypatch.setattr(sys, "argv", ["docker-update.py", *args])
    updater.main()
    return capsys.readouterr()


def writes(world):
    return [payload for request, payload in world.requests if request.full_url.endswith(("/write", "/execute"))]


def test_cli_update_verify_deploy_and_runtime(updater, world, monkeypatch, capsys):
    preflight = json.loads(run_cli(updater, monkeypatch, capsys, "preflight").out)
    assert preflight["status"] == "OK"
    plan = json.loads(run_cli(updater, monkeypatch, capsys, "plan", "--stacks", "host-1-media", "--services", "web").out)
    assert plan["summary"] == {"READY": 1}
    assert writes(world) == []
    failed_verify = json.loads(run_cli(updater, monkeypatch, capsys, "verify").out)
    assert failed_verify["status"] == "FAILED"
    assert not Path(updater.VERIFY_STATE_FILE).exists()
    prepared = json.loads(run_cli(updater, monkeypatch, capsys, "update", "--confirm").out)
    assert prepared["count_updated"] == 1
    assert [payload["type"] for payload in writes(world)] == ["UpdateStack"]
    verified = json.loads(run_cli(updater, monkeypatch, capsys, "verify").out)
    assert verified["status"] == "OK" and verified["count_verified"] == 1
    result = run_cli(updater, monkeypatch, capsys, "deploy", "--confirm", "--follow", "--verify-runtime")
    deployed = json.loads(result.out)
    assert deployed["status"] == "DEPLOY_COMPLETED"
    assert deployed["items"][0]["runtime_verify"]["status"] == "OK"
    assert deployed["items"][0]["komodo_duration_ms"] == 1500
    assert "service started" in result.err
    assert not Path(updater.VERIFY_STATE_FILE).exists()
    assert [payload["type"] for payload in writes(world)] == ["UpdateStack", "DeployStack"]


def test_digest_cli_pulls_before_deployment(updater, world, monkeypatch, capsys):
    world.containers[0]["updateKind"] = {"kind": "digest"}
    world.containers[0]["result"] = {"digest": "sha256:new"}
    before = world.stack["config"]["file_contents"]
    prepared = json.loads(run_cli(updater, monkeypatch, capsys, "update", "--include-digest", "--confirm").out)
    assert prepared["items"][0]["compose_modified"] is False
    verified = json.loads(run_cli(updater, monkeypatch, capsys, "verify", "--include-digest").out)
    assert verified["count_digest_verified"] == 1
    result = json.loads(run_cli(updater, monkeypatch, capsys, "deploy", "--confirm", "--follow").out)
    assert result["status"] == "DEPLOY_COMPLETED"
    assert [payload["type"] for payload in writes(world)] == ["PullStack", "DeployStack"]
    assert writes(world)[0]["params"]["services"] == ["web"]
    assert world.stack["config"]["file_contents"] == before


def test_history_and_rollback_cli_preserve_deployment_boundary(updater, world, monkeypatch, capsys):
    run_cli(updater, monkeypatch, capsys, "update", "--confirm")
    for command in ("history", "rollback-info"):
        result = json.loads(run_cli(updater, monkeypatch, capsys, command, "--stack", "host-1-media", "--service", "web").out)
        assert result["count"] == 1
        assert result["items"][0]["old_image"] == "nginx:1.2.3"
    rolled_back = json.loads(run_cli(updater, monkeypatch, capsys, "rollback", "--stack", "host-1-media", "--service", "web", "--confirm").out)
    assert rolled_back["deployment"] == "NOT_RUN"
    assert updater.find_service_image(world.stack["config"]["file_contents"], "web") == "nginx:1.2.3"
    assert [payload["type"] for payload in writes(world)] == ["UpdateStack", "UpdateStack"]


@pytest.mark.parametrize("command,extra,key,value", [
    ("wud-updates", ["--stack", "host-1-media", "--service", "web"], "count", 1),
    ("wud-updates", ["--stack", "other"], "count", 0),
    ("wud-updates", ["--service", "other"], "count", 0),
    ("plan", ["--stack", "other"], "items", []),
    ("plan", ["--service", "other"], "items", []),
    ("update", ["--dry-run"], "status", "DRY_RUN"),
    ("update", [], "status", "CONFIRMATION_REQUIRED"),
])
def test_read_only_cli_filters(updater, world, monkeypatch, capsys, command, extra, key, value):
    result = json.loads(run_cli(updater, monkeypatch, capsys, command, *extra).out)
    assert result[key] == value
    assert writes(world) == []


@pytest.mark.parametrize("args,expected", [
    (["status", "--human"], "READY: 1"),
    (["update", "--dry-run", "--human"], "nginx:1.2.4"),
])
def test_human_cli_has_actionable_summary_without_writes(updater, world, monkeypatch, capsys, args, expected):
    assert expected in run_cli(updater, monkeypatch, capsys, *args).out
    assert writes(world) == []


def test_auto_cli_verifies_without_deploying(updater, world, monkeypatch, capsys):
    result = json.loads(run_cli(updater, monkeypatch, capsys, "auto", "--stack", "media").out)
    assert result["status"] == "OK" and result["deployment"] == "NOT_RUN"
    assert [payload["type"] for payload in writes(world)] == ["UpdateStack"]
    world.requests.clear()
    result = json.loads(run_cli(updater, monkeypatch, capsys, "auto").out)
    assert result["status"] == "NO_CHANGES"
    assert writes(world) == []


@pytest.mark.parametrize("answers,status,changes", [
    (["cancel"], "CANCELLED", 0), (["invalid"], "INVALID_SELECTION", 0),
    (["0"], "INVALID_SELECTION", 0), (["2"], "INVALID_SELECTION", 0),
    (["1", "n"], "CANCELLED", 0), (["1,1", "y"], "OK", 1), (["all", "yes"], "OK", 1),
])
def test_interactive_selection_and_confirmation(updater, world, monkeypatch, answers, status, changes):
    inputs = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))
    result = updater.interactive_update()
    assert result["status"] == status
    assert len(writes(world)) == changes
    assert all(payload["type"] == "UpdateStack" for payload in writes(world))
    assert not Path(updater.VERIFY_STATE_FILE).exists()


def test_interactive_cli_handles_no_updates(updater, world, monkeypatch, capsys):
    world.containers = []
    output = run_cli(updater, monkeypatch, capsys, "interactive").out
    assert '"count_selected": 0' in output
    assert writes(world) == []


@pytest.mark.parametrize("envelope", [False, True])
def test_wud_response_parses_watcher_and_ignores_unavailable_updates(updater, world, monkeypatch, envelope):
    raw = world.containers + [None, {"updateAvailable": False}]
    world.containers[0].pop("service")  # Fall back to container name.
    response = {"containers": raw} if envelope else raw
    monkeypatch.setattr(updater, "wud_request", lambda path: response)
    result = updater.get_wud_updates()
    assert len(result) == 1
    assert result[0]["server"] == "host-1"
    assert result[0]["service"] == "web"
    assert result[0]["target_tag"] == "1.2.4"


@pytest.mark.parametrize("response", [None, "invalid", {"containers": {}}])
def test_wud_rejects_invalid_payload(updater, monkeypatch, response):
    monkeypatch.setattr(updater, "wud_request", lambda path: response)
    with pytest.raises(RuntimeError, match="WUD"):
        updater.get_wud_updates()


@pytest.mark.parametrize("function,args,response", [
    ("list_stacks", (), {}), ("get_stack", ("s1",), []),
    ("get_update", ("op",), []), ("pull_stack_request", ("s1", ["web"]), []),
    ("deploy_stack_request", ("s1",), []),
])
def test_komodo_rejects_invalid_payload(updater, monkeypatch, function, args, response):
    monkeypatch.setattr(updater, "komodo_request", lambda *a: response)
    with pytest.raises(RuntimeError):
        getattr(updater, function)(*args)


@pytest.mark.parametrize("code", [408, 425, 429, 500, 502, 503, 504])
def test_transient_http_errors_retry_then_succeed(updater, monkeypatch, capsys, code):
    error = urllib.error.HTTPError("http://service.invalid", code, "retry", {}, io.BytesIO(b"temporary"))
    network = Mock(side_effect=[error, io.BytesIO(b'{"ok": true}')])
    sleep = Mock()
    monkeypatch.setattr(urllib.request, "urlopen", network)
    monkeypatch.setattr(updater.time, "sleep", sleep)
    assert updater.komodo_request("/read", {"type": "ListStacks"}) == {"ok": True}
    assert network.call_count == 2
    sleep.assert_called_once_with(1.0)
    output = capsys.readouterr()
    assert output.out == ""
    assert "retry 1/3" in output.err
    assert "test-secret" not in output.err


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_permanent_http_errors_are_not_retried(updater, monkeypatch, code):
    error = urllib.error.HTTPError("http://service.invalid", code, "denied", {}, io.BytesIO(b"denied"))
    network = Mock(side_effect=error)
    sleep = Mock()
    monkeypatch.setattr(urllib.request, "urlopen", network)
    monkeypatch.setattr(updater.time, "sleep", sleep)
    with pytest.raises(RuntimeError, match=f"HTTP {code}"):
        updater.wud_request("/api/containers")
    assert network.call_count == 1
    sleep.assert_not_called()


@pytest.mark.parametrize("error", [TimeoutError("timeout"), urllib.error.URLError("offline"), OSError("reset")])
def test_network_retries_are_bounded(updater, monkeypatch, error):
    network = Mock(side_effect=error)
    sleep = Mock()
    monkeypatch.setattr(urllib.request, "urlopen", network)
    monkeypatch.setattr(updater.time, "sleep", sleep)
    with pytest.raises(RuntimeError):
        updater.wud_request("/api/containers")
    assert network.call_count == 4
    assert [call.args[0] for call in sleep.call_args_list] == [1.0, 2.0, 4.0]


@pytest.mark.parametrize("body,expected", [(b"", {}), (b"[]", [])])
def test_empty_http_responses(updater, monkeypatch, body, expected):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(body))
    assert updater.komodo_request("/read") == expected


def test_invalid_json_is_not_retried(updater, monkeypatch):
    network = Mock(return_value=io.BytesIO(b"not json"))
    monkeypatch.setattr(urllib.request, "urlopen", network)
    with pytest.raises(json.JSONDecodeError):
        updater.wud_request("/api/containers")
    assert network.call_count == 1


@pytest.mark.parametrize("success,status", [(True, "DEPLOY_COMPLETED"), (False, "DEPLOY_FAILED")])
def test_follow_polls_and_emits_each_log_once(updater, world, monkeypatch, capsys, success, status):
    log = {"stage": "pull", "success": True, "stdout": "image downloaded"}
    world.polls = [
        {"status": "InProgress", "logs": [log]},
        {"status": "InProgress", "logs": [log]},
        {"status": "Complete", "success": success, "start_ts": 1000, "end_ts": 2000,
         "logs": [log, {"stage": "start", "success": success, "stderr": "last message"}]},
    ]
    ticks = [0.0]
    monkeypatch.setattr(updater.time, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(updater.time, "sleep", lambda interval: ticks.__setitem__(0, ticks[0] + interval))
    result = updater.follow_stack_deploy("media", {"_id": "operation-1"}, interval=2)
    assert result["status"] == status
    assert result["duration_seconds"] == 4.0
    assert result["komodo_duration_ms"] == 1000
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.count("image downloaded") == 1
    assert output.err.count("last message") == 1


def test_follow_times_out_without_sleeping_in_real_time(updater, world, monkeypatch):
    world.polls = [{"status": "InProgress"}] * 3
    ticks = [0.0]
    monkeypatch.setattr(updater.time, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(updater.time, "sleep", lambda interval: ticks.__setitem__(0, ticks[0] + interval))
    result = updater.follow_stack_deploy("media", {"_id": "operation-1"}, interval=2, timeout=3)
    assert result["status"] == "DEPLOY_TIMEOUT"
    assert result["last_update"]["status"] == "InProgress"
    assert len(world.requests) == 2


@pytest.mark.parametrize("response", [{}, [], {"_id": {}}, {"_id": ""}])
def test_follow_without_operation_id_does_not_poll(updater, world, response):
    assert updater.follow_stack_deploy("media", response)["status"] == "FOLLOW_UNAVAILABLE"
    assert world.requests == []


@pytest.mark.parametrize("service,expected", [
    ({"service": "web", "image": "nginx:1.2.4", "state": "Running"}, "RUNTIME_OK"),
    ({"service": "web", "current_image": "nginx:1.2.4", "status": "Healthy"}, "RUNTIME_OK"),
    ({"service": "web", "deployed_image": "nginx:old", "state": "Running"}, "RUNTIME_MISMATCH"),
    ({"service": "web", "image": "nginx:1.2.4", "state": "Exited"}, "RUNTIME_UNHEALTHY"),
    ({"service": "other"}, "RUNTIME_NOT_FOUND"),
])
def test_runtime_verifies_image_and_state(updater, world, service, expected):
    world.stack["info"]["services"] = [service]
    result = updater._verify_runtime_once([{"stack_id": "s1", "service": "web", "target_image": "nginx:1.2.4"}])
    assert result["items"][0]["status"] == expected
    assert result["count_verified"] == int(expected == "RUNTIME_OK")


@pytest.mark.parametrize("recovers", [False, True])
def test_runtime_waits_for_health_or_stops_at_deadline(updater, world, monkeypatch, recovers):
    service = world.stack["info"]["services"][0]
    service.update(image="nginx:1.2.4", state="Starting")
    ticks = [1000]
    monkeypatch.setattr(updater.time, "time", lambda: ticks[0])
    def advance(interval):
        ticks[0] += interval
        if recovers:
            service["state"] = "Healthy"
    monkeypatch.setattr(updater.time, "sleep", advance)
    result = updater.verify_runtime([{"stack_id": "s1", "service": "web", "target_image": "nginx:1.2.4"}], health_timeout=2, health_interval=1)
    assert result["status"] == ("OK" if recovers else "FAILED")
    if not recovers:
        assert result["reason"] == "RUNTIME_STABILITY_TIMEOUT"
    assert ticks[0] == (1001 if recovers else 1002)
