"""Attach a stable account suffix to person names inside extracted facts.

Facts are stored as graph nodes keyed by their literal text
(``fact:<text>``), so two different people who trigger the same sentence share
one node. Nicknames are neither unique nor stable — in a real group, three
separate accounts used the identical nickname and one account cycled through
ten — which means fact nodes silently merge unrelated people and recall then
attributes one member's actions to another.

Anchoring rewrites ``爱丽丝`` into ``爱丽丝#4778`` (the account id tail) so the
node key carries the account, while the summary shown to the user and the
raw conversation text stay untouched.
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

#: Names shorter than this are too generic to rewrite safely.
MIN_ANCHOR_NAME_LENGTH = 2

#: Marker that indicates a name has already been anchored.
ANCHOR_MARKER = "#"

DEFAULT_ANCHOR_FORMAT = "{name}#{tail}"


class IdentityAnchor:
    """Rewrite participant names in fact strings into ``name#idtail`` form."""

    def __init__(self, config: dict[str, Any] | None = None):
        options = config or {}
        self.enabled = bool(options.get("anchor_enabled", True))
        anchor_format = str(options.get("anchor_format") or DEFAULT_ANCHOR_FORMAT)
        if "{name}" not in anchor_format or "{tail}" not in anchor_format:
            logger.warning(
                "[IdentityAnchor] anchor_format 缺少 {name}/{tail} 占位符，回退默认值"
            )
            anchor_format = DEFAULT_ANCHOR_FORMAT
        self.anchor_format = anchor_format
        try:
            self.tail_length = int(options.get("anchor_tail_length", 4))
        except (TypeError, ValueError):
            self.tail_length = 4
        if self.tail_length < 0:
            self.tail_length = 0

    # ------------------------------------------------------------------ helpers

    def _tail_for(self, sender_id: str) -> str:
        """Return the id fragment used as the anchor suffix."""
        if self.tail_length <= 0 or len(sender_id) <= self.tail_length:
            return sender_id
        return sender_id[-self.tail_length :]

    def build_name_map(self, identities: list[dict[str, Any]]) -> dict[str, str]:
        """Map每个可安全锚定的昵称 -> 锚定后的写法.

        A name is skipped when it is too short, already anchored, identical to
        the raw account id, owned by a bot, flagged unstable, or — critically —
        shared by more than one account inside this same window. In the last
        case the text itself is ambiguous and guessing would fabricate a link.
        """
        candidates: dict[str, list[dict[str, Any]]] = {}
        for identity in identities or []:
            if not isinstance(identity, dict):
                continue
            if identity.get("is_bot") or identity.get("name_unstable"):
                continue
            sender_id = str(identity.get("sender_id") or "").strip()
            if not sender_id:
                continue
            name = " ".join(str(identity.get("display_name") or "").split())
            if len(name) < MIN_ANCHOR_NAME_LENGTH:
                continue
            if ANCHOR_MARKER in name:
                continue
            if name == sender_id:
                continue
            candidates.setdefault(name, []).append(identity)

        name_map: dict[str, str] = {}
        for name, owners in candidates.items():
            unique_ids = {str(item.get("sender_id") or "").strip() for item in owners}
            if len(unique_ids) > 1:
                logger.debug(
                    "[IdentityAnchor] 昵称 %s 在本窗口对应 %d 个账号，跳过锚定",
                    name,
                    len(unique_ids),
                )
                continue
            sender_id = unique_ids.pop()
            anchored = self.anchor_format.replace("{name}", name).replace(
                "{tail}", self._tail_for(sender_id)
            )
            if anchored != name:
                name_map[name] = anchored
        return name_map

    @staticmethod
    def _rewrite(text: str, ordered_names: list[str], name_map: dict[str, str]) -> str:
        """Leftmost-longest single pass so an anchor is never re-anchored."""
        if not text:
            return text
        out: list[str] = []
        index = 0
        length = len(text)
        while index < length:
            matched = None
            for name in ordered_names:
                if text.startswith(name, index):
                    matched = name
                    break
            if matched is None:
                out.append(text[index])
                index += 1
                continue
            out.append(name_map[matched])
            index += len(matched)
        return "".join(out)

    # -------------------------------------------------------------------- entry

    def anchor_facts(
        self,
        facts: list[Any],
        identities: list[dict[str, Any]],
    ) -> tuple[list[str], dict[str, str]]:
        """Return ``(anchored_facts, applied_map)``.

        The input list is not mutated. When anchoring is disabled or nothing is
        anchorable, the facts are returned as plain strings unchanged.
        """
        normalised = [str(fact) for fact in (facts or []) if str(fact or "").strip()]
        if not self.enabled or not normalised or not identities:
            return normalised, {}

        name_map = self.build_name_map(identities)
        if not name_map:
            return normalised, {}

        ordered_names = sorted(name_map, key=len, reverse=True)
        applied: dict[str, str] = {}
        result: list[str] = []
        for fact in normalised:
            rewritten = self._rewrite(fact, ordered_names, name_map)
            if rewritten != fact:
                for name, anchored in name_map.items():
                    if name in fact:
                        applied[name] = anchored
            result.append(rewritten)

        if applied:
            logger.debug("[IdentityAnchor] 已锚定 %d 个昵称: %s", len(applied), applied)
        return result, applied


__all__ = ["IdentityAnchor", "MIN_ANCHOR_NAME_LENGTH", "DEFAULT_ANCHOR_FORMAT"]
