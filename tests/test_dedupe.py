"""Tests for shingle Jaccard near-duplicate detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from okf_core.config import BundleConfig
from okf_core.dedupe import (
    NearDuplicateBasis,
    NearDuplicateWarning,
    _warning_sort_key,
    find_near_duplicates,
    near_duplicate_warnings,
)
from okf_core.manifest import scan_bundle


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


def test_distinct_text_without_ascii_words_does_not_warn() -> None:
    assert (
        find_near_duplicates(
            [
                ("concepts/a.md", "東京の知識は別の主題です。"),
                ("concepts/b.md", "大阪の港は船舶を扱います。"),
            ]
        )
        == ()
    )
    assert (
        find_near_duplicates(
            [
                ("concepts/a.md", "---"),
                ("concepts/b.md", "###"),
            ]
        )
        == ()
    )


def test_hindi_vowel_signs_keep_distinct_words() -> None:
    assert (
        find_near_duplicates(
            [
                ("concepts/a.md", "कला कला कला कला कला"),
                ("concepts/b.md", "काला काला काला काला काला"),
            ]
        )
        == ()
    )


def test_underscore_separates_words() -> None:
    warnings = find_near_duplicates(
        [
            ("concepts/a.md", "foo_bar foo_bar foo_bar foo_bar foo_bar"),
            ("concepts/b.md", "foo bar foo bar foo bar foo bar foo bar"),
        ]
    )
    assert len(warnings) == 1
    assert warnings[0].similarity == 1.0


def test_reported_similarity_stays_at_or_above_threshold() -> None:
    # 15 tokens, last token changed: 10/12 = 0.8333..., which rounds to 0.8333.
    left = " ".join(f"w{index}" for index in range(15))
    right = " ".join([*(f"w{index}" for index in range(14)), "other"])
    documents = [("concepts/a.md", left), ("concepts/b.md", right)]
    raw = find_near_duplicates(documents, threshold=0.0)[0].similarity
    rounded = round(raw, 4)
    threshold = (rounded + raw) / 2
    kept = find_near_duplicates(documents, threshold=threshold)
    assert rounded < threshold <= raw
    assert len(kept) == 1
    assert kept[0].similarity == raw
    assert kept[0].similarity >= kept[0].threshold


def test_spaced_cyrillic_near_the_threshold_warns() -> None:
    words = [
        "один",
        "два",
        "три",
        "четыре",
        "пять",
        "шесть",
        "семь",
        "восемь",
        "девять",
        "десять",
        "одиннадцать",
        "двенадцать",
        "тринадцать",
    ]
    left = " ".join(words)
    right = " ".join([*words[:-1], "другое"])
    warnings = find_near_duplicates([("concepts/a.md", left), ("concepts/b.md", right)])
    assert len(warnings) == 1
    assert warnings[0].similarity == 0.8


def test_shared_ascii_heading_does_not_hide_distinct_non_latin_bodies() -> None:
    assert (
        find_near_duplicates(
            [
                ("concepts/a.md", "# OKF Concepts\n\n東京は首都です。"),
                ("concepts/b.md", "# OKF Concepts\n\n大阪は港町です。"),
            ]
        )
        == ()
    )


def test_identical_text_without_ascii_words_warns() -> None:
    body = "東京の知識は別の主題です。"
    warnings = find_near_duplicates([("concepts/a.md", body), ("concepts/b.md", body)])
    assert len(warnings) == 1
    assert warnings[0].similarity == 1.0


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


def _bundle(root: Path) -> BundleConfig:
    return BundleConfig(
        name="default",
        bundle_root=root,
        include=("**/*.md",),
        exclude=(),
        reserved_filenames=("index.md", "log.md"),
        concept_path_strategy="relative-path",
    )


def test_near_duplicate_warnings_reuses_a_supplied_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.md").write_text(
        "---\ntype: concept\ntitle: A\n---\nAlpha bravo charlie delta echo.\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text(
        "---\ntype: concept\ntitle: B\n---\nAlpha bravo charlie delta echo.\n",
        encoding="utf-8",
    )
    bundle = _bundle(tmp_path)
    manifest = scan_bundle(bundle)

    def fail_scan(bundle: BundleConfig) -> object:
        raise AssertionError(f"rescanned {bundle.name}")

    monkeypatch.setattr("okf_core.dedupe.scan_bundle", fail_scan)
    result = near_duplicate_warnings(bundle, {}, manifest=manifest)

    assert result.problems == manifest.problems
    assert len(result.warnings) == 1
    assert result.warnings[0].similarity == 1.0


def test_near_duplicate_warnings_reports_unparsed_documents(tmp_path: Path) -> None:
    (tmp_path / "good.md").write_text(
        "---\ntype: concept\ntitle: Good\n---\nAlpha bravo charlie delta echo.\n",
        encoding="utf-8",
    )
    broken = tmp_path / "broken.md"
    broken.write_text("---\ntype: [unterminated\n---\nBody\n", encoding="utf-8")

    result = near_duplicate_warnings(_bundle(tmp_path), {})

    assert result.warnings == ()
    assert len(result.problems) == 1
    assert result.problems[0].path == broken.resolve()
    assert result.problems[0].kind == "parse-error"


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
