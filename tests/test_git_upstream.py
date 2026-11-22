import subprocess
from pathlib import Path

import pytest
from git_upstream import GitUpstream


def test_creates_bare_repository(git_upstream: GitUpstream) -> None:
    result = subprocess.run(
        ["git", "rev-parse", "--is-bare-repository"],
        cwd=git_upstream.path,
        env=git_upstream.env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "true"
    assert git_upstream.url == git_upstream.path.as_uri()


def test_commit_resolves_by_branch_and_sha(git_upstream: GitUpstream) -> None:
    sha = git_upstream.commit({"nested/concept.md": "first\n"}, "First commit")
    assert len(sha) == 40
    assert git_upstream.resolve("main") == sha
    assert git_upstream.resolve(sha) == sha


def test_second_commit_advances_main(git_upstream: GitUpstream) -> None:
    first = git_upstream.commit({"concept.md": "first\n"}, "First commit")
    second = git_upstream.commit({"concept.md": "second\n"}, "Second commit")
    assert second != first
    assert git_upstream.resolve("main") == second
    assert git_upstream.resolve("main^") == first


def test_empty_commit(git_upstream: GitUpstream) -> None:
    sha = git_upstream.commit({}, "Empty commit")
    assert git_upstream.resolve("main") == sha


def test_unknown_ref_raises(git_upstream: GitUpstream) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        git_upstream.resolve("unknown")


def test_non_file_protocol_is_refused(
    git_upstream: GitUpstream, tmp_path: Path
) -> None:
    with pytest.raises(subprocess.CalledProcessError) as error:
        subprocess.run(
            ["git", "clone", "https://example.invalid/x", str(tmp_path / "clone")],
            env=git_upstream.env,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    assert "transport 'https' not allowed" in error.value.stderr
