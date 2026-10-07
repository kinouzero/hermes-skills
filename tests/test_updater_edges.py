"""Defensive update and deployment behavior with malformed or changing inputs."""
import copy
import json
import runpy
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from test_docker_updater import planned_stack, wud_update
from test_updater_workflows import world, run_cli


@pytest.mark.parametrize("value", [None, "", "   "])
def test_empty_image_references(updater, value):
    assert updater.image_repository(value) is None
    assert updater.image_tag(value) is None
    assert updater.canonical_image(value) is None


def test_registry_only_or_untagged_images(updater):
    assert updater.normalized_repository(None) is None
    assert updater.canonical_image("nginx") == "nginx"
    assert updater.is_version_blocked(None, "1") is None
    assert updater.is_version_blocked(" ", "1") is None


@pytest.mark.parametrize("value", ["invalid", '42', '{"blocked": {}}'])
def test_invalid_blocklist_fails_closed(updater, value):
    Path(updater.BLOCKLIST_FILE).write_text(value)
    with pytest.raises(RuntimeError):
        updater.load_blocklist()


def test_legacy_blocklist_and_invalid_history(updater):
    Path(updater.BLOCKLIST_FILE).write_text('[" nginx:2 ", "", null]')
    assert updater.load_blocklist() == ["nginx:2"]
    Path(updater.HISTORY_FILE).write_text('{}')
    assert updater.load_history() == []
    updater.append_history({"service": "web", "komodo_stack": "a"})
    updater.append_history({"service": "db", "komodo_stack": "b"})
    assert updater.history_entries("a", None)[0]["service"] == "web"
    assert updater.history_entries(None, "db")[0]["komodo_stack"] == "b"


@pytest.mark.parametrize("function,key", [("require_komodo_env", "KOMODO_API_KEY"), ("require_wud_env", "WUD_USER"), ("require_wud_env", "WUD_PASSWORD")])
def test_missing_credentials_stop_before_requests(updater, monkeypatch, capsys, function, key):
    monkeypatch.setattr(updater, key, None)
    with pytest.raises(SystemExit):
        getattr(updater, function)()
    assert key in capsys.readouterr().out


def test_lock_can_wait_for_other_writer(updater, monkeypatch):
    real_flock = updater.fcntl.flock
    attempts = []
    def lock(fd, flags):
        if flags & updater.fcntl.LOCK_NB:
            attempts.append(fd)
            if len(attempts) == 1:
                raise BlockingIOError("busy")
        return real_flock(fd, flags)
    sleeper = Mock()
    monkeypatch.setattr(updater.fcntl, "flock", lock)
    monkeypatch.setattr(updater.time, "sleep", sleeper)
    with updater.updater_lock("waiting", timeout=1):
        assert json.loads(Path(updater.LOCK_FILE).read_text())["operation"] == "waiting"
    sleeper.assert_called_once_with(0.25)


@pytest.mark.parametrize("stack,expected", [
    ({}, ""), ({"config": None, "info": []}, ""),
    ({"config": {"file_contents": ""}, "info": {"deployed_contents": [None, {"path": "other", "contents": 42}]}}, ""),
    ({"info": {"deployed_contents": [None, {"path": "other", "contents": "fallback"}]}}, "fallback"),
    ({"info": {"deployed_contents": [{"path": "other", "contents": "other"}, {"path": "compose.yaml", "contents": "preferred"}]}}, "preferred"),
])
def test_legacy_compose_locations(updater, stack, expected):
    assert updater.get_stack_file_contents(stack) == expected
    assert updater.stack_name({"name": None}) is None
    assert updater.stack_services({"info": {"services": {}}}) == []


