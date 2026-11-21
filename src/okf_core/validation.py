"""Bundle-level validation logic."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from okf_core.attribution import check_attribution_consistency
from okf_core.change_envelope import DocumentChangePlanningError
from okf_core.documents import (
    ConceptDocument,
    DocumentParseError,
    ValidationFinding,
    parse_concept_document,
    validate_concept_document,
    validate_concept_document_with_profile,
)
from okf_core.index import (
    concept_directories,
    diff_index,
    entries_for_directory,
    generate_index,
    parse_index,
)
from okf_core.logs import ParsedLog, log_conformance_findings, parse_log
from okf_core.manifest import BundleManifest, _is_excluded, scan_bundle
from okf_core.patching import (
    _validate_actor_string,
    _validate_date_value,
    _validate_datetime_value,
    _validate_existing_sources,
    _validate_existing_verified,
    _validate_source_entry,
    _validate_status_value,
    _validate_verified_entry,
)

if TYPE_CHECKING:
    from okf_core.config import BundleConfig, OkfConfig, ProfileConfig, TaxonomyConfig


def validate_bundle(
    bundle: BundleConfig,
    config: OkfConfig,
) -> dict[Path, tuple[ValidationFinding, ...]]:
    """Validate concept documents, index drift, and existing log.md files.

    After concept and index-drift checks, every existing ``log.md`` is
    checked for OKF v0.2 §9 conformance. Date headings MUST be ISO 8601
    ``YYYY-MM-DD`` calendar dates, and date-grouped sections MUST be newest
    first. Findings are keyed at the ``log.md`` path. A bundle with no
    ``log.md`` skips this check.

    Concept checks also include OKF v0.2 provenance, trust, and lifecycle
    shape (``sources``, ``generated``, ``verified``, ``status``,
    ``stale_after``) via the same validators the stamp and source-upsert
    writes use. Spec-shape violations are errors. Two spec-permissive
    oddities are warnings: ``generated`` with no ``at``, and a calendar-date
    ``stale_after``. See ``_provenance_lifecycle_findings``.
    """
    findings: dict[Path, tuple[ValidationFinding, ...]] = {}

    # Scan the bundle to discover and parse concepts
    manifest = scan_bundle(bundle)

    # Record scan problems as validation errors
    for problem in manifest.problems:
        findings[problem.path] = (
            ValidationFinding(
                severity="error",
                message=f"Scan error ({problem.kind}): {problem.message}",
            ),
        )

    # Resolve the profile to use
    profile = None
    if bundle.profile is not None:
        profile = config.profiles[bundle.profile]

    for entry in manifest.concepts:
        # Reconstruct a document with the concept's frontmatter
        doc = ConceptDocument(frontmatter=dict(entry.frontmatter))

        if profile is not None:
            doc_findings = validate_concept_document_with_profile(
                doc, profile, project_taxonomy=config.taxonomy
            )
        else:
            doc_findings = validate_concept_document(doc)

        # v0.2 field shapes, then attribution consistency. Attribution
        # (footnote labels vs. sources[].id) is base spec behavior (§5.1),
        # not profile-specific, and is unchanged by the shape check: a
        # malformed sources value is a shape finding here, while a
        # well-formed but uncited sources[].id stays an attribution warning.
        # Shape checks re-parse the document. The scan freezes YAML dates to
        # ISO strings, which would let a date pass a string check the
        # source-upsert validator rejects. Attribution keeps using the
        # scan's frontmatter and body.
        doc_findings = (
            tuple(doc_findings)
            + _provenance_lifecycle_findings(entry.path, entry.content)
            + check_attribution_consistency(entry.frontmatter, entry.body)
        )

        if doc_findings:
            findings[entry.path] = doc_findings

    for index_path, drift_findings in _index_drift_findings_by_path(
        bundle, manifest, profile, config.taxonomy
    ).items():
        findings[index_path] = findings.get(index_path, ()) + drift_findings

    for log_path, log_findings in _log_conformance_findings_by_path(bundle).items():
        findings[log_path] = findings.get(log_path, ()) + log_findings

    return findings


# Spec-permissive oddities. Shape violations use the write-path validators'
# own messages and are errors; these two are warnings because the spec does
# not treat them as malformed.
_GENERATED_AT_ABSENT = (
    "generated.at is absent; only generated.by is required (OKF v0.2 §5.2). "
    "When at is present it must be an ISO 8601 datetime with a UTC offset"
)
_STALE_AFTER_CALENDAR_DATE = (
    "stale_after is a calendar date; OKF v0.2 §5.5 defines an absolute "
    "instant (an ISO 8601 datetime with a UTC offset). A YYYY-MM-DD date "
    "is the form stamp_stale_after writes, so it is a warning rather than "
    "an error"
)
_TOP_LEVEL_KEY = re.compile(r"^([A-Za-z_][\w-]*)\s*:")


def _provenance_lifecycle_findings(
    path: Path,
    content: str,
) -> tuple[ValidationFinding, ...]:
    """Check OKF v0.2 §5 provenance, trust, and lifecycle field shapes.

    Reuses the stamp and source-upsert validators (``_validate_source_entry``,
    ``_validate_actor_string``, ``_validate_datetime_value``,
    ``_validate_verified_entry``, ``_validate_status_value``,
    ``_validate_date_value``). Absent fields are not findings (§11: missing
    optional families are legal). Extra keys and source credibility signals
    (``author``, ``usage_count``, ``last_modified``, ``usage_window``) are
    not findings either: the source-upsert validator does not schema-check
    them, and §11 says unknown keys must not reject a bundle.

    Severity:

    * **Error** — a present value the reused validator rejects. ``sources``
      must be a list of mappings with a non-empty string ``resource`` and,
      when ``id`` is present and not null, a non-empty string ``id``
      (``id: null`` is omitted). ``generated`` must
      be a mapping whose ``by`` is an actor (§7) and whose ``at``, when
      present, is an offset-aware ISO 8601 datetime; an offset-naive
      timestamp is an error. ``verified`` must be a list of ``{by, at}``
      events or one bare mapping (§5.2's shorthand is valid, not an error).
      ``status`` must be ``draft``, ``stable``, or ``deprecated``.
      ``stale_after`` must be a calendar date or an offset-aware instant;
      anything else is an error.
    * **Warning** — ``generated`` omits ``at`` (only ``by`` is required), and
      ``stale_after`` is a ``YYYY-MM-DD`` calendar date (the spec's shape is
      an absolute instant; the stamp writer emits a date).

    ``line`` is the 1-based line of the top-level key in ``content`` when
    that key is written at column 0 in the frontmatter block, else ``None``.
    The check parses ``content`` itself. ``scan_bundle`` freezes YAML dates
    and datetimes to ISO strings before publishing frontmatter, including
    through the SQLite cache, and the string check would then accept a date
    that ``source_upsert`` rejects. An explicit ``id: null`` stays legal:
    the source-entry validator treats it as omitted.
    """
    try:
        frontmatter = parse_concept_document(content).frontmatter
    except DocumentParseError as exc:
        return (
            _shape_finding(
                "error",
                f"could not parse frontmatter for v0.2 shape check: {exc}",
                "frontmatter",
                None,
            ),
        )
    key_lines = _top_level_key_lines(content)
    findings: list[ValidationFinding] = []
    if "sources" in frontmatter:
        findings.extend(
            _sources_findings(path, frontmatter["sources"], key_lines.get("sources"))
        )
    if "generated" in frontmatter:
        findings.extend(
            _generated_findings(
                path, frontmatter["generated"], key_lines.get("generated")
            )
        )
    if "verified" in frontmatter:
        findings.extend(
            _verified_findings(path, frontmatter["verified"], key_lines.get("verified"))
        )
    if "status" in frontmatter:
        findings.extend(
            _status_findings(path, frontmatter["status"], key_lines.get("status"))
        )
    if "stale_after" in frontmatter:
        findings.extend(
            _stale_after_findings(
                path, frontmatter["stale_after"], key_lines.get("stale_after")
            )
        )
    return tuple(findings)


def _top_level_key_lines(content: str) -> dict[str, int]:
    """Map top-level frontmatter keys to their 1-based source lines.

    Stops at the closing ``---`` so a later body line that looks like a key
    is not used. A key that is not at column 0 (flow-only nesting, a quoted
    key) is left unknown.
    """
    lines = content.splitlines()
    if not lines or lines[0] != "---":
        return {}
    found: dict[str, int] = {}
    for index, line in enumerate(lines[1:], start=2):
        if line == "---":
            break
        match = _TOP_LEVEL_KEY.match(line)
        if match is not None:
            found.setdefault(match.group(1), index)
    return found


def _thaw_lists(value: Any) -> Any:
    """Turn scan-frozen tuples and mappings back into lists and dicts.

    ``scan_bundle`` freezes frontmatter (lists become tuples, mappings
    become ``MappingProxyType``, dates and datetimes become ISO strings).
    The write-path validators accept ISO strings, but they require a real
    ``list`` for ``sources`` and ``verified``.
    """
    if isinstance(value, tuple):
        return [_thaw_lists(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _thaw_lists(item) for key, item in value.items()}
    return value


def _rejected(check: Callable[[], Any]) -> str | None:
    """Return a write-path validator's message, or ``None`` when it accepts."""
    try:
        check()
    except DocumentChangePlanningError as exc:
        return str(exc)
    return None


def _shape_finding(
    severity: str, message: str, field: str, line: int | None
) -> ValidationFinding:
    return ValidationFinding(severity=severity, message=message, field=field, line=line)


def _sources_findings(
    path: Path, sources: Any, line: int | None
) -> tuple[ValidationFinding, ...]:
    thawed = _thaw_lists(sources)
    if not isinstance(thawed, list):
        message = _rejected(lambda: _validate_existing_sources(path, thawed))
        if message is None:
            return ()
        return (_shape_finding("error", message, "sources", line),)
    findings: list[ValidationFinding] = []
    for entry in thawed:
        message = _source_entry_problem(path, entry)
        if message is not None:
            findings.append(_shape_finding("error", message, "sources", line))
    return tuple(findings)


def _source_entry_problem(path: Path, entry: Any) -> str | None:
    return _rejected(lambda: _validate_source_entry(path, entry, context="Existing"))


def _generated_findings(
    path: Path, generated: Any, line: int | None
) -> tuple[ValidationFinding, ...]:
    if not isinstance(generated, Mapping):
        return (
            _shape_finding(
                "error",
                f"generated must be a mapping (OKF v0.2 §5.2): {generated!r}",
                "generated",
                line,
            ),
        )
    findings: list[ValidationFinding] = []
    by_message = _rejected(
        lambda: _validate_actor_string(
            path, generated.get("by"), field_name="generated.by"
        )
    )
    if by_message is not None:
        findings.append(_shape_finding("error", by_message, "generated.by", line))
    if "at" not in generated:
        findings.append(
            _shape_finding("warning", _GENERATED_AT_ABSENT, "generated.at", line)
        )
    else:
        at_message = _rejected(
            lambda: _validate_datetime_value(
                path, generated.get("at"), field_name="generated.at"
            )
        )
        if at_message is not None:
            findings.append(_shape_finding("error", at_message, "generated.at", line))
    return tuple(findings)


def _verified_findings(
    path: Path, verified: Any, line: int | None
) -> tuple[ValidationFinding, ...]:
    thawed = _thaw_lists(verified)
    if isinstance(thawed, list):
        return _verified_entry_findings(path, thawed, line)
    if isinstance(thawed, Mapping):
        return _verified_entry_findings(path, [thawed], line)
    message = _rejected(lambda: _validate_existing_verified(path, thawed))
    if message is None:
        return ()
    return (_shape_finding("error", message, "verified", line),)


def _verified_entry_findings(
    path: Path, entries: list[Any], line: int | None
) -> tuple[ValidationFinding, ...]:
    findings: list[ValidationFinding] = []
    for entry in entries:
        message = _verified_entry_problem(path, entry)
        if message is not None:
            findings.append(_shape_finding("error", message, "verified", line))
    return tuple(findings)


def _verified_entry_problem(path: Path, entry: Any) -> str | None:
    return _rejected(lambda: _validate_verified_entry(path, entry, context="Existing"))


def _status_findings(
    path: Path, status: Any, line: int | None
) -> tuple[ValidationFinding, ...]:
    message = _rejected(lambda: _validate_status_value(path, status))
    if message is None:
        return ()
    return (_shape_finding("error", message, "status", line),)


def _stale_after_findings(
    path: Path, value: Any, line: int | None
) -> tuple[ValidationFinding, ...]:
    # A calendar date is what stamp_stale_after writes. The spec's shape is
    # an offset-aware instant, which _validate_datetime_value accepts and
    # _validate_date_value rejects. Try the date validator first so a date
    # is the documented warning rather than an instant-shaped error.
    if (
        _rejected(lambda: _validate_date_value(path, value, field_name="stale_after"))
        is None
    ):
        return (
            _shape_finding("warning", _STALE_AFTER_CALENDAR_DATE, "stale_after", line),
        )
    message = _rejected(
        lambda: _validate_datetime_value(path, value, field_name="stale_after")
    )
    if message is None:
        return ()
    return (_shape_finding("error", message, "stale_after", line),)


def _committed_log_paths(bundle: BundleConfig) -> tuple[Path, ...]:
    """Collect every existing ``log.md`` under the bundle root.

    Spec §9 says ``log.md`` MAY appear at any hierarchy level, so this walk
    is not limited to concept-bearing directories. The exact name ``log.md``
    matches how index drift looks for exact ``index.md``.
    """
    root = bundle.bundle_root.resolve()
    if not root.is_dir():
        return ()
    paths: list[Path] = []
    for directory, _dirnames, filenames in os.walk(root):
        if "log.md" in filenames:
            log_path = Path(directory) / "log.md"
            if _is_bundle_file(log_path, root) and not _is_excluded(
                log_path, root, bundle
            ):
                paths.append(log_path)
    return tuple(sorted(paths))


def _is_bundle_file(path: Path, root: Path) -> bool:
    """Return whether ``path`` is a regular file whose resolve stays in ``root``.

    Matches ``scan_bundle`` containment. A ``log.md`` symlink whose target
    leaves the bundle is excluded, not read as this bundle's log.
    """
    if not path.is_file():
        return False
    try:
        path.resolve().relative_to(root)
    except ValueError:
        return False
    return True


def _read_log_for_validation(
    path: Path,
) -> tuple[ParsedLog | None, ValidationFinding | None]:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, ValidationFinding(
            severity="error",
            message=f"could not read log.md: {exc}",
        )
    return parse_log(content), None


def _log_conformance_findings_by_path(
    bundle: BundleConfig,
) -> dict[Path, tuple[ValidationFinding, ...]]:
    findings: dict[Path, tuple[ValidationFinding, ...]] = {}
    for log_path in _committed_log_paths(bundle):
        parsed, read_problem = _read_log_for_validation(log_path)
        if read_problem is not None:
            findings[log_path] = (read_problem,)
            continue
        assert parsed is not None
        log_findings = log_conformance_findings(parsed)
        if log_findings:
            findings[log_path] = log_findings
    return findings


def _index_drift_findings_by_path(
    bundle: BundleConfig,
    manifest: BundleManifest,
    profile: ProfileConfig | None,
    project_taxonomy: TaxonomyConfig | None,
) -> dict[Path, tuple[ValidationFinding, ...]]:
    """Compare every concept-bearing directory's committed index.md, if any.

    Directories with no committed ``index.md`` are out of scope entirely --
    "missing index" is a different, unrequested check (#200). Advisory only:
    every finding is ``severity="warning"``, so this never affects
    ``okf validate``'s exit code.
    """
    resolved_bundle_root = bundle.bundle_root.resolve()
    concept_dirs, _ = concept_directories(manifest, resolved_bundle_root)

    findings: dict[Path, tuple[ValidationFinding, ...]] = {}
    for directory in sorted(concept_dirs):
        index_path = directory / "index.md"
        if not index_path.is_file():
            continue
        drift = _directory_index_drift(
            directory, index_path, manifest, bundle, profile, project_taxonomy
        )
        if drift:
            findings[index_path] = drift
    return findings


def _directory_index_drift(
    directory: Path,
    index_path: Path,
    manifest: BundleManifest,
    bundle: BundleConfig,
    profile: ProfileConfig | None,
    project_taxonomy: TaxonomyConfig | None,
) -> tuple[ValidationFinding, ...]:
    """Diff one directory's committed index.md against a fresh regeneration.

    Uses the same ``entries_for_directory`` + ``generate_index`` parameters
    ``okf index`` itself uses (no ``describe_directory`` callback), so drift
    findings never reflect a divergent baseline.
    """
    direct_entries, subdirectories = entries_for_directory(directory, manifest)
    generated = generate_index(
        directory,
        direct_entries,
        subdirectories,
        directory_metadata_file=bundle.directory_metadata_file,
        profile=profile,
        project_taxonomy=project_taxonomy,
    )

    try:
        committed_content = index_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return (
            ValidationFinding(
                severity="warning",
                message=f"could not read index.md for drift check: {exc}",
            ),
        )

    # generate_index's body never carries frontmatter (render_index_document
    # adds it only when writing the bundle root's index.md); strip any
    # committed frontmatter the same way before parsing so both sides compare
    # like for like.
    try:
        committed_body = parse_concept_document(committed_content).body
    except DocumentParseError as exc:
        return (
            ValidationFinding(
                severity="warning",
                message=f"could not parse index.md for drift check: {exc}",
            ),
        )
    committed = parse_index(committed_body)
    return diff_index(generated, committed)
