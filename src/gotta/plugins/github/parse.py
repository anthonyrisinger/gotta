"""GitHub argument parsing and invocation naming."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import sys
from typing import Any
import urllib.parse

from gotta.helptext import is_long_help_request

from .route import COMMITS_RE, COMMITS_ROOT_RE


USAGE = """usage: gotta github [status [--output json|summary] | search [--global] [--type repo|issue|pr|code] [--repo owner/repo] [--filename NAME] [--extension EXT] [--language LANG] [--match file|path] [--limit N] [--output json|summary|markdown] <query...> | [--output json|summary|markdown] [--limit N] <github_url>]

Supported URL shapes:
  - repository root
  - blob URLs
  - tree URLs
  - pull request URLs
  - issue URLs
  - commit URLs
  - commit history URLs (`/commits` resolves through the repo default branch; `/commits/HEAD` follows the current tip; `/commits/<ref>` requires an existing ref)
  - workflow run URLs (`/actions/runs/<run-id>`)
  - workflow job URLs (`/actions/runs/<run-id>/job/<job-id>`)
  - release list URLs
  - release tag URLs

Output:
  markdown  body/content rendering (default)
  summary   metadata-first terminal summary
  json      raw-ish structured payload from the GitHub CLI / API

URL list shaping:
  --limit N applies only to commit history URLs (`/commits`, `/commits/HEAD`,
            or `/commits/<ref>`)

Search:
  repo      repository discovery (default)
  issue     issue search
  pr        pull request search
  code      code and file discovery

Unscoped search is owned-scope by default: the authenticated user plus visible
organizations only. Use `--global` to search the wider GitHub corpus while
excluding those owned-scope results.

Use --help-all for the same long-form usage output.
"""

SEARCH_USAGE = """usage: gotta github search [--global] [--type repo|issue|pr|code] [--repo owner/repo] [--filename NAME] [--extension EXT] [--language LANG] [--match file|path] [--limit N] [--output json|summary|markdown] <query...>

Search:
  default             owned-scope only (authenticated user + visible orgs)
  --global            wider GitHub corpus, excluding owned-scope hits
  --type repo         repository discovery (default)
  --type issue        issue search
  --type pr           pull request search
  --type code         code and file discovery
  --repo owner/repo   narrow search to one repository
  --filename NAME     code search only; narrow by filename
  --extension EXT     code search only; narrow by extension
  --language LANG     code search only; narrow by language
  --match file|path   code search only; restrict matches to content or path

Examples:
  gotta github search ABC
  gotta github search --global ABC
  gotta github search --type pr --repo acme/widgets auth proxy
  gotta github search --type code --repo acme/widgets --filename package.json lint
