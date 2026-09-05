"""
向量检索器 - 基于Faiss的向量密集检索
封装AstrBot的FaissVecDB,提供统一的检索接口
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB

_TRUNCATED_CONTENT_MARKER = "\n...[中间内容已截断]...\n"


@dataclass
class VectorResult:
    """向量检索结果"""

    doc_id: int
    score: float
    content: str
    metadata: dict[str, Any]


async def delete_faiss_documents_by_ids(
    faiss_db,
    doc_ids: list[int],
) -> list[int] | None:
    """Delete known FAISS document IDs with one index persistence operation.

    Returns ``None`` when the installed AstrBot storage does not expose the
    required bulk-capable primitives, allowing callers to use a compatibility
    fallback.
    """
    if not doc_ids:
        return []

    embedding_storage = getattr(faiss_db, "embedding_storage", None)
    document_storage = getattr(faiss_db, "document_storage", None)
    embedding_delete = getattr(embedding_storage, "delete", None)
    get_documents = getattr(document_storage, "get_documents", None)
    delete_by_uuid = getattr(document_storage, "delete_document_by_doc_id", None)
    if not (
        callable(embedding_delete)
        and callable(get_documents)
        and callable(delete_by_uuid)
    ):
        return None

    unique_ids = list(dict.fromkeys(int(doc_id) for doc_id in doc_ids))
    documents = await get_documents(
        metadata_filters={},
        ids=unique_ids,
        offset=0,
        limit=len(unique_ids),
    )
    deletable = [doc for doc in documents if doc.get("doc_id")]
    if not deletable:
        return []

    found_ids = [int(doc["id"]) for doc in deletable]
    await embedding_delete(found_ids)
    for document in deletable:
        await delete_by_uuid(document["doc_id"])
    return found_ids


class VectorRetriever:
    """
    向量密集检索器

    封装AstrBot的FaissVecDB,提供统一的向量相似度检索接口。
    主要特性:
    1. 保持查询文本原样检索，避免额外预处理带来的行为分叉
    2. 元数据包含:importance, create_time, last_access_time, session_id, persona_id
    3. 相似度分数已归一化到[0,1]区间
    4. 支持通过metadata过滤session_id和persona_id
    5. ID映射缓存优化UUID查询性能
    """

    def __init__(
        self,
        faiss_db: FaissVecDB,
        config: dict[str, Any] | None = None,
    ):
        """
        初始化向量检索器

        Args:
            faiss_db: FaissVecDB实例
            config: 配置字典(可选)
        """
        self.faiss_db = faiss_db
        self.config = config or {}

        # 优化3: ID映射缓存 (int_id -> uuid)。
        # 用 OrderedDict 实现真 LRU：原实现满 1000 条后直接停止写入，
        # 长期运行后新 id 永远进不了缓存，等于缓存完全失效。
        self._id_cache: OrderedDict[int, str] = OrderedDict()
        try:
            self._cache_max_size = max(0, int(self.config.get("id_cache_size", 1000)))
        except (TypeError, ValueError):
            self._cache_max_size = 1000

    @staticmethod
    def _fit_content_for_embedding(content: str, max_chars: int) -> str:
        """Keep both the opening context and tail conclusion within a char budget."""
        if len(content) <= max_chars:
            return content

        if max_chars <= len(_TRUNCATED_CONTENT_MARKER):
            return content[:max_chars]

        available = max_chars - len(_TRUNCATED_CONTENT_MARKER)
        head_chars = available // 2
        tail_chars = available - head_chars
        return content[:head_chars] + _TRUNCATED_CONTENT_MARKER + content[-tail_chars:]

    async def add_document(
        self, content: str, metadata: dict[str, Any] | None = None
    ) -> int:
        """
        添加文档到向量库

        Args:
            content: 文档内容
            metadata: 文档元数据(必须包含:importance, create_time, last_access_time,
                     session_id, persona_id)

        Returns:
            int: 文档ID
        """
        # 确保metadata存在
        metadata = metadata or {}

        # 验证必需的元数据字段
        required_fields = [
            "importance",
            "create_time",
            "last_access_time",
            "session_id",
            "persona_id",
        ]
        for field in required_fields:
            if field not in metadata:
                # 提供默认值
                if field == "importance":
                    metadata[field] = 0.5
                elif field in ["create_time", "last_access_time"]:
                    import time

                    metadata[field] = time.time()
                else:  # session_id, persona_id
                    metadata[field] = None

        # 插入到Faiss向量库，同样截断过长内容防止 embedding token 超限
        _MAX_CONTENT_CHARS = 4000
        insert_content = content
        if len(insert_content) > _MAX_CONTENT_CHARS:
            from astrbot.api import logger as _logger

            _logger.warning(
                f"[VectorRetriever] 记忆内容过长 ({len(insert_content)} 字符)，"
                f"保留开头和结尾并压缩至 {_MAX_CONTENT_CHARS} 字符"
            )
            insert_content = self._fit_content_for_embedding(
                insert_content,
                _MAX_CONTENT_CHARS,
            )
        doc_id = await self.faiss_db.insert(content=insert_content, metadata=metadata)

        return doc_id

    async def search(
        self,
        query: str,
        k: int = 10,
        session_id: str | None = None,
        persona_id: str | None = None,
    ) -> list[VectorResult]:
        """
        执行向量相似度搜索

        Args:
            query: 查询字符串
            k: 返回的结果数量
            session_id: 会话ID过滤(可选)
            persona_id: 人格ID过滤(可选)

        Returns:
            List[VectorResult]: 向量检索结果,按相似度降序排列
        """
        if not query or not query.strip():
            return []

        processed_query = query

        # 防止 embedding API token 超限：截断过长的查询文本
        # 大多数 embedding 模型限制在 8192 tokens 以内，按字符数保守截断
        _MAX_QUERY_CHARS = 2000
        if len(processed_query) > _MAX_QUERY_CHARS:
            from astrbot.api import logger as _logger

            _logger.warning(
                f"[VectorRetriever] 查询文本过长 ({len(processed_query)} 字符)，"
                f"截断至 {_MAX_QUERY_CHARS} 字符以避免 token 超限"
            )
            processed_query = processed_query[:_MAX_QUERY_CHARS]

        # 构建元数据过滤器
        metadata_filters = {}
        if session_id is not None:
            metadata_filters["session_id"] = session_id
        if persona_id is not None:
            metadata_filters["persona_id"] = persona_id

        # 执行向量检索
        # fetch_k设置为k*2以确保过滤后有足够的结果
        fetch_k = k * 4 if metadata_filters else k * 2

        faiss_results = await self.faiss_db.retrieve(
            query=processed_query,
            k=k,
            fetch_k=fetch_k,
            rerank=False,
            metadata_filters=metadata_filters if metadata_filters else None,
        )

        # 转换为VectorResult格式
        results = []
        for result in faiss_results:
            # FaissVecDB返回的Result对象包含similarity和data
            # data是包含id, text, metadata的字典
            doc_data = result.data
            metadata = doc_data.get("metadata")
            if isinstance(metadata, dict) and str(
                metadata.get("status") or "active"
            ) != "active":
                continue
            results.append(
                VectorResult(
                    doc_id=doc_data["id"],
                    score=result.similarity,  # FaissVecDB已经归一化到[0,1]
                    content=doc_data["text"],
                    metadata=doc_data["metadata"],
                )
            )

        return results[:k]

    async def _get_uuid_from_id(self, doc_id: int) -> str | None:
        """
        获取文档的UUID（带缓存优化）

        Args:
            doc_id: 整数文档ID

        Returns:
            Optional[str]: UUID字符串，如果不存在返回None
        """
        # 优化3: 先查缓存（命中时刷新 LRU 顺序）
        cached = self._id_cache.get(doc_id)
        if cached is not None:
            self._id_cache.move_to_end(doc_id)
            return cached

        from astrbot.api import logger

        try:
            doc_storage = self.faiss_db.document_storage
            docs = await doc_storage.get_documents(
                metadata_filters={}, ids=[doc_id], limit=1
            )

            if not docs or len(docs) == 0:
                return None

            uuid_doc_id = docs[0].get("doc_id")

            # 更新缓存：满了淘汰最久未使用项，而不是停止缓存
            if uuid_doc_id and self._cache_max_size > 0:
                self._id_cache[doc_id] = uuid_doc_id
                self._id_cache.move_to_end(doc_id)
                while len(self._id_cache) > self._cache_max_size:
                    self._id_cache.popitem(last=False)

            return uuid_doc_id

        except Exception as e:
            logger.error(f"[UUID查询] 失败 (doc_id={doc_id}): {e}")
            return None

    async def update_metadata(self, doc_id: int, metadata: dict[str, Any]) -> bool:
        """
        更新文档元数据（使用ORM方式）

        Args:
            doc_id: 文档ID (整数 id)
            metadata: 新的元数据字典

        Returns:
            bool: 是否成功更新
        """
        import json

        from astrbot.api import logger

        try:
            doc_storage = self.faiss_db.document_storage

            # 通过 id 获取文档
            docs = await doc_storage.get_documents(
                metadata_filters={}, ids=[doc_id], limit=1
            )

            if not docs or len(docs) == 0:
                logger.warning(f"[元数据更新] 文档不存在 (doc_id={doc_id})")
                return False

            doc = docs[0]

            # 获取当前元数据并更新
            current_metadata_str = doc.get("metadata", "{}")
            if isinstance(current_metadata_str, str):
                try:
                    current_metadata = json.loads(current_metadata_str)
                except (json.JSONDecodeError, TypeError):
                    current_metadata = {}
            else:
                current_metadata = current_metadata_str or {}

            # 合并新元数据
            current_metadata.update(metadata)

            # 优化2: 使用参数化查询确保SQL安全
            async with doc_storage.get_session() as session, session.begin():
                from sqlalchemy import text

                # 使用参数化查询，避免SQL注入
                stmt = text("UPDATE documents SET metadata = :metadata WHERE id = :id")
                await session.execute(
                    stmt,
                    {
                        "metadata": json.dumps(current_metadata, ensure_ascii=False),
                        "id": doc_id,
                    },
                )

            logger.debug(f"[元数据更新] 成功 (doc_id={doc_id})")
            return True

        except Exception as e:
            from astrbot.api import logger

            logger.error(f"[元数据更新] 失败 (doc_id={doc_id}): {e}", exc_info=True)
            return False

    async def delete_document(self, doc_id: int) -> bool:
        """
        删除文档（修复版：正确使用 FaissVecDB.delete API + 缓存优化）

        Args:
            doc_id: 文档ID (documents表中的整数id)

        Returns:
            bool: 是否成功删除
        """
        from astrbot.api import logger

        try:
            # 优化3: 使用缓存的UUID查询方法
            uuid_doc_id = await self._get_uuid_from_id(doc_id)

            if not uuid_doc_id:
                logger.warning(f"[向量删除] 文档不存在或缺少UUID (doc_id={doc_id})")
                return False

            # 使用 UUID 调用 FaissVecDB.delete()
            # 这会同时删除 document_storage 和 embedding_storage
            await self.faiss_db.delete(uuid_doc_id)

            # 从缓存中移除
            self._id_cache.pop(doc_id, None)

            logger.debug(f"[向量删除] 成功删除 (doc_id={doc_id}, uuid={uuid_doc_id})")
            return True

        except Exception as e:
            from astrbot.api import logger

            logger.error(f"[向量删除] 失败 (doc_id={doc_id}): {e}", exc_info=True)
            return False

    async def delete_documents(self, doc_ids: list[int]) -> list[int]:
        """Delete multiple vector documents with one index save when supported."""
        deleted_ids = await delete_faiss_documents_by_ids(self.faiss_db, doc_ids)
        if deleted_ids is None:
            deleted_ids = []
            for doc_id in dict.fromkeys(doc_ids):
                if await self.delete_document(doc_id):
                    deleted_ids.append(doc_id)
        if len(deleted_ids) != len(set(doc_ids)):
            missing = sorted(set(doc_ids) - set(deleted_ids))
            raise RuntimeError(f"批量向量删除未找到文档: {missing}")

        for doc_id in deleted_ids:
            self._id_cache.pop(doc_id, None)
        return deleted_ids

    async def find_similar_pairs(
        self,
        doc_ids: list[int],
        threshold: float,
        k: int = 5,
        batch_size: int = 1024,
    ) -> list[tuple[int, int, float]]:
        """在给定 doc_ids 之间批量查找相似对（供记忆整合的语义聚类使用）。

        直接复用索引中已存储的向量（index.reconstruct），不重复调用 Embedding API，
        并用 Faiss 的批量搜索在候选间找相似对，可扩展到上万条记忆。

        Bug H: faiss 是 CPU 密集的 C 扩展，向量重建与批量搜索会整体卸载到一次
        ``asyncio.to_thread``；此前逐条 reconstruct + 同步 search 直接跑在事件循环里，
        上万条候选会把整个 bot 卡死。行为（返回值、异常、排序）与改造前完全一致。

        Args:
            doc_ids: 参与聚类的文档 id 列表。
            threshold: 相似度阈值（余弦近似，与 retrieve 归一化一致）。
            k: 每个向量查找的邻居数。
            batch_size: 每次批量搜索的查询向量数（控制内存峰值）。

        Returns:
            [(a, b, similarity), ...]，其中 a < b 且 similarity >= threshold，去重。
        """
        unique_ids = list(dict.fromkeys(int(doc_id) for doc_id in doc_ids))
        if len(unique_ids) < 2:
            return []

        index = self.faiss_db.embedding_storage.index
        if index is None:
            return []

        # 前置校验留在事件循环（纯 Python、O(n)），faiss 相关计算整体进线程池
        return await asyncio.to_thread(
            self._find_similar_pairs_sync,
            index,
            unique_ids,
            threshold,
            k,
            batch_size,
        )

    @staticmethod
    def _reconstruct_vectors(
        index: Any, unique_ids: list[int]
    ) -> tuple[list[Any], list[int]]:
        """重建候选向量：优先一次批量 API，不可用/失败时退化为逐条重建。

        本机 faiss 1.13.2 上可用的批量接口是 ``reconstruct_batch(int64 ids)``，
        返回 ``(n, d)`` 的 ndarray；只要有一个 id 缺失就会整批抛 RuntimeError，
        所以必须保留逐条回退分支。``reconstruct_n`` 在 ``IndexIDMap2`` 上会导致
        进程硬崩溃（0xC0000409），一律不用。

        Returns:
            (vectors, valid_ids)，两者一一对应；缺失向量的 id 会被跳过。
        """
        import numpy as np

        from astrbot.api import logger as _logger

        batch_fn = getattr(index, "reconstruct_batch", None)
        if callable(batch_fn):
            try:
                batch = batch_fn(np.asarray(unique_ids, dtype="int64"))
            except Exception:
                _logger.debug(
                    "[VectorRetriever] reconstruct_batch 失败，退化为逐条重建",
                    exc_info=True,
                )
            else:
                # 严格校验形状：Mock / 老版本可能返回任意对象，错位会污染聚类结果
                if (
                    isinstance(batch, np.ndarray)
                    and batch.ndim == 2
                    and batch.shape[0] == len(unique_ids)
                ):
                    return (
                        [batch[i] for i in range(batch.shape[0])],
                        list(unique_ids),
                    )
                _logger.debug(
                    "[VectorRetriever] reconstruct_batch 返回形状异常，退化为逐条重建"
                )

        vectors: list[Any] = []
        valid_ids: list[int] = []
        for doc_id in unique_ids:
            try:
                vectors.append(index.reconstruct(doc_id))
                valid_ids.append(doc_id)
            except Exception:
                # 向量缺失（如从未写入或已删除）的文档跳过，不参与语义聚类
                _logger.debug(
                    f"[VectorRetriever] 向量重建失败，跳过 (doc_id={doc_id})",
                    exc_info=True,
                )
                continue
        return vectors, valid_ids

    def _find_similar_pairs_sync(
        self,
        index: Any,
        unique_ids: list[int],
        threshold: float,
        k: int,
        batch_size: int,
    ) -> list[tuple[int, int, float]]:
        """find_similar_pairs 的同步实现，整体在线程池里执行（Bug H）。"""
        import numpy as np

        from astrbot.api import logger as _logger

        vectors, valid_ids = self._reconstruct_vectors(index, unique_ids)
        if len(valid_ids) < 2:
            return []

        pairs: list[tuple[int, int, float]] = []
        seen: set[tuple[int, int]] = set()
        valid_id_set = set(valid_ids)

        for start in range(0, len(valid_ids), batch_size):
            chunk_ids = valid_ids[start : start + batch_size]
            matrix = np.stack(vectors[start : start + batch_size]).astype("float32")
            try:
                scores, indices = index.search(matrix, k + 1)
            except Exception as e:
                _logger.warning(f"[VectorRetriever] 批量向量检索失败: {e}")
                continue
            similarities = 1.0 - scores / 2.0
            for i, doc_id in enumerate(chunk_ids):
                for j in range(indices.shape[1]):
                    nb_id = int(indices[i][j])
                    if nb_id < 0 or nb_id == doc_id or nb_id not in valid_id_set:
                        continue
                    sim = float(similarities[i][j])
                    if sim < threshold:
                        continue
                    a, b = (doc_id, nb_id) if doc_id < nb_id else (nb_id, doc_id)
                    if (a, b) not in seen:
                        seen.add((a, b))
                        pairs.append((a, b, sim))

        return pairs
