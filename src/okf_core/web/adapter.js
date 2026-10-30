/* OKF bundle browser adapter.
 *
 * Renders raw bundle Markdown client-side through the `okf serve` permitted-
 * file boundary. Routes are `/#/<bundle-path>`; fetches always go to the
 * bundle-root path, so every document fetch is subject to the server-side
 * include/exclude/fleeting/dot-segment denies.
 *
 * Renderer choice: marked + js-yaml + mermaid, vendored under `vendor/`.
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
  var brokenCache = {};
  var mermaidPromise = null;

  // ---------- routing ----------

  function parseRoute() {
    var hash = location.hash;
    if (!hash || hash === "#" || hash === "#/") {
      return { path: "index.md", fragment: "" };
    }
    if (!hash.startsWith("#/")) {
      return null;
    }
    var rest = decodeSafe(hash.slice(2));
    var frag = "";
    var cut = rest.indexOf("#");
    if (cut >= 0) {
      frag = rest.slice(cut + 1);
      rest = rest.slice(0, cut);
    }
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

  /* Rewrite a link/image target into an adapter route or raw bundle URL. */
  function rewriteTarget(el, attr, fromPath) {
    var raw = el.getAttribute(attr);
    if (!raw || /^(https?:|mailto:|tel:|data:)/i.test(raw)) {
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
      el.setAttribute(attr, "#/" + fromPath + frag);
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
      el.setAttribute(attr, "#/" + resolved + frag);
    } else {
      el.setAttribute(attr, "/" + encodeURI(resolved));
    }
  }

  // ---------- document rendering ----------

  function splitFrontmatter(text) {
    var match = text.match(/^---\r?\n([\s\S]*?)\r?\n?---\r?\n?/);
    if (!match) {
      return { frontmatter: null, body: text };
    }
    var data = null;
    try {
      data = window.jsyaml.load(match[1]);
    } catch (err) {
      data = null;
    }
    return {
      frontmatter: data && typeof data === "object" ? data : null,
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
      var resolved = resolveBundlePath(entry.resource.split("#")[0], fromPath);
      if (resolved !== null) {
        var rel = document.createElement("a");
        rel.href = "#/" + resolved;
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

  function slugIds(root) {
    var seen = {};
    root.querySelectorAll("h1, h2, h3, h4, h5, h6").forEach(function (h) {
      if (h.id) {
        return;
      }
      var slug = h.textContent
        .toLowerCase()
        .replace(/[^\w\s-]/g, "")
        .trim()
        .replace(/\s+/g, "-");
      var base = slug || "section";
      var n = seen[base] || 0;
      seen[base] = n + 1;
      h.id = n ? base + "-" + n : base;
    });
  }

  function markBrokenLinks(fromPath) {
    content.querySelectorAll("a[href^='#/']").forEach(function (a) {
      var target = a.getAttribute("href").slice(2).split("#")[0];
      if (brokenCache[target] === false) {
        flagBroken(a, "target unavailable");
        return;
      }
      if (brokenCache[target] === true) {
        return;
      }
      fetch("/" + encodeURI(target), { method: "HEAD", cache: "no-store" })
        .then(function (res) {
          brokenCache[target] = res.ok;
          if (!res.ok) {
            flagBroken(a, "target unavailable (HTTP " + res.status + ")");
          }
        })
        .catch(function () {
          flagBroken(a, "target check failed");
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
    fetch("/" + encodeURI(route.path), { cache: "no-store" })
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
        var bodyHolder = document.createElement("div");
        bodyHolder.innerHTML = window.marked.parse(doc.body, { async: false });
        if (doc.frontmatter) {
          var fm = renderFrontmatter(doc.frontmatter, route.path);
          content.appendChild(fm.head);
          content.appendChild(bodyHolder);
          content.appendChild(fm.sources);
        } else {
          content.appendChild(bodyHolder);
        }
        slugIds(content);
        content.querySelectorAll("a[href]").forEach(function (a) {
          rewriteTarget(a, "href", route.path);
        });
        content.querySelectorAll("img[src]").forEach(function (img) {
          rewriteTarget(img, "src", route.path);
        });
        markBrokenLinks(route.path);
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

  function navLink(text, path) {
    var li = document.createElement("li");
    var a = document.createElement("a");
    a.href = "#/" + path;
    a.textContent = text;
    li.appendChild(a);
    return li;
  }

  function navFromConcepts(json) {
    var list = Array.isArray(json) ? json : json.concepts;
    if (!Array.isArray(list)) {
      return false;
    }
    var ul = document.createElement("ul");
    var count = 0;
    list.forEach(function (concept) {
      var path = concept && (concept.path || concept.concept_id);
      if (!path || /(^|\/)log\.md$/i.test(path)) {
        return;
      }
      ul.appendChild(navLink(concept.title || concept.concept_id || path, path));
      count += 1;
    });
    if (!count) {
      return false;
    }
    nav.appendChild(ul);
    return true;
  }

  function navFromIndex(text) {
    var doc = splitFrontmatter(text);
    var holder = document.createElement("div");
    holder.innerHTML = window.marked.parse(doc.body, { async: false });
    holder.querySelectorAll("a[href]").forEach(function (a) {
      rewriteTarget(a, "href", "index.md");
      if (/(^|\/)log\.md($|#)/i.test(decodeSafe(a.getAttribute("href") || ""))) {
        a.closest("li") ? a.closest("li").remove() : a.remove();
      }
    });
    nav.appendChild(holder);
  }

  function buildNav() {
    fetch("/concepts.json", { cache: "no-store" })
      .then(function (res) {
        return res.ok ? res.json() : null;
      })
      .then(function (json) {
        if (json && navFromConcepts(json)) {
          return;
        }
        return fetch("/index.md", { cache: "no-store" })
          .then(function (res) {
            return res.ok ? res.text() : "";
          })
          .then(function (text) {
            if (text) {
              navFromIndex(text);
            }
          });
      })
      .catch(function () {
        /* nav is optional; the document still renders */
      });
  }

  document.getElementById("refresh").addEventListener("click", function () {
    brokenCache = {};
    render();
  });
  window.addEventListener("hashchange", render);
  buildNav();
  render();
})();