def test_resolution_rejects_duplicate_exact_and_prefixed_stacks(updater, stack_factory, wud_update):
    exact = stack_factory(name="media")
    assert updater.find_komodo_stack([exact, exact], wud_update) is None
    prefixed = stack_factory()
    assert updater.find_komodo_stack([prefixed, prefixed], wud_update) is None
    assert updater.find_komodo_stack([prefixed], {"stack": "media"}) is None
    assert updater.find_komodo_stack([exact], {"stack": "media", "service": "other"}) is None


def test_compose_parser_skips_other_fields_and_services(updater):
    contents = 'services:\n  web:\n    restart: always\n    # comment\n    image: nginx:1 # pinned\n  db:\n    image: postgres:16\n'
    assert updater.find_service_image(contents, "web") == "nginx:1"
    updated, changed, old = updater.replace_service_image(contents, "web", "nginx:2")
    assert changed and old == "nginx:1"
    assert "postgres:16" in updated
    no_image = 'services:\n  web:\n    restart: always\n  db:\n    image: postgres:16\n'
    assert updater.find_service_image(no_image, "web") is None
    assert updater.replace_service_image(no_image, "web", "nginx:2") == (no_image, False, None)
    assert updater.service_matches({"service": "web"}, None) is True


@pytest.mark.parametrize("case,reason", [
    ("no-stack", "NO_KOMODO_STACK"), ("unsupported", "UNSUPPORTED_UPDATE_KIND"),
    ("get-fails", "GET_STACK_FAILED"), ("no-compose", "NO_COMPOSE_CONTENTS"),
    ("missing-service", "SERVICE_IMAGE_NOT_FOUND"), ("digest-mismatch", "DIGEST_IMAGE_DOES_NOT_MATCH_COMPOSE"),
    ("missing-target", "NO_TARGET_TAG"),
])
def test_plan_reports_unusable_updates(updater, planned_stack, wud_update, monkeypatch, case, reason):
    if case == "no-stack":
        monkeypatch.setattr(updater, "list_stacks", lambda: [])
    elif case == "unsupported":
        wud_update["update_kind"] = "unknown"
    elif case == "get-fails":
        monkeypatch.setattr(updater, "get_stack", Mock(side_effect=OSError("offline")))
    elif case == "no-compose":
        planned_stack["config"]["file_contents"] = ""
    elif case == "missing-service":
        planned_stack["config"]["file_contents"] = "services:\n  db:\n    image: postgres:16\n"
    elif case == "digest-mismatch":
        wud_update.update(update_kind="digest", current_tag="different")
    else:
        wud_update["target_tag"] = None
    assert updater.build_plan(include_digest=True)["items"][0]["reason"] == reason


def test_plan_can_use_compose_repository_when_wud_omits_it(updater, planned_stack, wud_update):
    wud_update["image"] = None
    assert updater.build_plan()["items"][0]["target_image"] == "nginx:1.2.4"


@pytest.mark.parametrize("case,reason", [
    ("read", "GET_STACK_BEFORE_UPDATE_FAILED"), ("missing", "SERVICE_IMAGE_NOT_FOUND_DURING_UPDATE"),
    ("replace", "SERVICE_IMAGE_NOT_REPLACED"), ("write", "UPDATE_STACK_FAILED"),
])
def test_preparation_faults_are_returned_without_deploy(updater, planned_stack, monkeypatch, case, reason):
    original = copy.deepcopy(planned_stack)
    if case == "read":
        monkeypatch.setattr(updater, "get_stack", Mock(side_effect=[original, OSError("offline")]))
    elif case == "missing":
        changed = copy.deepcopy(original)
        changed["config"]["file_contents"] = "services: {}"
        monkeypatch.setattr(updater, "get_stack", Mock(side_effect=[original, changed]))
    elif case == "replace":
        monkeypatch.setattr(updater, "replace_service_image", lambda text, *a: (text, False, None))
    else:
        monkeypatch.setattr(updater, "update_stack_file_contents", Mock(side_effect=OSError("write failed")))
    result = updater.update_ready_stacks(confirm=True)
    assert result["items"][0]["reason"] == reason
    assert result["count_updated"] == 0
    assert not Path(updater.HISTORY_FILE).exists()


