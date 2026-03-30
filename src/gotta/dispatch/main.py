"""Dispatch orchestration for installed gotta surface bindings."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
import sys

from gotta.builtin import (
    SurfaceBinding,
    available_bindings as discovered_binding_names,
    get_binding,
)
from gotta.content.model import (
    CommonOptions,
    ContentError,
    Materialization,
    ResolvedDirs,
)
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
    ResolvedInvocation,
    canonical_locator,
    infer_content_type,
    invocation_locator,
    preferred_name as derive_preferred_name,
    resolve_invocation,
    should_materialize,
)

__all__ = [
    "available_surfaces",
    "surface_binding",
    "print_usage",
    "load_surface_runner",
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
    "run_surface",
    "_materialize_invocation",
]


def die(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    return code


def available_surfaces() -> list[str]:
    return discovered_binding_names()


def surface_binding(surface: str) -> SurfaceBinding | None:
    return get_binding(surface)


def print_usage() -> int:
    print("usage: gotta <surface> [args...]", file=sys.stderr)
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
    print("available top-level surfaces:", file=sys.stderr)
    for surface in available_surfaces():
        binding = surface_binding(surface)
        description = binding.description if binding else ""
        if description:
            print(f"  - {surface:<10} {description}", file=sys.stderr)
            continue
        print(f"  - {surface}", file=sys.stderr)
    return 0


def load_surface_runner(surface: str) -> Callable[[list[str]], int]:
    binding = surface_binding(surface)
    if binding is None:
        raise KeyError(surface)
    return binding.runner


@dataclass(slots=True)
class _SurfaceRunState:
    surface: str
    quiet: bool
    binding: SurfaceBinding | None
    options: CommonOptions
    cleaned: list[str]
    runner: Callable[[list[str]], int]
    resolved: ResolvedInvocation
    access: str
    rerun_command: str
    budget_output: bool
    runtime_dirs: ResolvedDirs | None = None

    @classmethod
    def prepare(cls, surface: str, argv: list[str]) -> _SurfaceRunState:
        quiet, argv = strip_quiet_flag(argv)
        full_output, argv = strip_full_output_flag(argv)
        binding = surface_binding(surface)
        if surface == "session":
            options = CommonOptions()
            cleaned = argv
        else:
            options, cleaned = split_common_options(
                argv,
                strip_actor=bool(binding and binding.shared_actor_option),
            )
        return cls(
            surface=surface,
            quiet=quiet,
            binding=binding,
            options=options,
            cleaned=cleaned,
            runner=load_surface_runner(surface),
            resolved=resolve_invocation(surface, cleaned, options),
            access=session_access_mode(surface, cleaned),
            rerun_command=_rerun_full_output_command(surface, cleaned),
            budget_output=not full_output,
        )

    def execute(self) -> int:
        if not self.resolved.should_materialize and _streams_live(
            self.surface, self.cleaned
        ):
            return self._execute_live()
        if not self.resolved.should_materialize:
            return self._execute_nonmaterialized()
        self._require_runtime_dirs()
        capture_hook_result = self._execute_materialized_capture()
        if capture_hook_result is not None:
            return capture_hook_result
        return self._execute_materialized_stdout()

    def _emit_success(
        self,
        stdout_data: bytes,
        *,
        stderr_data: bytes = b"",
        result: Materialization | None = None,
    ) -> int:
        emitted = emit_budgeted_output(
            stdout_data,
            output_format=requested_output_format(
                self.surface, self.cleaned, stdout_data
            ),
            budget_output=self.budget_output,
            follow_command=self.rerun_command,
        )
        if stderr_data and not self.quiet:
            _emit_captured_stderr(stderr_data)
        if _should_emit_receipt(self.surface, self.cleaned):
            _emit_receipt(
                _receipt_payload(
                    emitted=emitted,
                    result=result,
                    extra=_receipt_extra(
                        self.surface,
                        self.cleaned,
                        dirs=self.runtime_dirs,
                    ),
                ),
                quiet=self.quiet,
            )
        return 0

    def _replay_stdout(self, stdout_data: bytes) -> None:
        if not stdout_data:
            return
        emit_budgeted_output(
            stdout_data,
            output_format=requested_output_format(
                self.surface, self.cleaned, stdout_data
            ),
            budget_output=self.budget_output,
            follow_command=self.rerun_command,
        )

    def _execute_live(self) -> int:
        with self._optional_runtime_scope():
            return _run_callable(self.runner, self.cleaned)

    def _execute_nonmaterialized(self) -> int:
        with self._optional_runtime_scope():
            with (
                capture_stdout(preserve_tty=True) as stdout_capture,
                capture_stderr(preserve_tty=True) as stderr_capture,
            ):
                code = _run_callable(self.runner, self.cleaned)
        stderr_data = stderr_capture.getvalue()
        stdout_data = stdout_capture.getvalue()
        if code == 0:
            if not self.quiet:
                stderr_data += _sessionless_notice_bytes(self.resolved)
            return self._emit_success(
                stdout_data,
                stderr_data=stderr_data,
            )
        self._replay_stdout(stdout_data)
        _emit_captured_stderr(stderr_data)
        return code

    def _execute_materialized_capture(self) -> int | None:
        if not self._uses_capture_projection():
            return None
        stderr_data = b""
        dirs = self._require_runtime_dirs()
        try:
            with scoped_runtime_env(dirs):
                with capture_stderr(preserve_tty=True) as stderr_capture:
                    capture, projection = _captured_execution(
                        self.surface,
                        self.cleaned,
                        self.options,
                    )
                    stderr_data = stderr_capture.getvalue()
        except NotImplementedError:
            return None
        if capture is None or projection is None:
            return None
        try:
            result = _materialize_invocation(
                self.resolved,
                capture.data,
                options=self.options,
                capture=capture,
                dirs=dirs,
            )
        except ContentError as exc:
            raise RuntimeError(str(exc)) from exc
        self._emit_success(
            projection.data,
            stderr_data=stderr_data,
            result=result,
        )
        return int(capture.exit_status)

    def _execute_materialized_stdout(self) -> int:
        dirs = self._require_runtime_dirs()
        with scoped_runtime_env(dirs):
            with (
                capture_stdout(preserve_tty=True) as stdout_capture,
                capture_stderr(preserve_tty=True) as stderr_capture,
            ):
                code = _run_callable(self.runner, self.cleaned)
        stdout_data = stdout_capture.getvalue()
        stderr_data = stderr_capture.getvalue()
        if code == 0:
            try:
                result = _materialize_invocation(
                    self.resolved,
                    stdout_data,
                    options=self.options,
                    dirs=dirs,
                )
            except ContentError as exc:
                raise RuntimeError(str(exc)) from exc
            return self._emit_success(
                stdout_data,
                stderr_data=stderr_data,
                result=result,
            )
        self._replay_stdout(stdout_data)
        _emit_captured_stderr(stderr_data)
        return code

    def _uses_capture_projection(self) -> bool:
        return bool(
            self.binding
            and self.binding.capture is not None
            and self.binding.project is not None
            and self.resolved.artifact_intent in {"evidence", "discovery"}
        )

    def _needs_optional_runtime_scope(self) -> bool:
        return self.access != "none" and bool(
            self.options.session_dir or self.options.content_dir or self.options.actor
        )

    def _optional_runtime_scope(self):
        if not self._needs_optional_runtime_scope():
            return nullcontext()
        return scoped_runtime_env(self._require_runtime_dirs())

    def _require_runtime_dirs(self) -> ResolvedDirs:
        if self.runtime_dirs is None:
            self.runtime_dirs = _runtime_dirs(self.options, access=self.access)
            require_operational_session(self.runtime_dirs)
        return self.runtime_dirs


def run_surface(surface: str, argv: list[str]) -> int:
    try:
        state = _SurfaceRunState.prepare(surface, argv)
    except KeyError:
        surfaces = ", ".join(available_surfaces())
        return die(f"unknown gotta surface: {surface}. available surfaces: {surfaces}")
    except SystemExit as exc:
        return system_exit_status(exc)
    except ContentError as exc:
        return die(str(exc))
    except RuntimeError as exc:
        return die(str(exc), code=1)
    try:
        return state.execute()
    except SystemExit as exc:
        return system_exit_status(exc)
    except ContentError as exc:
        return die(str(exc))
    except RuntimeError as exc:
        return die(str(exc), code=1)
