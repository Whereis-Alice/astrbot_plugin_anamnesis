"""Regression tests for the identity-hardening layer.

Person nodes are keyed by account id, but everything a human sees -- and
everything the summarising LLM writes -- is a nickname. Nicknames are neither
unique nor stable, so the pipeline needs three cooperating pieces:

* :class:`IdentityGuard` rejects sender records the adapter clearly got wrong;
* :class:`IdentityAnchor` welds an account tail onto names inside fact text;
* :class:`AliasStore` remembers every nickname an account ever used.

:class:`IdentityRepair` then cleans up the damage older builds already wrote.
"""

import json
import time
from collections import deque

import pytest
from astrbot_plugin_anamnesis.core.identity_guard import IdentityGuard
from astrbot_plugin_anamnesis.core.identity_repair import (
    BACKUP_TABLE,
    IdentityRepair,
    normalise_platform,
)
from astrbot_plugin_anamnesis.core.models.conversation_models import Message
from astrbot_plugin_anamnesis.core.models.graph_models import GraphNode
from astrbot_plugin_anamnesis.core.processors.identity_anchor import IdentityAnchor
from astrbot_plugin_anamnesis.storage.alias_store import (
    AliasStore,
    canonicalize_alias,
    expand_query_tokens,
)
from astrbot_plugin_anamnesis.storage.conversation_store import ConversationStore
from astrbot_plugin_anamnesis.storage.graph_store import GraphStore


def _identity(sender_id, display_name, **overrides):
    """Build one raw identity record the way MemoryProcessor does."""
    platform = overrides.pop("platform", "aiocqhttp")
    payload = {
        "identity_key": f"{platform}:{sender_id}",
        "sender_id": sender_id,
        "platform": platform,
        "display_name": display_name,
        "aliases": [display_name],
        "is_bot": False,
    }
    payload.update(overrides)
    return payload


async def _alias_store(tmp_path, name="aliases.db", **config):
    store = AliasStore(str(tmp_path / name), config or None)
    await store.initialize()
    return store


