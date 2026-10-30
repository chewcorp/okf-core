"""Browser smoke/regression oracle for the ``okf serve`` adapter.

Drives a real headless browser against a running server over synthetic
bundles: navigation, OKF link/fragment resolution, frontmatter display,
broken links, Mermaid fences, and denied fetches through the permitted-file
boundary. Requires the ``browser`` extra (``pip install -e ".[test,browser]"``)
and a Chromium-family binary; skipped when neither is available.
"""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

playwright_sync = pytest.importorskip(
    "playwright.sync_api", reason="playwright not installed (browser extra)"
)

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Page, Playwright

from okf_core.config import load_config
from okf_core.serve import create_server

_CONFIG = """\
[bundles.alpha]
bundle_root = "alpha"
include = ["**/*.md", "**/*.json"]
exclude = ["secret/**"]

[bundles.beta]
bundle_root = "beta"
"""

_NOTE_A = """\
---
type: concept
title: Alpha Root Note
description: The alpha root concept summary.
sources:
  - id: spec
    resource: https://example.com/spec
    title: External spec
  - resource: shared/ledger.md#shared-ledger
---

# Alpha Root Note

Body of the root note. See [sibling](./sibling.md), the
[root-relative note](/deep/down/note.md), the
[odd file](./odd%23name.md), and
[a missing note](./not-here.md).
"""

_SIBLING = (
    """\
---
type: concept
title: Sibling Note
description: Sibling summary.
---

# Sibling

Jump to [the section](#target-section) or the
[root note's section](/root-note.md#sibling-anchor).

"""
    + "\n".join(
        f"Filler paragraph {i} keeps the target below the fold.\n" for i in range(40)
    )
    + """\

## Target Section

Deep content lives here.
"""
)

_LEDGER = """\
---
type: concept
title: Shared Ledger
---

# Shared Ledger

Back to [alpha root](/root-note.md).
"""

_DEEP = """\
---
type: concept
title: Deep Note
---

# Deep Note

See the [parent-relative note](../up-note.md).

```mermaid
graph TD
  A[alpha] --> B[beta]
```
"""

_UP_NOTE = """\
---
type: concept
title: Up Note
---

# Up Note
"""

_DEEP_INDEX = """\
# Deep Section

* [Deep Note](down/note.md) - nested concept
"""

_INDEX = """\
# Alpha Bundle

* [Alpha Root Note](root-note.md) - the root
* [Deep Note](deep/down/note.md) - nested
* [Update Log](log.md) - history
"""

_LOG = """\
# Log

## 2026-10-01

- **Update**: entry
"""

_CONCEPTS_JSON = """\
{
  "concepts": [
    {"concept_id": "root-note", "path": "root-note.md", "title": "Alpha Root Note"},
    {"concept_id": "deep/down/note", "path": "deep/down/note.md", "title": "Deep Note"},
    {"concept_id": "log", "path": "log.md", "title": "Update Log"}
  ]
}
"""

_ODD = """\
# Odd Name

A file whose name contains a `#`.
"""

_BAD_FM = """\
---
title: [unclosed
---

# Bad Frontmatter

This frontmatter cannot be parsed.
"""

_HOSTILE = """\
# Hostile Note

<script>window.__xss = 1</script>

<img src="x" onerror="window.__err = 1">

![remote](https://example.com/track.png)
"""

_BETA_INDEX = """\
# Beta Bundle

* [Beta Note](beta-note.md) - the beta page
"""

