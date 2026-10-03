"""The storage mixin contracts must describe, not own, their host state."""

from typing import get_type_hints

from astrbot_plugin_anamnesis.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_anamnesis.core.managers.memory_engine_batch import (
    MemoryEngineBatchMixin,
)
from astrbot_plugin_anamnesis.core.managers.memory_engine_crud import (
    MemoryEngineCrudMixin,
)
from astrbot_plugin_anamnesis.core.managers.memory_engine_write_ops import (
    MemoryEngineWriteOpsMixin,
)
from astrbot_plugin_anamnesis.storage.graph_store import GraphStore
from astrbot_plugin_anamnesis.storage.graph_store_read import GraphStoreReadMixin
from astrbot_plugin_anamnesis.storage.graph_store_write import GraphStoreWriteMixin


def test_memory_engine_mixin_state_is_declared_but_owned_by_host(tmp_path):
    engine = MemoryEngine(str(tmp_path / "memory.db"), faiss_db=object())

    for mixin in (
        MemoryEngineBatchMixin,
        MemoryEngineCrudMixin,
        MemoryEngineWriteOpsMixin,
    ):
        fields = get_type_hints(mixin)
        assert fields
        assert fields.keys() <= vars(engine).keys()
        # An annotation must not shadow an instance value or alter MRO lookup.
        assert not (fields.keys() & (mixin.__dict__.keys() - {"__annotations__"}))


def test_graph_store_mixin_contract_uses_host_values(tmp_path):
    store = GraphStore(str(tmp_path / "graph.db"))

    assert get_type_hints(GraphStoreReadMixin) == {
        "_NODE_TOKEN_QUERY_BATCH_SIZE": int
    }
    assert get_type_hints(GraphStoreWriteMixin) == {
        "_SQLITE_BATCH_SIZE": int,
        "person_alias_limit": int,
    }
    for mixin in (GraphStoreReadMixin, GraphStoreWriteMixin):
        assert not (
            mixin.__annotations__.keys() & (mixin.__dict__.keys() - {"__annotations__"})
        )
    assert store._NODE_TOKEN_QUERY_BATCH_SIZE == 200
    assert store._SQLITE_BATCH_SIZE == 500
    assert store.person_alias_limit == 40
