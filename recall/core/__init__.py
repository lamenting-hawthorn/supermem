"""Core ABCs — the only contracts that cross layer boundaries."""
from recall.core.retriever import BaseRetriever, RetrievalResult
from recall.core.storage import BaseStorage
from recall.core.model_client import BaseModelClient
from recall.core.connector import BaseConnector

__all__ = [
    "BaseRetriever",
    "RetrievalResult",
    "BaseStorage",
    "BaseModelClient",
    "BaseConnector",
]
