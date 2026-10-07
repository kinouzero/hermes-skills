import json
from pathlib import Path
from unittest.mock import Mock, call

import pytest


@pytest.fixture
def wud_update():
    return {"stack": "media", "service": "web", "server": "host-1", "image": "nginx",
            "current_tag": "1.2.3", "target_tag": "1.2.4", "update_kind": "tag", "semver_diff": "patch"}


@pytest.fixture
def planned_stack(updater, stack_factory, wud_update, monkeypatch):
    stack = stack_factory()
    monkeypatch.setattr(updater, "get_wud_updates", lambda: [wud_update])
    monkeypatch.setattr(updater, "list_stacks", lambda: [stack])
    monkeypatch.setattr(updater, "get_stack", lambda stack_id: stack)
    return stack


@pytest.mark.parametrize("image,repository,tag,new_image", [
    ("nginx:1.2.3", "nginx", "1.2.3", "nginx:2.0.0"),
    ("registry.invalid:5000/team/app:1.0", "registry.invalid:5000/team/app", "1.0", "registry.invalid:5000/team/app:2.0.0"),
    ("registry.invalid:5000/team/app", "registry.invalid:5000/team/app", None, "registry.invalid:5000/team/app:2.0.0"),
    ("ghcr.io/team/app:latest@sha256:abc", "ghcr.io/team/app", "latest", "ghcr.io/team/app:2.0.0"),
])
def test_image_references_preserve_registry(updater, image, repository, tag, new_image):
    assert updater.image_repository(image) == repository
    assert updater.image_tag(image) == tag
    assert updater.replace_image_tag(image, "2.0.0") == new_image


def test_compose_replacement_changes_only_requested_service(updater):
    compose = 'services:\n  web: # public\n    image: "nginx:1.2.3" # pin\n  worker:\n    image: nginx:1.2.3\n'
    updated, changed, old = updater.replace_service_image(compose, "web", "nginx:1.2.4")
    assert changed
    assert old.strip('"') == "nginx:1.2.3"
    assert updater.find_service_image(updated, "web") == "nginx:1.2.4"
    assert updater.find_service_image(updated, "worker") == "nginx:1.2.3"
    assert "# pin" in updated
    assert updated.endswith("    image: nginx:1.2.3\n")
    assert updater.replace_service_image(compose, "missing", "nginx:2") == (compose, False, None)


def test_stack_resolution_prefers_exact_name(updater, stack_factory, wud_update):
    exact = stack_factory("exact", "media")
    assert updater.find_komodo_stack([stack_factory(), exact], wud_update) == exact


def test_stack_resolution_requires_service_for_inferred_name(updater, stack_factory, wud_update):
    stack = stack_factory()
    stack["info"]["services"] = [{"service": "other"}]
    assert updater.find_komodo_stack([stack], wud_update) is None


def test_stack_resolution_rejects_ambiguous_hosts(updater, stack_factory, wud_update):
    wud_update.pop("server")
    stacks = [stack_factory(), stack_factory("s2", "host-2-media", "host-2")]
    assert updater.find_komodo_stack(stacks, wud_update) is None


@pytest.mark.parametrize("name,host", [("host-2-media", "host-2"), ("host-1-media", None), ("media", "host-2")])
def test_explicit_wud_host_never_falls_back_to_incompatible_stack(updater, stack_factory, wud_update, name, host):
    assert updater.find_komodo_stack([stack_factory(name=name, host=host)], wud_update) is None


@pytest.mark.parametrize("diff,allow_major,status", [
    ("patch", False, "READY"), ("minor", False, "READY"),
    ("major", False, "SKIP"), ("major", True, "READY"),
])
def test_version_policy(updater, planned_stack, wud_update, diff, allow_major, status):
    wud_update["semver_diff"] = diff
    item = updater.build_plan(update_major=allow_major)["items"][0]
    assert item["status"] == status
    assert updater.find_service_image(planned_stack["config"]["file_contents"], "web") == "nginx:1.2.3"


