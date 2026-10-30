"""Integration tests for ``okf serve`` (read-only localhost bundle server)."""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from okf_core.cli import cli
from okf_core.config import load_config
from okf_core.serve import (
    ServeError,
    _include_match,
    create_server,
    serve_until_stopped,
)

_CONFIG = """\
[bundles.default]
bundle_root = "bundle"
include = ["**/*.md", "**/*.toml"]
exclude = ["secret/**", "draft-[0-9].md", "temp-?.md", "nested-secret.md"]
okf_cache_dir = "okf-cache"

[bundles.other]
bundle_root = "other"
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    _write(root / "okf-core.toml", _CONFIG)
    _write(root / "bundle" / "index.md", "# root index\n")
    _write(root / "bundle" / "page.md", "# page\n")
    _write(root / "bundle" / "notes.txt", "plain text\n")
    _write(root / "bundle" / "sub" / "index.md", "# sub index\n")
    _write(root / "bundle" / "sub" / "inner.md", "# inner\n")
    _write(root / "bundle" / "sub" / "nested-secret.md", "# nested secret\n")
    _write(root / "bundle" / "secret" / "hidden.md", "# hidden\n")
    _write(root / "bundle" / "draft-7.md", "# excluded draft\n")
    _write(root / "bundle" / "draft-x.md", "# included draft\n")
    _write(root / "bundle" / "temp-9.md", "# excluded temp\n")
    _write(root / "bundle" / "temp-xy.md", "# included temp\n")
    _write(root / "bundle" / "data.toml", "x = 1\n")
    _write(root / "bundle" / "fleeting" / "note.md", "# fleeting\n")
    _write(root / "bundle" / "deep" / "fleeting" / "x.md", "# nested fleeting\n")
    _write(root / "bundle" / ".git" / "objects" / "aa.md", "# git object\n")
    _write(root / "bundle" / ".env.md", "# secret env\n")
    _write(root / "bundle" / "okf-cache" / "cached.md", "# cache\n")
    _write(root / "other" / "index.md", "# other bundle\n")
    _write(root / "other" / "other.md", "# other page\n")
    _write(tmp_path / "outside.md", "# outside\n")
    if hasattr(os, "symlink"):
        os.symlink("../../outside.md", root / "bundle" / "evil.md")
    return root


def _snapshot(root: Path) -> dict[str, str]:
    """Map every path under root to its content hash or symlink target."""
    result: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in (*dirnames, *filenames):
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            if full.is_symlink():
                result[rel] = f"link:{os.readlink(full)}"
            elif full.is_file():
                result[rel] = hashlib.sha256(full.read_bytes()).hexdigest()
            else:
                result[rel] = "dir"
    return result


@contextlib.contextmanager
def _running_server(project: Path, bundle_name: str = "default") -> Iterator[str]:
    cfg = load_config(config_path=project / "okf-core.toml")
    httpd = create_server(cfg.bundles[bundle_name], cfg, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=10)


def _request(
    address: str,
    path: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    host, _, port = address.rpartition(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=10)
    conn.request(method, path, headers=headers or {})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, dict(resp.getheaders()), body


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_serves_permitted_markdown(project: Path) -> None:
    with _running_server(project) as address:
        status, headers, body = _request(address, "/page.md")
    assert status == 200
    assert body == b"# page\n"
    assert headers["Content-Type"] == "text/markdown; charset=utf-8"


def test_head_returns_headers_without_body(project: Path) -> None:
    with _running_server(project) as address:
        status, headers, body = _request(address, "/page.md", method="HEAD")
    assert status == 200
    assert body == b""
    assert headers["Content-Length"] == str(len(b"# page\n"))


def test_root_serves_index_md(project: Path) -> None:
    with _running_server(project) as address:
        status, _, body = _request(address, "/")
    assert status == 200
    assert body == b"# root index\n"


def test_subdirectory_serves_its_index(project: Path) -> None:
    with _running_server(project) as address:
        status, _, body = _request(address, "/sub/")
    assert status == 200
    assert body == b"# sub index\n"


def test_directory_without_trailing_slash_redirects(project: Path) -> None:
    with _running_server(project) as address:
        status, headers, _ = _request(address, "/sub")
    assert status == 301
    assert headers["Location"] == "/sub/"


def test_directory_without_index_returns_404(project: Path) -> None:
    _write(project / "bundle" / "bare" / "a.md", "# a\n")
    with _running_server(project) as address:
        status, _, _ = _request(address, "/bare/")
    assert status == 404


@pytest.mark.parametrize(
    "path",
    [
        "/secret/hidden.md",  # configured exclude glob
        "/draft-7.md",  # excluded via character-class glob
        "/temp-9.md",  # excluded via '?' glob
        "/sub/nested-secret.md",  # slashless exclude matches at any depth
        "/fleeting/note.md",  # fleeting at depth 1
        "/deep/fleeting/x.md",  # fleeting nested
        "/.git/objects/aa.md",  # dot-segment under root
        "/.env.md",  # dot-file
        "/okf-cache/cached.md",  # configured okf_cache_dir
        "/notes.txt",  # not matched by include globs
        "/missing.md",  # does not exist
        "/../outside.md",  # literal traversal
        "/%2e%2e/outside.md",  # encoded traversal
        "/%2e%2e%2foutside.md",  # encoded traversal with encoded slash
        "/sub/../outside.md",  # traversal after a real segment
        "/page.md%00.md",  # encoded NUL
    ],
)
def test_denied_and_missing_paths_return_404(project: Path, path: str) -> None:
    with _running_server(project) as address:
        status, _, _ = _request(address, path)
    assert status == 404


@pytest.mark.parametrize("path", ["/draft-x.md", "/temp-xy.md"])
def test_glob_excludes_leave_other_permitted_files_served(
    project: Path, path: str
) -> None:
    with _running_server(project) as address:
        status, _, _ = _request(address, path)
    assert status == 200


def test_non_markdown_permitted_file_serves_guessed_type(project: Path) -> None:
    with _running_server(project) as address:
        status, headers, body = _request(address, "/data.toml")
    assert status == 200
    assert body == b"x = 1\n"
    assert "markdown" not in headers["Content-Type"]


def test_unreadable_file_returns_404(project: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("chmod 0 is still readable for root")
    target = project / "bundle" / "locked.md"
    _write(target, "# locked\n")
    target.chmod(0)
    try:
        with _running_server(project) as address:
            status, _, _ = _request(address, "/locked.md")
            head_status, _, _ = _request(address, "/locked.md", method="HEAD")
    finally:
        target.chmod(0o644)
    assert status == 404
    assert head_status == 404


def test_symlink_escape_returns_404(project: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    with _running_server(project) as address:
        status, _, _ = _request(address, "/evil.md")
    assert status == 404


def test_index_md_symlink_to_excluded_returns_404(project: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    (project / "bundle" / "alias").mkdir()
    os.symlink("../secret/hidden.md", project / "bundle" / "alias" / "index.md")
    with _running_server(project) as address:
        status, _, _ = _request(address, "/alias/")
    assert status == 404


def test_index_md_symlink_outside_root_returns_404(project: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    (project / "bundle" / "outdir").mkdir()
    os.symlink("../../outside.md", project / "bundle" / "outdir" / "index.md")
    with _running_server(project) as address:
        status, _, _ = _request(address, "/outdir/")
    assert status == 404


def test_denied_segment_symlink_alias_returns_404(project: Path) -> None:
    """A denied segment in the request path denies even a permitted target."""
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    (project / "bundle" / "x").mkdir()
    os.symlink("sub", project / "bundle" / ".dotlink")
    os.symlink("../sub", project / "bundle" / "x" / "fleeting")
    with _running_server(project) as address:
        status_dot, _, _ = _request(address, "/.dotlink/inner.md")
        status_fleeting, _, _ = _request(address, "/x/fleeting/inner.md")
    assert status_dot == 404
    assert status_fleeting == 404


def test_symlink_into_denied_directory_returns_404(project: Path) -> None:
    """A clean request path resolving into ``fleeting`` is still denied."""
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    os.symlink("fleeting", project / "bundle" / "fleeting-link")
    with _running_server(project) as address:
        status, _, _ = _request(address, "/fleeting-link/note.md")
    assert status == 404


def test_permitted_symlink_alias_is_served(project: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    os.symlink("sub/inner.md", project / "bundle" / "alias-page.md")
    with _running_server(project) as address:
        status, _, body = _request(address, "/alias-page.md")
    assert status == 200
    assert body == b"# inner\n"


def test_query_string_is_ignored(project: Path) -> None:
    with _running_server(project) as address:
        status, _, body = _request(address, "/page.md?raw=1")
    assert status == 200
    assert body == b"# page\n"


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_write_methods_are_rejected(project: Path, method: str) -> None:
    with _running_server(project) as address:
        status, headers, _ = _request(address, "/page.md", method=method)
    assert status == 405
    assert headers["Allow"] == "GET, HEAD"


def test_no_directory_listing(project: Path) -> None:
    """A denied directory must not leak a listing even at its index path."""
    with _running_server(project) as address:
        status, _, body = _request(address, "/secret/")
    assert status == 404
    assert b"hidden.md" not in body


def test_root_serves_raw_index_even_for_browser_accept(project: Path) -> None:
    """`/` always serves bundle content; the adapter only lives at /.okf/."""
    with _running_server(project) as address:
        status, _, body = _request(
            address, "/", headers={"Accept": "text/html,application/xhtml+xml"}
        )
    assert status == 200
    assert body == b"# root index\n"


def test_root_serves_raw_index_for_non_browser_accept(project: Path) -> None:
    with _running_server(project) as address:
        status, _, body = _request(address, "/", headers={"Accept": "*/*"})
        status_none, _, body_none = _request(address, "/")
    assert status == 200
    assert body == b"# root index\n"
    assert status_none == 200
    assert body_none == b"# root index\n"


@pytest.mark.parametrize(
    ("path", "type_prefix"),
    [
        ("/.okf/", "text/html"),
        ("/.okf/index.html", "text/html"),
        ("/.okf/adapter.js", "text/javascript"),
        ("/.okf/okf.css", "text/css"),
        ("/.okf/vendor/markdown-it.min.js", "text/javascript"),
        ("/.okf/vendor/markdown-it-anchor.min.js", "text/javascript"),
        ("/.okf/vendor/js-yaml.min.js", "text/javascript"),
        ("/.okf/vendor/mermaid.min.js", "text/javascript"),
        ("/.okf/files.json", "application/json"),
    ],
)
def test_adapter_assets_served(project: Path, path: str, type_prefix: str) -> None:
    with _running_server(project) as address:
        status, headers, body = _request(address, path)
    assert status == 200
    assert headers["Content-Type"].startswith(type_prefix)
    assert body


@pytest.mark.parametrize(
    "path",
    [
        "/.okf/../page.md",  # traversal through the adapter prefix
        "/.okf/%2e%2e/page.md",  # encoded traversal
        "/.okf/nope.js",  # unknown asset name
        "/.okf",  # bare prefix without slash
        "/.okf/fleeting/x.js",  # denied segment inside prefix
        "/.okf/vendor/../adapter.js",  # segment traversal inside prefix
    ],
)
def test_adapter_prefix_denies_non_asset_paths(project: Path, path: str) -> None:
    with _running_server(project) as address:
        status, _, _ = _request(address, path)
    assert status == 404


def test_adapter_assets_support_head(project: Path) -> None:
    with _running_server(project) as address:
        status, headers, body = _request(address, "/.okf/adapter.js", method="HEAD")
    assert status == 200
    assert body == b""
    assert int(headers["Content-Length"]) > 0


def test_named_bundle_serves_adapter_assets(project: Path) -> None:
    """The packaged adapter is shared: a second bundle serves the same files."""
    with _running_server(project) as default:
        _, _, default_js = _request(default, "/.okf/adapter.js")
    with _running_server(project, bundle_name="other") as other:
        status, _, other_js = _request(other, "/.okf/adapter.js")
        root_status, _, _ = _request(other, "/.okf/")
    assert status == 200
    assert other_js == default_js
    assert root_status == 200


def test_files_json_lists_only_permitted_paths(project: Path) -> None:
    """The adapter manifest mirrors the serve boundary exactly."""
    with _running_server(project) as address:
        status, headers, body = _request(address, "/.okf/files.json")
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    files = json.loads(body)
    assert {"index.md", "page.md", "sub/index.md", "sub/inner.md"} <= set(files)
    for denied in (
        "secret/hidden.md",
        "fleeting/note.md",
        "deep/fleeting/x.md",
        ".env.md",
        ".git/objects/aa.md",
        "okf-cache/cached.md",
        "draft-7.md",
        "temp-9.md",
        "evil.md",
        "notes.txt",
    ):
        assert denied not in files


def test_files_json_lists_symlink_alias(project: Path) -> None:
    """A permitted symlink alias is listed under its request-visible name."""
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    os.symlink("sub/inner.md", project / "bundle" / "alias-page.md")
    with _running_server(project) as address:
        _, _, body = _request(address, "/.okf/files.json")
    files = json.loads(body)
    assert "alias-page.md" in files


def test_files_json_omits_unreadable_files(project: Path) -> None:
    """A permitted file GET/HEAD would 404 on is not advertised."""
    unreadable = project / "bundle" / "page.md"
    unreadable.chmod(0o000)
    try:
        unreadable.open("rb").close()
        pytest.skip("chmod does not make files unreadable here (root?)")
    except OSError:
        pass
    with _running_server(project) as address:
        _, _, body = _request(address, "/.okf/files.json")
        status, _, _ = _request(address, "/page.md")
    files = json.loads(body)
    assert status == 404
    assert "page.md" not in files


def test_named_bundle_serves_its_root(project: Path) -> None:
    with _running_server(project, bundle_name="other") as address:
        status, _, body = _request(address, "/")
        status_page, _, body_page = _request(address, "/other.md")
    assert status == 200
    assert body == b"# other bundle\n"
    assert status_page == 200
    assert body_page == b"# other page\n"


def test_config_file_under_root_is_never_served(tmp_path: Path) -> None:
    """A config permitted by include globs is still denied at request time."""
    root = tmp_path / "proj2"
    _write(
        root / "okf-core.toml",
        '[bundles.default]\nbundle_root = "."\ninclude = ["**/*"]\n',
    )
    _write(root / "index.md", "# index\n")
    with _running_server(root) as address:
        status, _, _ = _request(address, "/okf-core.toml")
    assert status == 404


def test_bundle_is_byte_identical_after_serving(project: Path) -> None:
    bundle_root = project / "bundle"
    before = _snapshot(bundle_root)
    with _running_server(project) as address:
        _request(address, "/page.md")
        _request(address, "/evil.md")
        _request(address, "/page.md", method="POST")
    assert _snapshot(bundle_root) == before


def test_serve_until_stopped_stops_on_shutdown(project: Path) -> None:
    cfg = load_config(config_path=project / "okf-core.toml")
    httpd = create_server(cfg.bundles["default"], cfg, "127.0.0.1", 0)
    thread = threading.Thread(target=serve_until_stopped, args=(httpd,), daemon=True)
    thread.start()
    httpd.shutdown()
    thread.join(timeout=10)
    assert not thread.is_alive()


def test_serve_until_stopped_closes_on_keyboard_interrupt() -> None:
    class _Interrupted:
        closed = False

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            self.closed = True

    httpd = _Interrupted()
    serve_until_stopped(httpd)  # type: ignore[arg-type]
    assert httpd.closed


def test_include_match_star_does_not_cross_directories() -> None:
    assert _include_match(("public", "page.md"), ("public", "*.md"))
    assert not _include_match(("public", "private", "hidden.md"), ("public", "*.md"))


def test_include_match_double_star_matches_zero_or_more_segments() -> None:
    assert _include_match(("x.md",), ("**", "*.md"))
    assert _include_match(("a", "b", "x.md"), ("**", "*.md"))
    assert _include_match(("a", "b", "x.md"), ("a", "**", "*.md"))
    assert not _include_match(("a", "x.txt"), ("**", "*.md"))


def test_include_match_question_mark_matches_one_segment_char() -> None:
    assert _include_match(("ab.md",), ("a?.md",))
    assert not _include_match(("abb.md",), ("a?.md",))


def test_include_match_leading_bracket_is_literal() -> None:
    assert _include_match(("draft-].md",), ("draft-[]].md",))
    assert not _include_match(("draft-x.md",), ("draft-[]].md",))


def test_malformed_include_glob_matches_nothing(project: Path) -> None:
    """A class fnmatch rejects matches nothing, like ``Path.glob``."""
    cfg = load_config(config_path=project / "okf-core.toml")
    bundle = cfg.bundles["default"].model_copy(update={"include": ("bad[z-a].md",)})
    httpd = create_server(bundle, cfg, "127.0.0.1", 0)
    httpd.server_close()


def test_include_match_malformed_class_never_matches() -> None:
    assert not _include_match(("badz.md",), ("bad[z-a].md",))
    assert not _include_match(("bad-a.md",), ("bad[z-a].md",))


def test_out_of_range_port_rejected(project: Path) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "serve",
            "--config",
            str(project / "okf-core.toml"),
            "--port",
            "65536",
        ],
    )
    assert result.exit_code == 2
    assert "range" in result.output


def test_include_match_character_class_negation() -> None:
    assert _include_match(("draft-x.md",), ("draft-[!0-9].md",))
    assert not _include_match(("draft-7.md",), ("draft-[!0-9].md",))


def test_include_match_unterminated_class_is_literal() -> None:
    assert _include_match(("draft[.md",), ("draft[.md",))
    assert not _include_match(("draftX.md",), ("draft[.md",))


def test_unknown_bundle_exits_2(project: Path) -> None:
    result = CliRunner().invoke(
        cli,
        ["serve", "--config", str(project / "okf-core.toml"), "--bundle", "nope"],
    )
    assert result.exit_code == 2
    assert "not found" in result.output


def test_missing_bundle_root_fails_clearly(project: Path) -> None:
    cfg = load_config(config_path=project / "okf-core.toml")
    bundle = cfg.bundles["default"].model_copy(
        update={"bundle_root": project / "bundle" / "nope"}
    )
    with pytest.raises(ServeError, match="Bundle root does not exist"):
        create_server(bundle, cfg, "127.0.0.1", 0)


def test_occupied_port_gives_clear_error(project: Path) -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    try:
        result = CliRunner().invoke(
            cli,
            [
                "serve",
                "--config",
                str(project / "okf-core.toml"),
                "--port",
                str(port),
            ],
        )
    finally:
        sock.close()
    assert result.exit_code == 1
    assert "Serve error" in result.output


def test_default_host_and_port(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _StubServer:
        server_port = 0

    def fake_create(bundle, config, host, port):
        captured.update(
            {
                "host": host,
                "port": port,
                "bundle": bundle.name,
                "config_path": str(config.config_path),
            }
        )
        return _StubServer()

    monkeypatch.setattr("okf_core.cli.create_server", fake_create)
    monkeypatch.setattr("okf_core.cli.serve_until_stopped", lambda *args: None)
    result = CliRunner().invoke(
        cli, ["serve", "--config", str(project / "okf-core.toml")]
    )
    assert result.exit_code == 0
    assert captured == {
        "host": "127.0.0.1",
        "port": 8080,
        "bundle": "default",
        "config_path": str(project / "okf-core.toml"),
    }
    assert "http://127.0.0.1:0/" in result.output


def test_cli_serves_named_bundle_and_stops_on_sigint(project: Path) -> None:
    """End-to-end: real subprocess, real HTTP, printed URL, clean SIGINT."""
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from okf_core.cli import cli; cli()",
            "serve",
            "--config",
            str(project / "okf-core.toml"),
            "--bundle",
            "other",
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        url_line = ""
        deadline = time.monotonic() + 15
        assert proc.stdout is not None
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if "http://" in line:
                url_line = line.strip()
                break
            if proc.poll() is not None:
                break
        assert (
            url_line
        ), f"server never printed a URL; stderr={proc.stderr and proc.stderr.read()}"
        assert f"http://127.0.0.1:{port}/" in url_line

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/")
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.read() == b"# other bundle\n"
        conn.close()
    finally:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=15)
    assert proc.returncode == 0
