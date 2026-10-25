"""Real FAISS path bridges and failures must preserve the previous index."""

import os
from pathlib import Path
from types import SimpleNamespace

import faiss
import numpy as np
import pytest
from astrbot_plugin_anamnesis.core import faiss_async_persist, plugin_initializer_faiss
from astrbot_plugin_anamnesis.core.utils import faiss_path_bridge as bridge


def _index():
    index = faiss.IndexIDMap(faiss.IndexFlatL2(3))
    index.add_with_ids(np.eye(3, dtype=np.float32), np.arange(3, dtype=np.int64))
    return index


def _ascii_faiss(monkeypatch):
    real_read, real_write = faiss.read_index, faiss.write_index
    paths = {"read": [], "write": []}

    def reader(path, *args, **kwargs):
        assert os.fsdecode(path).isascii()
        paths["read"].append(str(path))
        return real_read(path, *args, **kwargs)

    def writer(index, path, *args, **kwargs):
        assert os.fsdecode(path).isascii()
        paths["write"].append(str(path))
        return real_write(index, path, *args, **kwargs)

    monkeypatch.setattr(faiss, "read_index", reader)
    monkeypatch.setattr(faiss, "write_index", writer)
    monkeypatch.setattr(bridge, "needs_bridge", lambda path: not os.fsdecode(path).isascii())
    return paths


def test_ascii_process_temp_is_preferred_over_system_temp(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge.tempfile, "gettempdir", lambda: str(tmp_path))

    assert bridge.safe_temp_dir() == str(tmp_path)


@pytest.mark.parametrize("writer", [bridge.write_index_safe, faiss_async_persist._write_index_atomic])
def test_non_ascii_round_trip_without_global_patch(tmp_path, monkeypatch, writer):
    paths = _ascii_faiss(monkeypatch)
    target = tmp_path / "中文索引目录" / "memory.index"
    index = _index()

    writer(index, target)
    loaded = bridge.read_index_safe(target)

    assert loaded.ntotal == 3
    assert loaded.d == 3
    assert paths["read"] and paths["write"]
    assert all(not Path(path).exists() for path in paths["read"] + paths["write"])
    assert list(target.parent.iterdir()) == [target]


def test_failed_destination_copy_cannot_overwrite_existing_index(tmp_path, monkeypatch):
    _ascii_faiss(monkeypatch)
    target = tmp_path / "中文目录" / "memory.index"
    target.parent.mkdir()
    target.write_bytes(b"healthy-index")
    copied_into = []

    def interrupted_copy(source, destination):
        copied_into.append(str(destination))
        Path(destination).write_bytes(b"half-written")
        raise OSError("simulated cross-volume copy failure")

    monkeypatch.setattr(bridge.shutil, "copy2", interrupted_copy)
    with pytest.raises(OSError, match="cross-volume"):
        bridge.write_index_safe(_index(), target)

    assert target.read_bytes() == b"healthy-index"
    assert copied_into and str(target) not in copied_into
    assert list(target.parent.iterdir()) == [target]


def test_replacement_is_always_inside_destination_directory(tmp_path, monkeypatch):
    _ascii_faiss(monkeypatch)
    target = tmp_path / "中文目录" / "memory.index"
    real_replace = os.replace
    replacements = []

    def same_volume_only(source, destination):
        assert Path(source).parent == Path(destination).parent
        replacements.append((source, destination))
        real_replace(source, destination)

    monkeypatch.setattr(bridge.os, "replace", same_volume_only)
    bridge.write_index_safe(_index(), target)

    assert replacements
    assert bridge.read_index_safe(target).ntotal == 3


def test_ascii_write_failure_also_preserves_existing_index(tmp_path, monkeypatch):
    target = tmp_path / "memory.index"
    target.write_bytes(b"healthy-index")

    def interrupted_write(index, path):
        Path(path).write_bytes(b"incomplete")
        raise RuntimeError("write failed")

    monkeypatch.setattr(faiss, "write_index", interrupted_write)
    with pytest.raises(RuntimeError, match="write failed"):
        bridge.write_index_safe(_index(), target)

    assert target.read_bytes() == b"healthy-index"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("cached", [False, True])
def test_initializer_installs_bridge_before_returning_class(tmp_path, monkeypatch, cached):
    paths = _ascii_faiss(monkeypatch)
    original_read, original_write = faiss.read_index, faiss.write_index
    monkeypatch.setattr(faiss, "read_index", original_read)
    monkeypatch.setattr(faiss, "write_index", original_write)
    host = __import__("astrbot.core.db.vec_db.faiss_impl.vec_db", fromlist=["FaissVecDB"])
    expected = type("FakeFaissVecDB", (), {})
    monkeypatch.setattr(host, "FaissVecDB", expected)
    monkeypatch.setattr(plugin_initializer_faiss, "FaissVecDB", expected if cached else None)
    monkeypatch.setattr(plugin_initializer_faiss, "_needs_bridge", bridge.needs_bridge)
    initializer = SimpleNamespace(_check_faiss_runtime=lambda: None)

    actual = plugin_initializer_faiss.InitializerFaissMixin._load_faiss_vec_db_class(initializer)

    assert actual is expected
    assert bridge.bridge_installed()
    target = tmp_path / "中文目录" / "memory.index"
    faiss.write_index(_index(), str(target))
    assert faiss.read_index(str(target)).ntotal == 3
    assert paths["read"] and paths["write"]


def test_private_helper_aliases_remain_available():
    assert plugin_initializer_faiss._needs_bridge is bridge.needs_bridge
    assert plugin_initializer_faiss._safe_temp_dir is bridge.safe_temp_dir
    assert plugin_initializer_faiss._make_temp_file is bridge.safe_temp_file
