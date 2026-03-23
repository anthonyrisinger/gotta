from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def test_grafana_auth_persists_canonical_env(tmp_path: Path, monkeypatch, capsys) -> None:
    config_file = tmp_path / "gotta.toml"
    config_file.write_text("[providers.grafana.env]\n", encoding="utf-8")
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


def test_grafana_search_renders_markdown(monkeypatch, capsys) -> None:
    monkeypatch.setenv(grafana.GRAFANA_BASE_URL_ENV, "https://grafana.example.com")
    monkeypatch.setenv(grafana.GRAFANA_TOKEN_ENV, "glsa_secret")

    def fake_json(session, path, *, params=None):
        assert session.base_url == "https://grafana.example.com"
        assert path == "/api/search"
        assert ("query", "production") in (params or [])
        return [
            {
                "uid": "demo-dashboard-uid",
                "title": "Production Overview",
                "type": "dash-db",
                "url": "/d/demo-dashboard-uid/production-overview",
                "folderTitle": "Folder",
                "folderUid": "000000163",
                "tags": ["prod"],
                "isStarred": True,
            }
        ]

    monkeypatch.setattr(grafana, "_grafana_json", fake_json)

    assert grafana.main(["search", "production"]) == 0

    output = capsys.readouterr().out
    assert "# Grafana Search" in output
    assert "- Query: `production`" in output
    assert "Production Overview" in output
    assert "https://grafana.example.com/d/demo-dashboard-uid/production-overview" in output


def test_grafana_datasources_renders_summary(monkeypatch, capsys) -> None:
    monkeypatch.setenv(grafana.GRAFANA_BASE_URL_ENV, "https://grafana.example.com")
    monkeypatch.setenv(grafana.GRAFANA_TOKEN_ENV, "glsa_secret")

    def fake_json(session, path, *, method="GET", params=None, payload=None):
        assert session.base_url == "https://grafana.example.com"
        assert path == "/api/datasources"
        assert method == "GET"
        return [
            {
                "uid": "prom-main",
                "name": "Main Prometheus",
                "type": "prometheus",
                "url": "https://prom.example.com",
                "access": "proxy",
                "isDefault": True,
                "readOnly": False,
            }
        ]

    monkeypatch.setattr(grafana, "_grafana_json", fake_json)

    assert grafana.main(["datasources"]) == 0

    output = capsys.readouterr().out
    assert "total\t1" in output
    assert "shown\t1" in output
    assert "prom-main" in output
    assert "Main Prometheus" in output