@pytest.mark.parametrize("plan", [None, {"items": {}}, "invalid"])
def test_verify_rejects_invalid_plan_shape(updater, plan):
    with pytest.raises(RuntimeError):
        updater.verify_updates(plan)


def test_verify_skips_non_actionable_items_and_filters(updater, planned_stack):
    item = updater.build_plan()["items"][0]
    assert updater.verify_updates({"items": [None, {"status": "SKIP"}]}) == []
    assert updater.verify_updates([item], stack_filter="other") == []
    assert updater.verify_updates([item], service_filter="other") == []
    assert updater.verify_updates([item], stack_filter="media")[0]["status"] == "NOT_UPDATED"


@pytest.mark.parametrize("stack,reason", [
    (None, "VERIFY_FAILED"), ({}, "VERIFY_FAILED"), ({"config": {}}, "VERIFY_FAILED"),
    ({"config": {"file_contents": "services: {}"}}, "SERVICE_IMAGE_NOT_FOUND"),
])
def test_verify_requires_prepared_compose(updater, planned_stack, monkeypatch, stack, reason):
    plan = updater.build_plan()
    monkeypatch.setattr(updater, "get_stack", lambda _: stack)
    assert updater.verify_updates(plan)[0]["reason"] == reason


def test_verify_requires_target_image(updater, planned_stack):
    item = updater.build_plan()["items"][0]
    item["target_image"] = None
    assert updater.verify_updates([item])[0]["reason"] == "TARGET_IMAGE_MISSING"


@pytest.mark.parametrize("case,expected", [
    ("scope", "ERROR"), ("confirmation", "CONFIRMATION_REQUIRED"),
    ("missing-history", "ROLLBACK_NOT_FOUND"), ("incomplete", "ROLLBACK_ERROR"),
    ("missing-image", "ROLLBACK_ERROR"), ("no-change", "ROLLBACK_NOT_NEEDED"),
])
def test_rollback_guards(updater, planned_stack, monkeypatch, case, expected):
    if case in {"incomplete", "missing-image", "no-change"}:
        updater.append_history({"komodo_stack": "host-1-media", "service": "web", "stack_id": "s1",
                                "old_image": None if case == "incomplete" else "nginx:1.2.2"})
    if case == "missing-image":
        planned_stack["config"]["file_contents"] = "services: {}"
    if case == "no-change":
        monkeypatch.setattr(updater, "replace_service_image", lambda text, *a: (text, False, None))
    result = updater.rollback_update("" if case == "scope" else "host-1-media", "web", confirm=case != "confirmation")
    assert result["status"] == expected


def test_runtime_reports_missing_fields_and_fetch_errors(updater, monkeypatch):
    assert updater.runtime_service_image({}) is None
    assert updater.runtime_service_state({}) is None
    monkeypatch.setattr(updater, "get_stack", Mock(side_effect=OSError("offline")))
    result = updater._verify_runtime_once([{"stack_id": "s1", "service": "web"}])
    assert result["items"][0]["reason"] == "RUNTIME_VERIFY_GET_STACK_FAILED"


def test_verification_state_rejects_missing_or_changed_configuration(updater, verified_stacks):
    assert updater.validate_verify_state({"absent"})["status"] == "VERIFY_REQUIRED"
    verified_stacks["s1"]["config"] = {}
    assert updater.validate_verify_state({"s1"})["status"] == "CONFIG_CHANGED"
    with pytest.raises(RuntimeError):
        updater.save_verify_state([{"status": "UPDATED", "stack_id": "s1"}])
    updater.clear_verify_state()
    updater.clear_verify_state()
    assert updater.validate_verify_state()["status"] == "VERIFY_REQUIRED"


