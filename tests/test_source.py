from __future__ import annotations

import pytest

from gotta.source.visibility import classify_visibility_metadata


def test_classify_local_gotta_surfaces_as_personal() -> None:
    payload = classify_visibility_metadata(
        {},
        provider="gotta",
        plugin="notes",
        locator="actor:claude:notes",
    )

    assert payload["visibility_level"] == "personal"
    assert payload["visibility_boundary"] == "same_user"
    assert payload["visibility_confidence"] == "high"
    assert "provider=gotta" in payload["visibility_basis"]


def test_classify_unknown_provider_stays_unknown() -> None:
    payload = classify_visibility_metadata({}, provider="confluence")

    assert payload["visibility_level"] == "unknown"
    assert payload["visibility_boundary"] == "unknown"
    assert payload["visibility_confidence"] == "low"
    assert payload["visibility_basis"] == [
        "provider=confluence",
        "classification=insufficient_evidence",
    ]


def test_classify_slack_shared_public_channel_as_restricted_cross_company() -> None:
    payload = classify_visibility_metadata(
        {
            "channel": {
                "id": "C12345678",
                "type": "public_channel",
                "isShared": True,
                "isExtShared": True,
            }
        },
        provider="slack",
    )

    assert payload["visibility_level"] == "restricted"
    assert payload["visibility_boundary"] == "cross_company"
    assert payload["visibility_confidence"] == "high"


@pytest.mark.parametrize(
    ("raw_visibility", "level", "boundary"),
    [
        ("public", "public", "internet"),
        ("internal", "internal", "same_company"),
        ("private", "restricted", "same_company"),
    ],
)
def test_classify_github_repo_visibility(
    raw_visibility: str,
    level: str,
    boundary: str,
) -> None:
    payload = classify_visibility_metadata(
        {"visibility": raw_visibility},
        provider="github",
    )

    assert payload["visibility_level"] == level
    assert payload["visibility_boundary"] == boundary
    assert payload["visibility_confidence"] == "high"


def test_classify_jira_issue_defaults_to_restricted_same_company_medium() -> None:
    payload = classify_visibility_metadata(
        {
            "siteUrl": "https://example.atlassian.net",
            "issueUrl": "https://example.atlassian.net/browse/OPS-1",
            "key": "OPS-1",
        },
        provider="jira",
        subcommand="get",
        locator="jira:OPS-1",
    )

    assert payload["visibility_level"] == "restricted"
    assert payload["visibility_boundary"] == "same_company"
    assert payload["visibility_confidence"] == "medium"
    assert "provider=jira" in payload["visibility_basis"]


def test_classify_jira_issue_security_as_restricted_same_company_high() -> None:
    payload = classify_visibility_metadata(
        {
            "siteUrl": "https://example.atlassian.net",
            "issueUrl": "https://example.atlassian.net/browse/OPS-1",
            "key": "OPS-1",
            "security": {"name": "Operations Only"},
        },
        provider="jira",
        subcommand="get",
        locator="jira:OPS-1",
    )

    assert payload["visibility_level"] == "restricted"
    assert payload["visibility_boundary"] == "same_company"
    assert payload["visibility_confidence"] == "high"
    assert "issue.security=Operations Only" in payload["visibility_basis"]


def test_classify_jira_locator_fallback_as_restricted_same_company_medium() -> None:
    payload = classify_visibility_metadata(
        {},
        provider="jira",
        subcommand="get",
        locator="jira:OPS-1",
    )

    assert payload["visibility_level"] == "restricted"
    assert payload["visibility_boundary"] == "same_company"
    assert payload["visibility_confidence"] == "medium"
