"""Session actor registry, identity, and actor state helpers."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import uuid

from gotta.compat import UTC, datetime
from gotta.content.context import session_token
from gotta.content.env import load_state_env_at_root, state_dir_path
from gotta.content.file import ensure_private_dir, write_text_atomic
from gotta.content.scope import (
    session_identity,
    session_is_initialized,
    session_shared_id,
)
from gotta import topology
from gotta.actor import (
    actor_session_root,
    normalize_actor_name as _shared_normalize_actor_name,
    session_actor,
)

SESSION_ACTORS_ENV = "GOTTA_SESSION_ACTORS_JSON"

SESSION_WANT_PATH_ENV = "GOTTA_SESSION_WANT_PATH"

SESSION_ACTORS_SOURCE_ENV = "GOTTA_SESSION_ACTORS_SOURCE"

SESSION_VOICE_SOURCE_ENV = "GOTTA_SESSION_VOICE_SOURCE"

ACTOR_STATE_STATUS = {
    "pending",
    "bound",
    "starting",
    "active",
    "closing",
    "stalled",
    "completed",
    "failed",
    "signed_off",
}


def _normalize_actor_status(value: object) -> str:
    status = str(value or "pending")
    if status == "configured":
        return "bound"
    return status


WANT_FILE = "WANT.md"

ACTOR_STALL_SECONDS = 180


@dataclass(frozen=True, slots=True)
class ActorSpec:
    actor_id: str
    label: str
    default_model: str
    aliases: tuple[str, ...]


DEFAULT_ACTORS = (
    ActorSpec(
        actor_id="claude",
        label="Claude",
        default_model="claude-sonnet-4.6",
        aliases=(),
    ),
    ActorSpec(
        actor_id="codex",
        label="Codex",
        default_model="gpt-5.3-codex",
        aliases=("gpt",),
    ),
)

ACTOR_INDEX = {actor.actor_id: actor for actor in DEFAULT_ACTORS}

ACTOR_IDS = tuple(actor.actor_id for actor in DEFAULT_ACTORS)

ACTOR_ALIASES = {
    alias: actor.actor_id
    for actor in DEFAULT_ACTORS
    for alias in (actor.actor_id, *actor.aliases)
}

ACTOR_DEFAULT_MODEL = ACTOR_INDEX["codex"].default_model

SESSION_ACTORS_METADATA_KEY = "actors"

SESSION_MEMBERS_METADATA_KEY = "members"


def _join_labels(labels: tuple[str, ...], joiner: str, *, code: bool = False) -> str:
    rendered = tuple(f"`{label}`" if code else label for label in labels)
    if len(rendered) <= 1:
        return "".join(rendered)
    if len(rendered) == 2:
        return f" {joiner} ".join(rendered)
    return ", ".join(rendered[:-1]) + f", {joiner} {rendered[-1]}"


def _default_actor_summary(*, code: bool = False) -> str:
    return _join_labels(
        tuple(actor.label for actor in DEFAULT_ACTORS),
        "and",
        code=code,
    )


def _actor_bind_examples(*, prefix: str = "gotta actor bind") -> str:
    return " or ".join(f"`{prefix} {actor.label} ...`" for actor in DEFAULT_ACTORS)


def _actor_session_ref(actor_name: str) -> str:
    return _normalize_actor_name(actor_name)


def _actor_charter_command(
    actor_name: str, surface: str, *, mode: str = "--stdin"
) -> str:
    return f"gotta {surface} --actor {_actor_session_ref(actor_name)} {mode}"


def _actor_is_fingerprint(value: str) -> bool:
    normalized = topology.normalize_identity(value)
    return len(normalized) == 12 and all(ch in "0123456789abcdef" for ch in normalized)


def _actor_template_spec(value: str) -> ActorSpec | None:
    normalized = _shared_normalize_actor_name(value)
    if not normalized:
        return None
    template_id = ACTOR_ALIASES.get(normalized)
    if template_id is None:
        for spec in DEFAULT_ACTORS:
            if _shared_normalize_actor_name(spec.label) == normalized:
                template_id = spec.actor_id
                break
    if template_id is None:
        return None
    return ACTOR_INDEX.get(template_id)


def _resolve_actor_name(value: str, *, kind: str = "actor") -> str:
    normalized = _shared_normalize_actor_name(value)
    if not normalized:
        raise SystemExit(f"missing {kind}")
    return normalized


def _normalize_actor_name(value: str) -> str:
    return _resolve_actor_name(value, kind="actor")


def _actor_label(actor_name: str, *, work_dir: Path | None = None) -> str:
    normalized = _normalize_actor_name(actor_name)
    if work_dir is not None:
        payload = _actor_registry(work_dir).get(normalized)
        label = str(payload.get("label") or "").strip() if payload is not None else ""
        if label:
            return label
    spec = _actor_template_spec(normalized)
    if spec is not None:
        return spec.label
    return normalized.replace("-", " ").title()


def _default_actor_registry() -> dict[str, dict[str, str]]:
    return {}


def _group_session_root(work_dir: Path) -> Path:
    resolved = work_dir.expanduser().resolve()
    if (resolved / "actors").is_dir():
        return resolved
    if resolved.parent.name == "actors":
        return resolved.parent.parent.resolve()
    return resolved


def _actor_registry_from_state(state: dict[str, str]) -> dict[str, dict[str, str]]:
    raw = str(state.get(SESSION_ACTORS_ENV) or "").strip()
    registry = _default_actor_registry()
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"invalid actor registry: {exc}") from exc
        if not isinstance(payload, dict):
            raise SystemExit("invalid actor registry: expected an object")
        for actor_id, actor_payload in payload.items():
            normalized = _resolve_actor_name(str(actor_id))
            if not isinstance(actor_payload, dict):
                raise SystemExit(f"invalid actor registry payload for {normalized}")
            spec = _actor_template_spec(
                str(actor_payload.get("template") or normalized)
            )
            registry[normalized] = {
                "label": str(
                    actor_payload.get("label")
                    or (spec.label if spec else normalized.title())
                ),
                "model": str(
                    actor_payload.get("model")
                    or (spec.default_model if spec else ACTOR_DEFAULT_MODEL)
                ).strip(),
                "resume_uuid": str(actor_payload.get("resume_uuid") or "").strip(),
                "template": str(
                    actor_payload.get("template") or (spec.actor_id if spec else "")
                ).strip(),
            }
    for actor_id, actor_payload in registry.items():
        spec = _actor_template_spec(str(actor_payload.get("template") or actor_id))
        if spec is not None:
            actor_payload["label"] = actor_payload.get("label") or spec.label
            actor_payload["model"] = actor_payload.get("model") or spec.default_model
    return registry


def _actor_registry_json(registry: dict[str, dict[str, str]]) -> str:
    ordered = {
        actor_id: {
            "label": str(
                payload.get("label")
                or ACTOR_INDEX.get(
                    actor_id,
                    ActorSpec(actor_id, actor_id.title(), "", ()),
                ).label
            ),
            "model": str(payload.get("model") or ""),
            "resume_uuid": str(payload.get("resume_uuid") or ""),
            "template": str(payload.get("template") or ""),
        }
        for actor_id, payload in registry.items()
    }
    return json.dumps(ordered, sort_keys=True, separators=(",", ":"))


def _actor_ids_for_state(state: dict[str, str]) -> tuple[str, ...]:
    return tuple(_actor_registry_from_state(state))


def _session_metadata_path(work_dir: Path) -> Path:
    return _group_session_root(work_dir) / "session.json"


def _load_session_metadata(work_dir: Path) -> dict[str, object]:
    path = _session_metadata_path(work_dir)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid session metadata: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"invalid session metadata: {path}")
    return payload


def _write_session_metadata(work_dir: Path, payload: dict[str, object]) -> None:
    path = _session_metadata_path(work_dir)
    ensure_private_dir(path.parent)
    cleaned = dict(payload)
    cleaned["session_id"] = session_shared_id(work_dir)
    cleaned.setdefault(
        "created_at", datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    cleaned["updated_at"] = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_text_atomic(path, json.dumps(cleaned, indent=2, sort_keys=True) + "\n")


def _actor_registry_from_metadata(work_dir: Path) -> dict[str, dict[str, str]]:
    payload = _load_session_metadata(work_dir)
    registry: dict[str, dict[str, str]] = {}
    raw_actors = payload.get(SESSION_ACTORS_METADATA_KEY)
    if isinstance(raw_actors, dict):
        for actor_id, actor_payload in raw_actors.items():
            normalized = topology.normalize_identity(str(actor_id))
            if not normalized or topology.is_placeholder_identity(normalized):
                continue
            if not isinstance(actor_payload, dict):
                raise SystemExit(f"invalid session actor payload for {normalized}")
            spec = _actor_template_spec(str(actor_payload.get("template") or ""))
            registry[normalized] = {
                "label": str(
                    actor_payload.get("label") or (spec.label if spec else normalized)
                ).strip()
                or normalized,
                "model": str(
                    actor_payload.get("model")
                    or (spec.default_model if spec else ACTOR_DEFAULT_MODEL)
                ).strip(),
                "resume_uuid": str(actor_payload.get("resume_uuid") or "").strip(),
                "template": str(
                    actor_payload.get("template") or (spec.actor_id if spec else "")
                ).strip(),
            }
    raw_members = payload.get(SESSION_MEMBERS_METADATA_KEY)
    if isinstance(raw_members, list):
        for actor_id in raw_members:
            normalized = topology.normalize_identity(str(actor_id))
            if not normalized or topology.is_placeholder_identity(normalized):
                continue
            registry.setdefault(
                normalized,
                {
                    "label": normalized,
                    "model": ACTOR_DEFAULT_MODEL,
                    "resume_uuid": "",
                    "template": "",
                },
            )
    return registry


def _discovered_actor_registry(work_dir: Path) -> dict[str, dict[str, str]]:
    actors_dir = _group_session_root(work_dir).resolve() / "actors"
    registry: dict[str, dict[str, str]] = {}
    if not actors_dir.is_dir():
        return registry
    for actor_dir in sorted(actors_dir.iterdir()):
        if not actor_dir.is_dir():
            continue
        actor_id = topology.normalize_identity(actor_dir.name)
        if not actor_id or topology.is_placeholder_identity(actor_id):
            continue
        if not session_is_initialized(actor_dir.resolve()):
            continue
        registry[actor_id] = {
            "label": actor_id,
            "model": ACTOR_DEFAULT_MODEL,
            "resume_uuid": "",
            "template": "",
        }
    return registry


def _actor_registry(work_dir: Path) -> dict[str, dict[str, str]]:
    registry = _actor_registry_from_metadata(work_dir)
    discovered_registry = _discovered_actor_registry(work_dir)
    if registry:
        for actor_id, payload in discovered_registry.items():
            registry.setdefault(actor_id, payload)
        return registry
    if discovered_registry:
        return discovered_registry
    state_registry = _actor_registry_from_state(load_state_env_at_root(work_dir))
    if state_registry:
        return state_registry
    if topology.parse_grouped_session_root(work_dir) is not None:
        current = session_identity(work_dir)
    else:
        current = ""
    if current and not topology.is_placeholder_identity(current):
        return {
            current: {
                "label": current,
                "model": ACTOR_DEFAULT_MODEL,
                "resume_uuid": "",
                "template": "",
            }
        }
    return {}


def _store_actor_registry(work_dir: Path, registry: dict[str, dict[str, str]]) -> None:
    metadata = _load_session_metadata(work_dir)
    metadata[SESSION_MEMBERS_METADATA_KEY] = sorted(registry)
    metadata[SESSION_ACTORS_METADATA_KEY] = {
        actor_id: {
            "label": str(payload.get("label") or actor_id),
            "model": str(payload.get("model") or ACTOR_DEFAULT_MODEL),
            "resume_uuid": str(payload.get("resume_uuid") or ""),
            "template": str(payload.get("template") or ""),
        }
        for actor_id, payload in sorted(registry.items())
    }
    _write_session_metadata(work_dir, metadata)


def _resolve_bound_actor_name(
    work_dir: Path, actor_ref: str, *, kind: str = "actor"
) -> str:
    normalized = _normalize_actor_name(actor_ref)
    if not normalized:
        raise SystemExit(f"missing {kind}")
    registry = _actor_registry(work_dir)
    if normalized in registry:
        return normalized
    for actor_id, payload in registry.items():
        label = _shared_normalize_actor_name(str(payload.get("label") or ""))
        if label and label == normalized:
            return actor_id
    raise SystemExit(
        f"{normalized} is not bound for this session; bind them first with "
        f"`gotta actor bind {actor_ref}`"
    )


def _new_actor_identity(registry: dict[str, dict[str, str]], *, seed: str = "") -> str:
    candidate = topology.normalize_identity(seed) if seed else ""
    if candidate and _actor_is_fingerprint(candidate) and candidate not in registry:
        return candidate
    while True:
        candidate = session_token(str(uuid.uuid4()).lower())
        if candidate not in registry:
            return candidate


def _bind_actor_identity(session_root: Path, actor_ref: str) -> tuple[str, bool]:
    registry = _actor_registry(session_root)
    normalized = _normalize_actor_name(actor_ref)
    if normalized in registry:
        return normalized, False
    for actor_id, payload in registry.items():
        label = _shared_normalize_actor_name(str(payload.get("label") or ""))
        if label and label == normalized:
            return actor_id, False
    template = _actor_template_spec(actor_ref)
    label = template.label if template is not None else actor_ref.strip() or normalized
    model = template.default_model if template is not None else ACTOR_DEFAULT_MODEL
    resume_uuid = str(uuid.uuid4()).lower()
    actor_id = _new_actor_identity(
        registry,
        seed=normalized if _actor_is_fingerprint(normalized) else "",
    )
    registry[actor_id] = {
        "label": label,
        "model": model,
        "resume_uuid": resume_uuid,
        "template": template.actor_id if template is not None else "",
    }
    _store_actor_registry(session_root, registry)
    return actor_id, True


def _actor_dir_path(work_dir: Path, actor_name: str) -> Path:
    return _actor_session_dir(work_dir, actor_name)


def _actor_state_link_root(work_dir: Path) -> Path:
    return state_dir_path(work_dir)


def _actor_state_path(work_dir: Path, actor_name: str) -> Path:
    return _actor_session_dir(work_dir, actor_name) / "state" / "actor.json"


def _actor_events_path(work_dir: Path, actor_name: str) -> Path:
    return _actor_session_dir(work_dir, actor_name) / "state" / "actor.jsonl"


def _actor_want_path(work_dir: Path, actor_name: str) -> Path:
    return _actor_session_dir(work_dir, actor_name) / WANT_FILE


def _actor_goal_path(work_dir: Path, actor_name: str) -> Path:
    return _actor_session_dir(work_dir, actor_name) / "GOAL.md"


def _canonical_work_root(session_dir: Path) -> Path:
    return session_dir.expanduser().resolve()


def _actor_session_dir(work_dir: Path, actor_name: str) -> Path:
    return actor_session_root(work_dir.resolve(), _normalize_actor_name(actor_name))


def _actor_is_selected(work_dir: Path, actor_name: str) -> bool:
    normalized = _normalize_actor_name(actor_name)
    resolved = work_dir.resolve()
    if session_actor(resolved) == normalized:
        return True
    actor_root = actor_session_root(resolved, normalized)
    return session_is_initialized(actor_root)


def _read_actor_state(work_dir: Path, actor_name: str) -> dict[str, object]:
    registry = _actor_registry(work_dir)
    normalized = (
        _resolve_bound_actor_name(work_dir, actor_name)
        if registry
        else _normalize_actor_name(actor_name)
    )
    if not _actor_is_selected(work_dir, normalized):
        return {
            "actor": normalized,
            "label": _actor_label(normalized, work_dir=work_dir),
            "status": "pending",
        }
    path = _actor_state_path(work_dir, normalized)
    if not path.exists():
        return {
            "actor": normalized,
            "label": _actor_label(normalized, work_dir=work_dir),
            "status": "pending",
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid actor state file: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"invalid actor state file: {path}")
    data.setdefault("actor", normalized)
    data.setdefault("label", _actor_label(normalized, work_dir=work_dir))
    data["status"] = _normalize_actor_status(data.get("status") or "pending")
    return data


def _write_actor_state(
    work_dir: Path,
    actor_name: str,
    payload: dict[str, object],
) -> Path:
    normalized = (
        _resolve_bound_actor_name(work_dir, actor_name)
        if _actor_registry(work_dir)
        else _normalize_actor_name(actor_name)
    )
    state_dir = _actor_state_link_root(work_dir)
    ensure_private_dir(state_dir)
    path = _actor_state_path(work_dir, normalized)
    ensure_private_dir(path.parent)
    merged = _read_actor_state(work_dir, normalized)
    for key, value in payload.items():
        if value is None:
            merged.pop(key, None)
            continue
        merged[key] = value
    merged["actor"] = normalized
    merged["label"] = _actor_label(normalized, work_dir=work_dir)
    merged["updated_at"] = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    status = _normalize_actor_status(merged.get("status") or "pending")
    merged["status"] = status
    if status not in ACTOR_STATE_STATUS:
        raise SystemExit(f"invalid actor status: {status}")
    write_text_atomic(path, json.dumps(merged, indent=2, sort_keys=True) + "\n")
    return path
