# SPEC: Online Reader for Impression Editions (Phase 2)

**Goal:** Faithful in-browser reading experience for IE books, generated from EPUBs during the website build — no pipeline, no book dirs required.

---

## Architecture

```
build_catalog.py (during Netlify build)
    ↓
For each book repo with a release:
    1. Download the compatible EPUB from GitHub release
    2. Extract CSS, XHTML chapters, images from the EPUB zip
    3. Generate two output formats:
       a. Per-chapter XHTML pages (chapter-by-chapter reading)
       b. Single-page XHTML (all chapters concatenated)
    4. Write to static/reader/{author-slug}_{title-slug}/
    ↓
Hugo serves them as static files with correct content-type
```

---

## Key Insight from SE Research

Standard Ebooks serves their reader pages as `application/xhtml+xml`, not `text/html`. This is critical because:

- XHTML mode honors XML namespace declarations (`xmlns:epub="..."`)
- CSS selectors like `[epub|type~="subtitle"]` work correctly
- The EPUB's own CSS renders faithfully without modification

**Netlify serves `.xhtml` files as `application/xhtml+xml` by default.** No config needed — just use the `.xhtml` extension.

---

## URL Structure

| URL | Description |
|-----|-------------|
| `/ebooks/{author}/{title}/read/` | Chapter index + first chapter |
| `/ebooks/{author}/{title}/read/{chapter}/` | Individual chapter |
| `/ebooks/{author}/{title}/read/single-page/` | All chapters on one page |

The "Read online" link on the book detail page points to `/read/`.

---

## File Output (per book)

```
static/reader/{author}_{title}/
├── index.xhtml          — Table of contents + chapter list
├── single-page.xhtml    — All chapters in one file
├── chapter-1.xhtml      — Individual chapters
├── chapter-2.xhtml
├── ...
├── core.css             — EPUB's core.css (as-is)
├── local.css            — EPUB's local.css (as-is)
├── se.css               — EPUB's se.css (as-is, if present)
├── reader.css           — IE reader chrome (nav, wrapper)
└── images/              — All images from the EPUB
    ├── cover.jpg
    ├── illustration-1.jpg
    └── ...
```

---

## XHTML Page Template (per chapter)

```xml
<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:epub="http://www.idpf.org/2007/ops"
      epub:prefix="z3998: http://www.daisy.org/z3998/2012/vocab/structure/, se: https://standardebooks.org/vocab/1.0"
      lang="en-US" xml:lang="en-US">
<head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <title>{chapter title} — {book title}</title>
    <link rel="stylesheet" href="../core.css"/>
    <link rel="stylesheet" href="../local.css"/>
    <link rel="stylesheet" href="../se.css"/>
    <link rel="stylesheet" href="../reader.css"/>
</head>
<body>
    <!-- Reader chrome -->
    <nav class="reader-nav">
        <a href="../{prev-chapter}/">← Previous</a>
        <a href="../">Contents</a>
        <a href="../{next-chapter}/">Next →</a>
        <a href="../single-page/">Single page</a>
    </nav>

    <!-- Chapter content (from EPUB, as-is) -->
    <main class="reader-content">
        {body of the EPUB chapter XHTML}
    </main>

    <nav class="reader-nav bottom">
        <!-- same nav -->
    </nav>
</body>
</html>
```

### What stays unchanged

- The EPUB's `<body>` content goes into `<main>` verbatim
- CSS files are the EPUB's actual stylesheets, unmodified
- `epub:type` attributes work because of the namespace declaration + XHTML content-type
- Image paths rewritten from `../images/` to `images/`

### What gets added

- `reader.css` — minimal chrome: sticky nav bar, max-width content column, prev/next/contents/single-page links, back-to-book link. Does NOT override book CSS.

---

## Single-Page Template

Same structure but all chapter bodies concatenated inside `<main>`, separated by `<hr epub:type="hidden"/>` between chapters. Nav shows chapter anchors instead of separate page links.

---

## Index Page Template (chapter list)

```xml
<!-- Same head/nav wrapper -->
<main class="reader-content reader-toc">
    <h2>{book title}</h2>
    <ol>
        <li><a href="../chapter-1/">Chapter 1: Title</a></li>
        <li><a href="../chapter-2/">Chapter 2: Title</a></li>
        ...
    </ol>
    <p><a href="../single-page/">Read entire book on one page →</a></p>
</main>
```

---

## Implementation in build_catalog.py

Add a `generate_reader(epub_bytes, output_dir, book_metadata)` function:

