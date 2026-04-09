#!/usr/bin/env python3
"""Kapa-backed ask bindings."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from gotta.builtin import CommandPath, PackageSpec, SurfaceBinding, SurfaceSpec
from gotta.config import env_or_config, extract_provider_env, load_config
from gotta.helptext import is_long_help_request, print_long_help


KAPA_PROVIDER = "kapa"
KAPA_TIMEOUT_SECONDS = 30.0
KAPA_API_BASE_URL_ENV = "GOTTA_KAPA_API_BASE_URL"
KAPA_DEFAULT_API_BASE_URL = "https://api.kapa.ai/query/v1"


@dataclass(frozen=True, slots=True)
class KapaBindingSpec:
    name: str
    project_id: str
    description: str
    token_index: int | None = None
    token_env: str | None = None
    api_base_url: str = KAPA_DEFAULT_API_BASE_URL

    @property
    def auth_profile(self) -> str:
        if self.token_env:
            return self.token_env
        if self.token_index and self.token_index > 0:
            return f"kapa-token-{self.token_index}"
        return f"kapa-token-{_binding_env_suffix(self.name).lower()}"

    @property
    def defaults(self) -> tuple[tuple[str, str], ...]:
        entries = [
            ("provider", KAPA_PROVIDER),
            ("project_id", self.project_id),
            ("api_base_url", self.api_base_url),
        ]
        if self.token_env:
            entries.append(("token_env", self.token_env))
        elif self.token_index is not None:
            entries.append(("token_index", str(self.token_index)))
        return tuple(entries)

    @property
    def token_candidates(self) -> tuple[str, ...]:
        if self.token_env:
            return (self.token_env,)
        if self.token_index and self.token_index > 0:
            return (f"GOTTA_KAPA_TOKEN_{self.token_index}", "GOTTA_KAPA_TOKEN")
        return (
            f"GOTTA_KAPA_TOKEN_{_binding_env_suffix(self.name)}",
            "GOTTA_KAPA_TOKEN",
        )


class ToolError(RuntimeError):
    """Raised when the Kapa ask binding cannot satisfy a request."""


def die(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    return code


def _binding_env_suffix(name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", name.strip().upper()).strip("_")
    return normalized or "DEFAULT"


def _valid_gotta_env_name(name: str) -> bool:
    if not name.startswith("GOTTA_"):
        return False
    return bool(re.fullmatch(r"[A-Z0-9_]+", name))


def _build_parser(spec: KapaBindingSpec) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"gotta ask {spec.name}",
        description=spec.description,
    )
    parser.add_argument(
        "query",
        nargs="*",
        help="question text; when omitted, stdin is read instead",
    )
    parser.add_argument(
        "--output",
        choices=("markdown", "json"),
        default="markdown",
        help="render response as markdown or raw normalized JSON",
    )
    return parser


def _config_reference(spec: KapaBindingSpec) -> str:
    names = " or ".join(spec.token_candidates)
    return (
        f"set {names} via env or [providers.{KAPA_PROVIDER}.env], and configure "
        f"[providers.{KAPA_PROVIDER}.bindings.{spec.name}] if you need a non-default "
        "project/token mapping"
    )


def _resolve_token(config_env: Mapping[str, str], spec: KapaBindingSpec) -> str:
    if spec.token_env and not _valid_gotta_env_name(spec.token_env):
        raise ToolError(
            f"invalid token_env for `{spec.name}`: `{spec.token_env}`; token_env must "
            "name a GOTTA_* variable"
        )
    return env_or_config(config_env, *spec.token_candidates)


def _resolve_query(args: argparse.Namespace) -> str:
    if args.query:
        return " ".join(args.query).strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    return ""


def _response_json(response: Any) -> dict[str, Any]:
    raw = response.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolError(f"invalid Kapa response payload: {exc}") from exc


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    body = b""
    try:
        body = exc.read()
    except Exception:
        body = b""
    if body:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            detail = body.decode("utf-8", errors="replace").strip()
        else:
            detail = str(
                payload.get("message")
                or payload.get("detail")
                or payload.get("error")
                or ""
            ).strip()
        if detail:
            return f"Kapa request failed with {exc.code}: {detail}"
    return f"Kapa request failed with {exc.code}: {exc.reason}"


def _normalize_sources(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    sources: list[dict[str, Any]] = []
    raw = payload.get("relevant_sources")
    if not isinstance(raw, list):
        return sources
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        source_url = str(item.get("source_url") or "").strip()
        if not source_url or source_url in seen:
            continue
        seen.add(source_url)
        sources.append(
            {
                "title": str(item.get("title") or source_url).strip() or source_url,
                "source_url": source_url,
                "contains_internal_data": bool(item.get("contains_internal_data")),
            }
        )
    return sorted(sources, key=lambda item: (item["title"], item["source_url"]))


def _request_payload(spec: KapaBindingSpec, query: str) -> dict[str, Any]:
    config = load_config()
    config_env = extract_provider_env(config, KAPA_PROVIDER)
    token = _resolve_token(config_env, spec)
    if not token:
        raise ToolError(
            f"missing Kapa API token for `{spec.name}`; {_config_reference(spec)}"
        )

    url = (
        spec.api_base_url.rstrip("/")
        + "/projects/"
        + urllib.parse.quote(spec.project_id, safe="")
        + "/chat/"
    )
    request = urllib.request.Request(
        url,
        data=json.dumps({"query": query}).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-API-KEY": token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=KAPA_TIMEOUT_SECONDS) as response:
            payload = _response_json(response)
    except urllib.error.HTTPError as exc:
        raise ToolError(_http_error_message(exc)) from exc
    except urllib.error.URLError as exc:
        raise ToolError(f"Kapa request failed: {exc.reason}") from exc

    return {
        "binding": spec.name,
        "project_id": spec.project_id,
        "answer": str(payload.get("answer") or "").strip(),
        "thread_id": str(payload.get("thread_id") or "").strip(),
        "question_answer_id": str(payload.get("question_answer_id") or "").strip(),
        "is_uncertain": bool(payload.get("is_uncertain")),
        "sources": _normalize_sources(payload),
    }


def _render_markdown(payload: Mapping[str, Any]) -> str:
    answer = str(payload.get("answer") or "").strip()
    thread_id = str(payload.get("thread_id") or "").strip()
    question_answer_id = str(payload.get("question_answer_id") or "").strip()
    is_uncertain = "true" if bool(payload.get("is_uncertain")) else "false"
    source_lines: list[str] = []
    raw_sources = payload.get("sources")
    if isinstance(raw_sources, list):
        for item in raw_sources:
            if not isinstance(item, Mapping):
                continue
            title = str(item.get("title") or item.get("source_url") or "").strip()
            source_url = str(item.get("source_url") or "").strip()
            if not title or not source_url:
                continue
            internal = (
                " _(internal)_" if bool(item.get("contains_internal_data")) else ""
            )
            source_lines.append(f"- [{title}]({source_url}){internal}")
    sources = "\n".join(source_lines) if source_lines else "_No sources returned._"
    return (
        "# Answer\n\n"
        + answer
        + "\n\n---\n\n### Metadata\n\n"
        + f"- **Binding:** {payload.get('binding') or ''}\n"
        + f"- **Project ID:** {payload.get('project_id') or ''}\n"
        + f"- **Thread ID:** {thread_id}\n"
        + f"- **Question/Answer ID:** {question_answer_id}\n"
        + f"- **Is uncertain:** {is_uncertain}\n\n"
        + "### Sources\n\n"
        + sources
    )


def main(argv: list[str], *, spec: KapaBindingSpec) -> int:
    parser = _build_parser(spec)
    if is_long_help_request(argv):
        return print_long_help(parser)
    try:
        args = parser.parse_args(argv)
        query = _resolve_query(args)
        if not query:
            raise ToolError("missing query text; pass text arguments or pipe stdin")
        payload = _request_payload(spec, query)
        if args.output == "json":
            json.dump(payload, sys.stdout, indent=2, sort_keys=True)
            sys.stdout.write("\n")
        else:
            print(_render_markdown(payload))
        return 0
    except ToolError as exc:
        return die(str(exc), code=1)


def runner_for(spec: KapaBindingSpec):
    def run(argv: list[str]) -> int:
        return main(argv, spec=spec)

    return run


def invocation_locator_for(spec: KapaBindingSpec):
    def locate(argv: list[str]) -> str:
        if not argv:
            return spec.name
        return f"{spec.name} {' '.join(argv)}".strip()

    return locate


def canonical_locator_for(spec: KapaBindingSpec):
    def locate(argv: list[str]) -> str:
        if not argv:
            return f"ask:{spec.name}"
        return f"ask:{spec.name}:{' '.join(argv).strip()}"

    return locate


def preferred_name_for(spec: KapaBindingSpec):
    def resolve(argv: list[str], options: Any) -> str:
        if getattr(options, "save_as", ""):
            return str(options.save_as)
        return (
            f"{spec.name}.json"
            if "--output" in argv and "json" in argv
            else f"{spec.name}.md"
        )

    return resolve


def binding_for(
    spec: KapaBindingSpec,
    *,
    package_name: str = "gotta",
) -> SurfaceBinding:
    return SurfaceBinding(
        name=spec.name,
        command_path=CommandPath(("ask", spec.name)),
        package=PackageSpec(package_name),
        surface=SurfaceSpec(
            name=spec.name,
            description=spec.description,
            runner=runner_for(spec),
            session_access="none",
            invocation_locator=invocation_locator_for(spec),
            canonical_locator=canonical_locator_for(spec),
            preferred_name=preferred_name_for(spec),
            content_type=content_type_for(spec),
        ),
        auth_profile=spec.auth_profile,
        defaults=spec.defaults,
    )


def content_type_for(spec: KapaBindingSpec):
    def resolve(argv: list[str], _name: str) -> str:
        return (
            "application/json"
            if "--output" in argv and "json" in argv
            else "text/markdown"
        )

    return resolve
