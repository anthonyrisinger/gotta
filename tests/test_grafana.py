from __future__ import annotations

import json
from pathlib import Path

from gotta.plugins import grafana


def test_grafana_status_reports_missing_config(tmp_path: Path, monkeypatch) -> None:
    config_file = tmp_path / "gotta.toml"
    config_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("GOTTA_CONFIG_FILE", str(config_file))
    monkeypatch.delenv(grafana.GRAFANA_BASE_URL_ENV, raising=False)
    monkeypatch.delenv(grafana.GRAFANA_TOKEN_ENV, raising=False)

    payload = grafana._status_payload()

    assert payload["authStatus"] == "missing"
    assert payload["searchStatus"] == "unknown"
    assert grafana.GRAFANA_BASE_URL_ENV in payload["nextStep"]


def test_grafana_auth_persists_canonical_env_and_drops_legacy_keys(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config_file = tmp_path / "gotta.toml"
    config_file.write_text(
        "\n".join(
            [
                "[providers.grafana.env]",
                'GOTTA_GRAFANA_SERVICE_ACCOUNT_TOKEN_ID = "sa-legacy"',
                'GOTTA_GRAFANA_SERVICE_ACCOUNT_TOKEN_SECRET = "glsa_legacy"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GOTTA_CONFIG_FILE", str(config_file))

    assert (
        grafana.main(
            [
                "auth",
                "--base-url",
                "https://grafana.example.com",
                "--service-account-token",
                "glsa_secret",
                "--org-id",
                "7",
                "--output",
                "json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    rendered = config_file.read_text(encoding="utf-8")
    assert payload["baseUrl"] == "https://grafana.example.com"
    assert payload["orgId"] == "7"
    assert grafana.GRAFANA_BASE_URL_ENV in rendered
    assert grafana.GRAFANA_TOKEN_ENV in rendered
    assert grafana.GRAFANA_ORG_ID_ENV in rendered
    assert grafana.LEGACY_GRAFANA_TOKEN_ID_ENV not in rendered
    assert grafana.LEGACY_GRAFANA_TOKEN_SECRET_ENV not in rendered


def test_grafana_search_renders_markdown(monkeypatch, capsys) -> None:
    monkeypatch.setenv(grafana.GRAFANA_BASE_URL_ENV, "https://grafana.example.com")
    monkeypatch.setenv(grafana.GRAFANA_TOKEN_ENV, "glsa_secret")

    def fake_json(session, path, *, params=None):
        assert session.base_url == "https://grafana.example.com"
        assert path == "/api/search"
        assert ("query", "production") in (params or [])
        return [
            {
                "uid": "cIBgcSjkk",
                "title": "Production Overview",
                "type": "dash-db",
                "url": "/d/cIBgcSjkk/production-overview",
                "folderTitle": "Folder",
                "folderUid": "000000163",
                "tags": ["prod"],
                "isStarred": True,
            }
        ]

    monkeypatch.setattr(grafana, "_grafana_json", fake_json)

    assert grafana.main(["search", "production"]) == 0

    output = capsys.readouterr().out
    assert "# Grafana Search: production" in output
    assert "Production Overview" in output
    assert "https://grafana.example.com/d/cIBgcSjkk/production-overview" in output


def test_grafana_get_renders_markdown(monkeypatch, capsys) -> None:
    monkeypatch.setenv(grafana.GRAFANA_BASE_URL_ENV, "https://grafana.example.com")
    monkeypatch.setenv(grafana.GRAFANA_TOKEN_ENV, "glsa_secret")

    def fake_json(session, path, *, params=None):
        assert session.base_url == "https://grafana.example.com"
        assert path == "/api/dashboards/uid/cIBgcSjkk"
        return {
            "dashboard": {
                "uid": "cIBgcSjkk",
                "title": "Production Overview",
                "tags": ["prod"],
                "version": 7,
            },
            "meta": {
                "url": "/d/cIBgcSjkk/production-overview",
                "folderUid": "000000163",
                "folderTitle": "Folder",
                "canEdit": False,
            },
        }

    monkeypatch.setattr(grafana, "_grafana_json", fake_json)

    assert grafana.main(["get", "cIBgcSjkk"]) == 0

    output = capsys.readouterr().out
    assert "# Production Overview" in output
    assert "`cIBgcSjkk`" in output
    assert "https://grafana.example.com/d/cIBgcSjkk/production-overview" in output


def test_grafana_route_and_locator_contract() -> None:
    assert grafana.route_target("grafana:status") == ["status"]
    assert grafana.route_target("grafana:search production") == ["search", "production"]
    assert grafana.route_target("grafana:cIBgcSjkk") == ["get", "cIBgcSjkk"]
    assert grafana.route_target("https://grafana.example.com/d/cIBgcSjkk/production-overview") == [
        "https://grafana.example.com/d/cIBgcSjkk/production-overview"
    ]
    assert (
        grafana.canonical_locator(["search", "--type", "dash-db", "production"])
        == "grafana:search --type dash-db production"
    )
    assert grafana.canonical_locator(["get", "cIBgcSjkk"]) == "grafana:cIBgcSjkk"
    assert (
        grafana.preferred_name(["search", "production"], None)
        == "grafana-search-production.md"
    )
    assert grafana.preferred_name(["get", "cIBgcSjkk"], None) == "cIBgcSjkk.md"