def test_blocklist_normalizes_ghcr_only(updater):
    Path(updater.BLOCKLIST_FILE).write_text(json.dumps({"blocked": ["team/app:2.0"]}))
    assert updater.is_version_blocked("ghcr.io/team/app:1.0", "2.0") == "team/app:2.0"
    assert updater.is_version_blocked("private.invalid/team/app:1.0", "2.0") is None


def test_plan_skips_blocked_target(updater, planned_stack):
    Path(updater.BLOCKLIST_FILE).write_text('{"blocked": ["nginx:1.2.4"]}')
    assert updater.build_plan()["items"][0]["reason"] == "VERSION_BLOCKED"


def test_plan_rejects_registry_conflict(updater, planned_stack, wud_update):
    wud_update["image"] = "different.invalid/nginx"
    item = updater.build_plan()["items"][0]
    assert item["status"] == "CONFLICT"
    assert item["reason"] == "IMAGE_REPOSITORY_MISMATCH"


@pytest.mark.parametrize("confirm,dry_run,status", [(False, False, "CONFIRMATION_REQUIRED"), (True, True, "DRY_RUN")])
def test_update_requires_confirmation_and_respects_dry_run(updater, planned_stack, komodo_api, confirm, dry_run, status):
    assert updater.update_ready_stacks(confirm=confirm, dry_run=dry_run)["status"] == status
    komodo_api.assert_not_called()
    assert not Path(updater.HISTORY_FILE).exists()


def test_update_prepares_compose_and_history_without_deploy(updater, planned_stack, komodo_api):
    result = updater.update_ready_stacks(confirm=True)
    assert result["count_updated"] == 1
    assert komodo_api.call_count == 1
    endpoint, payload = komodo_api.call_args.args
    assert endpoint == "/write"
    assert payload["type"] == "UpdateStack"
    assert updater.load_history()[0]["new_image"] == "nginx:1.2.4"
    assert not Path(updater.VERIFY_STATE_FILE).exists()


def test_update_aborts_on_plan_conflict(updater, planned_stack, wud_update, komodo_api):
    wud_update["image"] = "other/app"
    assert updater.update_ready_stacks(confirm=True)["status"] == "ABORTED"
    komodo_api.assert_not_called()


def test_update_rechecks_compose_before_write(updater, planned_stack, monkeypatch, komodo_api, stack_factory):
    monkeypatch.setattr(updater, "get_stack", Mock(side_effect=[planned_stack, stack_factory(image="nginx:9.0")]))
    result = updater.update_ready_stacks(confirm=True)
    assert result["items"][0]["reason"] == "COMPOSE_CHANGED_SINCE_PLAN"
    komodo_api.assert_not_called()


def test_digest_preparation_does_not_change_compose(updater, planned_stack, wud_update, komodo_api):
    wud_update.update(update_kind="digest", target_digest="sha256:new")
    assert updater.build_plan()["items"][0]["reason"] == "DIGEST_UPDATE_REPORT_ONLY"
    before = planned_stack["config"]["file_contents"]
    result = updater.update_ready_stacks(confirm=True, include_digest=True)
    assert result["items"][0]["status"] == "DIGEST_REDEPLOY_PREPARED"
    assert result["items"][0]["compose_modified"] is False
    assert planned_stack["config"]["file_contents"] == before
    komodo_api.assert_not_called()
    assert updater.load_history()[0]["action"] == "DIGEST_REDEPLOY_PREPARED"


def test_digest_pinned_image_cannot_be_redeployed_to_new_digest(updater, planned_stack, wud_update):
    wud_update.update(update_kind="digest", target_digest="sha256:new")
    planned_stack["config"]["file_contents"] = "services:\n  web:\n    image: nginx:1.2.3@sha256:old\n"
    assert updater.build_plan(include_digest=True)["items"][0]["reason"] == "DIGEST_PINNED_IMAGE_CANNOT_UPDATE_BY_REDEPLOY"


