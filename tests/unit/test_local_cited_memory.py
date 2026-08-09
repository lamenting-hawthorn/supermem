"""Focused local integration tests for the frozen BM-0 authority path."""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

import pytest

from supermem.local_cited_memory import (
    LocalCitedMemory,
    RetrievalQueryV1,
    RetrievalTimeoutError,
)


@pytest.fixture
def store(tmp_path: Path) -> LocalCitedMemory:
    instance = LocalCitedMemory(tmp_path / "bm0.sqlite")
    yield instance
    instance.close()


def query(text: str, **kwargs: object) -> RetrievalQueryV1:
    return RetrievalQueryV1(
        query_id="query-1", query=text, correlation_id="test", **kwargs
    )


def test_revision_lifecycle_citation_and_rebuild_are_authoritative(
    store: LocalCitedMemory,
) -> None:
    uri = "memory://tests/revision.md"
    first = store.ingest_markdown(uri, "orchid-stale")
    second = store.ingest_markdown(uri, "orchid-current")
    assert second.revision == first.revision + 1
    results = store.retrieve(query("orchid"))
    assert [item.content for item in results] == ["orchid-current"]
    assert store.verify_citation(results[0])
    for field, value in {
        "memory_id": "wrong-memory-id",
        "memory_revision": 999,
        "content": "tampered",
        "source_uri": "memory://tests/other.md",
        "source_revision": 999,
        "source_span": "chars:0:999",
        "source_digest": "0" * 64,
        "retrieval_tier": "not-fts",
    }.items():
        assert not store.verify_citation(replace(results[0], **{field: value}))
    store.rebuild_fts()
    assert [item.content for item in store.retrieve(query("orchid"))] == [
        "orchid-current"
    ]


def test_source_revision_evidence_is_sqlite_immutable_and_citation_fails_closed(
    store: LocalCitedMemory,
) -> None:
    revision = store.ingest_markdown("memory://tests/immutable.md", "immutable-cinder")
    result = store.retrieve(query("immutable-cinder"))[0]
    original = dict(
        store._conn.execute(
            "SELECT * FROM bm0_source_revisions WHERE source_id = ? AND revision = ?",
            (revision.source_id, revision.revision),
        ).fetchone()
    )
    for field, value in {
        "source_uri": "memory://tests/tampered.md",
        "content": "tampered",
        "content_digest": "0" * 64,
        "captured_at": 0,
        "source_span": "chars:0:0",
        "previous_revision": 999,
        "lifecycle_state": "deleted",
    }.items():
        with pytest.raises(Exception, match="immutable"):
            store._conn.execute(
                f"UPDATE bm0_source_revisions SET {field} = ? WHERE source_id = ? AND revision = ?",
                (value, revision.source_id, revision.revision),
            )
        store._conn.rollback()
        assert (
            dict(
                store._conn.execute(
                    "SELECT * FROM bm0_source_revisions WHERE source_id = ? AND revision = ?",
                    (revision.source_id, revision.revision),
                ).fetchone()
            )
            == original
        )
    with pytest.raises(Exception, match="immutable"):
        store._conn.execute(
            "DELETE FROM bm0_source_revisions WHERE source_id = ? AND revision = ?",
            (revision.source_id, revision.revision),
        )
    store._conn.rollback()
    assert store.verify_citation(result)
    store._conn.execute(
        "UPDATE bm0_sources SET current_revision = 999 WHERE source_id = ?",
        (revision.source_id,),
    )
    store._conn.commit()
    assert not store.verify_citation(result)


def test_source_revision_bytes_survive_revise_delete_and_retract(
    store: LocalCitedMemory,
) -> None:
    uri = "memory://tests/lifecycle-bytes.md"
    first = store.ingest_markdown(uri, "lifecycle-cinder-one")
    second = store.ingest_markdown(uri, "lifecycle-cinder-two")
    before = [
        dict(row)
        for row in store._conn.execute(
            "SELECT * FROM bm0_source_revisions WHERE source_id = ? ORDER BY revision",
            (first.source_id,),
        ).fetchall()
    ]
    assert store.retract(f"{second.source_id}:{second.revision}")
    assert store.delete_source(uri)
    after = [
        dict(row)
        for row in store._conn.execute(
            "SELECT * FROM bm0_source_revisions WHERE source_id = ? ORDER BY revision",
            (first.source_id,),
        ).fetchall()
    ]
    assert after == before