def test_grafana_datasources_supports_limit_and_offset(monkeypatch, capsys) -> None:
    monkeypatch.setenv(grafana.GRAFANA_BASE_URL_ENV, "https://grafana.example.com")
    monkeypatch.setenv(grafana.GRAFANA_TOKEN_ENV, "glsa_secret")

    def fake_json(session, path, *, method="GET", params=None, payload=None):
        assert session.base_url == "https://grafana.example.com"
        assert path == "/api/datasources"
        assert method == "GET"
        return [
            {"uid": "ds-1", "name": "Datasource One", "type": "prometheus", "url": "", "access": "proxy"},
            {"uid": "ds-2", "name": "Datasource Two", "type": "prometheus", "url": "", "access": "proxy"},
            {"uid": "ds-3", "name": "Datasource Three", "type": "loki", "url": "", "access": "proxy"},
        ]

    monkeypatch.setattr(grafana, "_grafana_json", fake_json)

    assert grafana.main(["datasources", "--limit", "1", "--offset", "1", "--output", "json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["totalCount"] == 3
    assert payload["shownCount"] == 1
    assert payload["offset"] == 1
    assert payload["nextOffset"] == 2
    assert payload["truncated"] is True
    assert [item["uid"] for item in payload["datasources"]] == ["ds-2"]


def test_grafana_search_can_list_dashboards(monkeypatch, capsys) -> None:
    monkeypatch.setenv(grafana.GRAFANA_BASE_URL_ENV, "https://grafana.example.com")
    monkeypatch.setenv(grafana.GRAFANA_TOKEN_ENV, "glsa_secret")

    def fake_json(session, path, *, method="GET", params=None, payload=None):
        assert session.base_url == "https://grafana.example.com"
        assert path == "/api/search"
        assert method == "GET"
        assert ("type", "dash-db") in (params or [])
        assert ("query", "prod") in (params or [])
        return [
            {
                "uid": "demo-dashboard-uid",
                "title": "Production Overview",
                "type": "dash-db",
                "url": "/d/demo-dashboard-uid/production-overview",
                "folderTitle": "Folder",
                "folderUid": "000000163",
                "tags": ["prod"],
                "isStarred": True,
            }
        ]

    monkeypatch.setattr(grafana, "_grafana_json", fake_json)

    assert grafana.main(["search", "--type", "dash-db", "prod", "--output", "summary"]) == 0

    output = capsys.readouterr().out
    assert "surface\tsearch" in output
    assert "demo-dashboard-uid" in output
    assert "Production Overview" in output


def test_grafana_query_can_infer_datasource_from_dashboard(monkeypatch, capsys) -> None:
    monkeypatch.setenv(grafana.GRAFANA_BASE_URL_ENV, "https://grafana.example.com")
    monkeypatch.setenv(grafana.GRAFANA_TOKEN_ENV, "glsa_secret")

    def fake_json(session, path, *, method="GET", params=None, payload=None):
        if path == "/api/dashboards/uid/demo-dashboard-uid":
            return {
                "dashboard": {
                    "uid": "demo-dashboard-uid",
                    "title": "Production Overview",
                    "panels": [
                        {
                            "title": "Agents Up",
                            "datasource": {
                                "type": "prometheus",
                                "uid": "prom-main",
                            },
                            "targets": [
                                {
                                    "expr": "sum(agent_up)",
                                    "datasource": {
                                        "type": "prometheus",
                                        "uid": "prom-main",
                                    },
                                }
                            ],
                        }
                    ],
                },
                "meta": {
                    "url": "/d/demo-dashboard-uid/production-overview",
                },
            }
        if path == "/api/datasources/uid/prom-main":
            return {
                "uid": "prom-main",
                "name": "Main Prometheus",
                "type": "prometheus",
                "url": "https://prom.example.com",
                "access": "proxy",
                "isDefault": False,
                "readOnly": False,
            }
        if path == "/api/ds/query":
            assert method == "POST"
            assert payload is not None
            assert payload["queries"][0]["datasource"]["uid"] == "prom-main"
            assert payload["queries"][0]["expr"] == "sum(agent_up)"
            return {
                "results": {
                    "A": {
                        "status": 200,
                        "frames": [
                            {
                                "schema": {
                                    "fields": [
                                        {"name": "Time"},
                                        {
                                            "name": "Value",
                                            "labels": {"cluster": "prod"},
                                        },
                                    ]
                                },
                                "data": {"values": [[1700000000000], [9]]},
                            }
                        ],
                    }
                }
            }
        raise AssertionError(path)

    monkeypatch.setattr(grafana, "_grafana_json", fake_json)

    assert (
        grafana.main(
            [
                "query",
                "--dashboard",
                "demo-dashboard-uid",
                "--output",
                "json",
                "sum(agent_up)",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["datasource"]["uid"] == "prom-main"
    assert payload["dashboard"]["uid"] == "demo-dashboard-uid"
    assert payload["series"][0]["labels"] == {"cluster": "prod"}
    assert payload["series"][0]["points"][0]["value"] == 9


def test_grafana_query_inherits_dashboard_url_context(monkeypatch, capsys) -> None:
    monkeypatch.setenv(grafana.GRAFANA_BASE_URL_ENV, "https://grafana.example.com")
    monkeypatch.setenv(grafana.GRAFANA_TOKEN_ENV, "glsa_secret")

    dashboard_url = (
        "https://grafana.example.com/d/demo-dashboard-uid/production-overview"
        "?orgId=7&from=now-6h&to=now"
    )

    def fake_json(session, path, *, method="GET", params=None, payload=None):
        if path == "/api/dashboards/uid/demo-dashboard-uid":
            assert session.org_id == "7"
            return {
                "dashboard": {
                    "uid": "demo-dashboard-uid",
                    "title": "Production Overview",
                    "panels": [
                        {
                            "datasource": {"type": "prometheus", "uid": "prom-main"},
                            "targets": [
                                {
                                    "expr": "sum(agent_up)",
                                    "datasource": {"type": "prometheus", "uid": "prom-main"},
                                }
                            ],
                        }
                    ],
                },
                "meta": {"url": "/d/demo-dashboard-uid/production-overview"},
            }
        if path == "/api/datasources/uid/prom-main":
            assert session.org_id == "7"
            return {
                "uid": "prom-main",
                "name": "Main Prometheus",
                "type": "prometheus",
                "url": "https://prom.example.com",
                "access": "proxy",
                "isDefault": False,
                "readOnly": False,
            }
        if path == "/api/ds/query":
            assert method == "POST"
            assert payload is not None
            assert payload["from"] == "now-6h"
            assert payload["to"] == "now"
            assert payload["queries"][0]["expr"] == "sum(agent_up)"
            return {
                "results": {
                    "A": {
                        "status": 200,
                        "frames": [
                            {
                                "schema": {
                                    "fields": [
                                        {"name": "Time"},
                                        {"name": "Value"},
                                    ]
                                },
                                "data": {"values": [[1700000000000], [9]]},
                            }
                        ],
                    }
                }
            }
        raise AssertionError(path)

    monkeypatch.setattr(grafana, "_grafana_json", fake_json)

    assert (
        grafana.main(
            [
                "query",
                "--dashboard",
                dashboard_url,
                "--output",
                "json",
                "sum(agent_up)",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["orgId"] == "7"
    assert payload["from"] == "now-6h"
    assert payload["to"] == "now"
    assert payload["dashboard"]["uid"] == "demo-dashboard-uid"


def test_grafana_headers_use_non_default_user_agent(monkeypatch) -> None:
    monkeypatch.setenv(grafana.GRAFANA_BASE_URL_ENV, "https://grafana.example.com")
    monkeypatch.setenv(grafana.GRAFANA_TOKEN_ENV, "glsa_secret")

    session = grafana._load_session()

    headers = grafana._headers(session)

    assert headers["Authorization"] == "Bearer glsa_secret"
    assert headers["User-Agent"] == grafana.DEFAULT_USER_AGENT


def test_grafana_get_renders_markdown(monkeypatch, capsys) -> None:
    monkeypatch.setenv(grafana.GRAFANA_BASE_URL_ENV, "https://grafana.example.com")
    monkeypatch.setenv(grafana.GRAFANA_TOKEN_ENV, "glsa_secret")

    def fake_json(session, path, *, params=None):
        assert session.base_url == "https://grafana.example.com"
        assert path == "/api/dashboards/uid/demo-dashboard-uid"
        return {
            "dashboard": {
                "uid": "demo-dashboard-uid",
                "title": "Production Overview",
                "tags": ["prod"],
                "version": 7,
            },
            "meta": {
                "url": "/d/demo-dashboard-uid/production-overview",
                "folderUid": "000000163",
                "folderTitle": "Folder",
                "canEdit": False,
            },
        }

    monkeypatch.setattr(grafana, "_grafana_json", fake_json)

    assert grafana.main(["get", "demo-dashboard-uid"]) == 0

    output = capsys.readouterr().out
    assert "# Production Overview" in output
    assert "`demo-dashboard-uid`" in output
    assert "https://grafana.example.com/d/demo-dashboard-uid/production-overview" in output


def test_grafana_requires_explicit_get_for_dashboard_ref(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        grafana.main(["demo-dashboard-uid"])
    assert int(exc.value.code or 0) == 2

    err = capsys.readouterr().err
    assert "invalid choice" in err


def test_grafana_route_and_locator_contract() -> None:
    assert grafana.route_target("grafana:status") == ["status"]
    assert grafana.route_target("grafana:datasources") == ["datasources"]
    assert grafana.route_target("grafana:search") == ["search"]
    assert (
        grafana.route_target("grafana:search --type dash-db")
        == ["search", "--type", "dash-db"]
    )
    assert grafana.route_target("grafana:search production") == ["search", "production"]
    assert (
        grafana.route_target("grafana:query --dashboard demo-dashboard-uid sum(agent_up)")
        == ["query", "--dashboard", "demo-dashboard-uid", "sum(agent_up)"]
    )
    assert grafana.route_target("https://grafana.example.com/d/demo-dashboard-uid/production-overview") == [
        "get",
        "https://grafana.example.com/d/demo-dashboard-uid/production-overview",
    ]
    assert (
        grafana.canonical_locator(["search", "--type", "dash-db"])
        == "grafana:search --type dash-db"
    )
    assert (
        grafana.canonical_locator(["search", "--type", "dash-db", "production"])
        == "grafana:search --type dash-db production"
    )
    assert grafana.canonical_locator(["get", "demo-dashboard-uid"]) == "grafana:get demo-dashboard-uid"
    assert (
        grafana.canonical_locator(
            ["query", "--dashboard", "demo-dashboard-uid", "sum(agent_up)"]
        )
        == "grafana:query --dashboard demo-dashboard-uid sum(agent_up)"
    )
    assert (
        grafana.canonical_locator(
            [
                "query",
                "--dashboard",
                "https://grafana.example.com/d/demo-dashboard-uid/production-overview"
                "?orgId=7&from=now-6h&to=now",
                "sum(agent_up)",
            ]
        )
        == "grafana:query --dashboard demo-dashboard-uid --org-id 7 --from now-6h sum(agent_up)"
    )
    assert (
        grafana.preferred_name(["search", "--type", "dash-db"], None)
        == "grafana-search-dash-db.json"
    )
    assert (
        grafana.preferred_name(["search", "production"], None)
        == "grafana-search-production.json"
    )
    assert (
        grafana.preferred_name(["query", "--dashboard", "demo-dashboard-uid", "sum(agent_up)"], None)
        == "grafana-query-sum-agent_up.summary"
    )
    assert grafana.preferred_name(["get", "demo-dashboard-uid"], None) == "demo-dashboard-uid.json"
