# ADR-0005: Local browser renderer is a thin client-side adapter

- **Status:** ACCEPTED
- **Related:** `src/okf_core/serve.py` (the `okf serve` boundary this ADR
  builds on, CPB-32), `src/okf_core/web/` (the adapter), Linear CCP-783
  (CPB-31).

## Context

`okf serve` (CPB-32, merged in #241) serves a configured bundle over a
read-only localhost HTTP boundary: permitted files only, no directory
listings, hard denies for `fleeting`, dot-segments, cache, and config.
CPB-31 asks for a way to *browse* a synthetic bundle in a browser — raw
Markdown rendered live, frontmatter surfaced without raw YAML, OKF link
rules resolved, broken links shown without killing the site, Mermaid
fences rendered — without committing generated HTML and without a custom
framework. The ticket explicitly requires evaluating an existing renderer
first and recording the actual choice.

Docsify was evaluated as the existing-renderer candidate. It is a capable
client-side Markdown SPA, but nearly every load-bearing convention it
brings contradicts the OKF contract this adapter must honour:

- its landing page is `README.md` (or a configured homepage), not the
  directory's `index.md`;
- its router rewrites `?id=` fragments rather than following OKF §6 link
  targets;
- navigation comes from `_sidebar.md`/`_navbar.md` conventions, not the
  bundle's `index.md`;
- it has no frontmatter rendering of title/description/sources;
- broken-link handling, `fleeting`/excluded denials, and `log.md`
  viewable-but-not-navigable are all OKF-specific and would each need an
  overriding plugin.

Using Docsify would have meant disabling or overriding most of its core
behaviour — at that point the "existing renderer" is a shell around
plugins doing the real work anyway.

## Decision

1. **A thin client-side adapter ships inside `okf-core`** at
   `src/okf_core/web/` and is served by `okf serve` under the reserved
   prefix `/.okf/`. Dot-segments are already hard-denied for bundle
   content, so a permitted bundle file can never shadow or be shadowed by
   an adapter asset.
2. **The adapter lives only at `/.okf/`** (`/.okf/` itself serves
   `index.html`). There is no content negotiation anywhere: every other
   path serves raw bundle files exactly as CPB-32 served them, and the
   CLI prints the `/.okf/` URL as the browser entry point.
3. **The adapter is plain JavaScript plus three vendored libraries** —
   `markdown-it` (Markdown → HTML, default `html: false`) with
   `markdown-it-anchor` (GitHub-style unicode heading slugs), `js-yaml`
   (frontmatter), and `mermaid` (fences lazy-loaded on first Mermaid
   block) — no build step, no framework, no runtime dependency on
   Python. `html: false` means raw HTML in a note renders as inert text
   and the engine's link validator already drops `javascript:`/`vbscript:`
   URLs, so no post-render sanitization pass exists. Rendered markup is
   composed inside a `<template>` (inert DOM), so remote image `src` is
   stripped before any node can trigger a request. Vendored copies in
   `src/okf_core/web/vendor/` keep `okf serve` fully offline.
4. **Routing is `#/<bundle-path>` hash routing.** Directory routes land
   on that directory's `index.md`; an empty route lands on the root
   `index.md`. The adapter fetches `/{path}` directly through the CPB-32
   boundary, so include/exclude/fleeting/denial rules apply to browser
   fetches identically — nothing client-side can widen the boundary.
5. **OKF §6 link semantics resolve client-side.** Root-relative
   (`/x.md`), relative (`./x.md`), parent-relative (`../x.md`), and
   fragment links are rewritten against the source document's directory.
   Broken links are marked `okf-broken` against `/.okf/files.json`, a
   live manifest of permitted paths computed by the server — one request
   per render instead of a HEAD per link.
6. **Frontmatter renders, raw YAML never does.** `title`, `description`,
   and `sources` get dedicated rendering; unknown fields are ignored for
   display. The closing frontmatter fence must be a whole `---` line,
   matching `documents.py`.
7. **Navigation comes from the rendered root `index.md`.** Nothing in
   okf-core produces a `concepts.json` nav catalog, so no schema is
   guessed; `log.md` is filtered from nav but remains viewable by route.
8. **The regression oracle is Playwright** (`tests/test_browser_render.py`,
   optional `browser` extra) driving a real browser over two synthetic
   bundles, covering navigation, link/fragment resolution, frontmatter,
   broken links, Mermaid, and denied-fetch paths.

## Alternatives rejected

- **Docsify (or another Markdown SPA) with OKF plugins** — rejected. Its
  landing-page, router, and nav conventions each contradict the OKF
  contract, so it would host the same amount of custom code with less
  control.
- **Quartz** — rejected. Its graph view would have been nice, but it is
  not worth Node, a second server, and rebuilding the `okf serve`
  boundary to get it.
- **marked as the vendored renderer** — rejected on review. markdown-it
  escapes raw HTML by default and validates link protocols, which
  removes the sanitization denylist and the fetch-ordering workaround a
  raw-HTML pipeline needed.
- **Server-side rendering to HTML** — rejected. It would need generated
  HTML (committed or cached), a template layer in Python, and a second
  content type through the serving boundary; the ticket wants raw
  Markdown rendered live.
- **A Python-bundled JS framework with a build step** — rejected. The
  ticket forbids custom framework code and the repo has no JS toolchain;
  vendored browser-global scripts keep the diff reviewable and `pip
  install` sufficient.
- **Multiple adapters (e.g. pluggable themes/backends)** — rejected as
  scope. The ticket calls for one shared adapter; a second bundle reuses
  it with no copying, which is all the reuse the contract demands.

## Revisit trigger

Reopen this decision if OKF browsing needs richer interaction than the
adapter can carry without growing a framework (live search, graph view,
editing), if the vendored libraries become a maintenance burden, or if
something real starts producing a `concepts.json`-style nav catalog the
adapter should consume.