def test_verify_state_skips_incomplete_items_and_deduplicates_services(updater, verified_stacks):
    state = updater.save_verify_state([
        {"status": "ERROR", "stack_id": "bad"}, {"status": "UPDATED"},
        {"status": "UPDATED", "stack_id": "s1", "service": "web"},
        {"status": "UPDATED", "stack_id": "s1", "service": "web"},
        {"status": "UPDATED", "stack_id": "s2"},
    ])
    assert len(state["items"]) == 2
    assert next(x for x in state["items"] if x["stack_id"] == "s1")["services"] == ["web"]


@pytest.mark.parametrize("state", [[], {}, {"items": {}}])
def test_invalid_verify_state_is_not_deployable(updater, state):
    path = Path(updater.VERIFY_STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))
    assert updater.load_verify_state() is None
    assert updater.deploy_verified_stacks(True)["status"] == "DEPLOY_BLOCKED"


def test_deploy_skips_malformed_state_entries(updater):
    path = Path(updater.VERIFY_STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"items": [null, {}]}')
    result = updater.deploy_verified_stacks(True)
    assert result["reason"] == "NO_VERIFIED_STACKS_MATCH_FILTER"


def test_deploy_request_failure_keeps_verification(updater, verified_stacks, monkeypatch):
    monkeypatch.setattr(updater, "deploy_stack_request", Mock(side_effect=OSError("unavailable")))
    result = updater.deploy_stacks(confirm=True)
    assert result["status"] == "PARTIAL_OR_FAILED"
    assert all(item["status"] == "ERROR" for item in result["items"])
    assert len(updater.load_verify_state()["items"]) == 2


def test_auto_does_not_save_failed_verification(updater, planned_stack, monkeypatch):
    monkeypatch.setattr(updater, "update_stack_file_contents", lambda *a: {})
    # The API accepted a write but GetStack still returns the old Compose.
    result = updater.auto_update()
    assert result["status"] == "FAILED"
    assert result["verify"]["verify_state"] is None
    assert not Path(updater.VERIFY_STATE_FILE).exists()


@pytest.mark.parametrize("response", [None, {}, "invalid"])
def test_preflight_reports_service_failure(updater, world, monkeypatch, response):
    monkeypatch.setattr(updater, "wud_request", lambda path: response)
    assert updater.preflight()["status"] == "FAILED"


def test_cli_errors_and_interrupts_are_structured(updater, world, monkeypatch, capsys):
    for error, code in [(OSError("failed"), 1), (KeyboardInterrupt(), 130)]:
        monkeypatch.setattr(updater, "show_plan", Mock(side_effect=error))
        with pytest.raises(SystemExit) as exc:
            run_cli(updater, monkeypatch, capsys, "--debug", "--verbose", "plan")
        assert exc.value.code == code
        result = capsys.readouterr()
        assert "commande=plan" in result.err and "debug:" in result.err
        assert json.loads(result.out)


def test_script_entrypoint_help(updater, monkeypatch):
    monkeypatch.setattr(sys, "argv", [updater.__file__, "--help"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(updater.__file__, run_name="__main__")
    assert exc.value.code == 0


def test_deployment_logs_handle_partial_entries(updater, capsys):
    for entry in [None, {}, {"stdout": " \n "}, {"command": "pull", "stdout": "done"}]:
        updater.emit_deploy_log(entry, 0)
    output = capsys.readouterr()
    assert output.out == "" and "done" in output.err
    assert updater.format_duration(3601) == "01:00:01"


def test_follow_completion_without_timing_metadata(updater, monkeypatch):
    monkeypatch.setattr(updater, "get_update", lambda _: {"status": "Complete", "success": True})
    assert "komodo_duration_ms" not in updater.follow_stack_deploy("a", {"_id": "id"})


def test_dry_run_without_actionable_items(updater, capsys):
    updater.print_dry_run_human({"items": []})
    assert "Aucune mise à jour actionnable" in capsys.readouterr().out
