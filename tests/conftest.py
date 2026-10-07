"""Load the versioned scripts without running their CLI or using live services."""
import importlib.util
import socket
import urllib.request
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script(name, filename):
    path = ROOT / name / "v1.0.0" / "scripts" / filename
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    for key, value in {
        "KOMODO_URL": "https://komodo.invalid",
        "KOMODO_API_KEY": "test-key",
        "KOMODO_API_SECRET": "test-secret",
        "WUD_URL": "https://wud.invalid",
        "WUD_USER": "test-user",
        "WUD_PASSWORD": "test-password",
        "NTFY_ENDPOINT": "https://ntfy.invalid",
        "NTFY_TOKEN": "test-token",
        "HERMES_WRITE_SAFE_ROOT": str(tmp_path),
        "DOCKER_UPDATER_BLOCKLIST": str(tmp_path / "blocklist.json"),
        "DOCKER_UPDATER_HISTORY": str(tmp_path / "history.json"),
        "DOCKER_UPDATER_LOCK": str(tmp_path / ".lock"),
    }.items():
        monkeypatch.setenv(key, value)

    def deny_network(*args, **kwargs):
        pytest.fail("Unexpected network access: mock the external service")

    monkeypatch.setattr(urllib.request, "urlopen", deny_network)
    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", deny_network)


@pytest.fixture
def health(isolated_environment):
    return load_script("docker-health", "docker-health.py")


@pytest.fixture
def updater(isolated_environment):
    return load_script("docker-updater", "docker-update.py")


@pytest.fixture
def health_config(tmp_path):
    return {
        "hosts": [{"name": "host-1", "mode": "remote", "url": "http://docker.invalid"}],
        "ntfy": {"enabled": False, "topic": "tests", "cooldown_seconds": 900},
        "options": {
            "ack_state_file": str(tmp_path / "acks.json"),
            "silence_state_file": str(tmp_path / "silences.json"),
            "ntfy_state_file": str(tmp_path / "ntfy.json"),
            "ntfy_history_file": str(tmp_path / "ntfy-history.json"),
        },
    }


@pytest.fixture
def stack_factory():
    def make(stack_id="s1", name="host-1-media", host="host-1", image="nginx:1.2.3"):
        return {
            "id": stack_id,
            "name": name,
            "config": {"file_contents": f"services:\n  web:\n    image: {image}\n"},
            "info": {"server_name": host, "services": [{"service": "web"}]},
        }
    return make


@pytest.fixture
def verified_stacks(updater, stack_factory, monkeypatch):
    stacks = {
        "s1": stack_factory(),
        "s2": stack_factory("s2", "host-2-media", "host-2"),
    }
    monkeypatch.setattr(updater, "get_stack", lambda stack_id: stacks[stack_id])
    updater.save_verify_state([
        {"status": "UPDATED", "stack_id": key, "komodo_stack": stack["name"],
         "service": "web", "target_image": "nginx:1.2.3"}
        for key, stack in stacks.items()
    ])
    return stacks


@pytest.fixture
def komodo_api(updater, monkeypatch):
    api = Mock(return_value={"id": "operation-1"})
    monkeypatch.setattr(updater, "komodo_request", api)
    return api
