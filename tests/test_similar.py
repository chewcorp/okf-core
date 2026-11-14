from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from okf_core import BundleConfig, GraphProblem, find_similar_concepts
from okf_core.cli import cli


def _note(root: Path, name: str, title: str, body: str = "") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.md").write_text(
        f"---\ntype: concept\ntitle: '{title}'\n---\n{body}", encoding="utf-8"
    )


def _bundle(tmp_path: Path) -> BundleConfig:
    return BundleConfig(
        name="default",
        bundle_root=tmp_path / "docs",
        include=("**/*.md",),
        exclude=(),
        reserved_filenames=("index.md", "log.md"),
        concept_path_strategy="relative-path",
        okf_cache_dir=tmp_path / "cache",
    )


def _config(tmp_path: Path, *, cache: bool = True) -> str:
    path = tmp_path / "okf-core.toml"
    path.write_text(
        '[bundles.default]\nbundle_root = "docs"\n'
        + ('okf_cache_dir = "cache"\n' if cache else ""),
        encoding="utf-8",
    )
    return str(path)


def test_reworded_duplicate_ranks_first(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    first = "Memory retrieval improves through spaced practice and active recall."
    second = "Active recall and spaced retrieval practice strengthen memory."
    assert not {
        tuple(first.split()[i : i + 5]) for i in range(len(first.split()) - 4)
    } & {tuple(second.split()[i : i + 5]) for i in range(len(second.split()) - 4)}
    _note(root, "source", "Memory retrieval", first)
    _note(root, "duplicate", "Spaced memory practice", second)
    for name, text in [
        ("decoy", "Memory hardware chip storage"),
        ("other", "Ocean waves sand beach"),
        ("third", "Practice piano music scales"),
        ("fourth", "Retrieval database records"),
    ]:
        _note(root, name, text)
    neighbours = (
        find_similar_concepts(_bundle(tmp_path), ["source"]).results[0].neighbours
    )
    assert neighbours[0].concept_id == "duplicate"
    assert len(neighbours) == 3


@pytest.mark.parametrize("source,target", [("alpha", "beta"), ("beta", "alpha")])
def test_link_either_way_excludes_pair(
    tmp_path: Path, source: str, target: str
) -> None:
    root = tmp_path / "docs"
    for name in ("alpha", "beta"):
        _note(
            root,
            name,
            "Memory recall",
            f"[related]({target}.md)" if name == source else "",
        )
    assert (
        find_similar_concepts(_bundle(tmp_path), ["alpha"]).results[0].neighbours == ()
    )


def test_cyrillic_neighbours(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    _note(root, "alpha", "Память обучение", "Практика помогает")
    _note(root, "beta", "Обучение практика", "Память помогает")
    assert (
        find_similar_concepts(_bundle(tmp_path), ["alpha"])
        .results[0]
        .neighbours[0]
        .concept_id
        == "beta"
    )


def test_json_order_and_tie(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    for name in ("source", "b", "a"):
        _note(root, name, "Memory recall")
    result = CliRunner().invoke(
        cli, ["similar", "source", "a", "--config", _config(tmp_path), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["bundle"] == "default"
    assert [r["concept"] for r in payload["results"]] == ["source", "a"]
    neighbours = payload["results"][0]["neighbours"]
    assert [n["path"] for n in neighbours] == ["a.md", "b.md"]
    assert neighbours[0]["score"] == neighbours[1]["score"]
    for entry in payload["results"]:
        assert entry["neighbours"] == sorted(
            entry["neighbours"], key=lambda n: (-n["score"], n["path"])
        )
        assert all(
            isinstance(n["score"], float) and n["concept_id"]
            for n in entry["neighbours"]
        )


def test_missing_cache_matches_search_error(tmp_path: Path) -> None:
    config = _config(tmp_path, cache=False)
    runner = CliRunner()
    similar = runner.invoke(cli, ["similar", "alpha", "--config", config])
    search = runner.invoke(cli, ["search", "alpha", "--config", config])
    assert similar.exit_code == search.exit_code == 2
    assert similar.stderr == search.stderr


def test_unknown_id_is_problem_and_exits_zero(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli, ["similar", "missing", "--config", _config(tmp_path), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["results"] == []
    assert payload["problems"][0]["kind"] == "unknown-concept"


@pytest.mark.parametrize("title,body", [("", ""), ("!!!", "???"), ("Unique", "")])
def test_empty_or_unmatched_query(tmp_path: Path, title: str, body: str) -> None:
    _note(tmp_path / "docs", "source", title, body)
    assert (
        find_similar_concepts(_bundle(tmp_path), ["source"]).results[0].neighbours == ()
    )


def test_listing_and_graph_problems_surface(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    _note(root, "source", "Memory", "[missing](absent.md)")
    (root / "invalid.md").write_text(
        "---\ntitle: Invalid\n---\nMemory", encoding="utf-8"
    )
    (root / "malformed.md").write_text("---\ninvalid: [\n---\n", encoding="utf-8")
    result = find_similar_concepts(_bundle(tmp_path), ["source"])
    assert any(p.kind == "missing-type" for p in result.problems)
    assert any(
        isinstance(p, GraphProblem) and p.kind == "parse-error" for p in result.problems
    )
    assert result.results[0].neighbours == ()


def test_text_output_and_problem(tmp_path: Path) -> None:
    for name in ("source", "other"):
        _note(tmp_path / "docs", name, "Memory")
    result = CliRunner().invoke(
        cli, ["similar", "source", "unknown", "--config", _config(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("source\n  other.md  ")
    assert "Unknown concept ID: unknown" in result.stderr