"""


@dataclass(frozen=True)
class ParsedArgs:
    command: str
    output: str
    url: str = ""
    fragment: str = ""
    query: str = ""
    search_type: str = "repo"
    repo: str = ""
    filename: str = ""
    extension: str = ""
    language: str = ""
    match: str = ""
    limit: int | None = None
    global_search: bool = False


def _slug(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower())
    return normalized.strip("-") or "github"


def _normalize_search_locator_tail(parsed: ParsedArgs) -> list[str]:
    args = ["search"]
    if parsed.global_search and not parsed.repo:
        args.append("--global")
    if parsed.search_type != "repo":
        args.extend(["--type", parsed.search_type])
    if parsed.repo:
        args.extend(["--repo", parsed.repo])
    if parsed.filename:
        args.extend(["--filename", parsed.filename])
    if parsed.extension:
        args.extend(["--extension", parsed.extension])
    if parsed.language:
        args.extend(["--language", parsed.language])
    if parsed.match:
        args.extend(["--match", parsed.match])
    args.append(parsed.query)
    return args


def _canonical_search_locator(parsed: ParsedArgs) -> str:
    return "github:" + " ".join(_normalize_search_locator_tail(parsed))


def _preferred_render_name(parsed: ParsedArgs, extension: str) -> str:
    def render_name(base: str) -> str:
        suffix = f".{extension}"
        return base if base.endswith(suffix) else f"{base}{suffix}"

    parsed_url = urllib.parse.urlparse(parsed.url.split("#", 1)[0].split("?", 1)[0])
    parts = [part for part in parsed_url.path.split("/") if part]
    if len(parts) >= 2:
        repo = _slug(parts[1])
        if len(parts) == 2:
            return render_name(repo)
        if len(parts) >= 4 and parts[2] == "pull" and parts[3].isdigit():
            suffix = f"{repo}-pr-{parts[3]}"
            if len(parts) >= 5 and parts[4] == "commits":
                suffix = f"{suffix}-commits"
            return render_name(suffix)
        if len(parts) >= 4 and parts[2] == "issues" and parts[3].isdigit():
            return render_name(f"{repo}-issue-{parts[3]}")
        if len(parts) >= 4 and parts[2] == "commit":
            return render_name(f"{repo}-commit-{_slug(parts[3])}")
        if len(parts) >= 3 and parts[2] == "commits":
            suffix = f"{repo}-commits"
            if len(parts) >= 4:
                suffix = f"{suffix}-{_slug(parts[3])}"
            if len(parts) >= 5:
                suffix = f"{suffix}-{_slug('/'.join(parts[4:]))}"
            return render_name(suffix)
        if len(parts) >= 5 and parts[2] == "actions" and parts[3] == "runs":
            suffix = f"{repo}-run-{_slug(parts[4])}"
            if len(parts) >= 7 and parts[5] == "job":
                suffix = f"{suffix}-job-{_slug(parts[6])}"
            return render_name(suffix)
        if len(parts) >= 4 and parts[2] == "tree":
            suffix = f"{repo}-tree-{_slug(parts[3])}"
            if len(parts) >= 5:
                suffix = f"{suffix}-{_slug('/'.join(parts[4:]))}"
            return render_name(suffix)
        if len(parts) >= 5 and parts[2] == "blob":
            return render_name(
                f"{repo}-blob-{_slug(parts[3])}-{_slug('/'.join(parts[4:]))}"
            )
        if len(parts) >= 3 and parts[2] == "releases":
            if len(parts) >= 5 and parts[3] == "tag":
                return render_name(f"{repo}-release-{_slug(parts[4])}")
            return render_name(f"{repo}-releases")
    name = Path(parsed_url.path.rstrip("/")).name or "github"
    return render_name(name)


def die(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    return code


def _parse_limit_value(args: list[str], index: int, *, context: str) -> tuple[int, int]:
    if index + 1 >= len(args):
        raise SystemExit(die(f"{context} requires an integer after `--limit`", code=2))
    try:
        value = max(1, min(int(args[index + 1]), 100))
    except ValueError:
        raise SystemExit(
            die(f"{context} requires an integer after `--limit`", code=2)
        ) from None
    return value, index + 2


def unsupported_render_limit_error() -> int:
    return die(
        "`--limit` is only supported for GitHub commit-history URLs "
        "(`/commits`, `/commits/HEAD`, or `/commits/<ref>`). "
        "Use `/commits` or `/commits/HEAD` for the canonical branch-agnostic forms.",
        code=2,
    )


def _supports_render_limit(url: str) -> bool:
    target = url.split("#", 1)[0].split("?", 1)[0]
    return bool(COMMITS_ROOT_RE.match(target) or COMMITS_RE.match(target))


def parse_args(argv: list[str], *, emit_help: bool = True) -> ParsedArgs:
    args = list(argv)
    if args[:2] == ["search", "--help"] or args[:2] == ["search", "-h"]:
        if emit_help:
            print(SEARCH_USAGE)
        return ParsedArgs(command="", output="")
    if args and args[0] == "search" and is_long_help_request(args[1:]):
        if emit_help:
            print(SEARCH_USAGE)
        return ParsedArgs(command="", output="")
    if len(args) == 1 and args[0] in {"-h", "--help"}:
        if emit_help:
            print(USAGE)
        return ParsedArgs(command="", output="")
    if is_long_help_request(args):
        if emit_help:
            print(USAGE)
        return ParsedArgs(command="", output="")
    if args and args[0] == "status":
        output = "summary"
        index = 1
        while index < len(args):
            token = args[index]
            if token == "--output":
                if index + 1 >= len(args) or args[index + 1] not in {"json", "summary"}:
                    raise SystemExit(
                        die(
                            "GitHub status accepts only `--output summary` or `--output json`.",
                            code=2,
                        )
                    )
                output = args[index + 1]
                index += 2
                continue
            if token.startswith("--"):
                raise SystemExit(
                    die(
                        f"unsupported GitHub status flag `{token}`. "
                        "Use `gotta github status [--output summary|json]`.",
                        code=2,
                    )
                )
            raise SystemExit(
                die(
                    f"unexpected GitHub status argument `{token}`. "
                    "Use `gotta github status [--output summary|json]`.",
                    code=2,
                )
            )
        return ParsedArgs(command="status", output=output)
    if args and args[0] == "search":
        output = "markdown"
        search_type = "repo"
        repo = ""
        filename = ""
        extension = ""
        language = ""
        match = ""
        limit = 10
        global_search = False
        query_parts: list[str] = []
        index = 1
        while index < len(args):
            token = args[index]
            if token == "--output":
                if index + 1 >= len(args):
                    raise SystemExit(die(USAGE))
                output = args[index + 1]
                if output not in {"json", "summary", "markdown"}:
                    raise SystemExit(die(USAGE))
                index += 2
                continue
            if token == "--type":
                if index + 1 >= len(args):
                    raise SystemExit(die(USAGE))
                search_type = args[index + 1]
                if search_type not in {"repo", "issue", "pr", "code"}:
                    raise SystemExit(die(USAGE))
                index += 2
                continue
            if token == "--repo":
                if index + 1 >= len(args):
                    raise SystemExit(die(USAGE))
                repo = args[index + 1].strip()
                if not re.fullmatch(r"[^/\s]+/[^/\s]+", repo):
                    raise SystemExit(die(USAGE))
                index += 2
                continue
            if token == "--filename":
                if index + 1 >= len(args):
                    raise SystemExit(die(USAGE))
                filename = args[index + 1].strip()
                if not filename:
                    raise SystemExit(die(USAGE))
                index += 2
                continue
            if token == "--extension":
                if index + 1 >= len(args):
                    raise SystemExit(die(USAGE))
                extension = args[index + 1].strip()
                if not extension:
                    raise SystemExit(die(USAGE))
                index += 2
                continue
            if token == "--language":
                if index + 1 >= len(args):
                    raise SystemExit(die(USAGE))
                language = args[index + 1].strip()
                if not language:
                    raise SystemExit(die(USAGE))
                index += 2
                continue
            if token == "--match":
                if index + 1 >= len(args):
                    raise SystemExit(die(USAGE))
                match = args[index + 1].strip()
                if match not in {"file", "path"}:
                    raise SystemExit(die(USAGE))
                index += 2
                continue
            if token == "--global":
                global_search = True
                index += 1
                continue
            if token == "--limit":
                limit, index = _parse_limit_value(args, index, context="GitHub search")
                continue
            if token.startswith("--"):
                raise SystemExit(die(USAGE))
            query_parts.append(token)
            index += 1
        query = " ".join(part.strip() for part in query_parts if part.strip()).strip()
        if not query:
            raise SystemExit(die(USAGE))
        if search_type != "code" and any((filename, extension, language, match)):
            raise SystemExit(die(USAGE))
        return ParsedArgs(
            command="search",
            output=output,
            query=query,
            search_type=search_type,
            repo=repo,
            filename=filename,
            extension=extension,
            language=language,
            match=match,
            limit=limit,
            global_search=global_search,
        )
    output = "markdown"
    url = ""
    fragment = ""
    limit: int | None = None
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--output":
            if index + 1 >= len(args):
                raise SystemExit(die(USAGE))
            output = args[index + 1]
            if output not in {"json", "summary", "markdown"}:
                raise SystemExit(die(USAGE))
            index += 2
            continue
        if token == "--limit":
            limit, index = _parse_limit_value(args, index, context="GitHub URL render")
            continue
        if token.startswith("--"):
            raise SystemExit(
                die(
                    f"unsupported GitHub render flag `{token}`. URL renders accept only "
                    "`--output` and `--limit`; `--limit` applies only to commit-history URLs.",
                    code=2,
                )
            )
        if url:
            raise SystemExit(
                die(
                    f"unexpected second GitHub URL target `{token}`. "
                    "GitHub renders accept exactly one URL target.",
                    code=2,
                )
            )
        url = token
        index += 1
    if not url:
        raise SystemExit(die(USAGE))
    parsed_url = urllib.parse.urlparse(url)
    fragment = parsed_url.fragment
    if limit is not None and not _supports_render_limit(url):
        raise SystemExit(unsupported_render_limit_error())
    return ParsedArgs(
        command="render", output=output, url=url, fragment=fragment, limit=limit
    )


def canonical_locator(argv: list[str]) -> str:
    parsed = parse_args(argv, emit_help=False)
    if not parsed.command:
        return "github:help"
    if parsed.command == "status":
        return "github:status"
    if parsed.command == "search":
        return _canonical_search_locator(parsed)
    return parsed.url.split("#", 1)[0].split("?", 1)[0]


def preferred_name(argv: list[str], options: Any) -> str:
    if getattr(options, "save_as", ""):
        return str(options.save_as)
    parsed = parse_args(argv, emit_help=False)
    if not parsed.command or parsed.command == "status":
        extension = "json" if parsed.output == "json" else "summary"
        return f"github.{extension}"
    if parsed.command == "search":
        search_type = {
            "repo": "repos",
            "issue": "issues",
            "pr": "prs",
            "code": "code",
        }.get(parsed.search_type, parsed.search_type)
        scope = ""
        if parsed.repo:
            scope = f"-{_slug(parsed.repo)}"
        elif parsed.global_search:
            scope = "-global"
        filter_parts: list[str] = []
        if parsed.filename:
            filter_parts.append(f"file-{_slug(parsed.filename)}")
        if parsed.extension:
            filter_parts.append(f"ext-{_slug(parsed.extension)}")
        if parsed.language:
            filter_parts.append(f"lang-{_slug(parsed.language)}")
        if parsed.match:
            filter_parts.append(f"match-{_slug(parsed.match)}")
        filter_suffix = ("-" + "-".join(filter_parts)) if filter_parts else ""
        return f"github-search-{search_type}{scope}{filter_suffix}-{_slug(parsed.query)}.json"
    extension = {
        "json": "json",
        "markdown": "md",
        "summary": "summary",
    }.get(parsed.output, "md")
    if parsed.command == "render":
        return _preferred_render_name(parsed, "json")
    return _preferred_render_name(parsed, extension)