def test_verify_uses_prepared_config_not_deployed_contents(updater, planned_stack):
    planned_stack["info"]["deployed_contents"] = "services:\n  web:\n    image: nginx:1.2.4\n"
    plan = updater.build_plan()
    result = updater.verify_updates(plan["items"])
    assert result[0]["status"] == "NOT_UPDATED"
    planned_stack["config"]["file_contents"] = planned_stack["info"]["deployed_contents"]
    assert updater.verify_updates(plan["items"])[0]["status"] == "UPDATED"


def test_auto_prepares_and_verifies_but_never_deploys(updater, planned_stack, monkeypatch):
    writes = []
    def api(endpoint, payload):
        writes.append((endpoint, payload))
        assert endpoint == "/write"
        assert payload["type"] == "UpdateStack"
        planned_stack["config"]["file_contents"] = payload["params"]["config"]["file_contents"]
        return {"ok": True}
    monkeypatch.setattr(updater, "komodo_request", api)
    result = updater.auto_update()
    assert result["status"] == "OK"
    assert result["deployment"] == "NOT_RUN"
    assert len(writes) == 1
    assert updater.validate_verify_state()["status"] == "OK"


def test_rollback_prepares_previous_image_without_deploy(updater, planned_stack, komodo_api):
    updater.append_history({"stack_id": "s1", "komodo_stack": "host-1-media", "service": "web", "old_image": "nginx:1.2.2"})
    result = updater.rollback_update("host-1-media", "web", confirm=True)
    assert result["status"] == "ROLLBACK_PREPARED"
    assert result["deployment"] == "NOT_RUN"
    assert updater.load_history()[-1]["new_image"] == "nginx:1.2.2"
    assert komodo_api.call_count == 1
    assert komodo_api.call_args.args[0] == "/write"


def test_lock_blocks_concurrent_operation_and_releases_after_failure(updater):
    with pytest.raises(ValueError):
        with updater.updater_lock("update"):
            with pytest.raises(RuntimeError, match="déjà en cours"):
                with updater.updater_lock("deploy"):
                    pytest.fail("Concurrent operation acquired the lock")
            raise ValueError("operation failed")
    with updater.updater_lock("retry"):
        pass


def test_verify_state_merges_stacks(updater, verified_stacks):
    updater.save_verify_state([{"status": "UPDATED", "stack_id": "s1", "komodo_stack": "host-1-media", "service": "web"}])
    assert {x["stack_id"] for x in updater.load_verify_state()["items"]} == {"s1", "s2"}
    assert updater.validate_verify_state()["status"] == "OK"


def test_deploy_requires_confirmation(updater, verified_stacks, komodo_api):
    assert updater.deploy_stacks(confirm=False)["status"] == "CONFIRMATION_REQUIRED"
    komodo_api.assert_not_called()


def test_deploy_requires_verified_state(updater, komodo_api):
    assert updater.deploy_stacks(confirm=True)["reason"] == "NO_VERIFIED_STACKS"
    komodo_api.assert_not_called()


def test_deploy_rejects_unverified_requested_stack(updater, verified_stacks, komodo_api):
    result = updater.deploy_stacks(confirm=True, stack_filter="host-1-media,unknown")
    assert result["reason"] == "STACK_NOT_VERIFIED"
    komodo_api.assert_not_called()


def test_deploy_rejects_compose_changed_since_verify(updater, verified_stacks, komodo_api):
    verified_stacks["s1"]["config"]["file_contents"] += "# manual change\n"
    result = updater.deploy_stacks(confirm=True)
    assert result["status"] == "DEPLOY_BLOCKED"
    assert result["guard"]["status"] == "CONFIG_CHANGED"
    komodo_api.assert_not_called()


