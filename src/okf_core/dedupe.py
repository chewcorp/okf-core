"""Near-duplicate detection for concept bodies.

Compares concept text with word-shingle Jaccard similarity. The check is
read-only: callers pass text in, and nothing here writes a file.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from okf_core.config import BundleConfig
from okf_core.documents import ValidationFinding
from okf_core.manifest import ConceptManifestEntry, scan_bundle

SHINGLE_SIZE = 5
NEAR_DUPLICATE_THRESHOLD = 0.8
SHARED_TERMS_SAMPLE_LIMIT = 8
_METHOD = "shingle_jaccard"
_WORD = re.compile(r"[0-9A-Za-z]+")


@dataclass(frozen=True)
class NearDuplicateBasis:
    """How a near-duplicate score was produced."""

    method: str
    shingle_size: int
    shared_terms_sample: tuple[str, ...]


@dataclass(frozen=True)
class NearDuplicateWarning:
    """One soft warning for a pair of near-duplicate concepts."""

    code: str
    left: str
    right: str
    similarity: float
    threshold: float
    basis: NearDuplicateBasis
    suggested_action: str
    destructive: bool

    def as_dict(self) -> dict[str, object]:
        """Return the stable JSON record for this warning."""

        return {
            "code": self.code,
            "left": self.left,
            "right": self.right,
            "similarity": self.similarity,
            "threshold": self.threshold,
            "basis": {
                "method": self.basis.method,
                "shingle_size": self.basis.shingle_size,
                "shared_terms_sample": list(self.basis.shared_terms_sample),
            },
            "suggested_action": self.suggested_action,
            "destructive": self.destructive,
        }


@dataclass(frozen=True)
class _PreparedDocument:
    path: str
    shingles: frozenset[tuple[str, ...]]
    terms: frozenset[str]


def find_near_duplicates(
    documents: Sequence[tuple[str, str]],
    *,
    threshold: float = NEAR_DUPLICATE_THRESHOLD,
    shingle_size: int = SHINGLE_SIZE,
) -> tuple[NearDuplicateWarning, ...]:
    """Return near-duplicate warnings for ``(path, body)`` documents.

    A pair is reported when its shingle Jaccard similarity is greater than
    or equal to ``threshold`` (default ``0.8``). ``left`` and ``right`` are
    the two paths in alphabetical order. Warnings are ordered by that path
    pair ascending, then by similarity descending. ``shared_terms_sample``
    is the alphabetically first shared words, at most 8. Each record's
    ``destructive`` field is ``False``: this function never modifies text.

    ``shingle_size`` must be at least 1.
    """

    if shingle_size < 1:
        raise ValueError("shingle_size must be at least 1")
    prepared = [_prepare_document(path, body, shingle_size) for path, body in documents]
    warnings: list[NearDuplicateWarning] = []
    for index, left in enumerate(prepared):
        for right in prepared[index + 1 :]:
            warning = _pair_warning(
                left, right, threshold=threshold, shingle_size=shingle_size
            )
            if warning is not None:
                warnings.append(warning)
    warnings.sort(key=_warning_sort_key)
    return tuple(warnings)


def near_duplicate_warnings(
    bundle: BundleConfig,
    findings: Mapping[Path, Sequence[ValidationFinding]],
) -> tuple[NearDuplicateWarning, ...]:
    """Compare valid concept bodies in ``bundle`` and return soft warnings.

    A concept is valid when it was scanned and its path has no
    error-severity finding in ``findings``. Paths in the result are
    bundle-relative and use forward slashes. This reads the bundle through
    ``scan_bundle`` and does not write files.
    """

    manifest = scan_bundle(bundle)
    root = bundle.bundle_root.resolve()
    error_paths = _error_paths(findings)
    documents: list[tuple[str, str]] = []
    for entry in manifest.concepts:
        document = _comparable_document(entry, root, error_paths)
        if document is not None:
            documents.append(document)
    return find_near_duplicates(documents)


def format_near_duplicate_warning(warning: NearDuplicateWarning) -> str:
    """Return a one-line warning naming both files and a merge-or-link action."""

    return (
        f"{warning.left} and {warning.right} are near-duplicates "
        f"(similarity {warning.similarity:.2f}); consider merging or linking them."
    )


def _warning_sort_key(warning: NearDuplicateWarning) -> tuple[str, str, float]:
    return (warning.left, warning.right, -warning.similarity)


def _error_paths(
    findings: Mapping[Path, Sequence[ValidationFinding]],
) -> set[Path]:
    return {
        Path(path).resolve()
        for path, path_findings in findings.items()
        if any(finding.severity == "error" for finding in path_findings)
    }


def _comparable_document(
    entry: ConceptManifestEntry,
    root: Path,
    error_paths: set[Path],
) -> tuple[str, str] | None:
    resolved = entry.path.resolve()
    if resolved in error_paths:
        return None
    return (resolved.relative_to(root).as_posix(), entry.body)


def _prepare_document(path: str, body: str, shingle_size: int) -> _PreparedDocument:
    tokens = _tokens(body)
    return _PreparedDocument(
        path=path,
        shingles=_shingles(tokens, shingle_size),
        terms=frozenset(tokens),
    )


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(match.group(0).casefold() for match in _WORD.finditer(text))


def _shingles(tokens: tuple[str, ...], size: int) -> frozenset[tuple[str, ...]]:
    # shortcut: documents shorter than shingle_size collapse to one shingle,
    # so only an exact token sequence matches; upgrade if short notes need
    # graded overlap.
    if not tokens:
        return frozenset()
    if len(tokens) < size:
        return frozenset((tokens,))
    return frozenset(
        tokens[index : index + size] for index in range(len(tokens) - size + 1)
    )


def _jaccard(
    left: frozenset[tuple[str, ...]], right: frozenset[tuple[str, ...]]
) -> float:
    # shortcut: two empty bodies score 1.0 and warn; upgrade if stub
    # concepts should be ignored.
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    union = len(left | right)
    return intersection / union


def _shared_terms_sample(
    left: frozenset[str], right: frozenset[str]
) -> tuple[str, ...]:
    return tuple(sorted(left & right)[:SHARED_TERMS_SAMPLE_LIMIT])


def _pair_warning(
    left: _PreparedDocument,
    right: _PreparedDocument,
    *,
    threshold: float,
    shingle_size: int,
) -> NearDuplicateWarning | None:
    if left.path == right.path:
        return None
    similarity = _jaccard(left.shingles, right.shingles)
    if similarity < threshold:
        return None
    first, second = sorted((left, right), key=lambda item: item.path)
    return NearDuplicateWarning(
        code="near_duplicate",
        left=first.path,
        right=second.path,
        similarity=round(similarity, 4),
        threshold=threshold,
        basis=NearDuplicateBasis(
            method=_METHOD,
            shingle_size=shingle_size,
            shared_terms_sample=_shared_terms_sample(first.terms, second.terms),
        ),
        suggested_action="merge_or_link",
        destructive=False,
    )
