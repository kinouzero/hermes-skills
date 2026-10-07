#!/usr/bin/env python3

import argparse
import hashlib
from contextlib import contextmanager
import fcntl
import json
import os
import time
import re
import sys
from typing import Any, Dict, List, Optional, Tuple


# ============================================================
# CONFIGURATION
# ============================================================

KOMODO_URL = os.getenv(
    "KOMODO_URL",
    "http://komodo:9120",
).rstrip("/")

KOMODO_API_KEY = os.getenv(
    "KOMODO_API_KEY"
)

KOMODO_API_SECRET = os.getenv(
    "KOMODO_API_SECRET"
)

WUD_URL = os.getenv(
    "WUD_URL",
    "http://wud:3000",
).rstrip("/")

WUD_USER = os.getenv(
    "WUD_USER"
)

WUD_PASSWORD = os.getenv(
    "WUD_PASSWORD"
)

HTTP_TIMEOUT = 30
HTTP_RETRIES = 3
HTTP_RETRY_BASE_DELAY = 1.0
HTTP_RETRY_MAX_DELAY = 8.0

BLOCKLIST_FILE = os.getenv(
    "DOCKER_UPDATER_BLOCKLIST",
    "/opt/data/.hermes/skills/docker-updater/update-blocklist.json",
)

HISTORY_FILE = os.getenv(
    "DOCKER_UPDATER_HISTORY",
    "/opt/data/.hermes/skills/docker-updater/update-history.json",
)
LOCK_FILE = os.getenv(
    "DOCKER_UPDATER_LOCK",
    "/opt/data/.hermes/skills/docker-updater/.lock",
)
HISTORY_LIMIT = 500


# ============================================================
# GENERAL
# ============================================================

def fail(
    message: str,
    code: int = 1,
) -> None:

    print(
        json.dumps(
            {
                "status": "ERROR",
                "message": message,
            },
            indent=2,
            ensure_ascii=False,
        )
    )

    sys.exit(code)


def print_json(
    data: Any,
) -> None:

    print(
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False,
        )
    )


@contextmanager
def updater_lock(operation: str, timeout: float = 0.0):
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    handle = open(LOCK_FILE, "a+", encoding="utf-8")
    start = time.time()
    try:
        while True:
            try:
                flags = fcntl.LOCK_EX | fcntl.LOCK_NB
                fcntl.flock(handle.fileno(), flags)
                break
            except BlockingIOError:
                if timeout <= 0 or time.time() - start >= timeout:
                    raise RuntimeError(f"Une autre opération docker-updater est déjà en cours: {operation}")
                time.sleep(0.25)
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"operation": operation, "pid": os.getpid(), "started_at": time.time()}))
        handle.flush()
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def load_history() -> List[Dict[str, Any]]:
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return []
    if not isinstance(data, list):
        return []
    return data


def append_history(entry: Dict[str, Any]) -> Dict[str, Any]:
    history = load_history()
    history.append({"timestamp": time.time(), **entry})
    history = history[-HISTORY_LIMIT:]
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(history, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, HISTORY_FILE)
    return history[-1]


