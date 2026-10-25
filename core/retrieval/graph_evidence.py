"""Explain literal graph support without treating it as a truth probability."""

from __future__ import annotations

import re
import unicodedata


def evidence_tokens(text: str) -> set[str]:
    """Small deterministic token set, supporting Chinese and Latin scripts."""
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    tokens = set(re.findall(r"[a-z0-9_]{2,}|[\u0400-\u04ff]{2,}", normalized))
    for segment in re.findall(r"[\u3400-\u9fff]+", normalized):
        tokens.update(segment[i : i + 2] for i in range(len(segment) - 1))
        if len(segment) == 1:
            tokens.add(segment)
    return set(sorted(tokens)[:256])


def graph_evidence(
    query: str, graph_text: str, source_text: str, *, minimum_support: float = 0.5
) -> dict:
    """Compute a conservative graph contribution gate from source overlap.

    The source is the authoritative memory document, never the graph entry
    itself. Aliases and paraphrases may have low literal overlap, hence an
    unsupported graph signal is discounted rather than asserted false.
    """
    graph_tokens = evidence_tokens(graph_text)
    source_tokens = evidence_tokens(source_text)
    query_tokens = evidence_tokens(query)
    support = (
        len(graph_tokens & source_tokens) / len(graph_tokens) if graph_tokens else 0.0
    )
    query_coverage = (
        len(query_tokens & graph_tokens) / len(query_tokens) if query_tokens else 0.0
    )
    supported = bool(source_text and graph_tokens and support >= minimum_support)
    factor = 1.0 if supported else 0.25
    return {
        "graph_evidence_support": round(support, 4),
        "graph_evidence_query_coverage": round(query_coverage, 4),
        "graph_evidence_gate": factor,
        "graph_evidence_reason": "literal_source_support"
        if supported
        else "source_unavailable"
        if not source_text
        else "weak_literal_source_support",
        "graph_evidence_matched_tokens": sorted(graph_tokens & source_tokens)[:12],
        "graph_evidence_basis": "memory_document_literal_overlap",
    }
