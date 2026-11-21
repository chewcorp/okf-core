from __future__ import annotations

import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory


class GitUpstream:
    def __init__(self, root: Path) -> None:
        self.path = root / "upstream.git"
        self.url = self.path.as_uri()
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        self.env.update(
            GIT_AUTHOR_NAME="Test Author",
            GIT_AUTHOR_EMAIL="author@example.invalid",
            GIT_COMMITTER_NAME="Test Committer",
            GIT_COMMITTER_EMAIL="committer@example.invalid",
            GIT_AUTHOR_DATE="2000-01-01T00:00:00+00:00",
            GIT_COMMITTER_DATE="2000-01-01T00:00:00+00:00",
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_ALLOW_PROTOCOL="file",
            GIT_TERMINAL_PROMPT="0",
            LC_ALL="C",
        )
        self._git("init", "--bare", "-b", "main", str(self.path))

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def commit(self, files: dict[str, str], message: str) -> str:
        with TemporaryDirectory(dir=self.path.parent) as directory:
            work = Path(directory) / "work"
            self._git("clone", str(self.path), str(work))
            for name, content in files.items():
                target = work / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            self._git("add", ".", cwd=work)
            self._git("commit", "--allow-empty", "-m", message, cwd=work)
            self._git("push", "origin", "main", cwd=work)
            return self._git("rev-parse", "HEAD", cwd=work)

    def resolve(self, ref: str) -> str:
        return self._git(
            "rev-parse", "--verify", "--end-of-options", ref, cwd=self.path
        )