def test_deploy_targets_only_requested_verified_stack(updater, verified_stacks, komodo_api):
    result = updater.deploy_stacks(confirm=True, stack_filter="host-2-media")
    assert result["status"] == "DEPLOY_STARTED"
    komodo_api.assert_called_once_with("/execute", {"type": "DeployStack", "params": {"stack": "s2"}})
    assert len(updater.load_verify_state()["items"]) == 2  # Accepted is not completed.


def test_follow_removes_only_successful_stacks(updater, verified_stacks, komodo_api, monkeypatch):
    monkeypatch.setattr(updater, "follow_stack_deploy", Mock(side_effect=[
        {"status": "DEPLOY_COMPLETED"}, {"status": "DEPLOY_FAILED"}]))
    result = updater.deploy_stacks(confirm=True, follow=True)
    assert result["status"] == "PARTIAL_OR_FAILED"
    assert result["count_deploy_success"] == 1
    assert [item["stack_id"] for item in updater.load_verify_state()["items"]] == ["s2"]


@pytest.mark.parametrize("pull_status,expected_calls", [("PULL_COMPLETED", 2), ("PULL_FAILED", 1), ("PULL_TIMEOUT", 1)])
def test_digest_pull_must_succeed_before_deploy(updater, verified_stacks, komodo_api, monkeypatch, pull_status, expected_calls):
    updater.save_verify_state([{"status": "DIGEST_VERIFIED", "stack_id": "s1", "komodo_stack": "host-1-media",
                               "service": "web", "target_image": "nginx:1.2.3", "action": "digest_redeploy"}])
    monkeypatch.setattr(updater, "follow_stack_deploy", Mock(return_value={"status": pull_status}))
    result = updater.deploy_stacks(confirm=True, stack_filter="s1")
    assert komodo_api.call_args_list[0] == call("/execute", {"type": "PullStack", "params": {"stack": "s1", "services": ["web"]}})
    assert komodo_api.call_count == expected_calls
    if pull_status == "PULL_COMPLETED":
        assert komodo_api.call_args_list[1] == call("/execute", {"type": "DeployStack", "params": {"stack": "s1"}})
    else:
        assert result["status"] == "PARTIAL_OR_FAILED"


def test_failed_runtime_check_preserves_verified_state(updater, verified_stacks, komodo_api, monkeypatch):
    monkeypatch.setattr(updater, "follow_stack_deploy", Mock(return_value={"status": "DEPLOY_COMPLETED"}))
    monkeypatch.setattr(updater, "verify_runtime", Mock(return_value={"status": "FAILED"}))
    result = updater.deploy_stacks(confirm=True, stack_filter="s1", follow=True, verify_runtime_after=True)
    assert result["status"] == "PARTIAL_OR_FAILED"
    assert len(updater.load_verify_state()["items"]) == 2


def test_hostless_resolution_still_accepts_one_unambiguous_stack(updater, stack_factory, wud_update):
    wud_update.pop("server")
    stack = stack_factory()
    assert updater.find_komodo_stack([stack], wud_update) == stack


def test_explicit_host_accepts_one_matching_suffix(updater, stack_factory, wud_update):
    stack = stack_factory(name="production-media")
    assert updater.find_komodo_stack([stack], wud_update) == stack


def test_status_cli_returns_json_without_writes(updater, planned_stack, komodo_api, monkeypatch, capsys):
    monkeypatch.setattr(updater.sys, "argv", ["docker-update.py", "status", "--stack", "host-1-media"])
    updater.main()
    result = json.loads(capsys.readouterr().out)
    assert result["ready"] == 1
    assert result["errors"] == 0
    komodo_api.assert_not_called()


def test_missing_komodo_credentials_fail_before_network(updater, monkeypatch, capsys):
    monkeypatch.setattr(updater, "KOMODO_API_SECRET", None)
    monkeypatch.setattr(updater.sys, "argv", ["docker-update.py", "status"])
    with pytest.raises(SystemExit) as error:
        updater.main()
    assert error.value.code == 1
    output = capsys.readouterr().out
    assert "KOMODO_API_SECRET" in output
    assert "test-key" not in output
    assert "test-password" not in output
