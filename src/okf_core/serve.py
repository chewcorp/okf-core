"""Read-only localhost HTTP serving for one configured bundle.

``okf serve`` exposes a bundle's permitted files over a foreground
``http.server`` so a reader can browse the rendered Markdown without picking
a separate static-server root. The serving contract is checked per request
against the resolved bundle root:

- the configured ``include`` globs must match and ``exclude`` globs must not
- any ``fleeting`` segment, at any depth, is denied
- any dot-segment (``.git``, ``.env``, cache sidecar dirs) is denied
- the resolved ``okf_cache_dir`` and the config file itself are denied
- requests that escape the root (``..``, encoded traversal, symlink-out) get a
  404 like every other denied or missing path

Directories never produce a listing: a directory URL redirects to a trailing
slash and then serves that directory's permitted ``index.md``, or 404.

On top of raw file serving the server ships a small client-side adapter (the
files under ``okf_core/web``) so a browser can render a bundle's Markdown with
frontmatter, links and diagrams. Adapter assets live under the reserved
``/.okf/`` prefix, which is safe because dot-segments are already hard-denied
for bundle content, so no permitted bundle file can ever be shadowed. ``/.okf/``
serves the adapter page itself and ``/.okf/files.json`` is a live manifest of
permitted root-relative paths the adapter uses for broken-link checks.
"""

from __future__ import annotations

import fnmatch
import json
import mimetypes
import os
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from okf_core.config import BundleConfig, OkfConfig

_FLEETING_SEGMENT = "fleeting"

_MARKDOWN_SUFFIXES = {".md", ".markdown"}

_ADAPTER_PREFIX = "/.okf/"

_WEB_DIR = Path(__file__).parent / "web"


class ServeError(Exception):
    """Raised when the bundle server cannot start."""


@dataclass(frozen=True)
class _ServeContext:
    """Precomputed serving rules for one bundle."""

    root: Path
    include: tuple[tuple[str, ...], ...]
    exclude: tuple[str, ...]
    cache_dir: Path | None
    config_path: Path | None


def _include_match(parts: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    """Match path segments against ``Path.glob``-style pattern segments.

    ``fnmatch.fnmatchcase`` gives the segment semantics ``Path.glob``
    uses for ``*``/``?``/``[seq]`` -- ``*`` cannot cross a directory
    boundary. A whole-segment ``**`` matches zero or more segments, so
    ``**/*.md`` covers ``x.md`` at the root -- the case ``PurePath.match``
    gets wrong and ``PurePath.full_match`` only fixes on Python 3.13+.
    """
    if not pattern:
        return not parts
    head = pattern[0]
    if head == "**":
        return any(
            _include_match(parts[i:], pattern[1:]) for i in range(len(parts) + 1)
        )
    return (
        bool(parts)
        and fnmatch.fnmatchcase(parts[0], head)
        and _include_match(parts[1:], pattern[1:])
    )


def _is_denied(rel: PurePosixPath) -> bool:
    """Hard denies that apply regardless of configured globs."""
    for part in rel.parts:
        if part.startswith(".") or part == _FLEETING_SEGMENT:
            return True
    return False


def _is_permitted(rel: PurePosixPath, ctx: _ServeContext) -> bool:
    if not any(_include_match(rel.parts, pat) for pat in ctx.include):
        return False
    return not any(rel.match(pattern) for pattern in ctx.exclude)


def _served_rel(candidate: Path, ctx: _ServeContext) -> PurePosixPath | None:
    """Return the root-relative path a resolved candidate may be served as."""
    try:
        rel = PurePosixPath(candidate.relative_to(ctx.root).as_posix())
    except ValueError:
        return None
    if _is_denied(rel):
        return None
    if ctx.cache_dir is not None and candidate.is_relative_to(ctx.cache_dir):
        return None
    if ctx.config_path is not None and candidate == ctx.config_path:
        return None
    return rel


def _resolve_request(url: str, ctx: _ServeContext) -> tuple[Path, PurePosixPath] | None:
    """Resolve a request URL to a candidate path inside the root, or None.

    Applies containment (traversal, encoded traversal, symlink escape) and
    the hard denies on both the request path and the resolved target, so a
    ``fleeting``/dot-segment in the URL or in the target is denied alike.
    Include/exclude permission is checked separately by the caller so
    directories can be handled first.
    """
    decoded = unquote(urlsplit(url).path)
    parts = [p for p in decoded.split("/") if p not in ("", ".")]
    if any(p == ".." or "\\" in p or "\x00" in p for p in parts):
        return None
    if _is_denied(PurePosixPath(*parts)):
        return None
    try:
        candidate = ctx.root.joinpath(*parts).resolve(strict=False)
    except OSError:
        return None
    rel = _served_rel(candidate, ctx)
    if rel is None:
        return None
    return candidate, rel


def _content_type(path: Path) -> str:
    if path.suffix.lower() in _MARKDOWN_SUFFIXES:
        return "text/markdown; charset=utf-8"
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _permitted_files(ctx: _ServeContext) -> list[str]:
    """Root-relative request paths the server would serve.

    Entries are the on-disk (unresolved) paths a request would use, so a
    permitted symlink alias lists under its own name. Permission is
    checked on the resolved target exactly like ``_serve`` does, and a
    file that cannot be opened is omitted because GET/HEAD return 404.
    """
    out = []
    for path in sorted(ctx.root.rglob("*")):
        if not path.is_file():
            continue
        rel_req = PurePosixPath(path.relative_to(ctx.root).as_posix())
        if _is_denied(rel_req):
            continue
        try:
            resolved = path.resolve(strict=False)
        except OSError:
            continue
        rel = _served_rel(resolved, ctx)
        if rel is None or not _is_permitted(rel, ctx):
            continue
        try:
            with resolved.open("rb"):
                pass
        except OSError:
            continue
        out.append(rel_req.as_posix())
    return out


def _adapter_assets() -> dict[str, tuple[bytes, str]]:
    """Map ``/.okf/<path>`` URLs to packaged adapter asset bytes and types.

    Exact-key matching means traversal, encoded segments and unknown names
    simply miss the map and fall through to the normal (denied) bundle path
    resolution.
    """
    assets: dict[str, tuple[bytes, str]] = {}
    for file in sorted(_WEB_DIR.rglob("*")):
        if file.is_file():
            rel = file.relative_to(_WEB_DIR).as_posix()
            assets[_ADAPTER_PREFIX + rel] = (file.read_bytes(), _content_type(file))
    assets[_ADAPTER_PREFIX] = assets[_ADAPTER_PREFIX + "index.html"]
    return assets


class _BundleHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], ctx: _ServeContext):
        self.ctx = ctx
        self.assets = _adapter_assets()
        super().__init__(server_address, _BundleRequestHandler)


