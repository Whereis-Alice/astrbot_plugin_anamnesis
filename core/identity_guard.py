"""Reject identity records that upstream platform adapters got wrong.

Person nodes are keyed by ``account:<platform>:<sender_id>``, so a wrong
``sender_id`` silently welds two humans together and a churning
``sender_name`` destroys the nickname history. Both happen in the wild:

* some adapters fall back to the session id when they cannot resolve a sender;
* bot self-identity resolution can latch onto *the other party's* nickname,
  producing hundreds of distinct "names" for a single bot account.

This guard is a cheap, bounded, in-process sanity filter that runs between raw
message extraction and graph writing. It never invents data: it only drops or
flags records that cannot be trusted.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from typing import Any

from astrbot.api import logger

#: Per-identity name history depth. Bounded so a long-running process cannot
#: grow without limit on a memory-constrained host.
_NAME_HISTORY_DEPTH = 32

#: Distinct bot names kept per identity while voting for the canonical one.
_BOT_NAME_BALLOT_SIZE = 8


class IdentityGuard:
    """Validate and stabilise participant identities before they reach the graph."""

    def __init__(self, config: dict[str, Any] | None = None):
        options = config or {}
        self.enabled = bool(options.get("identity_guard_enabled", True))
        self.max_tracked = max(
            16, int(options.get("identity_guard_max_tracked", 512) or 512)
        )
        self.max_distinct_names = max(
            2, int(options.get("max_distinct_names_per_identity", 12) or 12)
        )
        self.window_seconds = (
            max(1.0, float(options.get("name_stability_window_hours", 24.0) or 24.0))
            * 3600.0
        )
        # identity_key -> deque[(name, observed_at)]
        self._name_history: OrderedDict[str, deque] = OrderedDict()
        # identity_key -> {name: votes}, only for bot identities
        self._bot_ballots: OrderedDict[str, dict[str, int]] = OrderedDict()
        self._unstable: set[str] = set()
        self._dropped_missing_id = 0
        self._dropped_session_echo = 0
        self._bot_names_corrected = 0
        self._flagged_unstable = 0

    # ------------------------------------------------------------------ helpers

    def _touch(self, store: OrderedDict, key: str, factory) -> Any:
        """LRU-get-or-create with a hard cap on tracked identities."""
        if key in store:
            store.move_to_end(key)
            return store[key]
        store[key] = factory()
        while len(store) > self.max_tracked:
            evicted, _ = store.popitem(last=False)
            self._unstable.discard(evicted)
        return store[key]

    def _record_name(self, identity_key: str, name: str, now: float) -> int:
        """Append a sighting and return the distinct-name count in the window."""
        history = self._touch(
            self._name_history, identity_key, lambda: deque(maxlen=_NAME_HISTORY_DEPTH)
        )
        history.append((name, now))
        cutoff = now - self.window_seconds
        return len({item[0] for item in history if item[1] >= cutoff})

    def _vote_bot_name(self, identity_key: str, name: str) -> str:
        """Return the canonical bot nickname, decided by simple majority."""
        ballot = self._touch(self._bot_ballots, identity_key, dict)
        if name in ballot:
            ballot[name] += 1
        elif len(ballot) < _BOT_NAME_BALLOT_SIZE:
            ballot[name] = 1
        else:
            # Ballot is full of other candidates: weaken the weakest instead of
            # tracking an unbounded stream of adapter noise.
            weakest = min(ballot, key=lambda key: (ballot[key], key))
            if ballot[weakest] <= 1:
                del ballot[weakest]
                ballot[name] = 1
            else:
                ballot[weakest] -= 1
        return max(ballot, key=lambda key: (ballot[key], key))

    # -------------------------------------------------------------------- entry

    def filter_identities(
        self,
        identities: list[dict[str, Any]],
        session_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return a cleaned copy of ``identities``.

        Args:
            identities: records produced from raw message sender fields.
            session_ids: session ids seen in the same window; an identity whose
                ``sender_id`` equals one of them is an adapter fallback, not a
                real account.

        Returns:
            A new list of new dicts. Untrustworthy records are dropped;
            unstable ones are kept but marked with ``name_unstable``.
        """
        if not identities:
            return []
        if not self.enabled:
            return [dict(identity) for identity in identities if isinstance(identity, dict)]

        echoes = {str(item).strip() for item in (session_ids or set()) if item}
        now = time.time()
        cleaned: list[dict[str, Any]] = []

        for raw in identities:
            if not isinstance(raw, dict):
                continue
            identity = dict(raw)
            identity["aliases"] = list(identity.get("aliases") or [])

            sender_id = str(identity.get("sender_id") or "").strip()
            if not sender_id:
                self._dropped_missing_id += 1
                continue
            if sender_id in echoes:
                # The adapter reused the session id as the sender id. Keeping it
                # would merge every participant of that session into one person.
                self._dropped_session_echo += 1
                logger.debug(
                    "[IdentityGuard] 丢弃与会话 ID 相同的发送者身份: %s", sender_id
                )
                continue

            identity_key = str(identity.get("identity_key") or "").strip()
            if not identity_key:
                platform = str(identity.get("platform") or "unknown").strip().lower()
                identity_key = (platform or "unknown") + ":" + sender_id
                identity["identity_key"] = identity_key

            display_name = str(identity.get("display_name") or sender_id).strip()
            if not display_name:
                display_name = sender_id

            if identity.get("is_bot"):
                canonical = self._vote_bot_name(identity_key, display_name)
                if canonical != display_name:
                    self._bot_names_corrected += 1
                    logger.debug(
                        "[IdentityGuard] Bot 昵称已归一: %s -> %s (%s)",
                        display_name,
                        canonical,
                        identity_key,
                    )
                identity["display_name"] = canonical
                identity["aliases"] = [canonical]
                cleaned.append(identity)
                continue

            distinct = self._record_name(identity_key, display_name, now)
            identity["display_name"] = display_name
            if display_name not in identity["aliases"]:
                identity["aliases"].append(display_name)

            if distinct > self.max_distinct_names:
                if identity_key not in self._unstable:
                    self._unstable.add(identity_key)
                    self._flagged_unstable += 1
                    logger.warning(
                        "[IdentityGuard] 身份 %s 在 %.0f 小时内出现 %d 个不同昵称，"
                        "停止记录其别名并排除出锚点",
                        identity_key,
                        self.window_seconds / 3600.0,
                        distinct,
                    )
            if identity_key in self._unstable:
                identity["name_unstable"] = True

            cleaned.append(identity)

        return cleaned

    def stats(self) -> dict[str, Any]:
        """Expose counters for the diagnostics command."""
        return {
            "enabled": self.enabled,
            "tracked_identities": len(self._name_history),
            "tracked_bots": len(self._bot_ballots),
            "unstable_identities": len(self._unstable),
            "dropped_missing_id": self._dropped_missing_id,
            "dropped_session_echo": self._dropped_session_echo,
            "bot_names_corrected": self._bot_names_corrected,
            "flagged_unstable": self._flagged_unstable,
            "max_tracked": self.max_tracked,
            "max_distinct_names": self.max_distinct_names,
            "window_hours": self.window_seconds / 3600.0,
        }


__all__ = ["IdentityGuard"]
