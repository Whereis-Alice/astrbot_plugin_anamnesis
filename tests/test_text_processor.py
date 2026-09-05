"""
Tests for TextProcessor behaviors.
"""

import threading
from pathlib import Path

import astrbot_plugin_anamnesis.core.processors.text_processor as text_processor_mod
import pytest
from astrbot_plugin_anamnesis.core.processors.text_processor import TextProcessor
from astrbot_plugin_anamnesis.core.utils.stopwords_manager import StopwordsManager


def test_tokenize_handles_empty_and_basic_cleaning():
    processor = TextProcessor()
    assert processor.tokenize("") == []
    assert processor.tokenize("   ") == []

    tokens = processor.tokenize("Visit https://example.com now!!!")
    # URL/punctuation should be cleaned; keep meaningful tokens.
    assert "Visit" in tokens or "visit" in [t.lower() for t in tokens]


def test_tokenize_removes_common_stopwords():
    processor = TextProcessor()
    # 先加载停用词，否则 remove_stopwords 无效果
    processor.add_stopwords(["我"])

    tokens = processor.tokenize("我 今天 去 图书馆", remove_stopwords=True)
    # "我" 是停用词，应被移除
    assert "我" not in tokens
    assert len(tokens) >= 1


@pytest.mark.asyncio
async def test_load_stopwords_and_custom_words(tmp_path: Path):
    processor = TextProcessor()
    path = tmp_path / "stopwords.txt"
    path.write_text("# comment\nalpha\nbeta\n", encoding="utf-8")

    loaded = await processor.load_stopwords(str(path))
    assert "alpha" in loaded
    assert processor.is_stopword("alpha")

    processor.add_stopwords(["gamma"])
    assert processor.is_stopword("gamma")
    processor.remove_stopwords_from_list(["gamma"])
    assert not processor.is_stopword("gamma")


def test_preprocess_for_bm25_and_word_freq():
    processor = TextProcessor()
    processed = processor.preprocess_for_bm25("编程 很 有趣，编程 真 快乐")
    assert isinstance(processed, str)
    assert len(processed) > 0

    freq = processor.get_word_freq(["我 爱 编程", "编程 很 有趣"])
    assert isinstance(freq, dict)
    assert len(freq) > 0


def test_tokenize_falls_back_when_jieba_runtime_fails(monkeypatch):
    class BrokenJieba:
        @staticmethod
        def cut_for_search(text):
            raise AttributeError(
                "module 'pkg_resources' has no attribute 'resource_stream'"
            )

    monkeypatch.setattr(text_processor_mod, "JIEBA_AVAILABLE", True)
    monkeypatch.setattr(text_processor_mod, "JIEBA_RUNTIME_DISABLED", False)
    monkeypatch.setattr(text_processor_mod, "jieba", BrokenJieba)

    processor = TextProcessor()
    with pytest.warns(UserWarning, match="jieba 分词初始化失败"):
        tokens = processor.tokenize("编程快乐")

    assert tokens
    assert "编" in tokens
    assert text_processor_mod.JIEBA_RUNTIME_DISABLED is True


@pytest.mark.asyncio
async def test_stopwords_manager_materializes_fallback_when_builtin_missing(
    tmp_path: Path,
):
    manager = StopwordsManager(str(tmp_path))
    manager.builtin_stopwords_dir = tmp_path / "missing"

    stopwords_path = await manager.get_stopwords()
    loaded = await manager.load_stopwords()

    assert stopwords_path is not None
    assert Path(stopwords_path).exists()
    assert "的" in loaded


@pytest.mark.asyncio
async def test_text_processor_async_init_loads_builtin_stopwords(tmp_path: Path):
    processor = TextProcessor(str(tmp_path))

    await processor.async_init()

    assert processor.is_stopword("的")
    assert not (tmp_path / "stopwords_hit.txt").exists()

# ── Bug H：CPU 密集分词的异步包装（避免阻塞事件循环）────────────────────────


def _track_worker_thread(processor: TextProcessor, name: str) -> list[int]:
    """把实例上的同步方法换成记录执行线程的包装，返回线程 id 收集列表。"""
    original = getattr(processor, name)
    seen: list[int] = []

    def _wrapper(*args, **kwargs):
        seen.append(threading.get_ident())
        return original(*args, **kwargs)

    setattr(processor, name, _wrapper)
    return seen


@pytest.mark.asyncio
async def test_tokenize_async_runs_in_worker_thread():
    processor = TextProcessor()
    text = "我今天去图书馆看了一本很有趣的书"
    expected = processor.tokenize(text)

    seen = _track_worker_thread(processor, "tokenize")
    actual = await processor.tokenize_async(text)

    assert actual == expected
    assert len(seen) == 1
    assert seen[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_tokenize_batch_async_offloads_whole_batch_once():
    processor = TextProcessor()
    texts = ["我今天去图书馆看书", "编程很有趣", "hello async world"]
    expected = processor.tokenize_batch(texts)

    seen = _track_worker_thread(processor, "tokenize_batch")
    actual = await processor.tokenize_batch_async(texts)

    assert actual == expected
    assert len(seen) == 1, "整批只允许一次线程池卸载"
    assert seen[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_preprocess_for_bm25_async_matches_sync():
    processor = TextProcessor()
    text = "我今天去图书馆看了一本很有趣的书"
    expected = processor.preprocess_for_bm25(text)

    seen = _track_worker_thread(processor, "preprocess_for_bm25")
    actual = await processor.preprocess_for_bm25_async(text)

    assert actual == expected
    assert len(seen) == 1
    assert seen[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_get_word_freq_async_matches_sync():
    processor = TextProcessor()
    texts = ["我爱编程", "编程很有趣", "我也爱学习"]
    expected = processor.get_word_freq(texts)

    seen = _track_worker_thread(processor, "get_word_freq")
    actual = await processor.get_word_freq_async(texts)

    assert actual == expected
    assert len(seen) == 1
    assert seen[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_async_wrappers_keep_remove_stopwords_flag():
    processor = TextProcessor()
    processor.add_stopwords(["编程"])
    texts = ["我爱编程"]

    kept = await processor.tokenize_batch_async(texts, remove_stopwords=False)
    dropped = await processor.tokenize_batch_async(texts, remove_stopwords=True)

    assert "编程" in kept[0]
    assert "编程" not in dropped[0]
