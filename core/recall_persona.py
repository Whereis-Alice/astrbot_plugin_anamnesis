"""Distinguish an explicit no-persona selection from an omitted read filter.

Legacy retriever APIs use ``None`` for an unfiltered search. The scoped recall
entry point carries its explicit selection through concurrent routes without
changing those APIs. AstrBot's old no-persona marker shares the same boundary.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

NO_PERSONA = "[%None]"
_exact_null_persona: ContextVar[bool] = ContextVar(
    "anamnesis_exact_null_persona", default=False
)


@contextmanager
def exact_null_persona_scope(enabled: bool) -> Iterator[None]:
    token = _exact_null_persona.set(bool(enabled))
    try:
        yield
    finally:
        _exact_null_persona.reset(token)


def is_null_persona_filter(persona_id: str | None) -> bool:
    return persona_id == NO_PERSONA or (persona_id is None and _exact_null_persona.get())


def persona_filter_requested(persona_id: str | None) -> bool:
    return persona_id is not None or _exact_null_persona.get()


def persona_matches(stored: Any, requested: str | None) -> bool:
    if requested is None or requested == NO_PERSONA:
        return stored is None or stored == NO_PERSONA
    return stored == requested


def atom_persona_sql_match(atom_expression: str, parent_expression: str) -> str:
    """Allow legacy omitted atom scope; populated scope must match its parent.

    Expressions are internal SQL fragments, never caller-supplied identifiers.
    A NULL parent is an explicit no-persona value, not a wildcard.
    """
    return (
        f"({atom_expression} IS NULL OR {atom_expression}={parent_expression} "
        f"OR ({atom_expression}='[%None]' AND {parent_expression} IS NULL))"
    )


def persona_sql_filter(
    expression: str, persona_id: str | None
) -> tuple[str, list[str | None]]:
    if is_null_persona_filter(persona_id):
        return f"({expression} IS NULL OR {expression}=?)", [NO_PERSONA]
    return f"{expression}=?", [persona_id]