def test_delete_retract_expiry_and_effective_bounds_are_live(
    store: LocalCitedMemory,
) -> None:
    store.ingest_markdown("memory://tests/deleted.md", "deleted-cinder")
    assert store.delete_source("memory://tests/deleted.md")
    assert store.retrieve(query("deleted-cinder")) == []
    retracted = store.ingest_markdown("memory://tests/retracted.md", "retracted-cinder")
    assert store.retract(f"{retracted.source_id}:{retracted.revision}")
    assert store.retrieve(query("retracted-cinder")) == []
    store.ingest_markdown(
        "memory://tests/expiry.md", "expiry-cinder", expires_at=time.time() + 0.01
    )
    time.sleep(0.02)
    assert store.retrieve(query("expiry-cinder")) == []
    store.ingest_markdown(
        "memory://tests/interval.md",
        "interval-cinder",
        effective_from=10,
        effective_until=20,
    )
    assert len(store.retrieve(query("interval-cinder", temporal_bound=15))) == 1
    assert store.retrieve(query("interval-cinder", temporal_bound=21)) == []
    assert store.compression_enabled is False


def test_retract_after_source_delete_is_an_idempotent_noop(
    store: LocalCitedMemory,
) -> None:
    revision = store.ingest_markdown(
        "memory://tests/delete-then-retract.md", "deleted-state-cinder"
    )
    memory_id = f"{revision.source_id}:{revision.revision}"

    assert store.delete_source(revision.source_uri)
    events_before = store.source_events(revision.source_id)
    assert store.retract(memory_id)

    state = store._conn.execute(
        "SELECT lifecycle_state FROM bm0_memory_records WHERE memory_id = ?",
        (memory_id,),
    ).fetchone()[0]
    assert state == "deleted"
    assert store.source_events(revision.source_id) == events_before


def test_private_nested_and_unclosed_blocks_never_persist(
    store: LocalCitedMemory,
) -> None:
    store.ingest_markdown(
        "memory://tests/private.md",
        "public-cinder <private>private-canary <private>inner-canary</private> end</private> done <private>unclosed-canary",
    )
    assert store.retrieve(query("private-canary")) == []
    assert store.retrieve(query("unclosed-canary")) == []
    assert [item.content for item in store.retrieve(query("public-cinder"))] == [
        "public-cinder  done"
    ]


def test_stray_private_closer_fails_closed_at_local_persistence_boundary(
    store: LocalCitedMemory,
) -> None:
    store.ingest_markdown(
        "memory://tests/closer.md", "public-cinder </private> private-canary"
    )
    assert [item.content for item in store.retrieve(query("public-cinder"))] == [
        "public-cinder"
    ]
    assert store.retrieve(query("private-canary")) == []


def test_query_validation_unknown_and_idempotency(store: LocalCitedMemory) -> None:
    revision = store.ingest_markdown(
        "memory://tests/idempotent.md", "idempotent-cinder"
    )
    assert (
        store.ingest_markdown("memory://tests/idempotent.md", "idempotent-cinder")
        == revision
    )
    assert store.retrieve(query("unknown-zircon")) == []
    for invalid in ("", "x" * 1025):
        with pytest.raises(ValueError):
            query(invalid)
    with pytest.raises(ValueError):
        query("idempotent", max_records=101)
    with pytest.raises(ValueError):
        query("idempotent", timeout_ms=5001)
    with pytest.raises(ValueError):
        query("idempotent", scope="other")
    with pytest.raises(ValueError):
        store.ingest_markdown("file:///tmp/escape.md", "not allowed")
    with pytest.raises(ValueError):
        store.ingest_markdown("memory://tests/../escape.md", "not allowed")
    assert store.retrieve(query('" OR *')) == []


