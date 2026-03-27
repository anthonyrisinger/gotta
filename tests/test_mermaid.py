from gotta.plugins import session


def test_render_semantic_mermaid_escapes_quotes_in_labels() -> None:
    payload = {
        "nodes": [
            {
                "id": 'query:search "Condor Integration" --limit 10 --output markdown',
                "label": 'search "Condor Integration" --limit 10 --output markdown',
                "kind": "query",
            },
            {
                "id": "provider:jira",
                "label": "jira",
                "kind": "provider",
            },
        ],
        "edges": [
            {
                "source": "provider:jira",
                "target": 'query:search "Condor Integration" --limit 10 --output markdown',
                "label": "query",
            }
        ],
    }

    mermaid = session._render_semantic_mermaid(payload)

    assert "&quot;Condor Integration&quot;" in mermaid
    assert '\\"Condor Integration\\"' not in mermaid


def test_render_analysis_mermaid_revision_path_is_label_safe() -> None:
    payload = {
        "sources": [],
        "content": [
            {
                "checksum": "a" * 64,
                "preferredName": "OOPS.md",
                "providers": ["read"],
                "actors": ["primary"],
                "resourceHints": [],
                "nameCollision": False,
            },
            {
                "checksum": "b" * 64,
                "preferredName": "OOPS.md",
                "providers": ["read"],
                "actors": ["primary"],
                "resourceHints": [],
                "nameCollision": False,
            },
        ],
        "sourceEdges": [],
        "revisionEdges": [
            {
                "from": "a" * 64,
                "to": "b" * 64,
                "locator": "/tmp/gotta-session-demo/OOPS.md",
            }
        ],
    }

    mermaid = session._render_analysis_mermaid(payload)

    assert "-->|revision:/tmp/gotta-session-demo/OOPS.md|" in mermaid
    assert "-. revision:/tmp/gotta-session-demo/OOPS.md .->" not in mermaid


def test_render_analysis_mermaid_uses_real_newlines_in_labels() -> None:
    payload = {
        "sources": [
            {
                "locator": "slack:thread:C123:1761932107.621519",
                "actors": ["Codex", "primary"],
                "collision": False,
            }
        ],
        "content": [
            {
                "checksum": "c" * 64,
                "preferredName": "220.md",
                "providers": ["slack"],
                "actors": ["Codex", "primary"],
                "resourceHints": ["thread:C123:1761932107.621519"],
                "nameCollision": True,
            }
        ],
        "sourceEdges": [
            {
                "source": "slack:thread:C123:1761932107.621519",
                "checksum": "c" * 64,
                "plugins": ["read", "read"],
                "actors": ["Codex", "primary"],
            }
        ],
        "revisionEdges": [],
    }

    mermaid = session._render_analysis_mermaid(payload)

    assert "\\n" not in mermaid
    assert (
        '["slack:thread:C123:1761932107.621519<br/>actor: Codex, primary"]' in mermaid
    )
    assert (
        '["220.md<br/>thread:C123:1761932107.621519<br/>slack<br/>actor: Codex, primary<br/>cccccccccccc"]'
        in mermaid
    )
    assert "-->|read, read<br/>actor: Codex, primary|" in mermaid
