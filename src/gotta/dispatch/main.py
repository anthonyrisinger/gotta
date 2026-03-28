"""Dispatch orchestration for packaged gotta commands."""

from __future__ import annotations

from collections.abc import Callable
import sys

from gotta.builtin import (
    PluginSpec,
    available_plugins as discovered_plugin_names,
    get_plugin,
)
from gotta.content import CommonOptions, ContentError, ResolvedDirs
from gotta.dispatch.budget import (
    emit_budgeted_output,
    requested_output_format,
)
from gotta.dispatch.materialize import _materialize_invocation
from gotta.dispatch.option import (
    split_common_options,
    strip_full_output_flag,
    strip_quiet_flag,
)
from gotta.dispatch.receipt import (
    _emit_receipt,
    _receipt_extra,
    _receipt_payload,
    _rerun_full_output_command,
    _result_follow_command,
    _should_emit_receipt,
)
from gotta.dispatch.runtime import (
    _captured_execution,
    _run_callable,
    _runtime_dirs,
    _sessionless_notice_bytes,
    _streams_live,
    require_operational_session,
    scoped_runtime_env,
    system_exit_status,
)
from gotta.dispatch.stream import capture_stderr, capture_stdout, _emit_captured_stderr
from gotta.resolve.intent import session_access_mode
from gotta.resolve.invoke import (
    canonical_locator,
    infer_content_type,
    invocation_locator,
    preferred_name as derive_preferred_name,
    resolve_invocation,
    should_materialize,
)

__all__ = [
    "available_plugins",
    "plugin_spec",
    "print_usage",
    "load_plugin_runner",
    "should_materialize",
    "invocation_locator",
    "canonical_locator",
    "derive_preferred_name",
    "infer_content_type",
    "session_access_mode",
    "resolve_invocation",
    "split_common_options",
    "capture_stdout",
    "capture_stderr",
    "emit_budgeted_output",
    "require_operational_session",
    "run_plugin",
    "_materialize_invocation",
]


