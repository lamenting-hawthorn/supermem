"""Embedding providers and vector-backend selection.

Single source of truth for:

- ``get_embedder()`` — resolves the active embedding provider
  (fastembed / OpenAI-compatible local endpoint) into an
  ``(identity, embedding_function)`` pair shared by ALL vector backends.
  Identity dicts always carry ``provider`` + ``model`` (+ ``dim`` when known)
  so receipts are comparable across backends.
- ``create_vector_manager()`` — picks the vector backend from
  SUPERMEM_VECTOR_BACKEND ("" → auto: sqlite-vec if importable, else chroma
  if importable, else an always-unavailable manager).

Embedding-provider semantics:
- provider "" (default): fastembed when importable, else no explicit
  provider. Chroma then falls back to its built-in ONNX MiniLM; the sqlite
  backend has no built-in embedder, so it reports available=False.
- provider "fastembed": same as above but a missing/failing fastemit install
  is treated as an error (warn once, report unavailable).
- provider "local-endpoint": sync OpenAI client POSTs to
  {SUPERMEM_EMBEDDING_BASE_URL}/embeddings; dim is inferred from the first
  real response (no network probe at construction) and attached to persisted
  identities by the backends once known.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from supermem.config import (
    DEFAULT_FASTEMBED_MODEL,
    SUPERMEM_EMBEDDING_BASE_URL,
    embedding_model_from_env,
    embedding_provider_from_env,
    vector_backend_from_env,
)
from supermem.logging import get_logger

log = get_logger(__name__)

_LEGACY_IDENTITY: dict[str, Any] = {"provider": "unknown-legacy"}

_fallback_warned = False


# ── Identity persistence helpers ─────────────────────────────────────────────


def format_identity(identity: Mapping[str, Any]) -> str:
    """Canonical JSON string used for persistence and equality comparison."""
    return json.dumps(dict(identity), sort_keys=True, separators=(",", ":"))


def parse_stored_identity(raw: Any) -> dict[str, Any]:
    """Parse a persisted identity string; anything unusable reports legacy."""
    if raw:
        try:
            data = json.loads(str(raw))
            if isinstance(data, dict) and data.get("provider"):
                return data
        except (TypeError, ValueError):
            pass
    return dict(_LEGACY_IDENTITY)


def identity_matches(
    identity_a: Mapping[str, Any] | None,
    identity_b: Mapping[str, Any] | None,
) -> bool:
    """True when both identities are present and canonically equal.

    Seam for callers that must detect mismatched stores before mixing
    vectors from different embedding models.
    """
    if not identity_a or not identity_b:
        return False
    return format_identity(identity_a) == format_identity(identity_b)


# ── Embedding providers ──────────────────────────────────────────────────────


class FastembedEmbeddingFunction:
    """Adapter exposing fastembed's TextEmbedding via the shared
    embedding-function protocol: callable taking documents, returning
    lists of float vectors."""

    def __init__(self, model: Any) -> None:
        self._model = model

    def __call__(self, input: Any) -> list[list[float]]:  # noqa: A002
        docs = list(input)
        return [[float(x) for x in vec] for vec in self._model.embed(docs)]

    def query_embed(self, input: Any) -> list[list[float]]:  # noqa: A002
        """Query-side embedding for asymmetric retrieval.

        fastembed's ``query_embed`` prepends the model's retrieval
        instruction prefix (e.g. BGE's "Represent this sentence for
        searching relevant passages: ...") — embedding queries with plain
        ``embed()`` loses that signal and badly degrades question→document
        ranking. Falls back to plain ``embed()`` when the model does not
        support a query path.
        """
        queries = list(input)
        embed_query = getattr(self._model, "query_embed", None)
        if callable(embed_query):
            return [[float(x) for x in vec] for vec in embed_query(queries)]
        return self(queries)


class LocalEndpointEmbeddingFunction:
    """OpenAI-compatible /embeddings client (LM Studio, Ollama serve, ...).

    Uses the sync ``openai`` client; callers wrap invocations in
    ``asyncio.to_thread`` to keep their APIs async. Inputs are batched into
    a single request. ``dim`` is set from the first successful response so
    backends can persist it with the embedding identity.
    """

    def __init__(self, base_url: str, model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._client: Any = None
        self.dim: int | None = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                base_url=self._base_url,
                api_key=os.getenv("OPENAI_API_KEY", "local-not-required"),
            )
        return self._client

    def __call__(self, input: Any) -> list[list[float]]:  # noqa: A002
        docs = [str(d) for d in input]
        resp = self._ensure_client().embeddings.create(model=self._model, input=docs)
        vecs = [[float(x) for x in item.embedding] for item in resp.data]
        if vecs and self.dim is None:
            self.dim = len(vecs[0])
        return vecs


def probe_embedding_dim(model: Any) -> int | None:
    """Best-effort output-dimension probe for a locally constructed model."""
    for attr in ("embedding_size", "dim"):
        val = getattr(model, attr, None)
        if isinstance(val, int):
            return val
    getter = getattr(model, "get_embedding_size", None)
    if callable(getter):
        try:
            return int(getter())
        except Exception:
            pass
    try:
        return len(next(iter(model.embed([""])))[0])
    except Exception:
        return None


def _build_fastembed(model_name: str) -> tuple[dict[str, Any], Any] | None:
    from fastembed import TextEmbedding

    fe_model = TextEmbedding(**({"model_name": model_name} if model_name else {}))
    identity: dict[str, Any] = {
        "provider": "fastembed",
        "model": model_name or DEFAULT_FASTEMBED_MODEL,
    }
    dim = probe_embedding_dim(fe_model)
    if dim is not None:
        identity["dim"] = dim
    return identity, FastembedEmbeddingFunction(fe_model)


def get_embedder(
    provider: str | None = None, model: str | None = None
) -> tuple[dict[str, Any], Any] | None:
    """Resolve the active embedder → ``(identity, embedding_function)``.

    Returns ``None`` when no explicit provider can be provided; each backend
    then applies its own fallback (Chroma built-in ONNX vs. unavailable).
    Never raises: failures constructing the requested provider log a
    structured warning once and report no provider.
    """
    global _fallback_warned

    if provider is None:
        provider = embedding_provider_from_env()
    if model is None:
        model = embedding_model_from_env()

    if provider == "local-endpoint":
        fn = LocalEndpointEmbeddingFunction(SUPERMEM_EMBEDDING_BASE_URL, model)
        # dim intentionally omitted until the first real response.
        return {"provider": "local-endpoint", "model": model}, fn

    # "" (auto) and "fastembed" both land here: use fastembed when possible.
    try:
        built = _build_fastembed(model)
        if built is not None:
            return built
    except Exception as exc:
        if not _fallback_warned:
            log.warning(
                "fastembed_unavailable",
                error=str(exc),
                requested_provider=provider,
                hint=(
                    "Install fastembed, or set "
                    "SUPERMEM_EMBEDDING_PROVIDER=local-endpoint with "
                    "SUPERMEM_EMBEDDING_BASE_URL/SUPERMEM_EMBEDDING_MODEL"
                ),
            )
            _fallback_warned = True
        else:
            log.debug("fastembed_unavailable", error=str(exc))
    return None


# ── Backend importability flags (monkeypatch targets for tests) ──────────────


def _import_sqlite_vec() -> Any:
    try:
        import sqlite_vec

        return sqlite_vec
    except ImportError:
        return None


def _import_chromadb() -> Any:
    try:
        import chromadb

        return chromadb
    except ImportError:
        return None


# ── Always-unavailable placeholder ───────────────────────────────────────────


class UnavailableVectorManager:
    """Drop-in manager that reports unavailable and degrades to no-ops.

    Returned by :func:`create_vector_manager` when neither backend is
    importable or the user explicitly selects "none".
    """

    def __init__(self, reason: str = "") -> None:
        self._reason = reason
        if reason:
            log.info("vector_backend_unavailable", reason=reason)

    def init(self) -> None:  # noqa: D102 — mirrors manager surface
        pass

    @property
    def available(self) -> bool:
        return False

    @property
    def active_identity(self) -> dict[str, Any]:
        return {}

    def embedding_identity(self) -> dict[str, Any]:
        return {}

    @staticmethod
    def embedding_matches(
        identity_a: Mapping[str, Any] | None,
        identity_b: Mapping[str, Any] | None,
    ) -> bool:
        return identity_matches(identity_a, identity_b)

    async def upsert_chunks(
        self,
        chunks: list[str],
        *,
        obs_id: int | None = None,
        source_uri: str | None = None,
    ) -> None:
        pass

    async def search(self, query: str, limit: int = 10) -> list[tuple[int, float]]:
        return []

    async def delete_by_source(self, source_uri: str) -> None:
        pass

    async def delete_obs(self, obs_id: int) -> None:
        pass


def default_vectors_db_path() -> Path:
    """vectors.db lives beside the main supermem.db (single backup dir)."""
    from supermem.config import SUPERMEM_VECTORS_PATH

    return Path(SUPERMEM_VECTORS_PATH)


# ── Factory ──────────────────────────────────────────────────────────────────


def create_vector_manager(backend: str | None = None) -> Any:
    """Construct the configured/selected vector manager.

    backend "" or None reads SUPERMEM_VECTOR_BACKEND: auto-selection prefers
    the sqlite backend (sqlite-vec importable), falls back to legacy chroma,
    and finally returns an always-unavailable manager. Explicit "sqlite" /
    "chroma" construct that backend directly (each degrades gracefully on its
    own); "none" forces the unavailable placeholder.
    """
    selected = vector_backend_from_env() if backend is None else backend.strip().lower()
    if selected == "none":
        return UnavailableVectorManager(reason="vector backend disabled")

    if selected in ("", "sqlite"):
        if _import_sqlite_vec() is not None:
            from supermem.storage.vector_sqlite import SqliteVecManager

            return SqliteVecManager()
        if selected == "sqlite":
            return UnavailableVectorManager(reason="sqlite-vec not installed")
        log.info("vector_backend_fallback", from_="sqlite", to="chroma")

    if _import_chromadb() is not None:
        from supermem.storage.vector import ChromaManager

        return ChromaManager()
    if selected == "chroma":
        return UnavailableVectorManager(reason="chromadb not installed")
    return UnavailableVectorManager(reason="no vector backend installed")