def history_entries(stack_filter: Optional[str] = None, service_filter: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    entries = load_history()
    if stack_filter:
        stacks = {x.strip() for x in stack_filter.split(",") if x.strip()}
        entries = [x for x in entries if x.get("komodo_stack") in stacks]
    if service_filter:
        services = {x.strip() for x in service_filter.split(",") if x.strip()}
        entries = [x for x in entries if x.get("service") in services]
    return list(reversed(entries[-max(1, limit):]))


def rollback_info(stack_filter: Optional[str] = None, service_filter: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
    entries = history_entries(stack_filter, service_filter, limit)
    return {"status": "OK", "count": len(entries), "items": entries}


def rollback_update(stack_filter: str, service_filter: str, confirm: bool = False, lock_timeout: float = 0.0) -> Dict[str, Any]:
    if not stack_filter or not service_filter:
        return {"status": "ERROR", "reason": "STACK_AND_SERVICE_REQUIRED"}
    if not confirm:
        return {"status": "CONFIRMATION_REQUIRED", "message": "Utilise --confirm pour préparer le rollback."}
    with updater_lock("rollback", lock_timeout):
        entries = history_entries(stack_filter, service_filter, 1)
        if not entries:
            return {"status": "ROLLBACK_NOT_FOUND", "stack": stack_filter, "service": service_filter}
        entry = entries[0]
        stack_id = entry.get("stack_id")
        old_image = entry.get("old_image")
        if not stack_id or not old_image:
            return {"status": "ROLLBACK_ERROR", "reason": "HISTORY_ENTRY_INCOMPLETE", "item": entry}
        stack = get_stack(stack_id)
        contents = get_stack_file_contents(stack)
        current_image = find_service_image(contents, service_filter)
        if not current_image:
            return {"status": "ROLLBACK_ERROR", "reason": "SERVICE_IMAGE_NOT_FOUND"}
        new_contents, changed, previous_image = replace_service_image(contents, service_filter, old_image)
        if not changed:
            return {"status": "ROLLBACK_NOT_NEEDED", "current_image": current_image, "target_image": old_image}
        response = update_stack_file_contents(stack_id, new_contents)
        rollback_entry = append_history({"action": "ROLLBACK_PREPARED", "stack_id": stack_id, "komodo_stack": entry.get("komodo_stack"), "service": service_filter, "old_image": current_image, "new_image": old_image, "source_history_timestamp": entry.get("timestamp"), "komodo_response": response})
        return {"status": "ROLLBACK_PREPARED", "deployment": "NOT_RUN", "item": rollback_entry}


def require_komodo_env() -> None:

    missing = []

    if not KOMODO_API_KEY:
        missing.append(
            "KOMODO_API_KEY"
        )

    if not KOMODO_API_SECRET:
        missing.append(
            "KOMODO_API_SECRET"
        )

    if missing:

        fail(
            "Variables d'environnement Komodo manquantes: "
            + ", ".join(missing)
        )


def require_wud_env() -> None:

    missing = []

    if not WUD_USER:
        missing.append(
            "WUD_USER"
        )

    if not WUD_PASSWORD:
        missing.append(
            "WUD_PASSWORD"
        )

    if missing:

        fail(
            "Variables d'environnement WUD manquantes: "
            + ", ".join(missing)
        )


def require_env_for_command(command: str) -> None:
    """Valide uniquement les dépendances nécessaires à la commande."""

    require_komodo_env()

    if command in {
        "wud-updates",
        "plan",
        "update",
        "verify",
    }:
        require_wud_env()


# ============================================================
# VERSION BLOCKLIST
# ============================================================

def load_blocklist() -> List[str]:
    """Charge les références image:tag explicitement bloquées."""
    try:
        with open(BLOCKLIST_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Fichier de blacklist invalide: {BLOCKLIST_FILE}: {exc}"
        )

    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        entries = data.get("blocked", [])
    else:
        raise RuntimeError(
            f"Format de blacklist invalide: {BLOCKLIST_FILE}"
        )

    if not isinstance(entries, list):
        raise RuntimeError(
            f"La clé 'blocked' doit être une liste: {BLOCKLIST_FILE}"
        )

    return [
        str(entry).strip()
        for entry in entries
        if isinstance(entry, str) and entry.strip()
    ]


def is_version_blocked(
    image: Optional[str],
    tag: Optional[str],
) -> Optional[str]:
    """Retourne la référence bloquée correspondante, sinon None."""
    if not image or not tag:
        return None

    repository = normalized_repository(
        image_repository(image)
    )
    if not repository:
        return None

    candidate = f"{repository}:{tag}"
    for entry in load_blocklist():
        if canonical_image(entry) == candidate:
            return entry

    return None


# ============================================================
# IMAGE HELPERS
# ============================================================

def image_repository(
    image: Optional[str],
) -> Optional[str]:

    if not image:
        return None

    image = image.strip()

    if not image:
        return None

    # Retirer un digest éventuel.
    image = image.split(
        "@",
        1,
    )[0]

    last_part = image.rsplit(
        "/",
        1,
    )[-1]

    if ":" in last_part:

        return image.rsplit(
            ":",
            1,
        )[0]

    return image


def image_tag(
    image: Optional[str],
) -> Optional[str]:

    if not image:
        return None

    image = image.strip()

    if not image:
        return None

    image = image.split(
        "@",
        1,
    )[0]

    last_part = image.rsplit(
        "/",
        1,
    )[-1]

    if ":" not in last_part:
        return None

    return image.rsplit(
        ":",
        1,
    )[1]


def normalized_repository(
    repository: Optional[str],
) -> Optional[str]:
    """
    Normalise uniquement les registres explicites connus comme équivalents
    à la référence WUD sans registre.

    Actuellement, ghcr.io est le seul registre normalisé.
    Les autres registres sont conservés afin d'éviter de masquer un vrai
    conflit entre deux registries différents.
    """

    if not repository:
        return None

    repository = repository.strip()

    if repository.startswith("ghcr.io/"):
        return repository[len("ghcr.io/"):]

    return repository


def canonical_image(
    image: Optional[str],
) -> Optional[str]:

    if not image:
        return None

    image = image.strip().split(
        "@",
        1,
    )[0]

    repository = image_repository(image)
    tag = image_tag(image)

    normalized = normalized_repository(repository)

    if not normalized:
        return None

    if tag:
        return f"{normalized}:{tag}"

    return normalized


def replace_image_tag(
    image: str,
    new_tag: str,
) -> str:

    image = image.strip()

    if "@" in image:

        image = image.split(
            "@",
            1,
        )[0]

    last_slash = image.rfind(
        "/"
    )

    last_colon = image.rfind(
        ":"
    )

    if last_colon > last_slash:

        return (
            image[: last_colon + 1]
            + new_tag
        )

    return (
        image
        + ":"
        + new_tag
    )


# ============================================================
# HTTP KOMODO
# ============================================================

def _http_request_json(
    request,
    service_name: str,
) -> Any:
    """Exécute une requête JSON avec retries sur erreurs transitoires."""

    import time
    import urllib.error
    import urllib.request

    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(
                request,
                timeout=HTTP_TIMEOUT,
            ) as response:
                raw = response.read().decode("utf-8")
                if not raw:
                    return {}
                return json.loads(raw)

        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code not in (408, 425, 429, 500, 502, 503, 504):
                raise RuntimeError(
                    f"{service_name} HTTP {exc.code}: {body}"
                ) from exc
            last_error = RuntimeError(
                f"{service_name} HTTP {exc.code}: {body}"
            )

        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last_error = RuntimeError(
                f"Erreur {service_name}: {exc}"
            )

        if attempt >= HTTP_RETRIES:
            raise last_error

        delay = min(
            HTTP_RETRY_BASE_DELAY * (2 ** attempt),
            HTTP_RETRY_MAX_DELAY,
        )
        print(
            f"⚠️ {service_name}: erreur transitoire, retry {attempt + 1}/{HTTP_RETRIES} dans {delay:g}s",
            file=sys.stderr,
        )
        time.sleep(delay)
        attempt += 1


def komodo_request(
    path: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Any:
    import urllib.request

    url = f"{KOMODO_URL}{path}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        method="POST" if payload is not None else "GET",
    )
    request.add_header("Content-Type", "application/json")
    request.add_header("X-Api-Key", KOMODO_API_KEY)
    request.add_header("X-Api-Secret", KOMODO_API_SECRET)
    return _http_request_json(request, "Komodo")


# ============================================================
# HTTP WUD
# ============================================================

def wud_request(
    path: str,
) -> Any:
    import base64
    import urllib.request

    url = f"{WUD_URL}{path}"
    request = urllib.request.Request(url, method="GET")
    credentials = f"{WUD_USER}:{WUD_PASSWORD}".encode("utf-8")
    encoded = base64.b64encode(credentials).decode("ascii")
    request.add_header("Authorization", f"Basic {encoded}")
    return _http_request_json(request, "WUD")


# ============================================================
# KOMODO STACKS
# ============================================================

def list_stacks() -> List[Dict[str, Any]]:

    response = komodo_request(
        "/read",
        {
            "type": "ListStacks",
            "params": {},
        },
    )

    # Komodo 2.3.3 retourne directement
    # une liste.
    if not isinstance(
        response,
        list,
    ):

        raise RuntimeError(
            "ListStacks: réponse inattendue: "
            f"{type(response).__name__}"
        )

    return response


def get_stack(
    stack_id: str,
) -> Dict[str, Any]:

    response = komodo_request(
        "/read",
        {
            "type": "GetStack",
            "params": {
                "stack": stack_id,
            },
        },
    )

    if not isinstance(
        response,
        dict,
    ):

        raise RuntimeError(
            "GetStack: réponse inattendue: "
            f"{type(response).__name__}"
        )

    return response


def stack_name(
    stack: Dict[str, Any],
) -> Optional[str]:

    value = stack.get(
        "name"
    )

    if isinstance(
        value,
        str,
    ):
        return value

    return None


def stack_info(
    stack: Dict[str, Any],
) -> Dict[str, Any]:

    value = stack.get(
        "info"
    )

    if isinstance(
        value,
        dict,
    ):
        return value

    return {}


def stack_server(
    stack: Dict[str, Any],
) -> Optional[str]:

    info = stack_info(
        stack
    )

    value = info.get(
        "server_name"
    )

    if isinstance(
        value,
        str,
    ):
        return value

    return None


def stack_services(
    stack: Dict[str, Any],
) -> List[Dict[str, Any]]:

    info = stack_info(
        stack
    )

    services = info.get(
        "services"
    )

    if not isinstance(
        services,
        list,
    ):
        return []

    return [
        service
        for service in services
        if isinstance(
            service,
            dict,
        )
    ]


def find_komodo_stack(
    stacks: List[Dict[str, Any]],
    update: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Resolve a WUD stack to one unambiguous Komodo stack.

    Never uses displayName. When WUD does not provide a server,
    logical stack resolution is allowed only if exactly one Komodo
    stack matches both the logical stack name and the service.
    Ambiguous matches fail closed.
    """

    wud_stack = update.get("stack")
    wud_service = update.get("service")
    wud_server = update.get("server")

    wud_stack = wud_stack.strip() if isinstance(wud_stack, str) else ""
    wud_service = wud_service.strip() if isinstance(wud_service, str) else ""
    wud_server = wud_server.strip() if isinstance(wud_server, str) else ""

    def has_service(stack: Dict[str, Any]) -> bool:
        return any(
            service.get("service") == wud_service
            for service in stack_services(stack)
        )

    def server_compatible(stack: Dict[str, Any]) -> bool:
        server = stack_server(stack)
        return not (
            wud_server
            and server
            and server != wud_server
        )

    # 1. Exact stack name.
    exact = [
        stack for stack in stacks
        if stack_name(stack) == wud_stack
        and server_compatible(stack)
    ]

    if len(exact) == 1:
        candidate = exact[0]
        services = stack_services(candidate)

        # Preserve compatibility when Komodo omits service metadata.
        if not services or not wud_service or has_service(candidate):
            return candidate

    elif len(exact) > 1:
        return None

    if not wud_stack or not wud_service:
        return None

    # 2. Resolve a host-prefixed Komodo stack when WUD supplies a server.
    if wud_server:
        expected_name = f"{wud_server}-{wud_stack}"
        prefixed = [
            stack for stack in stacks
            if stack_name(stack) == expected_name
            and stack_server(stack) == wud_server
            and has_service(stack)
        ]

        if len(prefixed) == 1:
            return prefixed[0]

        if len(prefixed) > 1:
            return None

        # 3. Suffix fallback, constrained to the known server.
        suffix = f"-{wud_stack}"
        candidates = [
            stack for stack in stacks
            if (stack_name(stack) or "").endswith(suffix)
            and stack_server(stack) == wud_server
            and has_service(stack)
        ]

        if len(candidates) == 1:
            return candidates[0]

        # An explicit WUD host must never fall through to hostless matching.
        # No match on this host is safer than preparing a different host's stack.
        return None

    # 4. WUD server is absent: find stacks matching the logical name.
    # A candidate must contain the service. Never guess between hosts.
    suffix = f"-{wud_stack}"
    candidates = [
        stack for stack in stacks
        if (
            stack_name(stack) == wud_stack
            or (stack_name(stack) or "").endswith(suffix)
        )
        and has_service(stack)
    ]

    if len(candidates) == 1:
        return candidates[0]

    return None

# ============================================================
# STACK COMPOSE CONTENTS
# ============================================================

def get_stack_file_contents(
    stack: Dict[str, Any],
) -> str:
    """
    Retourne la configuration Compose actuellement préparée dans Komodo.

    La configuration modifiable se trouve dans stack["config"]["file_contents"].
    info.deployed_contents représente le dernier déploiement réel et ne doit
    être utilisé qu'en fallback si aucune configuration préparée n'est fournie.
    """

    config = stack.get("config")

    if isinstance(config, dict):
        file_contents = config.get("file_contents")

        if isinstance(file_contents, str) and file_contents:
            return file_contents

    # Fallback uniquement pour les stacks qui n'exposeraient pas encore
    # config.file_contents.
    info = stack_info(stack)

    deployed_contents = info.get("deployed_contents")

    if not isinstance(deployed_contents, list):
        return ""

    for file_entry in deployed_contents:

        if not isinstance(file_entry, dict):
            continue

        path = file_entry.get("path")
        contents = file_entry.get("contents")

        if (
            path == "compose.yaml"
            and isinstance(contents, str)
        ):
            return contents

    for file_entry in deployed_contents:

        if not isinstance(file_entry, dict):
            continue

        contents = file_entry.get("contents")

        if isinstance(contents, str):
            return contents

    return ""


# ============================================================
# YAML IMAGE EXTRACTION
# ============================================================

def find_service_image(
    file_contents: str,
    service_name: str,
) -> Optional[str]:

    lines = file_contents.splitlines()

    service_indent = None
    inside_service = False

    for line in lines:

        stripped = line.strip()

        # Service:
        #
        #   romm:
        #
        if (
            stripped == f"{service_name}:"
            or stripped.startswith(
                f"{service_name}: #"
            )
        ):

            service_indent = (
                len(line)
                - len(line.lstrip())
            )

            inside_service = True

            continue

        if not inside_service:
            continue

        current_indent = (
            len(line)
            - len(line.lstrip())
        )

        # Nouveau service.
        if (
            stripped
            and service_indent is not None
            and current_indent
            <= service_indent
            and not line.lstrip().startswith(
                "#"
            )
        ):

            inside_service = False

            continue

        match = re.match(
            r"^\s*image\s*:\s*(.+?)\s*$",
            line,
        )

        if not match:
            continue

        value = (
            match.group(1)
            .strip()
        )

        # Commentaire YAML simple.
        if " #" in value:

            value = value.split(
                " #",
                1,
            )[0].rstrip()

        value = value.strip(
            "\"'"
        )

        return value

    return None


def replace_service_image(
    file_contents: str,
    service_name: str,
    new_image: str,
) -> Tuple[
    str,
    bool,
    Optional[str],
]:

    lines = file_contents.splitlines(
        keepends=True
    )

    service_indent = None
    inside_service = False

    for index, line in enumerate(
        lines
    ):

        stripped = line.strip()

        if (
            stripped == f"{service_name}:"
            or stripped.startswith(
                f"{service_name}: #"
            )
        ):

            service_indent = (
                len(line)
                - len(line.lstrip())
            )

            inside_service = True

            continue

        if not inside_service:
            continue

        current_indent = (
            len(line)
            - len(line.lstrip())
        )

        if (
            stripped
            and service_indent is not None
            and current_indent
            <= service_indent
            and not line.lstrip().startswith(
                "#"
            )
        ):

            inside_service = False

            continue

        match = re.match(
            r"^(\s*image\s*:\s*)"
            r"([^#\r\n]+?)"
            r"(\s*(?:#.*)?)"
            r"(\r?\n)?$",
            line,
        )

        if not match:
            continue

        prefix = match.group(1)
        old_image = (
            match.group(2)
            .strip()
        )
        suffix = (
            match.group(3)
        )
        newline = (
            match.group(4)
            or ""
        )

        lines[index] = (
            prefix
            + new_image
            + suffix
            + newline
        )

        return (
            "".join(lines),
            True,
            old_image,
        )

    return (
        file_contents,
        False,
        None,
    )


# ============================================================
# WUD
# ============================================================


def get_wud_updates() -> List[Dict[str, Any]]:

    response = wud_request("/api/containers")

    # WUD retourne directement une liste de containers.
    if isinstance(response, list):
        containers = response

    # Compatibilité avec une éventuelle réponse enveloppée.
    elif isinstance(response, dict):
        containers = response.get("containers", [])

        if not isinstance(containers, list):
            raise RuntimeError(
                "WUD: champ 'containers' inattendu: "
                f"{type(containers).__name__}"
            )

    else:
        raise RuntimeError(
            "WUD: réponse inattendue: "
            f"{type(response).__name__}"
        )

    updates = []

    for container in containers:

        if not isinstance(container, dict):
            continue

        if not container.get("updateAvailable"):
            continue

        image = container.get("image") or {}
        tag_info = image.get("tag") or {}
        result = container.get("result") or {}
        update_kind = container.get("updateKind") or {}

        service = (
            container.get("service")
            or container.get("name")
        )

        # WUD expose l'hôte via "watcher".
        # "server" est conservé en priorité si présent.
        watcher = container.get("watcher")
        server = container.get("server") or watcher

        updates.append(
            {
                "name": container.get("name"),
                "displayName": container.get("displayName"),
                "server": server,
                "watcher": watcher,
                "stack": container.get("stack"),
                "service": service,
                "image": image.get("name"),

                "current_tag": (
                    tag_info.get("value")
                    if isinstance(tag_info, dict)
                    else None
                ),

                "target_tag": result.get("tag"),
                "target_digest": result.get("digest"),
                "update_kind": update_kind.get("kind"),
                "local_value": update_kind.get("localValue"),
                "remote_value": update_kind.get("remoteValue"),
                "semver_diff": update_kind.get("semverDiff"),
                "link": container.get("link"),
            }
        )

    return updates

# ============================================================
# FILTERS
# ============================================================

def filter_values(value: Optional[str]) -> Optional[List[str]]:
    if value is None:
        return None
    values = [item.strip() for item in value.split(",") if item.strip()]
    return values or None


def service_matches(
    update: Dict[str, Any],
    service_filter: Optional[str],
) -> bool:
    values = filter_values(service_filter)
    if values is None:
        return True
    return update.get("service") in values


def stack_matches(
    update: Dict[str, Any],
    komodo_stack: Optional[Dict[str, Any]],
    stack_filter: Optional[str],
) -> bool:

    values = filter_values(stack_filter)
    if values is None:
        return True

    if update.get("stack") in values:
        return True

    if komodo_stack and stack_name(komodo_stack) in values:
        return True

    return False


# ============================================================
# PLAN
# ============================================================

def build_plan(
    stack_filter: Optional[str] = None,
    service_filter: Optional[str] = None,
    update_major: bool = False,
    include_digest: bool = False,
) -> Dict[str, Any]:

    wud_updates = get_wud_updates()

    # Service peut être filtré immédiatement.
    if service_filter is not None:

        wud_updates = [
            update
            for update in wud_updates
            if service_matches(
                update,
                service_filter,
            )
        ]

    stacks = list_stacks()

    items = []

    for update in wud_updates:

        komodo_stack = find_komodo_stack(
            stacks,
            update,
        )

        # Le stack est filtré après résolution Komodo.
        if not stack_matches(
            update,
            komodo_stack,
            stack_filter,
        ):
            continue

        # ----------------------------------------------------
        # Pas de stack correspondant.
        # ----------------------------------------------------

        if komodo_stack is None:

            items.append(
                {
                    "status": "ERROR",
                    "reason": "NO_KOMODO_STACK",

                    "server": update.get(
                        "server"
                    ),

                    "wud_stack": update.get(
                        "stack"
                    ),

                    "service": update.get(
                        "service"
                    ),

                    "image": update.get(
                        "image"
                    ),

                    "current_tag": update.get(
                        "current_tag"
                    ),

                    "target_tag": update.get(
                        "target_tag"
                    ),

                    "update_kind": update.get(
                        "update_kind"
                    ),
                }
            )

            continue

        stack_id = komodo_stack.get(
            "id"
        )

        komodo_name = stack_name(
            komodo_stack
        )

        update_kind = update.get(
            "update_kind"
        )

        service = update.get(
            "service"
        )

        # ----------------------------------------------------
        # Major : ignoré par défaut, activable explicitement.
        # ----------------------------------------------------

        semver_diff = update.get(
            "semver_diff"
        )

        if (
            update_kind == "tag"
            and semver_diff == "major"
            and not update_major
        ):

            items.append(
                {
                    "status": "SKIP",
                    "reason": (
                        "MAJOR_UPDATE_REQUIRES_FLAG"
                    ),
                    "server": update.get(
                        "server"
                    ),
                    "wud_stack": update.get(
                        "stack"
                    ),
                    "komodo_stack": komodo_name,
                    "stack_id": stack_id,
                    "service": service,
                    "image": update.get(
                        "image"
                    ),
                    "current_tag": update.get(
                        "current_tag"
                    ),
                    "target_tag": update.get(
                        "target_tag"
                    ),
                    "update_kind": update_kind,
                    "semver_diff": semver_diff,
                }
            )

            continue

        # ----------------------------------------------------
        # Digest : par défaut report-only. Avec --include-digest,
        # prépare un redéploiement ciblé sans modifier le Compose.
        # ----------------------------------------------------

        if update_kind == "digest" and not include_digest:
            items.append(
                {
                    "status": "SKIP",
                    "reason": "DIGEST_UPDATE_REPORT_ONLY",
                    "server": update.get("server"),
                    "wud_stack": update.get("stack"),
                    "komodo_stack": komodo_name,
                    "stack_id": stack_id,
                    "service": service,
                    "image": update.get("image"),
                    "current_tag": update.get("current_tag"),
                    "target_digest": update.get("target_digest"),
                    "update_kind": update_kind,
                }
            )
            continue

        # ----------------------------------------------------
        # Pour l'instant uniquement les tags.
        # ----------------------------------------------------

        if update_kind != "tag" and not (update_kind == "digest" and include_digest):

            items.append(
                {
                    "status": "SKIP",
                    "reason": (
                        "UNSUPPORTED_UPDATE_KIND"
                    ),

                    "server": update.get(
                        "server"
                    ),

                    "wud_stack": update.get(
                        "stack"
                    ),

                    "komodo_stack": komodo_name,

                    "stack_id": stack_id,

                    "service": service,

                    "image": update.get(
                        "image"
                    ),

                    "current_tag": update.get(
                        "current_tag"
                    ),

                    "target_tag": update.get(
                        "target_tag"
                    ),

                    "update_kind": update_kind,
                }
            )

            continue

        # ----------------------------------------------------
        # GetStack.
        # ----------------------------------------------------

        try:

            stack_details = get_stack(
                stack_id
            )

        except Exception as exc:

            items.append(
                {
                    "status": "ERROR",
                    "reason": "GET_STACK_FAILED",
                    "error": str(exc),

                    "server": update.get(
                        "server"
                    ),

                    "wud_stack": update.get(
                        "stack"
                    ),

                    "komodo_stack": komodo_name,

                    "stack_id": stack_id,

                    "service": service,
                }
            )

            continue

        file_contents = (
            get_stack_file_contents(
                stack_details
            )
        )

        if not file_contents:

            items.append(
                {
                    "status": "ERROR",
                    "reason": "NO_COMPOSE_CONTENTS",

                    "komodo_stack": komodo_name,

                    "stack_id": stack_id,

                    "service": service,
                }
            )

            continue

        configured_image = (
            find_service_image(
                file_contents,
                service,
            )
        )

        if not configured_image:

            items.append(
                {
                    "status": "ERROR",
                    "reason": (
                        "SERVICE_IMAGE_NOT_FOUND"
                    ),

                    "komodo_stack": komodo_name,

                    "stack_id": stack_id,

                    "service": service,
                }
            )

            continue

        if update_kind == "digest":
            if "@" in configured_image:
                items.append({
                    "status": "ERROR",
                    "reason": "DIGEST_PINNED_IMAGE_CANNOT_UPDATE_BY_REDEPLOY",
                    "komodo_stack": komodo_name,
                    "stack_id": stack_id,
                    "service": service,
                    "configured_image": configured_image,
                    "update_kind": "digest",
                    "target_digest": update.get("target_digest"),
                })
                continue

            configured_repo = normalized_repository(image_repository(configured_image))
            wud_repo = normalized_repository(image_repository(update.get("image")))
            configured_tag = image_tag(configured_image)
            current_tag = update.get("current_tag")
            if configured_repo != wud_repo or (current_tag and configured_tag != current_tag):
                items.append({
                    "status": "ERROR",
                    "reason": "DIGEST_IMAGE_DOES_NOT_MATCH_COMPOSE",
                    "server": update.get("server"),
                    "wud_stack": update.get("stack"),
                    "komodo_stack": komodo_name,
                    "stack_id": stack_id,
                    "service": service,
                    "configured_image": configured_image,
                    "wud_image": update.get("image"),
                    "current_tag": current_tag,
                    "configured_tag": configured_tag,
                    "update_kind": "digest",
                    "target_digest": update.get("target_digest"),
                })
                continue

            items.append({
                "status": "DIGEST_REDEPLOY",
                "reason": "DIGEST_CHANGED_REDEPLOY_STACK_TO_PULL_IMAGE",
                "server": update.get("server"),
                "wud_stack": update.get("stack"),
                "komodo_stack": komodo_name,
                "stack_id": stack_id,
                "service": service,
                "image": update.get("image"),
                "configured_image": configured_image,
                "current_tag": current_tag,
                "target_image": configured_image,
                "target_digest": update.get("target_digest"),
                "update_kind": "digest",
                "action": "digest_redeploy",
            })
            continue

        target_tag = update.get(
            "target_tag"
        )

        if not target_tag:

            items.append(
                {
                    "status": "ERROR",
                    "reason": "NO_TARGET_TAG",

                    "komodo_stack": komodo_name,

                    "stack_id": stack_id,

                    "service": service,

                    "configured_image": (
                        configured_image
                    ),
                }
            )

            continue

        blocked_version = is_version_blocked(
            update.get("image"),
            target_tag,
        )

        if blocked_version:
            items.append(
                {
                    "status": "SKIP",
                    "reason": "VERSION_BLOCKED",
                    "blocked_image": blocked_version,
                    "server": update.get("server"),
                    "wud_stack": update.get("stack"),
                    "komodo_stack": komodo_name,
                    "stack_id": stack_id,
                    "service": service,
                    "image": update.get("image"),
                    "current_tag": update.get("current_tag"),
                    "target_tag": target_tag,
                    "update_kind": update_kind,
                    "semver_diff": semver_diff,
                }
            )
            continue

        # ----------------------------------------------------
        # Vérification repository.
        # ----------------------------------------------------

        configured_repo = image_repository(
            configured_image
        )

        wud_repo = image_repository(
            update.get("image")
        )

        if (
            configured_repo
            and wud_repo
        ):

            configured_repo_normalized = normalized_repository(
                configured_repo
            )

            wud_repo_normalized = normalized_repository(
                wud_repo
            )

            if (
                configured_repo_normalized
                != wud_repo_normalized
            ):

                items.append(
                    {
                        "status": "CONFLICT",
                        "reason": (
                            "IMAGE_REPOSITORY_MISMATCH"
                        ),

                        "server": update.get(
                            "server"
                        ),

                        "wud_stack": update.get(
                            "stack"
                        ),

                        "komodo_stack": (
                            komodo_name
                        ),

                        "stack_id": stack_id,

                        "service": service,

                        "configured_image": (
                            configured_image
                        ),

                        "wud_image": update.get(
                            "image"
                        ),

                        "current_tag": update.get(
                            "current_tag"
                        ),

                        "target_tag": target_tag,
                    }
                )

                continue

        # ----------------------------------------------------
        # Image cible.

                # ----------------------------------------------------


        target_image = replace_image_tag(
            configured_image,
            target_tag,
        )

        configured_tag = image_tag(
            configured_image
        )

        # ----------------------------------------------------
        # Déjà présent dans Compose.
        # ----------------------------------------------------

        if configured_tag == target_tag:

            items.append(
                {
                    "status": "ALREADY_UPDATED",
                    "reason": (
                        "TARGET_ALREADY_IN_COMPOSE"
                    ),

                    "server": update.get(
                        "server"
                    ),

                    "wud_stack": update.get(
                        "stack"
                    ),

                    "komodo_stack": komodo_name,

                    "stack_id": stack_id,

                    "service": service,

                    "configured_image": (
                        configured_image
                    ),

                    "current_tag": update.get(
                        "current_tag"
                    ),

                    "target_tag": target_tag,

                    "target_image": target_image,

                    "update_kind": update_kind,
                }
            )

            continue

        # ----------------------------------------------------
        # READY.
        # ----------------------------------------------------

        items.append(
            {
                "status": "READY",

                "server": update.get(
                    "server"
                ),

                "wud_stack": update.get(
                    "stack"
                ),

                "komodo_stack": komodo_name,

                "stack_id": stack_id,

                "service": service,

                "configured_image": (
                    configured_image
                ),

                "current_tag": update.get(
                    "current_tag"
                ),

                "target_tag": target_tag,

                "target_image": target_image,

                "update_kind": update_kind,

                "semver_diff": update.get(
                    "semver_diff"
                ),
            }
        )

    return {
        "status": "OK",
        "items": items,
    }


# ============================================================
# UPDATE STACK
# ============================================================

def update_stack_file_contents(
    stack_id: str,
    file_contents: str,
) -> Any:

    return komodo_request(
        "/write",
        {
            "type": "UpdateStack",
            "params": {
                "id": stack_id,
                "config": {
                    "file_contents": (
                        file_contents
                    ),
                },
            },
        },
    )


# ============================================================
# KOMODO UPDATE FOLLOWING
# ============================================================

def update_id_from_response(
    response: Any,
) -> Optional[str]:
    """
    Extrait l'identifiant Update retourné par Komodo.

    Komodo sérialise généralement l'identifiant Mongo sous :
    {"_id": {"$oid": "..."}}
    """

    if not isinstance(response, dict):
        return None

    value = response.get("_id")

    if isinstance(value, dict):
        oid = value.get("$oid")
        if isinstance(oid, str) and oid:
            return oid

    if isinstance(value, str) and value:
        return value

    return None


def get_update(
    update_id: str,
) -> Dict[str, Any]:
    """
    Récupère l'état courant d'une exécution Komodo.
    """

    response = komodo_request(
        "/read",
        {
            "type": "GetUpdate",
            "params": {
                "id": update_id,
            },
        },
    )

    if not isinstance(response, dict):
        raise RuntimeError(
            "GetUpdate: réponse inattendue: "
            f"{type(response).__name__}"
        )

    return response


def format_duration(
    seconds: float,
) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    return f"{minutes:02d}:{secs:02d}"


def emit_deploy_log(
    log_entry: Any,
    index: int,
) -> None:
    """
    Affiche un résumé lisible d'un nouveau log Komodo sur stderr.

    Le JSON final reste sur stdout afin de conserver une sortie exploitable
    par Hermes et les scripts appelants.
    """

    if not isinstance(log_entry, dict):
        return

    stage = log_entry.get("stage") or ""
    command = log_entry.get("command") or ""
    success = log_entry.get("success")
    stdout = log_entry.get("stdout") or ""
    stderr = log_entry.get("stderr") or ""

    if success is True:
        marker = "✅"
    elif success is False:
        marker = "❌"
    else:
        marker = "ℹ️"

    label = stage or command or f"log #{index + 1}"
    print(
        f"{marker} {label}",
        file=sys.stderr,
        flush=True,
    )

    # N'afficher que la dernière ligne utile pour éviter de noyer le terminal.
    text = stderr.strip() or stdout.strip()
    if text:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        print(
            f"   {lines[-1][:500]}",
            file=sys.stderr,
            flush=True,
        )


def follow_stack_deploy(
    stack_name_value: str,
    response: Dict[str, Any],
    interval: float = 2.0,
    timeout: float = 3600.0,
    operation_label: str = "Déploiement",
    completed_status: str = "DEPLOY_COMPLETED",
    failed_status: str = "DEPLOY_FAILED",
    timeout_status: str = "DEPLOY_TIMEOUT",
) -> Dict[str, Any]:
    """
    Suit une opération Komodo (pull ou déploiement) jusqu'à sa fin via GetUpdate.

    Le suivi est volontairement basé sur polling : il fonctionne sans
    WebSocket et suit le même mécanisme que le client officiel Komodo.
    """

    update_id = update_id_from_response(response)

    if not update_id:
        return {
            "status": "FOLLOW_UNAVAILABLE",
            "stack": stack_name_value,
            "reason": "UPDATE_ID_NOT_RETURNED_BY_KOMODO",
            "response": response,
        }

    started = time.monotonic()
    last_status = None
    last_log_count = 0
    latest: Dict[str, Any] = response

    print(
        f"🚀 {operation_label} lancé | stack={stack_name_value} | update={update_id}",
        file=sys.stderr,
        flush=True,
    )

    while True:
        elapsed = time.monotonic() - started

        if elapsed > timeout:
            return {
                "status": timeout_status,
                "operation": operation_label,
                "stack": stack_name_value,
                "update_id": update_id,
                "duration_seconds": round(elapsed, 1),
                "last_update": latest,
            }

        latest = get_update(update_id)
        current_status = latest.get("status")

        if current_status != last_status:
            print(
                f"⏳ État: {current_status or 'UNKNOWN'} | durée={format_duration(elapsed)}",
                file=sys.stderr,
                flush=True,
            )
            last_status = current_status

        logs = latest.get("logs")
        if isinstance(logs, list):
            for index in range(last_log_count, len(logs)):
                emit_deploy_log(logs[index], index)
            last_log_count = len(logs)

        if current_status == "Complete":
            success = latest.get("success")
            duration = latest.get("end_ts") and latest.get("start_ts")

            if success is True:
                final_status = completed_status
                print(
                    f"✅ {operation_label} terminé | durée={format_duration(elapsed)}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                final_status = failed_status
                print(
                    f"❌ {operation_label} terminé en échec | durée={format_duration(elapsed)}",
                    file=sys.stderr,
                    flush=True,
                )

            result: Dict[str, Any] = {
                "status": final_status,
                "operation": operation_label,
                "stack": stack_name_value,
                "update_id": update_id,
                "success": success,
                "duration_seconds": round(elapsed, 1),
                "update": latest,
            }

            if duration:
                result["komodo_duration_ms"] = (
                    latest.get("end_ts") - latest.get("start_ts")
                )

            return result

        time.sleep(interval)


# ============================================================
# UPDATE
# ============================================================

def print_dry_run_human(result: Dict[str, Any]) -> None:
    """Affiche un aperçu lisible du dry-run sans effectuer de modification."""
    print("\n=== DRY-RUN Docker Updater ===")
    print(f"READY        : {result.get("count_ready", 0)}")
    print(f"ALREADY      : {result.get("count_already_updated", 0)}")
    print(f"ERROR        : {result.get("count_errors", 0)}")
    print(f"DIGEST       : {result.get("count_digests", 0)}")
    print("\nAucune modification n'a été effectuée.\n")

    items = result.get("items", [])
    actionable = [x for x in items if x.get("status") in ("READY", "ALREADY_UPDATED", "DIGEST_REDEPLOY")]
    if not actionable:
        print("Aucune mise à jour actionnable.")
        return

    print("STACK / SERVICE                         AVANT -> APRÈS")
    print("-" * 78)
    for item in actionable:
        stack = item.get("komodo_stack") or item.get("wud_stack") or "?"
        service = item.get("service") or "?"
        before = item.get("current_image") or item.get("old_image") or "?"
        after = item.get("target_image") or item.get("new_image") or before
        status = item.get("status")
        print(f"{stack} / {service:<28} {before} -> {after} [{status}]")


def _update_ready_stacks_unlocked(
    stack_filter: Optional[str] = None,
    service_filter: Optional[str] = None,
    dry_run: bool = False,
    confirm: bool = False,
    skip_errors: bool = False,
    update_major: bool = False,
    include_digest: bool = False,
) -> Dict[str, Any]:

    result = build_plan(
        stack_filter,
        service_filter,
        update_major,
        include_digest=include_digest,
    )

    items = result.get(
        "items",
        [],
    )

    ready = [
        item
        for item in items
        if item.get(
            "status"
        ) == "READY"
    ]

    already = [
        item
        for item in items
        if item.get(
            "status"
        ) == "ALREADY_UPDATED"
    ]

    errors = [
        item
        for item in items
        if item.get(
            "status"
        ) in (
            "ERROR",
            "CONFLICT",
        )
    ]

    digests = [
        item for item in items
        if item.get("update_kind") == "digest"
        and item.get("status") in ("SKIP", "DIGEST_REDEPLOY")
    ]
    digest_candidates = [item for item in digests if item.get("status") == "DIGEST_REDEPLOY"]
    blocking_digests = [item for item in digests if item.get("status") == "SKIP"]

    blocking_items = errors + ([] if include_digest else blocking_digests)

    # --------------------------------------------------------
    # Dry run.
    # --------------------------------------------------------

    if dry_run:

        return {
            "status": "DRY_RUN",
            "count_ready": len(
                ready
            ),
            "count_already_updated": len(
                already
            ),
            "count_errors": len(
                errors
            ),
            "count_digests": len(
                digests
            ),
            "update_major": update_major,
            "skip_errors": skip_errors,
            "items": items,
        }

    # --------------------------------------------------------
    # Confirmation.
    # --------------------------------------------------------

    if not confirm:

        return {
            "status": "CONFIRMATION_REQUIRED",

            "message": (
                "Aucune modification effectuée. "
                "Relancer avec --confirm."
            ),

            "count_ready": len(
                ready
            ),

            "count_already_updated": len(
                already
            ),

            "count_errors": len(
                errors
            ),

            "items": items,
        }

    # --------------------------------------------------------
    # Ne rien modifier si erreur/conflit ou digest, sauf avec --skip-errors.
    # Les MAJOR sans --update-major sont simplement ignorés.
    # --------------------------------------------------------

    if blocking_items and not skip_errors:

        return {
            "status": "ABORTED",

            "reason": (
                "PLAN_CONTAINS_ERRORS_CONFLICTS_OR_DIGESTS"
            ),

            "count_ready": len(
                ready
            ),

            "count_already_updated": len(
                already
            ),

            "count_errors": len(
                errors
            ),
            "count_digests": len(
                digests
            ),
            "update_major": update_major,
            "skip_errors": skip_errors,

            "items": items,
        }

    updated = []

    skipped = (
        blocking_items
        if skip_errors
        else []
    )

    # Un digest ne change pas la référence Compose : on prépare seulement
    # un redéploiement explicite de la stack pour que Komodo récupère l'image.
    if include_digest:
        for item in digest_candidates:
            prepared = {
                **item,
                "status": "DIGEST_REDEPLOY_PREPARED",
                "compose_modified": False,
                "deployment": "NOT_RUN",
            }
            updated.append(prepared)
            append_history({
                "action": "DIGEST_REDEPLOY_PREPARED",
                "stack_id": item.get("stack_id"),
                "komodo_stack": item.get("komodo_stack"),
                "service": item.get("service"),
                "image": item.get("configured_image"),
                "target_digest": item.get("target_digest"),
                "update_kind": "digest",
            })

    # --------------------------------------------------------
    # Chaque stack READY.
    # --------------------------------------------------------

    for item in ready:

        stack_id = item.get(
            "stack_id"
        )

        service = item.get(
            "service"
        )

        # Relire juste avant écriture.
        try:

            stack_details = get_stack(
                stack_id
            )

        except Exception as exc:

            updated.append(
                {
                    **item,
                    "status": "ERROR",
                    "reason": (
                        "GET_STACK_BEFORE_UPDATE_FAILED"
                    ),
                    "error": str(exc),
                }
            )

            continue

        file_contents = (
            get_stack_file_contents(
                stack_details
            )
        )

        actual_image = (
            find_service_image(
                file_contents,
                service,
            )
        )

        if not actual_image:

            updated.append(
                {
                    **item,
                    "status": "ERROR",
                    "reason": (
                        "SERVICE_IMAGE_NOT_FOUND_DURING_UPDATE"
                    ),
                }
            )

            continue

        planned_image = item.get(
            "configured_image"
        )

        # ----------------------------------------------------
        # Protection contre une modification concurrente.
        # ----------------------------------------------------

        if (
            planned_image
            and canonical_image(
                actual_image
            )
            != canonical_image(
                planned_image
            )
        ):

            updated.append(
                {
                    **item,
                    "status": "CONFLICT",
                    "reason": (
                        "COMPOSE_CHANGED_SINCE_PLAN"
                    ),
                    "planned_image": (
                        planned_image
                    ),
                    "actual_image": (
                        actual_image
                    ),
                }
            )

            continue

        target_image = replace_image_tag(
            actual_image,
            item.get(
                "target_tag"
            ),
        )

        new_contents, changed, old_image = (
            replace_service_image(
                file_contents,
                service,
                target_image,
            )
        )

        if not changed:

            updated.append(
                {
                    **item,
                    "status": "ERROR",
                    "reason": (
                        "SERVICE_IMAGE_NOT_REPLACED"
                    ),
                }
            )

            continue

        # ----------------------------------------------------
        # Écriture Komodo.
        # ----------------------------------------------------

        try:

            response = (
                update_stack_file_contents(
                    stack_id,
                    new_contents,
                )
            )

            updated.append(
                {
                    **item,
                    "status": "UPDATED",

                    "old_image": old_image,

                    "new_image": target_image,

                    "komodo_response": response,
                }
            )

            append_history({
                "action": "UPDATE_PREPARED",
                "stack_id": stack_id,
                "komodo_stack": item.get("komodo_stack"),
                "service": service,
                "old_image": old_image,
                "new_image": target_image,
                "target_tag": item.get("target_tag"),
                "update_kind": item.get("update_kind"),
                "semver_diff": item.get("semver_diff"),
            })

        except Exception as exc:

            updated.append(
                {
                    **item,
                    "status": "ERROR",

                    "reason": (
                        "UPDATE_STACK_FAILED"
                    ),

                    "error": str(exc),
                }
            )

    return {
        "status": "UPDATED",

        "skip_errors": skip_errors,

        "count_updated": sum(
            1
            for item in updated
            if item.get(
                "status"
            ) == "UPDATED"
        ),

        "count_errors": sum(
            1
            for item in updated
            if item.get(
                "status"
            ) in (
                "ERROR",
                "CONFLICT",
            )
        ),

        "items": updated + skipped,
    }


def update_ready_stacks(*args, lock_timeout: float = 0.0, **kwargs) -> Dict[str, Any]:
    with updater_lock("update", lock_timeout):
        return _update_ready_stacks_unlocked(*args, **kwargs)


# ============================================================
# VERIFY
# ============================================================

def verify_updates(
    plan: List[Dict[str, Any]],
    stack_filter: Optional[str] = None,
    service_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Vérifie les modifications directement dans le Compose préparé
    dans Komodo.

    Ne déploie rien.
    Relit systématiquement GetStack.

    Les états READY et ALREADY_UPDATED sont vérifiables. ALREADY_UPDATED
    est notamment attendu après un update --confirm : le plan reconstruit
    alors que la cible est déjà présente dans le Compose.
    """

    if isinstance(plan, dict):
        items = plan.get("items", [])
    elif isinstance(plan, list):
        items = plan
    else:
        raise RuntimeError(
            f"verify_updates: plan inattendu: {type(plan).__name__}"
        )

    if not isinstance(items, list):
        raise RuntimeError(
            f"verify_updates: items inattendu: {type(items).__name__}"
        )

    results: List[Dict[str, Any]] = []

    for item in items:
        if not isinstance(item, dict):
            continue

        if item.get("status") not in (
            "READY",
            "ALREADY_UPDATED",
            "UPDATED",
            "DIGEST_REDEPLOY",
        ):
            continue

        komodo_stack = item.get("komodo_stack")
        stack_id = item.get("stack_id")
        wud_stack = item.get("wud_stack")
        service = item.get("service")
        target_image = item.get("target_image")

        if stack_filter:
            if stack_filter != komodo_stack and stack_filter != wud_stack:
                continue

        if service_filter and service_filter != service:
            continue

        base = {
            "komodo_stack": komodo_stack,
            "stack_id": stack_id,
            "wud_stack": wud_stack,
            "service": service,
        }

        try:
            # Use the shared GetStack wrapper, which sends Komodo's expected
            # `params.stack` identifier shape.
            stack = get_stack(stack_id)

            if not isinstance(stack, dict):
                raise RuntimeError(
                    f"GetStack: réponse inattendue: {type(stack).__name__}"
                )

            config = stack.get("config")

            if not isinstance(config, dict):
                raise RuntimeError(
                    "GetStack: champ 'config' absent ou invalide"
                )

            compose_contents = config.get("file_contents")

            if not isinstance(compose_contents, str):
                raise RuntimeError(
                    "GetStack: config.file_contents absent ou invalide"
                )

            configured_image = find_service_image(
                compose_contents,
                service,
            )

            if not configured_image:
                results.append({
                    **base,
                    "status": "ERROR",
                    "reason": "SERVICE_IMAGE_NOT_FOUND",
                    "target_image": target_image,
                })
                continue

            if not target_image:
                results.append({
                    **base,
                    "status": "ERROR",
                    "reason": "TARGET_IMAGE_MISSING",
                })
                continue

            configured_repo = image_repository(
                configured_image
            )
            target_repo = image_repository(
                target_image
            )

            configured_tag = image_tag(
                configured_image
            )
            target_tag = image_tag(
                target_image
            )

            configured_repo_normalized = normalized_repository(
                configured_repo
            )
            target_repo_normalized = normalized_repository(
                target_repo
            )

            repo_match = (
                configured_repo_normalized
                == target_repo_normalized
            )
            tag_match = configured_tag == target_tag

            digest_mode = item.get("update_kind") == "digest" and item.get("status") == "DIGEST_REDEPLOY"
            verified_status = (
                "DIGEST_VERIFIED" if digest_mode else "UPDATED"
            ) if repo_match and tag_match else "NOT_UPDATED"
            result = {
                **base,
                "status": verified_status,
                "configured_image": configured_image,
                "target_image": target_image,
                "configured_tag": configured_tag,
                "target_tag": target_tag,
                "repo_match": repo_match,
                "tag_match": tag_match,
                "update_kind": item.get("update_kind"),
                "action": "digest_redeploy" if digest_mode else "tag_update",
            }
            if digest_mode:
                result["target_digest"] = item.get("target_digest")
                result["reason"] = "COMPOSE_UNCHANGED_DIGEST_REDEPLOY"
            elif not repo_match or not tag_match:
                result["reason"] = "COMPOSE_DOES_NOT_MATCH_TARGET"
            results.append(result)

        except Exception as exc:
            results.append({
                **base,
                "status": "ERROR",
                "reason": "VERIFY_FAILED",
                "error": str(exc),
            })

    return results


# ============================================================
# WUD UPDATES DISPLAY
# ============================================================

def show_wud_updates(
    stack_filter: Optional[str] = None,
    service_filter: Optional[str] = None,
) -> Dict[str, Any]:

    updates = get_wud_updates()

    if service_filter is not None:

        updates = [
            item
            for item in updates
            if service_matches(
                item,
                service_filter,
            )
        ]

    if stack_filter is not None:

        stacks = list_stacks()

        filtered = []

        for item in updates:

            komodo_stack = (
                find_komodo_stack(
                    stacks,
                    item,
                )
            )

            if stack_matches(
                item,
                komodo_stack,
                stack_filter,
            ):

                filtered.append(
                    item
                )

        updates = filtered

    return {
        "status": "OK",
        "count": len(
            updates
        ),
        "items": updates,
    }


# ============================================================
# PLAN DISPLAY
# ============================================================

def show_plan(
    stack_filter: Optional[str] = None,
    service_filter: Optional[str] = None,
    update_major: bool = False,
    include_digest: bool = False,
) -> Dict[str, Any]:

    result = build_plan(
        stack_filter,
        service_filter,
        update_major,
        include_digest=include_digest,
    )

    items = result.get(
        "items",
        [],
    )

    summary = {}

    for item in items:

        status = item.get(
            "status",
            "UNKNOWN",
        )

        summary[status] = (
            summary.get(
                status,
                0,
            )
            + 1
        )

    return {
        "status": "OK",
        "summary": summary,
        "items": items,
    }


# ============================================================
# POST-DEPLOY RUNTIME VERIFY
# ============================================================

def extract_runtime_service(stack: Dict[str, Any], service_name: str) -> Optional[Dict[str, Any]]:
    for service in stack_services(stack):
        if service.get("service") == service_name:
            return service
    return None

def runtime_service_image(service: Dict[str, Any]) -> Optional[str]:
    for key in ("image", "current_image", "deployed_image"):
        value = service.get(key)
        if isinstance(value, str) and value:
            return value
    return None

def runtime_service_state(service: Dict[str, Any]) -> Optional[str]:
    for key in ("state", "status"):
        value = service.get(key)
        if isinstance(value, str) and value:
            return value
    return None

def _verify_runtime_once(expected_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    results = []
    healthy_states = {"Running", "running", "Healthy", "healthy"}
    for item in expected_items:
        stack_id = item.get("stack_id")
        service_name = item.get("service")
        target_image = item.get("target_image")
        try:
            stack = get_stack(stack_id) if stack_id else {}
            service = extract_runtime_service(stack, service_name) if service_name else None
            if service is None:
                results.append({**item, "status": "RUNTIME_NOT_FOUND", "reason": "RUNTIME_SERVICE_NOT_FOUND"})
                continue
            actual_image = runtime_service_image(service)
            state = runtime_service_state(service)
            image_match = bool(actual_image and target_image and canonical_image(actual_image) == canonical_image(target_image))
            state_ok = state in healthy_states
            if image_match and state_ok:
                status, reason = "RUNTIME_OK", None
            elif not image_match:
                status, reason = "RUNTIME_MISMATCH", "RUNTIME_IMAGE_MISMATCH"
            else:
                status, reason = "RUNTIME_UNHEALTHY", "RUNTIME_SERVICE_NOT_HEALTHY"
            results.append({**item, "status": status, "reason": reason, "runtime_image": actual_image, "runtime_state": state, "image_match": image_match, "state_ok": state_ok})
        except Exception as exc:
            results.append({**item, "status": "ERROR", "reason": "RUNTIME_VERIFY_GET_STACK_FAILED", "error": str(exc)})
    blocking = [x for x in results if x.get("status") != "RUNTIME_OK"]
    return {"status": "OK" if not blocking else "FAILED", "count_verified": len(results) - len(blocking), "count_errors": len(blocking), "items": results}


def verify_runtime(expected_items: List[Dict[str, Any]], health_timeout: float = 120.0, health_interval: float = 5.0) -> Dict[str, Any]:
    deadline = time.time() + max(0.0, health_timeout)
    last = None
    while True:
        last = _verify_runtime_once(expected_items)
        if last.get("status") == "OK":
            last["stable_seconds"] = max(0.0, health_timeout - max(0.0, deadline - time.time()))
            return last
        if time.time() >= deadline:
            last["status"] = "FAILED"
            last["reason"] = "RUNTIME_STABILITY_TIMEOUT"
            last["health_timeout"] = health_timeout
            return last
        time.sleep(max(0.5, health_interval))


# ============================================================
# VERIFY SNAPSHOT / DEPLOY PROTECTION
# ============================================================

VERIFY_STATE_FILE = os.path.join(
    os.getenv("HERMES_WRITE_SAFE_ROOT", "/opt/data"),
    ".hermes",
    "skills",
    "docker-updater",
    ".verify-state.json",
)


def compose_fingerprint(contents: str) -> str:
    return hashlib.sha256(
        contents.encode("utf-8")
    ).hexdigest()


def save_verify_state(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    # Keep previously verified stacks so multiple update/verify batches can be
    # deployed together. A newly verified stack replaces its older snapshot.
    previous = load_verify_state() or {"items": []}
    snapshots_by_id = {
        item.get("stack_id"): item
        for item in previous.get("items", [])
        if isinstance(item, dict) and item.get("stack_id")
    }

    grouped: Dict[str, Dict[str, Any]] = {}
    for item in items:
        if item.get("status") not in ("UPDATED", "ALREADY_UPDATED", "DIGEST_VERIFIED"):
            continue
        stack_id = item.get("stack_id")
        if not stack_id:
            continue
        grouped.setdefault(stack_id, {"stack_id": stack_id, "komodo_stack": item.get("komodo_stack"), "services": [], "targets": {}, "actions": {}, "digest_targets": {}})
        service = item.get("service")
        if service and service not in grouped[stack_id]["services"]:
            grouped[stack_id]["services"].append(service)
        if service and item.get("target_image"):
            grouped[stack_id]["targets"][service] = item.get("target_image")
        if service:
            action = item.get("action") or ("digest_redeploy" if item.get("status") == "DIGEST_VERIFIED" else "tag_update")
            grouped[stack_id]["actions"][service] = action
            if action == "digest_redeploy":
                grouped[stack_id]["digest_targets"][service] = item.get("target_digest")

    for stack_id, entry in grouped.items():
        stack = get_stack(stack_id)
        config = stack.get("config") if isinstance(stack, dict) else None
        contents = config.get("file_contents") if isinstance(config, dict) else None
        if not isinstance(contents, str):
            raise RuntimeError(f"Impossible de créer l'empreinte du stack {stack_id}")
        snapshots_by_id[stack_id] = {
            **entry,
            "fingerprint": compose_fingerprint(contents),
            "verified_at": time.time(),
        }

    state = {"created_at": time.time(), "items": list(snapshots_by_id.values())}
    os.makedirs(os.path.dirname(VERIFY_STATE_FILE), exist_ok=True)
    tmp = VERIFY_STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, VERIFY_STATE_FILE)
    return state


def load_verify_state() -> Optional[Dict[str, Any]]:
    try:
        with open(VERIFY_STATE_FILE, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except FileNotFoundError:
        return None

    if not isinstance(state, dict) or not isinstance(state.get("items"), list):
        return None
    return state


def validate_verify_state(stack_ids: Optional[set] = None) -> Dict[str, Any]:
    state = load_verify_state()

    if not state:
        return {
            "status": "VERIFY_REQUIRED",
            "reason": "NO_VERIFY_SNAPSHOT",
        }

    mismatches = []

    selected_snapshots = [
        snapshot for snapshot in state.get("items", [])
        if not stack_ids or snapshot.get("stack_id") in stack_ids
    ]
    if not selected_snapshots:
        return {"status": "VERIFY_REQUIRED", "reason": "NO_MATCHING_VERIFIED_STACKS"}

    for snapshot in selected_snapshots:
        stack_id = snapshot.get("stack_id")
        try:
            stack = get_stack(stack_id)
            config = stack.get("config") if isinstance(stack, dict) else None
            contents = config.get("file_contents") if isinstance(config, dict) else None

            if not isinstance(contents, str):
                raise RuntimeError("config.file_contents absent ou invalide")

            current = compose_fingerprint(contents)
            if current != snapshot.get("fingerprint"):
                mismatches.append({
                    **snapshot,
                    "current_fingerprint": current,
                    "reason": "COMPOSE_CHANGED_SINCE_VERIFY",
                })
        except Exception as exc:
            mismatches.append({
                **snapshot,
                "reason": "VERIFY_STATE_CHECK_FAILED",
                "error": str(exc),
            })

    if mismatches:
        return {
            "status": "CONFIG_CHANGED",
            "reason": "COMPOSE_CHANGED_SINCE_VERIFY",
            "items": mismatches,
        }

    return {
        "status": "OK",
        "count_verified": len(state.get("items", [])),
        "created_at": state.get("created_at"),
    }


def clear_verify_state() -> None:
    try:
        os.unlink(VERIFY_STATE_FILE)
    except FileNotFoundError:
        pass


# ============================================================
# DEPLOY VERIFIED STACKS
# ============================================================

def pull_stack_request(stack_id: str, services: List[str]) -> Dict[str, Any]:
    """Récupère les images des seuls services concernés par un changement de digest."""
    response = komodo_request(
        "/execute",
        {"type": "PullStack", "params": {"stack": stack_id, "services": services}},
    )
    if not isinstance(response, dict):
        raise RuntimeError(f"PullStack a retourné une réponse inattendue pour {stack_id}")
    return response


def deploy_stack_request(stack_id: str) -> Dict[str, Any]:
    """Déploie une seule Stack Komodo via DeployStack (jamais une procédure)."""
    response = komodo_request(
        "/execute",
        {"type": "DeployStack", "params": {"stack": stack_id}},
    )
    if not isinstance(response, dict):
        raise RuntimeError(f"DeployStack a retourné une réponse inattendue pour {stack_id}")
    return response


def deploy_verified_stacks(
    confirm: bool,
    stack_filter: Optional[str] = None,
    follow: bool = False,
    interval: float = 2.0,
    timeout: float = 3600.0,
    verify_runtime_after: bool = False,
    health_timeout: float = 120.0,
    health_interval: float = 5.0,
    lock_timeout: float = 0.0,
) -> Dict[str, Any]:
    """Déploie directement toutes les stacks vérifiées, sans procédure Komodo.

    Une stack est déployée une seule fois, même si plusieurs de ses services
    ont été modifiés. Une erreur sur une stack n'empêche pas de traiter les
    suivantes. En mode --follow, seules les stacks réellement réussies sont
    retirées de l'état de vérification ; les échecs restent disponibles pour
    diagnostic ou nouvelle tentative.
    """
    if not confirm:
        return {
            "status": "CONFIRMATION_REQUIRED",
            "message": "Utilise --confirm pour déployer les stacks vérifiées.",
        }

    state = load_verify_state()
    if not state or not state.get("items"):
        return {
            "status": "DEPLOY_BLOCKED",
            "reason": "NO_VERIFIED_STACKS",
            "message": "Lance verify après avoir préparé les Compose.",
        }

    requested = {
        value.strip()
        for value in (stack_filter or "").split(",")
        if value.strip()
    }

    # Dédupliquer explicitement par stack_id : plusieurs services modifiés
    # dans une même stack ne doivent déclencher qu'un seul DeployStack.
    verified_items = state.get("items", [])
    candidates_by_id: Dict[str, Dict[str, Any]] = {}
    for snapshot in verified_items:
        if not isinstance(snapshot, dict):
            continue
        stack_id = snapshot.get("stack_id")
        if not stack_id:
            continue
        candidates_by_id[stack_id] = snapshot

    candidates = list(candidates_by_id.values())
    if requested:
        candidates = [
            snapshot for snapshot in candidates
            if snapshot.get("komodo_stack") in requested
            or snapshot.get("stack_id") in requested
        ]
        found = {
            value
            for snapshot in candidates
            for value in (snapshot.get("komodo_stack"), snapshot.get("stack_id"))
            if value
        }
        missing = sorted(requested - found)
        if missing:
            return {
                "status": "DEPLOY_BLOCKED",
                "reason": "STACK_NOT_VERIFIED",
                "requested_stacks": sorted(requested),
                "not_verified": missing,
            }

    if not candidates:
        return {
            "status": "DEPLOY_BLOCKED",
            "reason": "NO_VERIFIED_STACKS_MATCH_FILTER",
        }

    candidate_ids = {snapshot["stack_id"] for snapshot in candidates}
    verify_guard = validate_verify_state(candidate_ids)
    if verify_guard.get("status") != "OK":
        return {"status": "DEPLOY_BLOCKED", "guard": verify_guard}

    results: List[Dict[str, Any]] = []
    successful_ids = set()

    for snapshot in candidates:
        stack_id = snapshot["stack_id"]
        name = snapshot.get("komodo_stack") or stack_id
        result: Dict[str, Any]

        try:
            actions = snapshot.get("actions", {})
            digest_services = sorted({
                service
                for service, action in actions.items()
                if action == "digest_redeploy"
            })

            # Les images dont le digest a changé doivent être tirées avant le
            # déploiement. Si ce pull échoue, on marque cette stack en échec,
            # puis on continue avec les stacks suivantes.
            if digest_services:
                pull_response = pull_stack_request(stack_id, digest_services)
                pull_result = follow_stack_deploy(
                    name,
                    pull_response,
                    interval=interval,
                    timeout=timeout,
                    operation_label="Pull des images",
                    completed_status="PULL_COMPLETED",
                    failed_status="PULL_FAILED",
                    timeout_status="PULL_TIMEOUT",
                )
                if pull_result.get("status") != "PULL_COMPLETED":
                    result = {
                        **pull_result,
                        "status": pull_result.get("status") or "PULL_FAILED",
                        "phase": "pull",
                        "stack": name,
                        "stack_id": stack_id,
                        "services": digest_services,
                    }
                    results.append(result)
                    continue

            # Appel direct à l'API Komodo : aucun appel à une procédure.
            response = deploy_stack_request(stack_id)

            if follow:
                result = follow_stack_deploy(
                    name,
                    response,
                    interval=interval,
                    timeout=timeout,
                )
                result["stack"] = name
                result["stack_id"] = stack_id

                deploy_ok = result.get("status") == "DEPLOY_COMPLETED"
                if deploy_ok and verify_runtime_after:
                    services = snapshot.get("services", [])
                    expected = [
                        {
                            "stack_id": stack_id,
                            "komodo_stack": name,
                            "service": service,
                            "target_image": snapshot.get("targets", {}).get(service),
                        }
                        for service in services
                    ]
                    runtime_result = verify_runtime(
                        expected,
                        health_timeout=health_timeout,
                        health_interval=health_interval,
                    )
                    result["runtime_verify"] = runtime_result
                    deploy_ok = runtime_result.get("status") in ("OK", "HEALTHY")

                if deploy_ok:
                    successful_ids.add(stack_id)
            else:
                # Komodo a accepté la demande. On conserve l'état de vérification
                # tant que le résultat final n'a pas été suivi et confirmé.
                result = {
                    "status": "DEPLOY_STARTED",
                    "stack": name,
                    "stack_id": stack_id,
                    "response": response,
                }

        except Exception as exc:
            result = {
                "status": "ERROR",
                "stack": name,
                "stack_id": stack_id,
                "error": str(exc),
            }

        results.append(result)

    # Nettoyer chaque stack confirmée comme réussie même si une autre stack
    # a échoué. Les stacks en échec restent dans l'état pour pouvoir être
    # diagnostiquées et relancées sans redéployer les stacks déjà réussies.
    if follow and successful_ids:
        remaining = [
            snapshot
            for snapshot in verified_items
            if not (
                isinstance(snapshot, dict)
                and snapshot.get("stack_id") in successful_ids
            )
        ]
        if remaining:
            state["items"] = remaining
            state["created_at"] = time.time()
            os.makedirs(os.path.dirname(VERIFY_STATE_FILE), exist_ok=True)
            tmp = VERIFY_STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, VERIFY_STATE_FILE)
        else:
            clear_verify_state()

    if follow:
        all_completed = len(successful_ids) == len(candidates)
        overall_status = "DEPLOY_COMPLETED" if all_completed else "PARTIAL_OR_FAILED"
    else:
        all_started = len(results) == len(candidates) and all(
            item.get("status") == "DEPLOY_STARTED" for item in results
        )
        overall_status = "DEPLOY_STARTED" if all_started else "PARTIAL_OR_FAILED"

    current_state = load_verify_state()
    return {
        "status": overall_status,
        "deployment_method": "PullStack then direct DeployStack for digest actions; direct DeployStack otherwise",
        "count_stacks": len(results),
        "count_deploy_success": len(successful_ids),
        "count_failed_or_pending": len(candidates) - len(successful_ids) if follow else sum(
            1 for item in results if item.get("status") != "DEPLOY_STARTED"
        ),
        "stacks": [item.get("stack") for item in results],
        "items": results,
        "remaining_verified_stacks": len(current_state.get("items", [])) if current_state else 0,
    }

def deploy_stacks(*args, lock_timeout: float = 0.0, **kwargs) -> Dict[str, Any]:
    with updater_lock("deploy-stacks", lock_timeout):
        return deploy_verified_stacks(*args, **kwargs)


# ============================================================
# INTERACTIVE MODE
# ============================================================

def interactive_update(
    update_major: bool = False,
    skip_errors: bool = False,
    stack_filter: Optional[str] = None,
    service_filter: Optional[str] = None,
) -> Dict[str, Any]:
    """Sélection interactive des mises à jour, sans jamais déployer."""
    plan = build_plan(stack_filter=stack_filter, service_filter=service_filter, update_major=update_major)
    items = [
        item for item in plan.get("items", [])
        if item.get("status") == "READY"
    ]

    if not items:
        print("\nAucune mise à jour READY à appliquer.")
        return {"status": "OK", "count_selected": 0, "items": []}

    print("\nMises à jour disponibles :\n")
    for index, item in enumerate(items, 1):
        print(
            f"[{index}] {item.get('komodo_stack')} / "
            f"{item.get('service')} : "
            f"{item.get('current_image')} -> {item.get('target_image')}"
        )

    raw = input("\nSélectionnez les numéros (ex: 1,3,5), ou 'all' : ").strip().lower()
    if raw in {"q", "quit", "exit", "annuler", "cancel"}:
        return {"status": "CANCELLED", "count_selected": 0, "items": []}

    if raw == "all":
        selected_indexes = list(range(1, len(items) + 1))
    else:
        try:
            selected_indexes = sorted({int(x.strip()) for x in raw.split(",") if x.strip()})
        except ValueError:
            return {"status": "INVALID_SELECTION", "message": "Sélection invalide."}

    if not selected_indexes or any(i < 1 or i > len(items) for i in selected_indexes):
        return {"status": "INVALID_SELECTION", "message": "Numéro hors plage."}

    selected = [items[i - 1] for i in selected_indexes]
    print("\nSélection :")
    for item in selected:
        print(f"  - {item.get('komodo_stack')} / {item.get('service')} -> {item.get('target_image')}")

    confirm = input("\nModifier les fichiers Compose ? [y/N] : ").strip().lower()
    if confirm not in {"y", "yes", "o", "oui"}:
        return {"status": "CANCELLED", "count_selected": len(selected), "items": selected}

    results = []
    for item in selected:
        result = update_ready_stacks(
            stack_filter=item.get("komodo_stack"),
            service_filter=item.get("service"),
            confirm=True,
            skip_errors=skip_errors,
            update_major=update_major,
        )
        results.append(result)

    print("\n✓ Compose modifiés. Aucun déploiement n'a été lancé.")
    return {
        "status": "OK",
        "count_selected": len(selected),
        "items": results,
        "deployment": "NOT_RUN",
    }


# ============================================================
# CLI
# ============================================================

def build_parser():

    parser = argparse.ArgumentParser(
        description=(
            "Hermes Docker Updater - "
            "WUD -> Komodo"
        )
    )

    parser.add_argument("--verbose", action="store_true", help="Journal synthétique sur stderr")
    parser.add_argument("--debug", action="store_true", help="Journal technique sur stderr")

    subparsers = (
        parser.add_subparsers(
            dest="command",
            required=True,
        )
    )

    # --------------------------------------------------------
    # wud-updates
    # --------------------------------------------------------

    p = subparsers.add_parser(
        "wud-updates",
        help=(
            "Afficher les mises à jour WUD"
        ),
    )

    p.add_argument(
        "--stack",
        help=(
            "Stack WUD ou stack Komodo"
        ),
    )

    p.add_argument(
        "--service",
        help=(
            "Service exact ou liste séparée par des virgules"
        ),
    )

    p.add_argument(
        "--stacks",
        help="Plusieurs stacks séparées par des virgules",
    )

    p.add_argument(
        "--services",
        help="Plusieurs services séparés par des virgules",
    )

    # --------------------------------------------------------
    # plan
    # --------------------------------------------------------

    p = subparsers.add_parser(
        "plan",
        help=(
            "Construire le plan"
        ),
    )

    p.add_argument(
        "--stack",
        help=(
            "Stack WUD ou stack Komodo"
        ),
    )

    p.add_argument(
        "--service",
        help=(
            "Service exact ou liste séparée par des virgules"
        ),
    )

    p.add_argument(
        "--stacks",
        help="Plusieurs stacks séparées par des virgules",
    )

    p.add_argument(
        "--services",
        help="Plusieurs services séparés par des virgules",
    )

    p.add_argument(
        "--update-major",
        action="store_true",
        help=(
            "Inclure les mises à jour de version MAJOR"
        ),
    )
    p.add_argument("--include-digest", action="store_true", help="Inclure les digests en redéploiement ciblé (sans modifier le Compose)")

    # --------------------------------------------------------
    # update
    # --------------------------------------------------------

    p = subparsers.add_parser(
        "update",
        help=(
            "Modifier les Compose"
        ),
    )

    p.add_argument(
        "--stack",
        help=(
            "Stack WUD ou stack Komodo"
        ),
    )

    p.add_argument(
        "--service",
        help=(
            "Service exact ou liste séparée par des virgules"
        ),
    )

    p.add_argument(
        "--stacks",
        help="Plusieurs stacks séparées par des virgules",
    )

    p.add_argument(
        "--services",
        help="Plusieurs services séparés par des virgules",
    )

    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Simulation sans modification"
        ),
    )

    p.add_argument(
        "--confirm",
        action="store_true",
        help=(
            "Autorise la modification"
        ),
    )

    p.add_argument(
        "--human",
        action="store_true",
        help="Afficher le dry-run sous forme de tableau lisible",
    )

    p.add_argument(
        "--skip-errors",
        action="store_true",
        help=(
            "Met à jour les READY en ignorant ERROR et CONFLICT"
        ),
    )

    p.add_argument(
        "--update-major",
        action="store_true",
        help=(
            "Autorise les mises à jour de version MAJOR"
        ),
    )
    p.add_argument("--include-digest", action="store_true", help="Préparer les redéploiements digest sans modifier le Compose")
    p.add_argument("--lock-timeout", type=float, default=0.0)

    # --------------------------------------------------------
    # verify
    # --------------------------------------------------------

    p = subparsers.add_parser(
        "verify",
        help=(
            "Vérifier les Compose"
        ),
    )

    p.add_argument(
        "--stack",
        help=(
            "Stack WUD ou stack Komodo"
        ),
    )

    p.add_argument(
        "--service",
        help=(
            "Service exact ou liste séparée par des virgules"
        ),
    )

    p.add_argument(
        "--stacks",
        help="Plusieurs stacks séparées par des virgules",
    )

    p.add_argument(
        "--services",
        help="Plusieurs services séparés par des virgules",
    )

    p.add_argument(
        "--update-major",
        action="store_true",
        help=(
            "Inclure les mises à jour de version MAJOR"
        ),
    )
    p.add_argument("--include-digest", action="store_true", help="Vérifier les stacks nécessitant un pull via redéploiement")
    p.add_argument("--human", action="store_true")

    # --------------------------------------------------------
    # interactive
    # --------------------------------------------------------

    p = subparsers.add_parser(
        "interactive",
        help="Sélectionner interactivement les mises à jour à préparer",
    )

    p.add_argument(
        "--update-major",
        action="store_true",
        help="Inclure les mises à jour MAJOR",
    )

    p.add_argument(
        "--skip-errors",
        action="store_true",
        help="Ignorer ERROR, CONFLICT et digest",
    )

    p.add_argument("--stack")
    p.add_argument("--service")
    p.add_argument("--stacks")
    p.add_argument("--services")

    # --------------------------------------------------------
    # auto
    # --------------------------------------------------------

    p = subparsers.add_parser(
        "auto",
        help=(
            "Préparer automatiquement les mises à jour PATCH/MINOR et vérifier, sans déployer"
        ),
    )

    p.add_argument(
        "--stack",
        help="Stack WUD ou stack Komodo",
    )

    p.add_argument(
        "--service",
        help="Service exact ou liste séparée par des virgules",
    )

    p.add_argument(
        "--stacks",
        help="Plusieurs stacks séparées par des virgules",
    )

    p.add_argument(
        "--services",
        help="Plusieurs services séparés par des virgules",
    )

    p.add_argument("--lock-timeout", type=float, default=0.0)

    # --------------------------------------------------------
    # status
    # --------------------------------------------------------

    p = subparsers.add_parser("status", help="Résumé de l'état des mises à jour")
    p.add_argument("--stack")
    p.add_argument("--service")
    p.add_argument("--stacks")
    p.add_argument("--services")
    p.add_argument("--update-major", action="store_true")
    p.add_argument("--include-digest", action="store_true", help="Afficher les digests comme redéploiements ciblés")
    p.add_argument("--human", action="store_true")

    # --------------------------------------------------------
    # preflight
    # --------------------------------------------------------

    subparsers.add_parser(
        "preflight",
        help=(
            "Vérifier les prérequis WUD et Komodo"
        ),
    )

    # --------------------------------------------------------
    # deploy
    # --------------------------------------------------------

    p = subparsers.add_parser(
        "deploy",
        help="Déployer uniquement les stacks vérifiées et modifiées",
    )
    p.add_argument("--stack", help="Nom ou ID d'une stack vérifiée")
    p.add_argument("--stacks", help="Plusieurs stacks vérifiées, séparées par des virgules")

    p.add_argument(
        "--confirm",
        action="store_true",
        help=(
            "Autorise le déploiement"
        ),
    )

    p.add_argument(
        "--follow",
        action="store_true",
        help=(
            "Suit l'exécution Komodo jusqu'à sa fin"
        ),
    )

    p.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help=(
            "Intervalle de polling en secondes (défaut: 2)"
        ),
    )

    p.add_argument(
        "--timeout",
        type=float,
        default=3600.0,
        help=(
            "Timeout du suivi en secondes (défaut: 3600)"
        ),
    )

    p.add_argument(
        "--verify-runtime",
        action="store_true",
        help=(
            "Vérifie le runtime après un déploiement suivi"
        ),
    )
    p.add_argument("--health-timeout", type=float, default=120.0)
    p.add_argument("--health-interval", type=float, default=5.0)
    p.add_argument("--lock-timeout", type=float, default=0.0)

    # history / rollback-info / rollback
    p = subparsers.add_parser("history", help="Afficher l'historique des mises à jour")
    p.add_argument("--stack")
    p.add_argument("--service")
    p.add_argument("--limit", type=int, default=20)

    p = subparsers.add_parser("rollback-info", help="Afficher les rollbacks disponibles")
    p.add_argument("--stack")
    p.add_argument("--service")
    p.add_argument("--limit", type=int, default=20)

    p = subparsers.add_parser("rollback", help="Préparer un rollback sans déployer")
    p.add_argument("--stack", required=True)
    p.add_argument("--service", required=True)
    p.add_argument("--confirm", action="store_true")
    p.add_argument("--lock-timeout", type=float, default=0.0)

    return parser




# ============================================================
# AUTO
# ============================================================

def auto_update(
    stack_filter: Optional[str] = None,
    service_filter: Optional[str] = None,
    lock_timeout: float = 0.0,
) -> Dict[str, Any]:
    """Prépare automatiquement les mises à jour PATCH/MINOR puis vérifie.

    Cette commande ne lance jamais la procédure Komodo Deploy.
    Les MAJOR sont ignorées, et les ERROR/CONFLICT/DIGEST sont ignorés
    pour permettre aux mises à jour sûres de continuer.
    """

    update_result = update_ready_stacks(
        stack_filter=stack_filter,
        service_filter=service_filter,
        dry_run=False,
        confirm=True,
        skip_errors=True,
        update_major=False,
        lock_timeout=lock_timeout,
    )

    updated_items = [
        item
        for item in update_result.get("items", [])
        if item.get("status") == "UPDATED"
    ]

    if not updated_items:
        return {
            "status": "NO_CHANGES",
            "deployment": "NOT_RUN",
            "update": update_result,
            "verify": None,
        }

    verify_items = verify_updates(
        updated_items,
        stack_filter,
        service_filter,
    )

    blocking = [
        item
        for item in verify_items
        if item.get("status") in ("NOT_UPDATED", "ERROR")
    ]

    verify_status = "FAILED" if blocking else "OK"
    verify_state = None
    if verify_status == "OK":
        verify_state = save_verify_state(verify_items)

    return {
        "status": "OK" if verify_status == "OK" else "FAILED",
        "deployment": "NOT_RUN",
        "policy": {
            "update_major": False,
            "skip_errors": True,
            "deploy": False,
        },
        "update": update_result,
        "verify": {
            "status": verify_status,
            "verify_state": verify_state,
            "count_verified": sum(
                1
                for item in verify_items
                if item.get("status") == "UPDATED"
            ),
            "count_errors": len(blocking),
            "items": verify_items,
        },
    }


# ============================================================
# PREFLIGHT
# ============================================================

def preflight() -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []

    def check(name: str, fn, blocking: bool = True) -> None:
        try:
            detail = fn()
            checks.append({
                "name": name,
                "status": "OK",
                "blocking": blocking,
                "detail": detail,
            })
        except Exception as exc:
            checks.append({
                "name": name,
                "status": "ERROR" if blocking else "WARNING",
                "blocking": blocking,
                "error": str(exc),
            })

    check("komodo_credentials", lambda: {"configured": True})
    check("wud_credentials", lambda: {"configured": True})
    check("komodo", lambda: {"stacks": len(list_stacks())})
    def check_wud():
        containers = wud_request("/api/containers")
        if not isinstance(containers, list):
            raise RuntimeError(f"Réponse WUD inattendue: {type(containers).__name__}")
        return {"containers": len(containers)}

    check("wud", check_wud)
    check("stack_deploy_api", lambda: {"operations": ["PullStack", "DeployStack"]})

    blocking = [x for x in checks if x.get("blocking") and x.get("status") != "OK"]
    warnings = [x for x in checks if x.get("status") == "WARNING"]

    return {
        "status": "OK" if not blocking else "FAILED",
        "count_checks": len(checks),
        "count_ok": sum(1 for x in checks if x.get("status") == "OK"),
        "count_warnings": len(warnings),
        "count_errors": len(blocking),
        "checks": checks,
    }


# ============================================================
# MAIN
# ============================================================

def main():

    parser = build_parser()

    args = parser.parse_args()

    if getattr(args, "verbose", False):
        print(f"ℹ️ commande={args.command}", file=sys.stderr)
    if getattr(args, "debug", False):
        print(f"🔧 debug: script={__file__}", file=sys.stderr)

    if hasattr(args, "stacks") and args.stacks:
        args.stack = ",".join(
            [value for value in (args.stack, args.stacks) if value]
        )
    if hasattr(args, "services") and args.services:
        args.service = ",".join(
            [value for value in (args.service, args.services) if value]
        )

    require_env_for_command(args.command)

    try:

        if args.command == "wud-updates":

            print_json(
                show_wud_updates(
                    args.stack,
                    args.service,
                )
            )

            return

        if args.command == "plan":

            print_json(
                show_plan(
                    args.stack,
                    args.service,
                    args.update_major,
                    include_digest=args.include_digest,
                )
            )

            return

        if args.command == "update":

            update_result = update_ready_stacks(
                stack_filter=args.stack,
                service_filter=args.service,
                dry_run=args.dry_run,
                confirm=args.confirm,
                skip_errors=args.skip_errors,
                update_major=args.update_major,
                include_digest=args.include_digest,
                lock_timeout=args.lock_timeout,
            )

            if args.dry_run and getattr(args, "human", False):
                print_dry_run_human(update_result)
            else:
                print_json(update_result)

            return

        if args.command == "verify":

            plan = build_plan(
                stack_filter=args.stack,
                service_filter=args.service,
                update_major=args.update_major,
                include_digest=args.include_digest,
            )

            verify_items = verify_updates(
                plan.get("items", []),
                args.stack,
                args.service,
            )

            blocking = [
                item
                for item in verify_items
                if item.get("status") in (
                    "NOT_UPDATED",
                    "ERROR",
                )
            ]

            verify_status = "FAILED" if blocking else "OK"
            verify_state = None
            if verify_status == "OK":
                verify_state = save_verify_state(verify_items)

            print_json(
                {
                    "status": verify_status,
                    "verify_state": verify_state,
                    "count_verified": sum(
                        1
                        for item in verify_items
                        if item.get("status") in ("UPDATED", "DIGEST_VERIFIED")
                    ),
                    "count_digest_verified": sum(1 for item in verify_items if item.get("status") == "DIGEST_VERIFIED"),
                    "count_errors": len(blocking),
                    "items": verify_items,
                }
            )

            return

        if args.command == "interactive":

            print_json(
                interactive_update(
                    update_major=args.update_major,
                    skip_errors=args.skip_errors,
                    stack_filter=args.stack,
                    service_filter=args.service,
                )
            )

            return

        if args.command == "auto":

            print_json(
                auto_update(
                    stack_filter=args.stack,
                    service_filter=args.service,
                    lock_timeout=args.lock_timeout,
                )
            )

            return

        if args.command == "history":
            print_json(rollback_info(args.stack, args.service, args.limit))
            return

        if args.command == "rollback-info":
            print_json(rollback_info(args.stack, args.service, args.limit))
            return

        if args.command == "rollback":
            print_json(rollback_update(args.stack, args.service, args.confirm, args.lock_timeout))
            return

        if args.command == "status":
            plan = show_plan(args.stack, args.service, args.update_major, include_digest=args.include_digest)
            items = plan.get("items", [])
            summary = {
                "status": "OK",
                "ready": sum(1 for x in items if x.get("status") == "READY"),
                "already_updated": sum(1 for x in items if x.get("status") == "ALREADY_UPDATED"),
                "skipped": sum(1 for x in items if x.get("status") == "SKIP"),
                "errors": sum(1 for x in items if x.get("status") in ("ERROR", "CONFLICT")),
                "items": items,
            }
            if getattr(args, "human", False):
                print(f"READY: {summary['ready']}")
                print(f"ALREADY_UPDATED: {summary['already_updated']}")
                print(f"SKIP: {summary['skipped']}")
                print(f"ERROR/CONFLICT: {summary['errors']}")
            else:
                print_json(summary)
            return

        if args.command == "deploy":
            print_json(
                deploy_stacks(
                    args.confirm,
                    stack_filter=args.stack,
                    follow=args.follow,
                    interval=args.interval,
                    timeout=args.timeout,
                    verify_runtime_after=args.verify_runtime,
                    health_timeout=args.health_timeout,
                    health_interval=args.health_interval,
                    lock_timeout=args.lock_timeout,
                )
            )
            return

        # All other required subcommands have returned; preflight is the final choice.
        print_json(preflight())

    except KeyboardInterrupt:

        print_json(
            {
                "status": "INTERRUPTED"
            }
        )

        sys.exit(130)

    except Exception as exc:

        fail(
            str(exc)
        )


if __name__ == "__main__":
    main()