_BETA_NOTE = """\
---
type: concept
title: Beta Note
description: Beta summary.
---

# Beta Note
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    _write(root / "okf-core.toml", _CONFIG)
    bundle = root / "alpha"
    _write(bundle / "index.md", _INDEX)
    _write(bundle / "concepts.json", _CONCEPTS_JSON)
    _write(bundle / "root-note.md", _NOTE_A)
    _write(bundle / "sibling.md", _SIBLING)
    _write(bundle / "log.md", _LOG)
    _write(bundle / "deep" / "index.md", _DEEP_INDEX)
    _write(bundle / "deep" / "down" / "note.md", _DEEP)
    _write(bundle / "deep" / "up-note.md", _UP_NOTE)
    _write(bundle / "odd#name.md", _ODD)
    _write(bundle / "bad-fm.md", _BAD_FM)
    _write(bundle / "hostile.md", _HOSTILE)
    _write(bundle / "shared" / "ledger.md", _LEDGER)
    _write(bundle / "secret" / "hidden.md", "# hidden\n")
    _write(bundle / "fleeting" / "draft.md", "# draft\n")
    _write(tmp_path / "outside.md", "# outside\n")
    if hasattr(os, "symlink"):
        os.symlink("../../../outside.md", bundle / "deep" / "leak.md")
    _write(root / "beta" / "index.md", _BETA_INDEX)
    _write(root / "beta" / "beta-note.md", _BETA_NOTE)
    return root


@contextlib.contextmanager
def _running_server(project: Path, bundle_name: str = "alpha") -> Iterator[str]:
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


def _chrome_candidates() -> list[str]:
    candidates = []
    env_path = os.environ.get("OKF_BROWSER")
    if env_path:
        candidates.append(env_path)
    candidates.append("/opt/google/chrome/chrome")
    devin = Path("/opt/.devin/chrome")
    if devin.is_dir():
        candidates.extend(
            str(p) for p in sorted(devin.glob("chrome/*/chrome-linux64/chrome"))
        )
    return candidates


def _try_launch(p: Playwright, **kwargs: Any) -> Browser | None:
    try:
        return p.chromium.launch(**kwargs)
    except playwright_sync.Error:
        return None


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    attempts: list[dict[str, Any]] = [
        {"executable_path": path, "headless": True, "args": ["--no-sandbox"]}
        for path in _chrome_candidates()
        if Path(path).is_file()
    ]
    attempts += [
        {"channel": "chrome", "headless": True},
        {"headless": True},
    ]
    with playwright_sync.sync_playwright() as p:
        b = next((b for b in (_try_launch(p, **kw) for kw in attempts) if b), None)
        if b is None:
            pytest.skip("no Chromium-family browser found")
        yield b
        b.close()


def _page(browser: Browser, address: str, route: str = "") -> Page:
    page = browser.new_page()
    page.goto(f"http://{address}/{route}")
    page.wait_for_selector("#nav a, #status:not([hidden]), article h1", timeout=15_000)
    return page


def test_root_landing_renders_index(browser: Browser, project: Path) -> None:
    with _running_server(project) as address:
        page = _page(browser, address)
        assert page.inner_text("article h1") == "Alpha Bundle"
        page.close()


def test_frontmatter_shown_without_raw_yaml(browser: Browser, project: Path) -> None:
    with _running_server(project) as address:
        page = _page(browser, address, "#/root-note.md")
        assert page.inner_text("#fm-head h1") == "Alpha Root Note"
        assert page.inner_text("#fm-description") == "The alpha root concept summary."
        sources = page.inner_text("#fm-sources")
        assert "External spec" in sources
        assert "ledger.md" in sources
        ledger_href = page.get_attribute("#fm-sources a[href*='ledger']", "href")
        assert ledger_href and ledger_href.endswith("#/shared/ledger.md#shared-ledger")
        assert "```" not in page.inner_text("article")
        assert "---" not in page.inner_text("#fm-head")
        assert "---" not in page.inner_text("article")
        assert "type: concept" not in page.inner_text("article")
        page.close()


def test_nav_driven_by_concepts_json(browser: Browser, project: Path) -> None:
    with _running_server(project) as address:
        page = _page(browser, address)
        nav = page.inner_text("#nav")
        assert "Alpha Root Note" in nav
        assert "Deep Note" in nav
        assert "Update Log" not in nav
        page.close()


def test_relative_link_navigates(browser: Browser, project: Path) -> None:
    with _running_server(project) as address:
        page = _page(browser, address, "#/root-note.md")
        page.click("text=sibling")
        page.wait_for_selector("article:has-text('Sibling Note')", timeout=10_000)
        assert page.url.endswith("#/sibling.md")
        page.close()


def test_parent_relative_link_navigates(browser: Browser, project: Path) -> None:
    with _running_server(project) as address:
        page = _page(browser, address, "#/deep/down/note.md")
        page.click("text=parent-relative note")
        page.wait_for_selector("article:has-text('Up Note')", timeout=10_000)
        assert page.url.endswith("#/deep/up-note.md")
        page.close()


def test_bundle_root_absolute_link_navigates(browser: Browser, project: Path) -> None:
    with _running_server(project) as address:
        page = _page(browser, address, "#/root-note.md")
        page.click("text=root-relative note")
        page.wait_for_selector("article:has-text('Deep Note')", timeout=10_000)
        assert page.url.endswith("#/deep/down/note.md")
        page.close()


def test_fragment_link_scrolls_to_heading(browser: Browser, project: Path) -> None:
    with _running_server(project) as address:
        page = _page(browser, address, "#/sibling.md")
        before = page.evaluate(
            "document.getElementById('target-section')" ".getBoundingClientRect().top"
        )
        assert before > 700
        page.click("text=the section")
        page.wait_for_function(
            "document.getElementById('target-section')"
            ".getBoundingClientRect().top < window.innerHeight",
            timeout=10_000,
        )
        page.close()


def test_directory_route_lands_on_index(browser: Browser, project: Path) -> None:
    with _running_server(project) as address:
        page = _page(browser, address, "#/deep/")
        assert page.inner_text("article h1") == "Deep Section"
        page.close()


def test_broken_link_marked_and_site_survives(browser: Browser, project: Path) -> None:
    with _running_server(project) as address:
        page = _page(browser, address, "#/root-note.md")
        page.wait_for_selector("a.okf-broken", timeout=15_000)
        broken = page.locator("a.okf-broken")
        assert broken.count() >= 1
        assert "a missing note" in broken.first.inner_text()
        assert "Alpha Root Note" in page.inner_text("article")
        page.close()


def test_mermaid_fence_renders_svg(browser: Browser, project: Path) -> None:
    with _running_server(project) as address:
        page = _page(browser, address, "#/deep/down/note.md")
        page.wait_for_selector(".mermaid svg", timeout=20_000)
        page.close()


def test_log_md_viewable_but_not_navigated(browser: Browser, project: Path) -> None:
    with _running_server(project) as address:
        page = _page(browser, address, "#/log.md")
        assert "Log" in page.inner_text("article")
        assert "Update Log" not in page.inner_text("#nav")
        page.close()


@pytest.mark.parametrize(
    "path",
    [
        "/fleeting/draft.md",
        "/secret/hidden.md",
        "/%2e%2e/outside.md",
        "/deep/leak.md",
    ],
)
def test_denied_fetches_unavailable_through_server(
    browser: Browser, project: Path, path: str
) -> None:
    with _running_server(project) as address:
        page = browser.new_page()  # type: ignore[attr-defined]
        resp = page.request.get(f"http://{address}{path}")
        assert resp.status == 404
        page.close()


def test_denied_route_shows_status_page(browser: Browser, project: Path) -> None:
    with _running_server(project) as address:
        page = _page(browser, address, "#/fleeting/draft.md")
        assert "Not available" in page.inner_text("#status")
        page.close()


def test_second_bundle_reuses_same_adapter(browser: Browser, project: Path) -> None:
    with _running_server(project, bundle_name="beta") as address:
        page = _page(browser, address)
        assert page.inner_text("article h1") == "Beta Bundle"
        page.click("text=Beta Note")
        page.wait_for_selector("article:has-text('Beta summary')", timeout=10_000)
        assert page.url.endswith("#/beta-note.md")
        assert "Beta Note" in page.inner_text("#nav")
        page.close()


def test_encoded_filename_link_navigates(browser: Browser, project: Path) -> None:
    with _running_server(project) as address:
        page = _page(browser, address, "#/root-note.md")
        page.click("text=odd file")
        page.wait_for_selector("article:has-text('Odd Name')", timeout=10_000)
        assert page.url.endswith("#/odd%23name.md")
        page.close()


def test_malformed_concepts_json_falls_back_to_index_nav(
    browser: Browser, project: Path
) -> None:
    (project / "alpha" / "concepts.json").write_text("{ not json")
    with _running_server(project) as address:
        page = _page(browser, address)
        assert page.locator("#nav a[href$='root-note.md']").count() == 1
        page.close()


def test_fragment_prefers_body_heading_over_frontmatter_title(
    browser: Browser, project: Path
) -> None:
    with _running_server(project) as address:
        page = _page(browser, address, "#/root-note.md")
        fm_id = page.get_attribute("#fm-head h1", "id")
        assert not fm_id
        body_h1 = page.locator("article h1").last
        assert body_h1.get_attribute("id") == "alpha-root-note"
        page.close()


def test_invalid_frontmatter_stays_visible(browser: Browser, project: Path) -> None:
    with _running_server(project) as address:
        page = _page(browser, address, "#/bad-fm.md")
        article = page.inner_text("article")
        assert "unclosed" in article
        assert "Bad Frontmatter" in article
        page.close()


def test_rendered_html_is_sanitized(browser: Browser, project: Path) -> None:
    with _running_server(project) as address:
        page = _page(browser, address, "#/hostile.md")
        assert page.evaluate("() => window.__xss === undefined")
        assert page.evaluate("() => window.__err === undefined")
        assert page.locator("article script").count() == 0
        remote_img = page.locator("article img.okf-broken")
        assert remote_img.count() == 1
        assert remote_img.get_attribute("src") is None
        page.close()
