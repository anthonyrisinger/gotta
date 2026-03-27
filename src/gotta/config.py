"""Shared user-config and state-path helpers."""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
from typing import Any
from platformdirs import PlatformDirs

from gotta.compat import tomllib
from gotta.vault import write_secret_text_atomic


APP_NAME = "gotta"
CONFIG_FILE_NAME = "gotta.toml"
PRIVATE_DIR_MODE = 0o700


def _dirs() -> PlatformDirs:
    return PlatformDirs(appname=APP_NAME, appauthor=False, ensure_exists=False)


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(PRIVATE_DIR_MODE)
    except OSError:
        pass


def user_config_dir() -> Path:
    override = os.environ.get("GOTTA_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(_dirs().user_config_path)


def user_data_dir() -> Path:
    override = os.environ.get("GOTTA_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(_dirs().user_data_path)


def user_state_dir() -> Path:
    override = os.environ.get("GOTTA_STATE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(_dirs().user_state_path)


def default_config_file() -> Path:
    return user_config_dir() / CONFIG_FILE_NAME


def primary_config_file() -> Path:
    override = os.environ.get("GOTTA_CONFIG_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return default_config_file()


def config_file_candidates() -> list[Path]:
    return [primary_config_file()]


def display_path(path: Path) -> str:
    try:
        return "~/" + str(path.expanduser().relative_to(Path.home()))
    except ValueError:
        return str(path)


def provider_env_reference(provider: str) -> str:
    return (
        f"[providers.{provider}.env] in GOTTA_CONFIG_FILE "
        f"(default {display_path(default_config_file())})"
    )


def env_or_config(config_env: Mapping[str, str], *names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    for name in names:
        value = str(config_env.get(name, "")).strip()
        if value:
            return value
    return default


def extract_provider_env(config: Mapping[str, Any], provider: str) -> dict[str, str]:
    providers = config.get("providers")
    if isinstance(providers, Mapping):
        provider_table = providers.get(provider)
        if isinstance(provider_table, Mapping):
            env_table = provider_table.get("env")
            if isinstance(env_table, Mapping):
                result = {
                    key: value
                    for key, value in env_table.items()
                    if isinstance(key, str) and isinstance(value, str)
                }
                if result:
                    return result
    return {}


def load_config() -> dict[str, Any]:
    path = primary_config_file()
    if not path.exists():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _toml_scalar(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    raise TypeError(f"unsupported config value: {value!r}")


def _render_table(
    prefix: list[str], table: Mapping[str, Any], lines: list[str]
) -> None:
    scalars = [
        (key, value) for key, value in table.items() if not isinstance(value, Mapping)
    ]
    subtables = [
        (key, value) for key, value in table.items() if isinstance(value, Mapping)
    ]
    if prefix:
        lines.append(f"[{'.'.join(prefix)}]")
        for key, value in scalars:
            lines.append(f"{key} = {_toml_scalar(value)}")
        lines.append("")
    for key, value in subtables:
        _render_table([*prefix, key], value, lines)


def write_config(config: Mapping[str, Any]) -> Path:
    path = primary_config_file()
    _ensure_private_dir(path.parent)
    lines: list[str] = []
    _render_table([], config, lines)
    text = "\n".join(lines).rstrip() + "\n" if lines else ""
    write_secret_text_atomic(
        path, text, ensure_dir=lambda: _ensure_private_dir(path.parent)
    )
    return path


def set_provider_env_values(provider: str, updates: Mapping[str, str]) -> Path:
    config = load_config()
    providers = config.setdefault("providers", {})
    if not isinstance(providers, dict):
        providers = {}
        config["providers"] = providers
    provider_table = providers.setdefault(provider, {})
    if not isinstance(provider_table, dict):
        provider_table = {}
        providers[provider] = provider_table
    env_table = provider_table.setdefault("env", {})
    if not isinstance(env_table, dict):
        env_table = {}
        provider_table["env"] = env_table
    for key, value in updates.items():
        env_table[key] = value
    return write_config(config)
