"""Validate provider vectors before changing retrieval indexes."""

from typing import Any

import numpy as np


def validate_embedding_vector(value: Any, storage: Any) -> np.ndarray:
    """Require a finite vector compatible with both configured and loaded indexes."""
    vector = np.asarray(value, dtype=np.float32)
    if vector.ndim != 1 or not vector.size or not np.isfinite(vector).all():
        raise ValueError("Embedding 返回无效向量，保留旧索引并等待重试")  # noqa: RUF001 -- Preserve the existing localized API diagnostic.
    dimensions = (
        getattr(storage, "dimension", None),
        getattr(getattr(storage, "index", None), "d", None),
    )
    if any(
        isinstance(dimension, int) and dimension > 0 and vector.size != dimension
        for dimension in dimensions
    ):
        raise ValueError("Embedding 维度与现有索引不一致，请执行完整索引重建")  # noqa: RUF001 -- Preserve the existing localized API diagnostic.
    return vector
