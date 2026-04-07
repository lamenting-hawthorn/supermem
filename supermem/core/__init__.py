"""Core ABCs — the only contracts that cross layer boundaries."""

from supermem.core.retriever import BaseRetriever, RetrievalResult
from supermem.core.storage import BaseStorage
from supermem.core.model_client import BaseModelClient
from supermem.core.connector import BaseConnector

__all__ = [
    "BaseRetriever",
    "RetrievalResult",
    "BaseStorage",
    "BaseModelClient",
    "BaseConnector",
]
