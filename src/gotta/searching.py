"""Shared routing helpers for top-level `gotta search`."""

from __future__ import annotations

from dataclasses import dataclass
import shlex

from gotta.builtin import get_plugin
from gotta.target import discover_plugin_route


SPECIALIZED_PROVIDER_COMMANDS: dict[tuple[str, str], str] = {
    ("jira", "jql"): "jira jql",
    ("confluence", "cql"): "confluence cql",
    ("granola", "search-transcript"): "granola search-transcript",
}

READ_LIKE_PROVIDER_COMMANDS = {"get", "transcript"}
READ_LIKE_TARGET_PREFIXES = ("http://", "https://", "artifact:", "content:")


@dataclass(frozen=True, slots=True)
class SearchRoute:
    provider: str
    provider_argv: list[str]
    target: str


class SearchRouteError(ValueError):
    """Raised when a top-level search target cannot be routed cleanly."""


def _first_word(text: str) -> str:
    if not text.strip():
        return ""
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    return parts[0].strip() if parts else ""


def _raw_tail(argv: list[str]) -> tuple[str, str, str]:
    if not argv:
        raise SearchRouteError(
            "search target must start with <provider>:<query>; use `gotta search jira:Architecture`"
        )
    target = str(argv[0] or "").strip()
    if not target:
        raise SearchRouteError(
            "search target must start with <provider>:<query>; use `gotta search jira:Architecture`"
        )
    if target.startswith("-"):
        raise SearchRouteError(
            "search target must start with <provider>:<query>; flags belong after the provider-qualified target"
        )
    if target.startswith(READ_LIKE_TARGET_PREFIXES):
        raise SearchRouteError(
            f"read-like targets belong on `gotta read`; use `gotta read {shlex.join(argv)}`"
        )
    provider, separator, tail = target.partition(":")
    provider = provider.strip()
    if not separator or not provider or not tail.strip():
        raise SearchRouteError(
            "search target must start with <provider>:<query>; use `gotta search jira:Architecture`"
        )
    extra_terms = [str(raw_part or "").strip() for raw_part in argv[1:] if str(raw_part or "").strip()]
    if extra_terms:
        raise SearchRouteError(
            "top-level `gotta search` takes exactly one provider-qualified plain-text query string; "
            f"quote the full `<provider>:<query>` argument or use `gotta {provider} search ...` "
            "for structured flags"
        )
    raw_tail = tail.strip()
    if not raw_tail:
        raise SearchRouteError(
            "search target must include a query seed after the provider; use `gotta search jira:Architecture`"
        )
    return provider, raw_tail, target


def _canonical_read_locator(
    provider: str,
    provider_argv: list[str],
    *,
    fallback: str,
) -> str:
    spec = get_plugin(provider)
    if spec and spec.canonical_locator is not None:
        locator = str(spec.canonical_locator(provider_argv) or "").strip()
        if locator:
            return locator
    return fallback


def _tail_after_first_word(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    try:
        parts = shlex.split(stripped)
    except ValueError:
        _first, _separator, rest = stripped.partition(" ")
        return rest.strip()
    if len(parts) < 2:
        return ""
    return " ".join(parts[1:]).strip()


def _discover_read_locator(provider: str, candidate: str) -> str:
    routed = discover_plugin_route(candidate)
    if routed is None:
        return ""
    routed_provider, provider_argv = routed
    if routed_provider != provider or not provider_argv:
        return ""
    if candidate.startswith(READ_LIKE_TARGET_PREFIXES):
        return _canonical_read_locator(provider, provider_argv, fallback=candidate)
    if provider_argv[0] not in READ_LIKE_PROVIDER_COMMANDS:
        return ""
    return _canonical_read_locator(provider, provider_argv, fallback=candidate)


def _canonical_read_redirect(provider: str, raw_tail: str) -> str:
    first = _first_word(raw_tail)
    subject = _tail_after_first_word(raw_tail)
    if not first or not subject:
        return ""
    candidates: list[str] = []
    if first == "get":
        if subject.startswith(READ_LIKE_TARGET_PREFIXES):
            candidates.append(subject)
        if provider == "github":
            normalized = subject.strip()
            if normalized.startswith("github.com/"):
                candidates.append(f"https://{normalized}")
            elif "/" in normalized and " " not in normalized:
                candidates.append(f"https://github.com/{normalized.lstrip('/')}")
        if provider == "slack" and " " not in subject:
            if ":" in subject:
                channel_id, thread_ref = subject.split(":", 1)
                if channel_id and thread_ref:
                    candidates.append(f"slack:thread:{channel_id}:{thread_ref}")
            else:
                candidates.append(f"slack:channel:{subject}")
        candidates.append(f"{provider}:{subject}")
    elif first == "transcript":
        candidates.append(f"{provider}:transcript {subject}")
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        locator = _discover_read_locator(provider, candidate)
        if locator:
            return locator
    return ""


def resolve_search_route(argv: list[str]) -> SearchRoute:
    provider, raw_tail, target = _raw_tail(argv)
    spec = get_plugin(provider)
    if (
        spec is None
        or spec.route_target is None
        or provider in {"read", "session", "search"}
    ):
        raise SearchRouteError(
            f"unknown search provider `{provider}`; use one of the routed provider plugins"
        )
    first = _first_word(raw_tail)
    specialized = SPECIALIZED_PROVIDER_COMMANDS.get((provider, first))
    if specialized:
        raise SearchRouteError(
            f"`gotta search` is for plain-text search; use `gotta {specialized}` for `{provider}:{first}`"
        )
    if first in READ_LIKE_PROVIDER_COMMANDS:
        read_target = _canonical_read_redirect(provider, raw_tail)
        if read_target:
            suggestion = shlex.join(["gotta", "read", read_target])
        else:
            suggestion = shlex.join(["gotta", provider, raw_tail])
        raise SearchRouteError(
            f"read-like provider targets belong on `gotta read`; use `{suggestion}`"
        )
    explicit_alias = first == "search"
    query = _tail_after_first_word(raw_tail) if explicit_alias else raw_tail
    query = query.strip()
    if not query:
        raise SearchRouteError(
            "search target must include a query seed after the provider; use `gotta search jira:Architecture`"
        )
    return SearchRoute(
        provider=provider,
        provider_argv=["search", query],
        target=target,
    )
