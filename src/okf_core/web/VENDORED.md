# Vendored browser libraries

Files in `vendor/` are verbatim upstream minified builds shipped inside
`okf-core` so `okf serve` works fully offline. They are not covered by
the repo's Apache-2.0 license header convention; each retains its own
license.

| File | Upstream | Version | License | Source |
| --- | --- | --- | --- | --- |
| `marked.min.js` | marked | 12.0.2 | MIT | https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js |
| `js-yaml.min.js` | js-yaml | 4.1.0 | MIT | https://cdn.jsdelivr.net/npm/js-yaml@4.1.0/dist/js-yaml.min.js |
| `mermaid.min.js` | mermaid | 11.12.0 | MIT | https://cdn.jsdelivr.net/npm/mermaid@11.12.0/dist/mermaid.min.js |

To upgrade: download the new minified build, replace the file, update
this table, and re-run the browser oracle
(`pytest tests/test_browser_render.py` with the `browser` extra
installed and a browser on `PATH`).
