"""Tests for shingle Jaccard near-duplicate detection."""

from __future__ import annotations

import pytest

from okf_core.dedupe import (
    NearDuplicateBasis,
    NearDuplicateWarning,
    _warning_sort_key,
    find_near_duplicates,
)


def _warning(left: str, right: str, similarity: float) -> NearDuplicateWarning:
    return NearDuplicateWarning(
        code="near_duplicate",
        left=left,
        right=right,
        similarity=similarity,
        threshold=0.8,
        basis=NearDuplicateBasis(
            method="shingle_jaccard",
            shingle_size=5,
            shared_terms_sample=(),
        ),
        suggested_action="merge_or_link",
        destructive=False,
    )


def _numbered_body(count: int, *, last: str | None = None) -> str:
    tokens = [f"w{index}" for index in range(count)]
    if last is not None:
        tokens[-1] = last
    return " ".join(tokens)


def test_find_near_duplicates_orders_paths_alphabetically() -> None:
    body = "Agent receipt validation confirms the bundle before merge."
    warnings = find_near_duplicates([("concepts/b.md", body), ("concepts/a.md", body)])
    assert len(warnings) == 1
    assert warnings[0].left == "concepts/a.md"
    assert warnings[0].right == "concepts/b.md"
    assert warnings[0].similarity == 1.0
    assert warnings[0].destructive is False


def test_similarity_at_threshold_warns() -> None:
    warnings = find_near_duplicates(
        [
            ("concepts/a.md", _numbered_body(13)),
            ("concepts/b.md", _numbered_body(13, last="other")),
        ]
    )
    assert len(warnings) == 1
    assert warnings[0].similarity == 0.8
    assert warnings[0].threshold == 0.8


def test_similarity_below_threshold_does_not_warn() -> None:
    changed = " ".join([*(f"w{index}" for index in range(11)), "other", "else"])
    assert (
        find_near_duplicates(
            [
                ("concepts/a.md", _numbered_body(13)),
                ("concepts/b.md", changed),
            ]
        )
        == ()
    )


def test_empty_bodies_are_exact_duplicates() -> None:
    warnings = find_near_duplicates([("concepts/a.md", ""), ("concepts/b.md", "")])
    assert len(warnings) == 1
    assert warnings[0].similarity == 1.0
    assert warnings[0].basis.shared_terms_sample == ()


def test_empty_body_does_not_match_text() -> None:
    assert (
        find_near_duplicates(
            [
                ("concepts/a.md", ""),
                ("concepts/b.md", "alpha bravo charlie delta echo foxtrot"),
            ]
        )
        == ()
    )


def test_short_exact_bodies_warn() -> None:
    warnings = find_near_duplicates(
        [
            ("concepts/a.md", "alpha bravo charlie"),
            ("concepts/b.md", "alpha bravo charlie"),
        ]
    )
    assert len(warnings) == 1
    assert warnings[0].similarity == 1.0


def test_identical_paths_are_not_paired() -> None:
    body = "alpha bravo charlie delta echo foxtrot"
    assert (
        find_near_duplicates([("concepts/a.md", body), ("concepts/a.md", body)]) == ()
    )


def test_single_document_returns_no_warnings() -> None:
    assert (
        find_near_duplicates([("concepts/a.md", "alpha bravo charlie delta echo")])
        == ()
    )


def test_find_near_duplicates_rejects_non_positive_shingle_size() -> None:
    with pytest.raises(ValueError, match="shingle_size"):
        find_near_duplicates(
            [("concepts/a.md", "one two three four five")],
            shingle_size=0,
        )


def test_same_path_pair_orders_by_similarity_descending() -> None:
    low = _warning("concepts/a.md", "concepts/b.md", 0.81)
    high = _warning("concepts/a.md", "concepts/b.md", 0.97)
    ordered = sorted((low, high), key=_warning_sort_key)
    assert [item.similarity for item in ordered] == [0.97, 0.81]


def test_path_pair_orders_ahead_of_higher_similarity() -> None:
    later_higher = _warning("concepts/m.md", "concepts/n.md", 1.0)
    earlier_lower = _warning("concepts/a.md", "concepts/b.md", 0.81)
    ordered = sorted((later_higher, earlier_lower), key=_warning_sort_key)
    assert [(item.left, item.right) for item in ordered] == [
        ("concepts/a.md", "concepts/b.md"),
        ("concepts/m.md", "concepts/n.md"),
    ]
