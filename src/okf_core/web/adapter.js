/* OKF bundle browser adapter.
 *
 * Renders raw bundle Markdown client-side through the `okf serve` permitted-
 * file boundary. Routes are `/#/<bundle-path>`; fetches always go to the
 * bundle-root path, so every document fetch is subject to the server-side
 * include/exclude/fleeting/dot-segment denies.
 *
 * Renderer choice: markdown-it (+ markdown-it-anchor) + js-yaml + mermaid,
 * vendored under `vendor/`. markdown-it renders with its default `html:
 * false`, so raw HTML in a note shows as text and its link validator already
 * drops javascript:/vbscript: URLs — no post-render sanitization pass is
 * needed. Rendered markup is composed inside a <template>, which is inert,
 * so image/link rewriting happens before any node can trigger a fetch.
 * Docsify was evaluated for the same role and rejected: its directory landing
 * convention (README.md, not index.md), `?id=` fragment rewriting, _sidebar.md
 * navigation convention, and absence of frontmatter handling would each have
 * needed an overriding plugin for the OKF contract. See docs/decisions/0005.
 */
(function () {
  "use strict";

  var content = document.getElementById("content");
  var statusBox = document.getElementById("status");
  var nav = document.getElementById("nav");
  var mermaidPromise = null;
  var filesPromise = null;

  var md = window.markdownit();
  md.use(window.markdownItAnchor, {
    // GitHub-style unicode slugs so fragment links survive non-ASCII headings.
    slugify: function (s) {
      return s
        .trim()
        .toLowerCase()
        .replace(/[^\p{L}\p{N}\s_-]/gu, "")
        .replace(/\s+/g, "-");
    },
  });

  // ---------- routing ----------

  function parseRoute() {
    var hash = location.hash;
    if (!hash || hash === "#" || hash === "#/") {
      return { path: "index.md", fragment: "" };
    }
    if (!hash.startsWith("#/")) {
      return null;
    }
    var rest = hash.slice(2);
    var frag = "";
    var cut = rest.indexOf("#");
    if (cut >= 0) {
      frag = decodeSafe(rest.slice(cut + 1));
      rest = rest.slice(0, cut);
    }
    rest = decodeSafe(rest);
    if (rest === "" || rest.endsWith("/")) {
      rest += "index.md";
    }
    return { path: rest, fragment: frag };
  }

  function decodeSafe(text) {
    try {
      return decodeURIComponent(text);
    } catch (err) {
      return text;
    }
  }

  function splitSegments(path) {
    var out = [];
    path.split("/").forEach(function (seg) {
      if (seg !== "" && seg !== ".") {
        out.push(seg);
      }
    });
    return out;
  }

  /* Resolve `href` (already URL-decoded) against the directory of `fromPath`
   * per OKF v0.2 §6: leading `/` is bundle-root-relative, otherwise relative
   * to the source document's directory. Returns a normalized bundle-relative
   * path, or null when the path escapes the root. */
  function resolveBundlePath(href, fromPath) {
    var base = fromPath.split("/").slice(0, -1);
    var parts;
    if (href.startsWith("/")) {
      parts = splitSegments(href);
    } else {
      parts = base.concat(splitSegments(href));
    }
    var out = [];
    for (var i = 0; i < parts.length; i += 1) {
      if (parts[i] === "..") {
        if (!out.length) {
          return null;
        }
        out.pop();
      } else {
        out.push(parts[i]);
      }
    }
    return out.join("/");
  }

  function isMarkdown(path) {
    return /\.(md|markdown)$/i.test(path);
  }

  /* Bundle-relative path -> URL path: encode per segment so `#`, `?` and
   * other reserved characters in filenames survive into routes/fetches. */
  function encodeRoutePath(path) {
    return path.split("/").map(encodeURIComponent).join("/");
  }

  /* Rewrite a link/image target into an adapter route or raw bundle URL.
   * Runs on inert <template> content, so stripping `src` here means no
   * request ever leaves the browser. */
  function rewriteTarget(el, attr, fromPath) {
    var raw = el.getAttribute(attr);
    if (!raw) {
      return;
    }
    if (/^(https?:|mailto:|tel:|data:)/i.test(raw)) {
      if (attr === "src" && /^https?:/i.test(raw)) {
        el.removeAttribute("src");
        el.classList.add("okf-broken");
        el.title = "remote image not fetched (external request blocked)";
      }
      return;
    }
    if (attr === "href" && raw.startsWith("#/")) {
      // Already an adapter route (e.g. rendered source links).
      return;
    }
    var frag = "";
    var cut = raw.indexOf("#");
    var href = raw;
    if (cut >= 0) {
      frag = raw.slice(cut);
      href = raw.slice(0, cut);
    }
    href = decodeSafe(href);
    if (href === "") {
      // Pure fragment: same-document anchor.
      el.setAttribute(attr, "#/" + encodeRoutePath(fromPath) + frag);
      return;
    }
    var resolved = resolveBundlePath(href, fromPath);
    if (resolved === null) {
      el.classList.add("okf-broken");
      el.title = "link target escapes the bundle root";
      if (attr === "href") {
        el.removeAttribute("href");
      }
      return;
    }
    if (attr === "href" && (isMarkdown(resolved) || raw.endsWith("/") || resolved === "")) {
      el.setAttribute(attr, "#/" + encodeRoutePath(resolved) + frag);
    } else {
      el.setAttribute(attr, "/" + encodeRoutePath(resolved));
    }
  }

  /* Render `text` into a fresh inert <template> and rewrite every link and
   * image target before it reaches the live DOM. */
  function renderToFragment(text, fromPath) {
    var tpl = document.createElement("template");
    tpl.innerHTML = md.render(text);
    tpl.content.querySelectorAll("a[href]").forEach(function (a) {
      rewriteTarget(a, "href", fromPath);
    });
    tpl.content.querySelectorAll("img").forEach(function (img) {
      rewriteTarget(img, "src", fromPath);
    });
    return tpl.content;
  }

  // ---------- document rendering ----------

  function splitFrontmatter(text) {
    // The closing fence must be a line containing only `---`, matching
    // documents.py _find_frontmatter_close, so `a --- b` in a value
    // cannot end the block early.
    var match = text.match(/^---\r?\n([\s\S]*?)\r?\n---[ \t]*(\r?\n|$)/);
    if (!match) {
      return { frontmatter: null, body: text };
    }
    var data = null;
    try {
      data = window.jsyaml.load(match[1]);
    } catch (err) {
      data = null;
    }
    if (!data || typeof data !== "object") {
      // Unparseable frontmatter stays visible as raw text rather than
      // disappearing silently.
      return { frontmatter: null, body: text };
    }
    return {
      frontmatter: data,
      body: text.slice(match[0].length),
    };
  }

  function sourceEntryHtml(entry, fromPath) {
    var li = document.createElement("li");
    if (typeof entry === "string") {
      li.textContent = entry;
      return li;
    }
    if (!entry || typeof entry !== "object") {
      li.textContent = String(entry);
      return li;
    }
    var label = entry.title || entry.id || entry.resource || JSON.stringify(entry);
    var linked = false;
    if (entry.resource && /^https?:\/\//.test(entry.resource)) {
      var a = document.createElement("a");
      a.href = entry.resource;
      a.textContent = label;
      li.appendChild(a);
      linked = true;
    } else if (entry.resource && /\.(md|markdown)(#.*)?$/.test(entry.resource)) {
      var parts = entry.resource.split("#");
      var resolved = resolveBundlePath(decodeSafe(parts[0]), fromPath);
      if (resolved !== null) {
        var rel = document.createElement("a");
        rel.href = "#/" + encodeRoutePath(resolved) + (parts[1] ? "#" + parts[1] : "");
        rel.textContent = label;
        li.appendChild(rel);
        linked = true;
      }
    }
    if (!linked) {
      li.textContent = label + (entry.resource ? " — " + entry.resource : "");
    }
    var extra = [entry.author, entry.last_modified].filter(Boolean).join(", ");
    if (extra) {
      var span = document.createElement("span");
      span.textContent = " (" + extra + ")";
      li.appendChild(span);
    }
    return li;
  }

  function renderFrontmatter(fm, fromPath) {
    var head = document.createElement("div");
    head.id = "fm-head";
    var title = fm.title;
    if (title) {
      var h1 = document.createElement("h1");
      h1.textContent = String(title);
      head.appendChild(h1);
      document.title = String(title);
    }
    if (fm.description) {
      var p = document.createElement("p");
      p.id = "fm-description";
      p.textContent = String(fm.description);
      head.appendChild(p);
    }
    var sources = document.createElement("section");
    sources.id = "fm-sources";
    if (Array.isArray(fm.sources) && fm.sources.length) {
      var h2 = document.createElement("h2");
      h2.textContent = "Sources";
      sources.appendChild(h2);
      var ul = document.createElement("ul");
      fm.sources.forEach(function (entry) {
        ul.appendChild(sourceEntryHtml(entry, fromPath));
      });
      sources.appendChild(ul);
    }
    return { head: head, sources: sources };
  }

  /* The server computes the permitted-path manifest once per request; the
   * adapter uses it to mark bundle links whose targets are missing or
   * denied, instead of issuing a HEAD request per link. */
  function permittedFiles() {
    if (!filesPromise) {
      filesPromise = fetch("/.okf/files.json", { cache: "no-store" })
        .then(function (res) {
          return res.ok ? res.json() : null;
        })
        .then(function (list) {
          return Array.isArray(list) ? new Set(list) : null;
        })
        .catch(function () {
          return null;
        });
    }
    return filesPromise;
  }

  function markBrokenLinks() {
    permittedFiles().then(function (files) {
      if (!files) {
        console.error("permitted-files manifest unavailable; links unchecked");
        return;
      }
      content.querySelectorAll("a[href^='#/']").forEach(function (a) {
        var target = decodeSafe(a.getAttribute("href").slice(2).split("#")[0]);
        var dirIndex = target ? target.replace(/\/+$/, "") + "/index.md" : "index.md";
        if (!files.has(target) && !files.has(dirIndex)) {
          flagBroken(a, "target unavailable");
        }
      });
    });
  }

  function flagBroken(a, title) {
    a.classList.add("okf-broken");
    a.title = title;
  }

  function renderMermaid() {
    var blocks = content.querySelectorAll("pre > code.language-mermaid");
    if (!blocks.length) {
      return;
    }
    var nodes = [];
    blocks.forEach(function (code) {
      var holder = document.createElement("div");
      holder.className = "mermaid";
      holder.textContent = code.textContent;
      code.parentNode.replaceWith(holder);
      nodes.push(holder);
    });
    if (!mermaidPromise) {
      mermaidPromise = new Promise(function (resolve, reject) {
        var script = document.createElement("script");
        script.src = "/.okf/vendor/mermaid.min.js";
        script.onload = resolve;
        script.onerror = reject;
        document.head.appendChild(script);
      }).then(function () {
        window.mermaid.initialize({ startOnLoad: false });
      });
    }
    mermaidPromise
      .then(function () {
        return window.mermaid.run({ nodes: nodes });
      })
      .catch(function () {
        nodes.forEach(function (node) {
          var pre = document.createElement("pre");
          pre.textContent = node.textContent;
          node.replaceWith(pre);
        });
      });
  }

  function showStatus(title, detail) {
    content.textContent = "";
    statusBox.hidden = false;
    statusBox.textContent = "";
    var h1 = document.createElement("h1");
    h1.textContent = title;
    var p = document.createElement("p");
    p.textContent = detail;
    statusBox.appendChild(h1);
    statusBox.appendChild(p);
  }

  function render() {
    var route = parseRoute();
    statusBox.hidden = true;
    if (route === null) {
      showStatus("Bad route", "Unrecognised location hash: " + location.hash);
      return;
    }
    var resolved = resolveBundlePath(route.path, "x.md");
    if (resolved === null || resolved !== route.path.replace(/^\.\//, "")) {
      showStatus("Outside the bundle", "#/" + route.path + " escapes the bundle root.");
      return;
    }
    fetch("/" + encodeRoutePath(route.path), { cache: "no-store" })
      .then(function (res) {
        if (!res.ok) {
          showStatus(
            "Not available",
            "GET /" + route.path + " returned HTTP " + res.status +
              " (missing, excluded or denied)."
          );
          return null;
        }
        return res.text();
      })
      .then(function (text) {
        if (text === null) {
          return;
        }
        content.textContent = "";
        document.title = "okf bundle";
        if (!isMarkdown(route.path)) {
          var pre = document.createElement("pre");
          pre.textContent = text;
          content.appendChild(pre);
          return;
        }
        var doc = splitFrontmatter(text);
        var body = renderToFragment(doc.body, route.path);
        if (doc.frontmatter) {
          var fm = renderFrontmatter(doc.frontmatter, route.path);
          content.appendChild(fm.head);
          content.appendChild(body);
          content.appendChild(fm.sources);
        } else {
          content.appendChild(body);
        }
        markBrokenLinks();
        renderMermaid();
        if (route.fragment) {
          var target = document.getElementById(route.fragment);
          if (target) {
            target.scrollIntoView();
          }
        } else {
          window.scrollTo(0, 0);
        }
      })
      .catch(function (err) {
        showStatus("Fetch failed", String(err));
      });
  }

  // ---------- navigation ----------

  function navWarning(text) {
    var p = document.createElement("p");
    p.className = "nav-warning";
    p.textContent = text;
    nav.appendChild(p);
  }

  function navFromIndex(text) {
    var doc = splitFrontmatter(text);
    var frag = renderToFragment(doc.body, "index.md");
    frag.querySelectorAll("a[href]").forEach(function (a) {
      if (/(^|\/)log\.md($|#)/i.test(decodeSafe(a.getAttribute("href") || ""))) {
        var li = a.closest("li");
        (li || a).remove();
      }
    });
    var holder = document.createElement("div");
    holder.appendChild(frag);
    nav.appendChild(holder);
  }

  function buildNav() {
    fetch("/index.md", { cache: "no-store" })
      .then(function (res) {
        return res.ok ? res.text() : "";
      })
      .then(function (text) {
        if (text) {
          navFromIndex(text);
        }
      })
      .catch(function (err) {
        console.error("navigation load failed", err);
        navWarning("navigation unavailable");
      });
  }

  document.getElementById("refresh").addEventListener("click", function () {
    filesPromise = null;
    render();
  });
  window.addEventListener("hashchange", render);
  buildNav();
  render();
})();