1. **Extract EPUB** to temp dir
2. **Read OPF** — get spine order, manifest (filename → href mapping), book title
3. **Copy CSS files** (core.css, local.css, se.css) to output dir
4. **Copy images/** to output dir
5. **For each spine item:**
   - Read the XHTML file from the EPUB
   - Extract `<body>` content
   - Rewrite image paths (`../images/` → `images/`)
   - Wrap in the reader template with prev/next/contents nav
   - Write as `{filename}.xhtml`
6. **Generate index.xhtml** — chapter list with links
7. **Generate single-page.xhtml** — all chapters concatenated
8. **Generate reader.css** — reader chrome styles

### Caching

- Cache the downloaded EPUB in `.cache/{repo}/reader-epub.epub`
- Regenerate reader files only if EPUB changed (compare file size)
- `--force` flag bypasses cache

### Error handling

- Skip reader generation if EPUB download fails (book still appears in catalog)
- Log: "reader skipped for {repo}: no EPUB in release"
- Reader link only appears on book page if reader files exist

---

## Hugo Integration

### Book detail page (`single.html`)

Update the "Read online" link:

```html
{{ if .Params.has_reader }}
<a href="/ebooks/{{ .Params.author_slug }}/{{ .Params.slug }}/read/" class="btn">Read online</a>
{{ end }}
```

The `has_reader` flag is set by `build_catalog.py` when reader files are generated.

### Netlify redirects

Add to `netlify.toml` or `static/_redirects`:

```
# Serve .xhtml files with correct content-type (Netlify does this by default)
# Clean URLs for reader pages
/ebooks/:author/:title/read    /reader/:author_:title/index.xhtml   200
/ebooks/:author/:title/read/:chapter  /reader/:author_:title/:chapter.xhtml  200
/ebooks/:author/:title/read/single-page  /reader/:author_:title/single-page.xhtml  200
```

---

## reader.css

```css
/* Reader chrome — wraps EPUB content without overriding book CSS */

.reader-nav {
    position: sticky;
    top: 0;
    background: var(--reader-nav-bg, #f8f8f8);
    border-bottom: 1px solid #ddd;
    padding: 0.5em 1em;
    display: flex;
    gap: 1em;
    font-family: sans-serif;
    font-size: 0.85em;
    z-index: 100;
}

.reader-nav a {
    color: #555;
    text-decoration: none;
}

.reader-nav a:hover {
    text-decoration: underline;
}

.reader-content {
    max-width: 40em;
    margin: 0 auto;
    padding: 2em 1.5em;
}

.reader-toc ol {
    list-style: decimal;
    padding-left: 1.5em;
}

.reader-toc li {
    margin: 0.3em 0;
}

.reader-toc a {
    text-decoration: none;
    color: inherit;
}

.reader-toc a:hover {
    text-decoration: underline;
}

/* Dark mode */
@media (prefers-color-scheme: dark) {
    :root {
        --reader-nav-bg: #1a1a1a;
    }
    .reader-nav {
        border-bottom-color: #333;
    }
    .reader-nav a {
        color: #aaa;
    }
}
```

---

## Fidelity Checklist

What renders faithfully with this approach:

- ✅ Typography (font families, sizes, weights from EPUB CSS)
- ✅ `epub:type` CSS selectors (XHTML namespace works in browsers)
- ✅ Poetry and verse formatting
- ✅ Blockquotes, tables, illustrations
- ✅ Chapter headings and subtitles
- ✅ Small caps, oldstyle numerals (if fonts available)
- ✅ Endnote links (within same file or cross-file)
- ✅ Images and SVG titlepages

What doesn't transfer:

- ❌ EPUB-embedded fonts may not load (font-face URLs need rewriting to relative paths)
- ❌ Pagination (continuous scroll, not ereader pages)
- ❌ Reading position tracking
- ❌ Font size controls (browser zoom works)

---

## Phase 3 Hook (future JS reader)

The generated reader files are structured so a JS reader (foliate.js, epub.js) could load them. The single-page.xhtml IS a valid EPUB chapter. A future enhancement could:

1. Load the actual EPUB file (from GitHub release URL) in-browser
2. Use foliate.js for pagination, font controls, bookmarks
3. Fall back to the XHTML pages for no-JS users

This spec doesn't build that, but doesn't preclude it.

---

## Testing

1. Generate reader for one book (Bushido — small, known good)
2. Open in browser — verify CSS renders (epub:type selectors work)
3. Navigate between chapters
4. Check single-page view
5. Verify images load
6. Test mobile viewport
7. Generate for all 15 books, verify no errors
