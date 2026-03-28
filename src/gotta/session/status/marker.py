"""Actor-managed TODO marker helpers."""

from __future__ import annotations

from gotta.session.registry import _actor_label, _normalize_actor_name


FINAL_SIGNOFF_MARKER = "actors-final-signoff"


def _actor_todo_marker(actor_name: str, phase: str) -> str:
    return f"actor-{_normalize_actor_name(actor_name)}-{phase}"


def _actor_todo_redirect(actor_name: str, phase: str) -> str:
    actor = _normalize_actor_name(actor_name)
    label = _actor_label(actor)
    if phase == "initial":
        return (
            f"that TODO item is owned by {label} lifecycle; inspect with "
            f"`gotta actor status {actor}` and advance it through "
            f"`gotta actor complete {actor}` or `gotta actor signoff {actor} ...`"
        )
    if phase == "complete":
        return (
            f"that TODO item is owned by {label} lifecycle; use "
            f"`gotta actor complete {actor}` once the actor run has materially landed"
        )
    if phase == "dispositioned":
        return (
            f"that TODO item is owned by {label} disposition; use "
            f"`gotta actor signoff {actor} --summary ...` after review"
        )
    raise ValueError(f"unknown actor TODO phase: {phase}")


def _managed_todo_redirect(managed_key: str) -> str:
    if managed_key == FINAL_SIGNOFF_MARKER:
        return (
            "that TODO item is owned by final actor sign-off; inspect all actors with "
            "`gotta actor status` and sign off each actor through "
            "`gotta actor signoff ...`"
        )
    prefix = "actor-"
    suffixes = ("-initial", "-complete", "-dispositioned")
    if managed_key.startswith(prefix):
        for suffix in suffixes:
            if managed_key.endswith(suffix):
                actor = managed_key[len(prefix) : -len(suffix)]
                phase = suffix.removeprefix("-")
                return _actor_todo_redirect(actor, phase)
    return (
        "that TODO item is managed by native actor state; use "
        "`gotta actor ...` to advance it instead of "
        "`gotta todo check`"
    )
