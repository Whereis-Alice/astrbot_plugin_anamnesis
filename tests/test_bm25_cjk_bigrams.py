"""中文短实体查询在写入、编辑和重建后使用同一词元规则。"""

import json

import pytest
from astrbot_plugin_anamnesis.core.processors.text_processor import (
    TextProcessor,
    with_cjk_bigrams,
)
from astrbot_plugin_anamnesis.core.retrieval.bm25_retriever import BM25Retriever


def test_bigrams_keep_original_term_frequency_and_do_not_cross_boundaries():
    original = ["Alpha", "Alpha", "2026", "咖啡"]
    merged = with_cjk_bigrams(original, "杭州，喝咖啡 Alpha Alpha 2026")
    assert merged[: len(original)] == original
    assert "杭州" in merged and "喝咖" in merged
    assert "州喝" not in merged
    assert merged.count("咖啡") == 1
    assert with_cjk_bigrams(original, "Alpha Alpha 2026") == original
    assert original == ["Alpha", "Alpha", "2026", "咖啡"]


@pytest.mark.asyncio
async def test_short_entity_survives_edit_and_rebuild_with_scope_filtering(tmp_path):
    processor = TextProcessor()
    # 模拟索引侧把实体切成长词、查询侧只给短词的真实分词不一致。
    processor.tokenize = lambda text, remove_stopwords=True: [text]
    retriever = BM25Retriever(str(tmp_path / "bm25.db"), processor)
    await retriever.initialize()
    text = "耶加雪菲浅烘焙"
    async with retriever._connect() as db:
        await db.execute(
            "CREATE TABLE documents (id INTEGER PRIMARY KEY, text TEXT, metadata TEXT)"
        )
        for doc_id, session, status in [
            (1, "group-a", "active"),
            (2, "group-b", "active"),
            (3, "group-a", "archived"),
        ]:
            await db.execute(
                "INSERT INTO documents VALUES (?, ?, ?)",
                (
                    doc_id,
                    text,
                    json.dumps(
                        {"session_id": session, "persona_id": "p1", "status": status}
                    ),
                ),
            )
        await db.commit()
    for doc_id in [1, 2, 3]:
        await retriever.add_document(doc_id, text)

    async def matching_ids():
        return [
            r.doc_id
            for r in await retriever.search(
                "雪菲", session_id="group-a", persona_id="p1"
            )
        ]

    assert await matching_ids() == [1]
    assert await retriever.update_document(1, "耶加雪菲咖啡")
    assert await matching_ids() == [1]

    # 索引重建直接使用 TextProcessor.preprocess_for_bm25，规则必须对称。
    async with retriever._connect() as db:
        await db.execute(
            f"UPDATE {retriever.fts_table} SET content = ? WHERE doc_id = 1",
            (processor.preprocess_for_bm25(text),),
        )
        await db.commit()
    assert await matching_ids() == [1]


@pytest.mark.asyncio
async def test_alias_expansion_still_runs_after_query_bigrams(tmp_path):
    class Aliases:
        query_expansion = True

        async def expand_tokens(self, query, tokens, limit=12):
            assert query == "青羽"
            return ["星岚"]

    processor = TextProcessor()
    processor.tokenize = lambda text, remove_stopwords=True: [text]
    retriever = BM25Retriever(
        str(tmp_path / "aliases.db"), processor, alias_store=Aliases()
    )
    await retriever.initialize()
    async with retriever._connect() as db:
        await db.execute(
            "CREATE TABLE documents (id INTEGER PRIMARY KEY, text TEXT, metadata TEXT)"
        )
        await db.execute("INSERT INTO documents VALUES (1, '星岚', '{}')")
        await db.commit()
    await retriever.add_document(1, "星岚")
    assert [r.doc_id for r in await retriever.search("青羽")] == [1]