class _BundleRequestHandler(BaseHTTPRequestHandler):
    server: _BundleHTTPServer
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        # Keep-alive connections hold one thread each; cap their idle time.
        self.request.settimeout(30)

    def handle(self) -> None:
        try:
            super().handle()
        except TimeoutError:
            pass

    def do_GET(self) -> None:
        self._serve(write_body=True)

    def do_HEAD(self) -> None:
        self._serve(write_body=False)

    def do_POST(self) -> None:
        self._method_not_allowed()

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_DELETE(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        self.send_response(405, "Method Not Allowed")
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_asset(self, asset: tuple[bytes, str], *, write_body: bool) -> None:
        data, content_type = asset
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if write_body:
            self.wfile.write(data)

    def _serve(self, *, write_body: bool) -> None:
        ctx = self.server.ctx
        decoded_path = unquote(urlsplit(self.path).path)
        if decoded_path == _ADAPTER_PREFIX + "files.json":
            self._send_asset(
                (
                    json.dumps(_permitted_files(ctx)).encode("utf-8"),
                    "application/json",
                ),
                write_body=write_body,
            )
            return
        asset = self.server.assets.get(decoded_path)
        if asset is not None:
            self._send_asset(asset, write_body=write_body)
            return
        resolved = _resolve_request(self.path, ctx)
        if resolved is None:
            self.send_error(404)
            return
        candidate, rel = resolved

        if candidate.is_dir():
            url_path = urlsplit(self.path).path
            if not url_path.endswith("/"):
                query = urlsplit(self.path).query
                self.send_response(301)
                self.send_header(
                    "Location", url_path + "/" + (f"?{query}" if query else "")
                )
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            try:
                index = (candidate / "index.md").resolve(strict=False)
            except OSError:
                self.send_error(404)
                return
            index_rel = _served_rel(index, ctx)
            if (
                index_rel is None
                or not index.is_file()
                or not _is_permitted(index_rel, ctx)
            ):
                self.send_error(404)
                return
            candidate = index
        elif not candidate.is_file() or not _is_permitted(rel, ctx):
            self.send_error(404)
            return

        if write_body:
            try:
                data = candidate.read_bytes()
            except OSError:
                self.send_error(404)
                return
            size = len(data)
        else:
            try:
                # Open rather than stat alone so HEAD agrees with GET on
                # unreadable files without loading the body.
                with candidate.open("rb") as fh:
                    size = os.fstat(fh.fileno()).st_size
            except OSError:
                self.send_error(404)
                return
            data = b""
        self.send_response(200)
        self.send_header("Content-Type", _content_type(candidate))
        self.send_header("Content-Length", str(size))
        self.end_headers()
        if write_body:
            self.wfile.write(data)


def create_server(
    bundle: BundleConfig,
    config: OkfConfig | None,
    host: str,
    port: int,
) -> _BundleHTTPServer:
    """Bind the read-only server for ``bundle``; raise ServeError on failure."""
    if not bundle.bundle_root.is_dir():
        raise ServeError(f"Bundle root does not exist: {bundle.bundle_root}")
    ctx = _ServeContext(
        root=bundle.bundle_root,
        include=tuple(tuple(p.split("/")) for p in bundle.include),
        exclude=bundle.exclude,
        cache_dir=bundle.okf_cache_dir,
        config_path=config.config_path if config is not None else None,
    )
    try:
        return _BundleHTTPServer((host, port), ctx)
    except OSError as exc:
        raise ServeError(
            f"Cannot listen on http://{host}:{port}/: {exc.strerror or exc}"
        ) from exc


def serve_until_stopped(httpd: _BundleHTTPServer) -> None:
    """Run the server in the foreground until Ctrl-C, then close it."""
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
