"""Top-level `gotta search` routing."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import shlex

from gotta.builtin import get_binding

READ_LIKE_TARGET_PREFIXES = ("http://", "https://", "artifact:", "content:")


@dataclass(frozen=True, slots=True)
class SearchRoute:
    provider: str
    provider_argv: list[str]
    target: str


class SearchRouteError(ValueError):
    """Raised when a top-level search target cannot be routed cleanly."""


ReadTargetResolver = Callable[[str], str]


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
    extra_terms = [
        str(raw_part or "").strip()
        for raw_part in argv[1:]
        if str(raw_part or "").strip()
    ]
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


def search_query(raw_tail: str) -> str:
    first = _first_word(raw_tail)
    query = _tail_after_first_word(raw_tail) if first == "search" else raw_tail
    query = query.strip()
    if not query:
        raise SearchRouteError(
            "search target must include a query seed after the provider; use `gotta search jira:Architecture`"
        )
    return query


def search_redirect_error(
    provider: str,
    raw_tail: str,
    *,
    read_target: str = "",
) -> SearchRouteError:
    suggestion = (
        shlex.join(["gotta", "read", read_target])
        if read_target
        else shlex.join(["gotta", provider, raw_tail])
    )
    return SearchRouteError(
        f"read-like provider targets belong on `gotta read`; use `{suggestion}`"
    )


def specialized_search_error(
    provider: str,
    raw_tail: str,
    *,
    command: str,
) -> SearchRouteError:
    return SearchRouteError(
        f"`gotta search` is for plain-text search; use `gotta {command}` for `{provider}:{_first_word(raw_tail)}`"
    )


def plain_text_search_route(
    provider: str,
    raw_tail: str,
    *,
    specialized_commands: Mapping[str, str] | None = None,
    read_redirects: Mapping[str, ReadTargetResolver] | None = None,
) -> list[str]:
    first = _first_word(raw_tail)
    specialized = str((specialized_commands or {}).get(first) or "").strip()
    if specialized:
        raise specialized_search_error(provider, raw_tail, command=specialized)
    redirect = (read_redirects or {}).get(first)
    if redirect is not None:
        subject = _tail_after_first_word(raw_tail)
        raise search_redirect_error(
            provider,
            raw_tail,
            read_target=redirect(subject) if subject else "",
        )
    return ["search", search_query(raw_tail)]


def resolve_search_route(argv: list[str]) -> SearchRoute:
    provider, raw_tail, target = _raw_tail(argv)
    binding = get_binding(provider)
    if (
        binding is None
        or binding.search_route is None
        or provider
        in {
            "read",
            "session",
            "search",
        }
    ):
        raise SearchRouteError(
            f"unknown search provider `{provider}`; use one of the routed provider plugins"
        )
    try:
        provider_argv = list(binding.search_route(raw_tail))
    except SearchRouteError:
        raise
    except ValueError as exc:
        raise SearchRouteError(str(exc)) from exc
    return SearchRoute(
        provider=provider,
        provider_argv=provider_argv,
        target=target,
    )