def test_file_ingest_is_contained_and_rejects_symlink_escape(
    store: LocalCitedMemory, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("file-cinder")
    revision = store.ingest_file(vault, Path("note.md"))
    assert revision.source_uri == "memory://vault/note.md"
    assert len(store.retrieve(query("file-cinder"))) == 1
    with pytest.raises(ValueError):
        store.ingest_file(vault, Path("../escape.md"))
    outside = tmp_path / "outside.md"
    outside.write_text("outside-cinder")
    (vault / "escape.md").symlink_to(outside)
    with pytest.raises(ValueError):
        store.ingest_file(vault, Path("escape.md"))


def test_file_ingest_percent_encodes_canonical_unicode_path(
    store: LocalCitedMemory, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    nested = vault / "Team Notes"
    nested.mkdir(parents=True)
    relative = Path("Team Notes/Meeting ü.md")
    (vault / relative).write_text("encoded-path-cinder")

    revision = store.ingest_file(vault, relative)

    assert revision.source_uri == "memory://vault/Team%20Notes/Meeting%20%C3%BC.md"
    assert len(store.retrieve(query("encoded-path-cinder"))) == 1
    with pytest.raises(ValueError, match="canonical memory"):
        store.ingest_markdown(
            "memory://vault/%2E%2E/escape.md", "encoded-traversal-cinder"
        )


def test_identical_content_with_changed_lifecycle_metadata_creates_revision(
    store: LocalCitedMemory,
) -> None:
    uri = "memory://tests/metadata-renewal.md"
    first = store.ingest_markdown(uri, "renewal-cinder", expires_at=1.0)
    second = store.ingest_markdown(uri, "renewal-cinder", expires_at=4_000_000_000.0)
    repeated = store.ingest_markdown(uri, "renewal-cinder", expires_at=4_000_000_000.0)

    assert second.revision == first.revision + 1
    assert repeated == second
    assert len(store.retrieve(query("renewal-cinder"))) == 1
    rows = store._conn.execute(
        "SELECT source_revision, expires_at, lifecycle_state FROM bm0_memory_records "
        "WHERE source_id = ? ORDER BY source_revision",
        (first.source_id,),
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (first.revision, 1.0, "superseded"),
        (second.revision, 4_000_000_000.0, "active"),
    ]


@pytest.mark.parametrize("field", ["effective_from", "effective_until", "expires_at"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_lifecycle_timestamps_are_rejected(
    store: LocalCitedMemory, field: str, value: float
) -> None:
    with pytest.raises(ValueError, match="finite"):
        store.ingest_markdown(
            f"memory://tests/non-finite-{field}.md",
            "non-finite-cinder",
            **{field: value},
        )

    with pytest.raises(ValueError, match="finite"):
        query("non-finite-cinder", temporal_bound=value)


def test_timeout_and_append_only_event_ledger(tmp_path: Path) -> None:
    clock_values = iter((0.0, 2.0, 3.0, 4.0))
    store = LocalCitedMemory(
        tmp_path / "timeout.sqlite", monotonic=lambda: next(clock_values)
    )
    try:
        first = store.ingest_markdown("memory://tests/events.md", "event-cinder-old")
        first_events = store.source_events(first.source_id)
        first_revision_row = store._conn.execute(
            "SELECT * FROM bm0_source_revisions WHERE source_id = ? AND revision = 1",
            (first.source_id,),
        ).fetchone()
        second = store.ingest_markdown("memory://tests/events.md", "event-cinder-new")
        events = store.source_events(first.source_id)
        assert events[: len(first_events)] == first_events
        assert dict(
            store._conn.execute(
                "SELECT * FROM bm0_source_revisions WHERE source_id = ? AND revision = 1",
                (first.source_id,),
            ).fetchone()
        ) == dict(first_revision_row)
        assert [event["event_type"] for event in events] == [
            "ingest",
            "supersede",
            "revise",
        ]
        with pytest.raises(Exception, match="append-only"):
            store._conn.execute("UPDATE bm0_source_events SET event_type = 'delete'")
        store._conn.rollback()
        assert store.retract(f"{second.source_id}:{second.revision}")
        assert store.source_events(first.source_id)[-1]["event_type"] == "retract"
        with pytest.raises(RetrievalTimeoutError):
            store.retrieve(query("event-cinder", timeout_ms=1))
    finally:
        store.close()