async def _person_node(store, canonical_value):
    """Read one person node back; GraphStore exposes no single-node getter."""
    async with store._connect() as db:
        cursor = await db.execute(
            "SELECT node_value, metadata FROM graph_nodes WHERE node_key = ?",
            ("person:" + canonical_value,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return {"node_value": str(row[0]), "metadata": json.loads(row[1])}


# --------------------------------------------------------------- AliasStore


@pytest.mark.asyncio
async def test_alias_store_records_and_dedupes_nicknames(tmp_path):
    store = await _alias_store(tmp_path)

    written = await store.record_identities(
        [
            _identity("111", "  Alice  ", aliases=["alice", "ALICE", "爱丽丝"]),
            _identity("222", "222"),
        ]
    )

    # "alice"/"ALICE"/"  Alice  " collapse into one row; the bare account id
    # carries no signal and is skipped entirely.
    assert written == 2
    assert await store.count() == 2
    assert sorted(await store.aliases_for("aiocqhttp:111")) == ["Alice", "爱丽丝"]
    assert await store.aliases_for("aiocqhttp:222") == []


@pytest.mark.asyncio
async def test_alias_store_ignores_unstable_identities(tmp_path):
    store = await _alias_store(tmp_path, name="unstable.db")

    written = await store.record_identities(
        [_identity("333", "改名狂魔", name_unstable=True)]
    )

    assert written == 0
    assert await store.count() == 0


@pytest.mark.asyncio
async def test_alias_store_prunes_beyond_max_per_identity(tmp_path):
    store = await _alias_store(tmp_path, name="prune.db", alias_max_per_identity=3)

    for index in range(6):
        await store.record_identities([_identity("444", f"名字{index}")])

    assert await store.count() == 3
    # Pruning drops the oldest sightings, so the newest names survive.
    assert set(await store.aliases_for("aiocqhttp:444")) == {"名字3", "名字4", "名字5"}


@pytest.mark.asyncio
async def test_alias_store_zero_cache_still_resolves_from_disk(tmp_path):
    """cache_max=0 is the low-memory mode: correctness must not depend on it."""
    store = await _alias_store(tmp_path, name="nocache.db", alias_cache_max=0)
    await store.record_identities([_identity("555", "无缓存")])

    await store.refresh_snapshot()

    assert store._alias_index == {}
    assert await store.aliases_for("aiocqhttp:555") == ["无缓存"]
    assert await store.resolve_alias("无缓存") == ["aiocqhttp:555"]


@pytest.mark.asyncio
async def test_alias_store_snapshot_respects_ttl(tmp_path):
    store = await _alias_store(tmp_path, name="ttl.db", alias_cache_ttl_seconds=600)
    await store.record_identities([_identity("666", "第一版")])
    await store.refresh_snapshot()
    assert canonicalize_alias("第一版") in store._alias_index

    # Write behind the store's back so only an explicit refresh can see it.
    async with store._connect() as db:
        await db.execute(
            "INSERT INTO person_aliases (identity_key, alias, canonical_alias, "
            "platform, sender_id, is_bot, first_seen, last_seen, hits) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?, 1)",
            (
                "aiocqhttp:666",
                "第二版",
                canonicalize_alias("第二版"),
                "aiocqhttp",
                "666",
                store._now_iso(),
                store._now_iso(),
            ),
        )
        await db.commit()

    await store.refresh_snapshot()
    assert canonicalize_alias("第二版") not in store._alias_index

    await store.refresh_snapshot(force=True)
    assert canonicalize_alias("第二版") in store._alias_index


@pytest.mark.asyncio
async def test_alias_store_stats_counts_shared_and_multi(tmp_path):
    store = await _alias_store(tmp_path, name="stats.db")
    await store.record_identities(
        [
            _identity("777", "爱丽丝", aliases=["爱丽丝", "小爱"]),
            _identity("888", "爱丽丝"),
        ]
    )

    stats = await store.stats()

    assert stats["aliases"] == 3
    assert stats["identities"] == 2
    assert stats["multi_alias_identities"] == 1
    assert stats["shared_aliases"] == 1
    assert stats["cache_max"] == store.cache_max


@pytest.mark.asyncio
async def test_alias_store_bot_flags_are_listed_and_cleared(tmp_path):
    """A single mislabelled row must not brand a member as the Bot forever."""
    store = await _alias_store(tmp_path, name="botflag.db")
    await store.record_identities(
        [
            _identity("3385375303", "爱丽丝", is_bot=True),
            _identity("3844501467", "星之头巾", is_bot=True),
            _identity("2515847162", "babyQ", is_bot=True),
        ]
    )

    rows = await store.bot_flag_rows()
    assert {row["identity_key"] for row in rows} == {
        "aiocqhttp:3385375303",
        "aiocqhttp:3844501467",
        "aiocqhttp:2515847162",
    }

    # Without a known Bot identity the method must stay a no-op rather than
    # wiping the whole column.
    assert await store.clear_false_bot_flags([]) == 0
    assert len(await store.bot_flag_rows()) == 3

    cleared = await store.clear_false_bot_flags(["aiocqhttp:3385375303"])

    assert cleared == 2
    remaining = await store.bot_flag_rows()
    assert [row["identity_key"] for row in remaining] == ["aiocqhttp:3385375303"]
    # The nicknames themselves are real and stay searchable.
    assert await store.aliases_for("aiocqhttp:3844501467") == ["星之头巾"]


@pytest.mark.asyncio
async def test_alias_store_backfills_from_document_metadata(tmp_path):
    """Existing installs carry years of nickname history inside metadata."""
    import aiosqlite

    store = await _alias_store(tmp_path, name="backfill.db")
    documents_path = tmp_path / "documents.db"
    db = await aiosqlite.connect(str(documents_path))
    try:
        await db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, metadata TEXT)")
        await db.execute(
            "INSERT INTO documents (metadata) VALUES (?)",
            (
                json.dumps(
                    {
                        "participant_identities": [
                            {
                                "identity_key": "aiocqhttp:999",
                                "sender_id": "999",
                                "platform": "aiocqhttp",
                                "display_name": "旧昵称",
                                "aliases": ["旧昵称", "更旧的昵称"],
                            }
                        ]
                    }
                ),
            ),
        )
        # Corrupt and empty rows must be skipped, not crash the scan.
        await db.execute("INSERT INTO documents (metadata) VALUES (?)", ("not json",))
        await db.execute("INSERT INTO documents (metadata) VALUES (?)", ("{}",))
        await db.commit()

        recorded = await store.backfill_from_documents(db)
    finally:
        await db.close()

    assert recorded == 2
    recovered = await store.aliases_for("aiocqhttp:999")
    assert set(recovered) == {"旧昵称", "更旧的昵称"}


@pytest.mark.asyncio
async def test_alias_store_backfill_without_connection_is_noop(tmp_path):
    store = await _alias_store(tmp_path, name="backfill_none.db")
    assert await store.backfill_from_documents(None) == 0


@pytest.mark.asyncio
async def test_expand_tokens_adds_sibling_nicknames(tmp_path):
    store = await _alias_store(tmp_path, name="expand.db")
    await store.record_identities(
        [_identity("1234", "爱丽丝", aliases=["爱丽丝", "Alice", "小爱"])]
    )

    expanded = await expand_query_tokens(store, "小爱昨天说了什么", ["小爱"])

    assert "小爱" in expanded
    assert "爱丽丝" in expanded
    assert "Alice" in expanded


@pytest.mark.asyncio
async def test_expand_query_tokens_degrades_quietly(tmp_path):
    """Recall must never break because alias expansion had a bad day."""
    store = await _alias_store(tmp_path, name="expand_off.db", alias_query_expansion=False)
    await store.record_identities([_identity("4321", "爱丽丝", aliases=["Alice"])])

    assert await expand_query_tokens(None, "爱丽丝", ["爱丽丝"]) == ["爱丽丝"]
    assert await expand_query_tokens(store, "爱丽丝", ["爱丽丝"]) == ["爱丽丝"]

    class _Boom:
        query_expansion = True

        async def expand_tokens(self, *args, **kwargs):
            raise RuntimeError("index unavailable")

    assert await expand_query_tokens(_Boom(), "爱丽丝", ["爱丽丝"]) == ["爱丽丝"]


@pytest.mark.asyncio
async def test_alias_store_skips_single_character_matches(tmp_path):
    """One-character aliases match nearly any Chinese sentence."""
    store = await _alias_store(tmp_path, name="short.db")
    await store.record_identities([_identity("5678", "宇", aliases=["宇", "宇宙无敌"])])

    matches = await store.match_in_text("今天宇很开心")

    assert matches == []


# ------------------------------------------------------------- IdentityGuard


def test_identity_guard_drops_session_echo():
    """Some adapters fall back to the session id when the sender is unknown."""
    guard = IdentityGuard()

    cleaned = guard.filter_identities(
        [
            _identity("group_12345", "群成员"),
            _identity("111", "爱丽丝"),
        ],
        session_ids={"group_12345"},
    )

    assert [item["sender_id"] for item in cleaned] == ["111"]
    assert guard.stats()["dropped_session_echo"] == 1


def test_identity_guard_drops_records_without_sender_id():
    guard = IdentityGuard()

    cleaned = guard.filter_identities(
        [
            {"identity_key": "aiocqhttp:", "sender_id": "", "display_name": "幽灵"},
            _identity("222", "爱丽丝"),
            "not-a-dict",
        ]
    )

    assert [item["sender_id"] for item in cleaned] == ["222"]
    assert guard.stats()["dropped_missing_id"] == 1


def test_identity_guard_flags_identity_that_renames_constantly():
    guard = IdentityGuard(
        {"max_distinct_names_per_identity": 3, "name_stability_window_hours": 24}
    )

    for index in range(4):
        cleaned = guard.filter_identities([_identity("333", f"昵称{index}")])

    assert cleaned[0]["name_unstable"] is True
    stats = guard.stats()
    assert stats["unstable_identities"] == 1
    assert stats["flagged_unstable"] == 1

    # A stable neighbour on the same platform is untouched.
    other = guard.filter_identities([_identity("444", "稳定的人")])
    assert "name_unstable" not in other[0]


def test_identity_guard_forgets_names_outside_the_window():
    """Renaming twice a year is normal; renaming twice an hour is not."""
    guard = IdentityGuard(
        {"max_distinct_names_per_identity": 2, "name_stability_window_hours": 24}
    )
    key = "aiocqhttp:555"
    stale = time.time() - 40 * 3600
    guard._name_history.setdefault(key, deque(maxlen=32))
    guard._name_history[key].extend([("旧名A", stale), ("旧名B", stale)])

    cleaned = guard.filter_identities([_identity("555", "现在的名字")])

    assert "name_unstable" not in cleaned[0]


def test_identity_guard_normalises_bot_nickname_by_majority():
    """Bot self-identity resolution sometimes latches onto the other party."""
    guard = IdentityGuard()
    bot = {"platform": "aiocqhttp", "is_bot": True}

    for _ in range(3):
        guard.filter_identities([_identity("3385375303", "爱丽丝", **bot)])
    cleaned = guard.filter_identities([_identity("3385375303", "某个群友", **bot)])

    assert cleaned[0]["display_name"] == "爱丽丝"
    assert cleaned[0]["aliases"] == ["爱丽丝"]
    assert guard.stats()["bot_names_corrected"] == 1


def test_identity_guard_bot_ballot_stays_bounded():
    guard = IdentityGuard()
    for index in range(40):
        guard.filter_identities(
            [_identity("3385375303", f"噪声{index}", is_bot=True)]
        )

    assert len(guard._bot_ballots["aiocqhttp:3385375303"]) <= 8


def test_identity_guard_caps_tracked_identities():
    """A long-running process on a 1.5GB host cannot grow without limit."""
    guard = IdentityGuard({"identity_guard_max_tracked": 16})

    for index in range(200):
        guard.filter_identities([_identity(str(index), f"人{index}")])

    assert guard.stats()["tracked_identities"] == 16
    assert len(guard._name_history) == 16


def test_identity_guard_disabled_passes_records_through():
    guard = IdentityGuard({"identity_guard_enabled": False})
    raw = [_identity("group_1", "回声"), _identity("666", "爱丽丝")]

    cleaned = guard.filter_identities(raw, session_ids={"group_1"})

    assert [item["sender_id"] for item in cleaned] == ["group_1", "666"]
    assert cleaned[0] is not raw[0]
    assert guard.stats()["enabled"] is False


def test_identity_guard_backfills_missing_identity_key():
    guard = IdentityGuard()

    cleaned = guard.filter_identities(
        [{"sender_id": "777", "display_name": "无 key", "platform": "AiocqHttp"}]
    )

    assert cleaned[0]["identity_key"] == "aiocqhttp:777"


# ------------------------------------------------------------ IdentityAnchor


def test_identity_anchor_welds_account_tail_onto_names():
    anchor = IdentityAnchor()
    identities = [_identity("112233444778", "爱丽丝")]

    facts, applied = anchor.anchor_facts(["爱丽丝喜欢滑雪"], identities)

    assert facts == ["爱丽丝#4778喜欢滑雪"]
    assert applied == {"爱丽丝": "爱丽丝#4778"}


def test_identity_anchor_skips_nickname_shared_by_two_accounts():
    """Two accounts, one nickname: the text is genuinely ambiguous."""
    anchor = IdentityAnchor()
    identities = [_identity("1111", "爱丽丝"), _identity("2222", "爱丽丝")]

    facts, applied = anchor.anchor_facts(["爱丽丝提出了方案"], identities)

    assert facts == ["爱丽丝提出了方案"]
    assert applied == {}


def test_identity_anchor_skips_bots_unstable_and_short_names():
    anchor = IdentityAnchor()
    identities = [
        _identity("1111", "机器人", is_bot=True),
        _identity("2222", "改名狂", name_unstable=True),
        _identity("3333", "宇"),
        _identity("4444", "4444"),
        {"sender_id": "", "display_name": "无账号"},
    ]

    assert anchor.build_name_map(identities) == {}


def test_identity_anchor_does_not_re_anchor():
    anchor = IdentityAnchor()
    identities = [_identity("1234", "爱丽丝#9999")]

    assert anchor.build_name_map(identities) == {}


def test_identity_anchor_prefers_the_longest_name():
    """Leftmost-longest, so "爱丽丝" never eats the prefix of "爱丽丝丝"."""
    anchor = IdentityAnchor()
    identities = [_identity("1111", "爱丽"), _identity("2222", "爱丽丝")]

    facts, _applied = anchor.anchor_facts(["爱丽丝和爱丽一起吃饭"], identities)

    assert facts == ["爱丽丝#2222和爱丽#1111一起吃饭"]


def test_identity_anchor_tail_length_zero_uses_full_id():
    anchor = IdentityAnchor({"anchor_tail_length": 0})

    assert anchor.build_name_map([_identity("112233", "爱丽丝")]) == {
        "爱丽丝": "爱丽丝#112233"
    }


def test_identity_anchor_short_id_is_used_verbatim():
    anchor = IdentityAnchor({"anchor_tail_length": 8})

    assert anchor.build_name_map([_identity("42", "爱丽丝")]) == {"爱丽丝": "爱丽丝#42"}


def test_identity_anchor_rejects_format_without_placeholders():
    anchor = IdentityAnchor({"anchor_format": "{name}"})

    assert anchor.anchor_format == "{name}#{tail}"


def test_identity_anchor_honours_custom_format():
    anchor = IdentityAnchor({"anchor_format": "{name}({tail})"})

    assert anchor.build_name_map([_identity("1234", "爱丽丝")]) == {
        "爱丽丝": "爱丽丝(1234)"
    }


def test_identity_anchor_disabled_returns_facts_unchanged():
    anchor = IdentityAnchor({"anchor_enabled": False})
    identities = [_identity("1234", "爱丽丝")]

    facts, applied = anchor.anchor_facts(["爱丽丝喜欢滑雪"], identities)

    assert facts == ["爱丽丝喜欢滑雪"]
    assert applied == {}


def test_identity_anchor_drops_blank_facts_and_stringifies():
    anchor = IdentityAnchor()

    facts, applied = anchor.anchor_facts(["", "  ", None, 42], [])

    assert facts == ["42"]
    assert applied == {}


def test_identity_anchor_negative_tail_length_is_clamped():
    assert IdentityAnchor({"anchor_tail_length": -5}).tail_length == 0
    assert IdentityAnchor({"anchor_tail_length": "oops"}).tail_length == 4


# --------------------------------------------------- person node accumulation


@pytest.mark.asyncio
async def test_person_node_accumulates_aliases_across_memories(tmp_path):
    """A plain metadata upsert would forget every former nickname."""
    store = GraphStore(str(tmp_path / "person_graph.db"))
    await store.initialize()

    await store.upsert_node(
        GraphNode(
            "person",
            "爱丽丝",
            "account:aiocqhttp:1234",
            {"aliases": ["爱丽丝"], "is_bot": True, "sender_id": "1234"},
        )
    )
    await store.upsert_node(
        GraphNode(
            "person",
            "小爱",
            "account:aiocqhttp:1234",
            {"aliases": ["小爱"], "sender_id": "1234"},
        )
    )

    node = await _person_node(store, "account:aiocqhttp:1234")

    assert node is not None
    assert node["metadata"]["aliases"] == ["小爱", "爱丽丝"]
    # Once known to be a bot it stays a bot: a message missing the flag must
    # not silently demote it back to a human.
    assert node["metadata"]["is_bot"] is True
    assert node["node_value"] == "小爱"


@pytest.mark.asyncio
async def test_person_node_alias_list_is_bounded(tmp_path):
    store = GraphStore(
        str(tmp_path / "person_bound.db"), {"alias_max_per_identity": 3}
    )
    await store.initialize()

    for index in range(6):
        await store.upsert_node(
            GraphNode(
                "person",
                f"名字{index}",
                "account:aiocqhttp:4321",
                {"aliases": [f"名字{index}"]},
            )
        )

    node = await _person_node(store, "account:aiocqhttp:4321")

    assert len(node["metadata"]["aliases"]) == 3
    assert node["metadata"]["aliases"][0] == "名字5"


# ------------------------------------------------------------ IdentityRepair


async def _conversation_store(tmp_path, name="conv.db"):
    store = ConversationStore(str(tmp_path / name))
    await store.initialize()
    return store


async def _log_assistant(
    store,
    sender_id,
    sender_name,
    count=1,
    platform="aiocqhttp",
    session_id="group_1",
):
    """Append ``count`` assistant rows attributed to one account."""
    for _ in range(count):
        await store.add_message(
            Message(
                id=0,
                session_id=session_id,
                role="assistant",
                content="reply",
                sender_id=sender_id,
                sender_name=sender_name,
                platform=platform,
            )
        )


async def _assistant_senders(store):
    cursor = await store.connection.execute(
        "SELECT sender_id, sender_name FROM messages "
        "WHERE role = 'assistant' ORDER BY id"
    )
    return [(str(row[0]), str(row[1] or "")) for row in await cursor.fetchall()]


def test_normalise_platform_folds_adapter_casing():
    assert normalise_platform("  AiocqHttp ") == "aiocqhttp"
    assert normalise_platform(None) == ""


@pytest.mark.asyncio
async def test_identity_repair_flags_rows_stolen_from_members(tmp_path):
    """Older builds filed every Bot reply under whoever spoke last."""
    store = await _conversation_store(tmp_path)
    await _log_assistant(store, "3385375303", "爱丽丝", count=6)
    await _log_assistant(store, "3844501467", "星之头巾", count=3)
    await _log_assistant(store, "2515847162", "babyQ", count=1)

    report = await IdentityRepair(store).analyse()

    assert report["ok"] is True
    assert report["total_wrong"] == 4
    assert report["backup_rows"] == 0
    platform = report["platforms"][0]
    assert platform["platform"] == "aiocqhttp"
    assert platform["bot_id"] == "3385375303"
    assert platform["bot_name"] == "爱丽丝"
    assert platform["source"] == "majority"
    assert platform["ambiguous"] is False
    assert (platform["total"], platform["correct"], platform["wrong"]) == (10, 6, 4)
    assert platform["distinct_wrong"] == 2
    assert [item["sender_id"] for item in platform["offenders"]] == [
        "3844501467",
        "2515847162",
    ]

    await store.close()


@pytest.mark.asyncio
async def test_identity_repair_trusts_the_live_adapter_over_the_majority(tmp_path):
    """A Bot outnumbered by its own misattributions is still the Bot."""
    store = await _conversation_store(tmp_path)
    await _log_assistant(store, "3844501467", "星之头巾", count=8)
    await _log_assistant(store, "3385375303", "爱丽丝", count=2)
    repair = IdentityRepair(store)

    blind = await repair.analyse()
    assert blind["platforms"][0]["bot_id"] == "3844501467"

    informed = await repair.analyse(
        live_bot_ids={"aiocqhttp": ("3385375303", "爱丽丝")}
    )
    platform = informed["platforms"][0]
    assert platform["bot_id"] == "3385375303"
    assert platform["source"] == "live"
    assert platform["ambiguous"] is False
    assert platform["wrong"] == 8

    # ``bot:<scope>`` placeholders appear when the adapter is mute and must
    # never be trusted as a real account id.
    synthetic = await repair.analyse(
        live_bot_ids={"aiocqhttp": ("bot:group_1", "爱丽丝")}
    )
    assert synthetic["platforms"][0]["source"] == "majority"

    await store.close()


@pytest.mark.asyncio
async def test_identity_repair_refuses_to_guess_on_a_tie(tmp_path):
    """A table where no id owns a majority cannot be repaired safely."""
    store = await _conversation_store(tmp_path)
    await _log_assistant(store, "111", "甲", count=2)
    await _log_assistant(store, "222", "乙", count=2)
    repair = IdentityRepair(store)

    report = await repair.analyse()
    assert report["platforms"][0]["ambiguous"] is True
    assert report["total_wrong"] == 0

    outcome = await repair.repair()
    assert outcome["ok"] is True
    assert outcome["repaired"] == 0
    assert outcome["skipped"] == ["aiocqhttp"]
    assert await _assistant_senders(store) == [
        ("111", "甲"),
        ("111", "甲"),
        ("222", "乙"),
        ("222", "乙"),
    ]

    await store.close()


@pytest.mark.asyncio
async def test_identity_repair_rewrites_rows_and_rollback_restores_them(tmp_path):
    store = await _conversation_store(tmp_path)
    await _log_assistant(store, "3385375303", "爱丽丝", count=4)
    await _log_assistant(store, "3844501467", "星之头巾", count=2)
    await store.add_message(
        Message(
            id=0,
            session_id="group_1",
            role="user",
            content="在吗",
            sender_id="3844501467",
            sender_name="星之头巾",
            platform="aiocqhttp",
        )
    )
    before = await _assistant_senders(store)
    repair = IdentityRepair(store)

    outcome = await repair.repair()

    assert outcome["ok"] is True
    assert outcome["repaired"] == 2
    assert outcome["skipped"] == []
    assert outcome["backup_rows"] == 2
    assert {sender for sender, _ in await _assistant_senders(store)} == {
        "3385375303"
    }

    cursor = await store.connection.execute(
        "SELECT old_sender_id, new_sender_id FROM " + BACKUP_TABLE
    )
    assert {(str(row[0]), str(row[1])) for row in await cursor.fetchall()} == {
        ("3844501467", "3385375303")
    }

    # Only the Bot's own attribution was wrong; member rows stay untouched.
    cursor = await store.connection.execute(
        "SELECT sender_id FROM messages WHERE role = 'user'"
    )
    assert [str(row[0]) for row in await cursor.fetchall()] == ["3844501467"]

    undo = await repair.rollback()
    assert undo == {"ok": True, "restored": 2, "empty": False}
    assert await _assistant_senders(store) == before
    assert await repair.rollback() == {"ok": True, "restored": 0, "empty": True}

    await store.close()


@pytest.mark.asyncio
async def test_identity_repair_clears_bot_flags_stuck_on_members(tmp_path):
    """``is_bot`` is aggregated with MAX(), so one bad row sticks forever."""
    store = await _conversation_store(tmp_path)
    await _log_assistant(store, "3385375303", "爱丽丝", count=5)
    await _log_assistant(store, "3844501467", "星之头巾", count=1)
    aliases = await _alias_store(tmp_path, name="repair_aliases.db")
    await aliases.record_identities(
        [
            _identity("3385375303", "爱丽丝", is_bot=True),
            _identity("3844501467", "星之头巾", is_bot=True),
        ]
    )
    assert len(await aliases.bot_flag_rows()) == 2
    repair = IdentityRepair(store, alias_store=aliases)

    report = await repair.analyse()
    assert report["bot_identity_keys"] == ["aiocqhttp:3385375303"]
    assert [row["identity_key"] for row in report["false_bot_flags"]] == [
        "aiocqhttp:3844501467"
    ]

    outcome = await repair.repair()
    assert outcome["cleared_bot_flags"] == 1
    assert [row["identity_key"] for row in await aliases.bot_flag_rows()] == [
        "aiocqhttp:3385375303"
    ]
    # The nickname is genuine history: only the misapplied flag is dropped.
    assert await aliases.aliases_for("aiocqhttp:3844501467") == ["星之头巾"]

    await store.close()


@pytest.mark.asyncio
async def test_identity_repair_platform_filter_spares_other_adapters(tmp_path):
    store = await _conversation_store(tmp_path)
    await _log_assistant(store, "3385375303", "爱丽丝", count=3)
    await _log_assistant(store, "3844501467", "星之头巾", count=1)
    await _log_assistant(
        store, "tg-bot", "Alice", count=3, platform="telegram", session_id="tg_1"
    )
    await _log_assistant(
        store, "tg-user", "Bob", count=1, platform="telegram", session_id="tg_1"
    )
    repair = IdentityRepair(store)

    report = await repair.analyse(platform_filter="AiocqHttp")
    assert [item["platform"] for item in report["platforms"]] == ["aiocqhttp"]

    outcome = await repair.repair(platform_filter="aiocqhttp")
    assert outcome["repaired"] == 1
    senders = [sender for sender, _ in await _assistant_senders(store)]
    assert senders[:4] == ["3385375303"] * 4
    assert senders[4:] == ["tg-bot", "tg-bot", "tg-bot", "tg-user"]

    await store.close()


@pytest.mark.asyncio
async def test_identity_repair_without_a_connection_reports_cleanly(tmp_path):
    """The command must degrade to a message instead of raising."""
    unavailable = {"ok": False, "reason": "conversation_store_unavailable"}
    repair = IdentityRepair(ConversationStore(str(tmp_path / "closed.db")))

    assert repair.connection is None
    assert await repair.analyse() == unavailable
    assert await repair.repair() == unavailable
    assert await repair.rollback() == unavailable
