"""Text budgets for automatic recall and the shared retrieval boundary."""

MAX_SEARCH_QUERY_CHARS = 2000
MIN_SEARCH_QUERY_CHARS = 256
MAX_CONFIGURED_QUERY_CHARS = 32000
_OMITTED_MIDDLE = "\n…\n"


def normalize_query_char_limit(value) -> int:
    """Use a safe default for invalid legacy/raw configuration values."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return MAX_SEARCH_QUERY_CHARS
    try:
        limit = int(value)
    except (ValueError, OverflowError):
        return MAX_SEARCH_QUERY_CHARS
    if MIN_SEARCH_QUERY_CHARS <= limit <= MAX_CONFIGURED_QUERY_CHARS:
        return limit
    return MAX_SEARCH_QUERY_CHARS


def query_char_limit(config=None, *, engine=None) -> int:
    """Read ConfigManager or the flat configuration passed to retrievers."""
    if config is None:
        config = getattr(engine, "config", None)
    if isinstance(config, dict):
        section = config.get("recall_engine", config)
        raw = (
            section.get("max_query_chars", MAX_SEARCH_QUERY_CHARS)
            if isinstance(section, dict) else None
        )
    elif callable(getattr(config, "get", None)):
        raw = config.get("recall_engine.max_query_chars", MAX_SEARCH_QUERY_CHARS)
    else:
        raw = None
    return normalize_query_char_limit(raw)


def build_automatic_recall_query(
    current: str, recent_context: str = "", *, max_chars: int = MAX_SEARCH_QUERY_CHARS
) -> str:
    """Keep the current question first in budget, then the newest context.

    This is a search-only copy. Callers retain the complete message for storage,
    the LLM request and calendar parsing. A long current message keeps both ends
    so a question following a pasted document is not silently discarded.
    """
    max_chars = normalize_query_char_limit(max_chars)
    current = current.strip()
    if len(current) > max_chars:
        available = max_chars - len(_OMITTED_MIDDLE)
        head = available // 2
        return current[:head] + _OMITTED_MIDDLE + current[-(available - head):]
    remaining = max_chars - len(current) - 1
    if remaining <= 0 or not recent_context:
        return current
    recent_context = recent_context[-remaining:].strip()
    return recent_context + " " + current if recent_context else current
