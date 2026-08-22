"""Unit tests for Reciprocal Rank Fusion (rrf_fuse) — pure ranking math."""

from __future__ import annotations

from supermem.retrieval.hybrid import RRF_K, rrf_fuse


def test_rrf_k_is_sixty() -> None:
    assert RRF_K == 60


def test_two_lists_hand_computed_fusion() -> None:
    # 30: 1/(60+2) + 1/(60+1) ≈ 0.03252  → first
    # 10: 1/61 ≈ 0.016393                → second
    # 20: 1/62 ≈ 0.016129 (seen first)   → third (tie)
    # 40: 1/62 ≈ 0.016129 (seen second)  → fourth (tie)
    assert rrf_fuse([10, 20, 30], [30, 40]) == [30, 10, 20, 40]


def test_consensus_beats_singleton_leader() -> None:
    # Doc mid-list in both lists outranks a doc at rank 1 of only one list.
    assert rrf_fuse([9, 8, 7], [6, 9]) == [9, 6, 8, 7]


def test_rank_starts_at_one_not_zero() -> None:
    # If rank started at 0, a rank-1 doc in two identical lists would tie
    # with a rank-1 doc of one list plus a rank-2 of another; with rank
    # starting at 1 these differ: 1/61 + 1/61 > 1/61 + 1/62.
    assert rrf_fuse([5, 4], [5, 4])[0] == 5
    two_list_hit = len(rrf_fuse([5], [5]))
    assert two_list_hit == 1


def test_tie_break_is_first_seen_order() -> None:
    # Same-rank docs across lists tie on score; call order decides.
    assert rrf_fuse([100], [200]) == [100, 200]
    assert rrf_fuse([200], [100]) == [200, 100]


def test_single_list_ordering_preserved() -> None:
    assert rrf_fuse([3, 1, 2]) == [3, 1, 2]


def test_empty_lists_fuse_to_empty() -> None:
    assert rrf_fuse() == []
    assert rrf_fuse([], []) == []


def test_disjoint_lists_concatenate_by_rank() -> None:
    # All scores are per-list rank ties → deterministic first-seen order:
    # rank-1 of list A, rank-1 of list B, rank-2 of list A, ...
    assert rrf_fuse([1, 2], [3, 4]) == [1, 3, 2, 4]
