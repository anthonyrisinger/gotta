"""Kapa provider-backed ask binding exports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gotta.ask import kapa as kapa_ask
from gotta.builtin import SurfaceBinding
from gotta.config import env_or_config, extract_provider_env, load_config


def _provider_table(config: Mapping[str, Any]) -> Mapping[str, Any]:
    providers = config.get("providers")
    if not isinstance(providers, Mapping):
        return {}
    provider = providers.get(kapa_ask.KAPA_PROVIDER)
    if not isinstance(provider, Mapping):
        return {}
    return provider


def _bindings_table(config: Mapping[str, Any]) -> Mapping[str, Any]:
    table = _provider_table(config).get("bindings")
    if not isinstance(table, Mapping):
        return {}
    return table


def _string_value(
    table: Mapping[str, Any],
    *keys: str,
    default: str = "",
) -> str:
    for key in keys:
        value = table.get(key)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
    return default


def _int_value(
    table: Mapping[str, Any],
    *keys: str,
    default: int | None = None,
) -> int | None:
    for key in keys:
        value = table.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.isdigit():
                return int(stripped)
    return default


def _spec_from_table(
    name: str,
    table: Mapping[str, Any],
    *,
    config_env: Mapping[str, str],
) -> kapa_ask.KapaBindingSpec | None:
    project_id = _string_value(table, "project_id", "project")
    if not project_id:
        return None
    token_env = _string_value(table, "token_env")
    token_index = _int_value(table, "token_index", "token", default=None)
    api_base_url = _string_value(
        table,
        "api_base_url",
        default=env_or_config(
            config_env,
            kapa_ask.KAPA_API_BASE_URL_ENV,
            default=kapa_ask.KAPA_DEFAULT_API_BASE_URL,
        ),
    )
    description = _string_value(
        table,
        "description",
        default=f"query the configured {name} Kapa knowledge base",
    )
    return kapa_ask.KapaBindingSpec(
        name=name,
        project_id=project_id,
        description=description,
        token_index=token_index,
        token_env=token_env or None,
        api_base_url=api_base_url,
    )


def binding_specs() -> list[kapa_ask.KapaBindingSpec]:
    config = load_config()
    config_env = extract_provider_env(config, kapa_ask.KAPA_PROVIDER)
    bindings: dict[str, kapa_ask.KapaBindingSpec] = {}
    for name, value in _bindings_table(config).items():
        if not isinstance(name, str) or not isinstance(value, Mapping):
            continue
        spec = _spec_from_table(name, value, config_env=config_env)
        if spec is None:
            continue
        bindings[name] = spec
    return [bindings[name] for name in sorted(bindings)]


def ask_bindings() -> list[SurfaceBinding]:
    return [kapa_ask.binding_for(spec) for spec in binding_specs()]
