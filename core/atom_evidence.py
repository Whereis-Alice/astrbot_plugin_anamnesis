"""Shared lifecycle and parent-scope policy for stored atom evidence."""

from __future__ import annotations

import math
import time
from collections.abc import Iterable
from typing import Any

from .models.memory_atom import MemoryAtom
from .recall_persona import persona_matches


def atom_scope_matches_parent(
    session_id: str | None, persona_id: str | None, metadata: dict[str, Any]
) -> bool:
    """Omitted legacy provenance is allowed; populated provenance must match."""
    return not (
        session_id is not None
        and metadata.get("session_id") is not None
        and session_id != metadata["session_id"]
    ) and (
        persona_id is None or persona_matches(persona_id, metadata.get("persona_id"))
    )


def live_atoms_for_parent(
    atoms: Iterable[MemoryAtom], metadata: dict[str, Any]
) -> list[MemoryAtom]:
    """Keep active, unexpired atoms inside their authoritative parent scope."""
    now, live = time.time(), []
    for atom in atoms:
        try:
            expiry = float(atom.expires_at)
        except (TypeError, ValueError, OverflowError):
            continue
        if (
            getattr(atom.status, "value", atom.status) == "active"
            and math.isfinite(expiry)
            and expiry > now
            and atom_scope_matches_parent(atom.session_id, atom.persona_id, metadata)
        ):
            live.append(atom)
    return live