def die(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    return code


def available_plugins() -> list[str]:
    return discovered_plugin_names()


def plugin_spec(plugin: str) -> PluginSpec | None:
    return get_plugin(plugin)


def print_usage() -> int:
    print("usage: gotta <plugin> [args...]", file=sys.stderr)
    print("", file=sys.stderr)
    print("canonical session-binding path: `gotta ...`", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "builtin non-session surfaces: `gotta version`, `gotta --version`, `gotta search`",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    print(
        "session investigative surfaces live under `gotta session`: "
        "`manifest`, `timeline`, `graph`, `leads`, `analyze`, `scan`",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    print("global flags: `--quiet`, `--full-output`", file=sys.stderr)
    print("", file=sys.stderr)
    print("use `gotta --help-all` for recursive command help", file=sys.stderr)
    print("", file=sys.stderr)
    print("available plugins:", file=sys.stderr)
    for plugin in available_plugins():
        spec = plugin_spec(plugin)
        description = spec.description if spec else ""
        if description:
            print(f"  - {plugin:<10} {description}", file=sys.stderr)
            continue
        print(f"  - {plugin}", file=sys.stderr)
    return 0


def load_plugin_runner(plugin: str) -> Callable[[list[str]], int]:
    spec = plugin_spec(plugin)
    if spec is None:
        raise KeyError(plugin)
    return spec.runner


def run_plugin(plugin: str, argv: list[str]) -> int:
    quiet, argv = strip_quiet_flag(argv)
    full_output, argv = strip_full_output_flag(argv)
    if plugin == "session":
        options = CommonOptions()
        cleaned = argv
    else:
        try:
            options, cleaned = split_common_options(
                argv,
                strip_actor=plugin
                in {
                    "read",
                    "confluence",
                    "gdocs",
                    "gdrive",
                    "github",
                    "grafana",
                    "granola",
                    "gsheets",
                    "jira",
                    "slack",
                },
            )
        except ContentError as exc:
            return die(str(exc))

    try:
        runner = load_plugin_runner(plugin)
    except KeyError:
        plugins = ", ".join(available_plugins())
        return die(f"unknown gotta plugin: {plugin}. available plugins: {plugins}")
    except RuntimeError as exc:
        return die(str(exc), code=1)

    try:
        resolved = resolve_invocation(plugin, cleaned, options)
    except SystemExit as exc:
        return system_exit_status(exc)
    except ContentError as exc:
        return die(str(exc))
    except RuntimeError as exc:
        return die(str(exc), code=1)
    access = session_access_mode(plugin, cleaned)
    spec = plugin_spec(plugin)
    follow_command = ""
    rerun_command = _rerun_full_output_command(plugin, cleaned)
    runtime_dirs: ResolvedDirs | None = None
    budget_output = not full_output

    def emit_success(
        stdout_data: bytes,
        *,
        stderr_data: bytes = b"",
        result: object | None = None,
        dirs: ResolvedDirs | None = None,
    ) -> int:
        nonlocal follow_command
        follow_command = _result_follow_command(result)
        emitted = emit_budgeted_output(
            stdout_data,
            output_format=requested_output_format(plugin, cleaned, stdout_data),
            budget_output=budget_output,
            follow_command=rerun_command,
        )
        if stderr_data and not quiet:
            _emit_captured_stderr(stderr_data)
        if _should_emit_receipt(plugin, cleaned):
            _emit_receipt(
                _receipt_payload(
                    emitted=emitted,
                    result=result,
                    extra=_receipt_extra(plugin, cleaned, dirs=dirs),
                ),
                quiet=quiet,
            )
        return 0

    def replay_stdout(stdout_data: bytes) -> None:
        if not stdout_data:
            return
        emit_budgeted_output(
            stdout_data,
            output_format=requested_output_format(plugin, cleaned, stdout_data),
            budget_output=budget_output,
            follow_command=rerun_command,
        )

    if not resolved.should_materialize and _streams_live(plugin, cleaned):
        if access != "none" and (
            options.session_dir or options.content_dir or options.actor
        ):
            try:
                runtime_dirs = _runtime_dirs(options, access=access)
                require_operational_session(runtime_dirs)
            except ContentError as exc:
                return die(str(exc))
            with scoped_runtime_env(runtime_dirs):
                return _run_callable(runner, cleaned)
        return _run_callable(runner, cleaned)

    if not resolved.should_materialize:
        if access != "none" and (
            options.session_dir or options.content_dir or options.actor
        ):
            try:
                runtime_dirs = _runtime_dirs(options, access=access)
                require_operational_session(runtime_dirs)
            except ContentError as exc:
                return die(str(exc))
            with scoped_runtime_env(runtime_dirs):
                with (
                    capture_stdout(preserve_tty=True) as stdout_capture,
                    capture_stderr(preserve_tty=True) as stderr_capture,
                ):
                    code = _run_callable(runner, cleaned)
        else:
            with (
                capture_stdout(preserve_tty=True) as stdout_capture,
                capture_stderr(preserve_tty=True) as stderr_capture,
            ):
                code = _run_callable(runner, cleaned)
        stderr_data = stderr_capture.getvalue()
        stdout_data = stdout_capture.getvalue()
        if code == 0:
            if not quiet:
                stderr_data += _sessionless_notice_bytes(resolved)
            return emit_success(stdout_data, stderr_data=stderr_data, dirs=runtime_dirs)
        replay_stdout(stdout_data)
        _emit_captured_stderr(stderr_data)
        return code

    try:
        runtime_dirs = _runtime_dirs(options, access=access)
        require_operational_session(runtime_dirs)
    except ContentError as exc:
        return die(str(exc))

    if (
        spec
        and spec.capture is not None
        and spec.project is not None
        and resolved.artifact_intent in {"evidence", "discovery"}
    ):
        try:
            with scoped_runtime_env(runtime_dirs):
                with capture_stderr(preserve_tty=True) as stderr_capture:
                    capture, display = _captured_execution(plugin, cleaned, options)
        except NotImplementedError:
            capture = None
            display = None
        except SystemExit as exc:
            return system_exit_status(exc)
        except (ContentError, RuntimeError) as exc:
            return die(str(exc), code=1)
        if capture is not None and display is not None:
            try:
                result = _materialize_invocation(
                    resolved,
                    capture.data,
                    options=options,
                    capture=capture,
                    dirs=runtime_dirs,
                )
            except ContentError as exc:
                return die(str(exc), code=1)
            return emit_success(
                display,
                stderr_data=stderr_capture.getvalue(),
                result=result,
                dirs=runtime_dirs,
            )

    with scoped_runtime_env(runtime_dirs):
        with (
            capture_stdout(preserve_tty=True) as stdout_capture,
            capture_stderr(preserve_tty=True) as stderr_capture,
        ):
            code = _run_callable(runner, cleaned)
    data = stdout_capture.getvalue()
    stderr_data = stderr_capture.getvalue()
    if code == 0:
        try:
            result = _materialize_invocation(
                resolved, data, options=options, dirs=runtime_dirs
            )
        except ContentError as exc:
            return die(str(exc), code=1)
        return emit_success(
            data, stderr_data=stderr_data, result=result, dirs=runtime_dirs
        )
    replay_stdout(data)
    _emit_captured_stderr(stderr_data)
    return code
