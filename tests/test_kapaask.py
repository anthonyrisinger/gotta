from __future__ import annotations

import io
import json
from pathlib import Path

from gotta.ask import kapa
from gotta.builtin import ASK_BINDING_GROUP, available_surfaces, clear_binding_cache
from gotta.plugins import ask


def test_kapa_default_sre_binding_appears_when_token_is_configured(
    tmp_path: Path, monkeypatch
) -> None:
    config_file = tmp_path / "gotta.toml"
    config_file.write_text(
        (
            "[providers.kapa.env]\n"
            'GOTTA_KAPA_TOKEN_SRE = "demo-token"\n\n'
            "[providers.kapa.bindings.sre]\n"
            'project = "project-sre"\n'
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GOTTA_CONFIG_FILE", str(config_file))

    clear_binding_cache()
    try:
        assert "sre" in available_surfaces(group=ASK_BINDING_GROUP)
    finally:
        clear_binding_cache()


def test_kapa_configured_binding_appears_without_default_token(
    tmp_path: Path, monkeypatch
) -> None:
    config_file = tmp_path / "gotta.toml"
    config_file.write_text(
        (
            "[providers.kapa.bindings.product]\n"
            'project = "project-123"\n'
            "token_index = 2\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GOTTA_CONFIG_FILE", str(config_file))

    clear_binding_cache()
    try:
        assert "product" in available_surfaces(group=ASK_BINDING_GROUP)
    finally:
        clear_binding_cache()


def test_kapa_ask_formats_markdown_response(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config_file = tmp_path / "gotta.toml"
    config_file.write_text(
        (
            "[providers.kapa.env]\n"
            'GOTTA_KAPA_TOKEN_SRE = "demo-token"\n\n'
            "[providers.kapa.bindings.sre]\n"
            'project = "project-sre"\n'
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GOTTA_CONFIG_FILE", str(config_file))
    seen: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps(
                {
                    "answer": "Restart the worker and drain the queue first.",
                    "thread_id": "thread-123",
                    "question_answer_id": "qa-456",
                    "is_uncertain": False,
                    "relevant_sources": [
                        {
                            "title": "Runbook",
                            "source_url": "https://example.com/runbook",
                            "contains_internal_data": True,
                        },
                        {
                            "title": "Alerts",
                            "source_url": "https://example.com/alerts",
                            "contains_internal_data": False,
                        },
                        {
                            "title": "Runbook duplicate",
                            "source_url": "https://example.com/runbook",
                            "contains_internal_data": True,
                        },
                    ],
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        seen["body"] = json.loads(request.data.decode("utf-8"))
        seen["token"] = request.headers.get("X-api-key")
        return FakeResponse()

    monkeypatch.setattr(kapa.urllib.request, "urlopen", fake_urlopen)

    clear_binding_cache()
    try:
        assert ask.main(["sre", "How", "do", "I", "restart", "the", "worker?"]) == 0
    finally:
        clear_binding_cache()

    output = capsys.readouterr().out
    assert seen["url"] == "https://api.kapa.ai/query/v1/projects/project-sre/chat/"
    assert seen["timeout"] == kapa.KAPA_TIMEOUT_SECONDS
    assert seen["body"] == {"query": "How do I restart the worker?"}
    assert seen["token"] == "demo-token"
    assert "# Answer" in output
    assert "Restart the worker and drain the queue first." in output
    assert "- **Binding:** sre" in output
    assert "- **Thread ID:** thread-123" in output
    assert "- [Alerts](https://example.com/alerts)" in output
    assert "- [Runbook](https://example.com/runbook) _(internal)_" in output
    assert "Runbook duplicate" not in output


def test_kapa_ask_reads_query_from_stdin(tmp_path: Path, monkeypatch, capsys) -> None:
    config_file = tmp_path / "gotta.toml"
    config_file.write_text(
        (
            "[providers.kapa.env]\n"
            'GOTTA_KAPA_TOKEN_SRE = "demo-token"\n\n'
            "[providers.kapa.bindings.sre]\n"
            'project = "project-sre"\n'
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GOTTA_CONFIG_FILE", str(config_file))

    class FakeStdin(io.StringIO):
        def isatty(self) -> bool:
            return False

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"answer": "stdin ok"}).encode("utf-8")

    seen: dict[str, object] = {}

    def fake_urlopen(request, timeout=None):
        seen["body"] = json.loads(request.data.decode("utf-8"))
        seen["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(kapa.sys, "stdin", FakeStdin("How do I drain a queue?\n"))
    monkeypatch.setattr(kapa.urllib.request, "urlopen", fake_urlopen)

    clear_binding_cache()
    try:
        assert ask.main(["sre"]) == 0
    finally:
        clear_binding_cache()

    output = capsys.readouterr().out
    assert seen["body"] == {"query": "How do I drain a queue?"}
    assert seen["timeout"] == kapa.KAPA_TIMEOUT_SECONDS
    assert "stdin ok" in output


def test_kapa_ask_can_render_json(tmp_path: Path, monkeypatch, capsys) -> None:
    config_file = tmp_path / "gotta.toml"
    config_file.write_text(
        (
            "[providers.kapa.env]\n"
            'GOTTA_KAPA_TOKEN_SRE = "demo-token"\n\n'
            "[providers.kapa.bindings.sre]\n"
            'project = "project-sre"\n'
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GOTTA_CONFIG_FILE", str(config_file))

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps(
                {
                    "answer": "json ok",
                    "thread_id": "thread-json",
                    "question_answer_id": "qa-json",
                    "is_uncertain": True,
                    "relevant_sources": [],
                }
            ).encode("utf-8")

    monkeypatch.setattr(
        kapa.urllib.request, "urlopen", lambda request, timeout=None: FakeResponse()
    )

    clear_binding_cache()
    try:
        assert ask.main(["sre", "--output", "json", "How do I inspect this?"]) == 0
    finally:
        clear_binding_cache()

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "answer": "json ok",
        "binding": "sre",
        "is_uncertain": True,
        "project_id": "project-sre",
        "question_answer_id": "qa-json",
        "sources": [],
        "thread_id": "thread-json",
    }


def test_kapa_explicit_binding_reports_missing_token_precisely(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config_file = tmp_path / "gotta.toml"
    config_file.write_text(
        (
            "[providers.kapa.bindings.product]\n"
            'project = "project-123"\n'
            "token_index = 2\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GOTTA_CONFIG_FILE", str(config_file))

    clear_binding_cache()
    try:
        assert ask.main(["product", "How do I ship this?"]) == 1
    finally:
        clear_binding_cache()

    error = capsys.readouterr().err
    assert "missing Kapa API token for `product`" in error
    assert "GOTTA_KAPA_TOKEN_2" in error


def test_kapa_binding_can_use_custom_token_env_name(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config_file = tmp_path / "gotta.toml"
    config_file.write_text(
        (
            "[providers.kapa.env]\n"
            'GOTTA_KAPA_TOKEN_IT_ALT = "demo-token"\n\n'
            "[providers.kapa.bindings.it]\n"
            'project = "project-it"\n'
            'token_env = "GOTTA_KAPA_TOKEN_IT_ALT"\n'
            'description = "query the IT Kapa knowledge base"\n'
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GOTTA_CONFIG_FILE", str(config_file))

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"answer": "it ok"}).encode("utf-8")

    seen: dict[str, object] = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["token"] = request.headers.get("X-api-key")
        return FakeResponse()

    monkeypatch.setattr(kapa.urllib.request, "urlopen", fake_urlopen)

    clear_binding_cache()
    try:
        assert "it" in available_surfaces(group=ASK_BINDING_GROUP)
        assert ask.main(["it", "How do I unlock VPN access?"]) == 0
    finally:
        clear_binding_cache()

    output = capsys.readouterr().out
    assert seen["url"] == "https://api.kapa.ai/query/v1/projects/project-it/chat/"
    assert seen["token"] == "demo-token"
    assert "it ok" in output


def test_kapa_binding_can_use_process_env_for_custom_token_name(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config_file = tmp_path / "gotta.toml"
    config_file.write_text(
        (
            "[providers.kapa.bindings.it]\n"
            'project = "project-it-env"\n'
            'token_env = "GOTTA_KAPA_TOKEN_IT"\n'
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GOTTA_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("GOTTA_KAPA_TOKEN_IT", "env-demo-token")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"answer": "env ok"}).encode("utf-8")

    seen: dict[str, object] = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["token"] = request.headers.get("X-api-key")
        return FakeResponse()

    monkeypatch.setattr(kapa.urllib.request, "urlopen", fake_urlopen)

    clear_binding_cache()
    try:
        assert ask.main(["it", "How do I unlock VPN access?"]) == 0
    finally:
        clear_binding_cache()

    output = capsys.readouterr().out
    assert seen["url"] == "https://api.kapa.ai/query/v1/projects/project-it-env/chat/"
    assert seen["token"] == "env-demo-token"
    assert "env ok" in output


def test_kapa_binding_defaults_token_name_from_binding_name(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config_file = tmp_path / "gotta.toml"
    config_file.write_text(
        ('[providers.kapa.bindings.it]\nproject = "project-it-default"\n'),
        encoding="utf-8",
    )
    monkeypatch.setenv("GOTTA_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("GOTTA_KAPA_TOKEN_IT", "default-demo-token")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"answer": "default env ok"}).encode("utf-8")

    seen: dict[str, object] = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["token"] = request.headers.get("X-api-key")
        return FakeResponse()

    monkeypatch.setattr(kapa.urllib.request, "urlopen", fake_urlopen)

    clear_binding_cache()
    try:
        assert ask.main(["it", "How do I unlock VPN access?"]) == 0
    finally:
        clear_binding_cache()

    output = capsys.readouterr().out
    assert (
        seen["url"] == "https://api.kapa.ai/query/v1/projects/project-it-default/chat/"
    )
    assert seen["token"] == "default-demo-token"
    assert "default env ok" in output


def test_kapa_binding_rejects_non_gotta_token_env_name(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config_file = tmp_path / "gotta.toml"
    config_file.write_text(
        (
            "[providers.kapa.bindings.it]\n"
            'project = "project-it-env"\n'
            'token_env = "IT_KAPA_TOKEN"\n'
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GOTTA_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("IT_KAPA_TOKEN", "bad-token")

    clear_binding_cache()
    try:
        assert ask.main(["it", "How do I unlock VPN access?"]) == 1
    finally:
        clear_binding_cache()

    error = capsys.readouterr().err
    assert "invalid token_env for `it`" in error
    assert "GOTTA_*" in error
