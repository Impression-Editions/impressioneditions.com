"""Pytest suite for build_catalog.py.

The pure helpers (slugify, OPF parsing, download mapping, front-matter
emission, feed rendering) are exercised directly. The per-repo orchestration
is tested by monkeypatching the module-level network functions so no real HTTP
is performed.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("build_catalog", ROOT / "build_catalog.py")
assert SPEC is not None and SPEC.loader is not None
bc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bc)
sys.modules["build_catalog"] = bc


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def tmp_project(tmp_path, monkeypatch):
    """Point the module's filesystem constants at a temporary project root."""
    monkeypatch.setattr(bc, "ROOT", tmp_path)
    monkeypatch.setattr(bc, "CACHE_DIR", tmp_path / ".cache")
    monkeypatch.setattr(bc, "CONTENT_EBOOKS", tmp_path / "content" / "ebooks")
    monkeypatch.setattr(bc, "COVERS_DIR", tmp_path / "static" / "covers")
    monkeypatch.setattr(bc, "PREVIEWS_DIR", tmp_path / "static" / "previews")
    monkeypatch.setattr(bc, "FEEDS_DIR", tmp_path / "static" / "feeds")
    monkeypatch.setattr(bc, "READER_DIR", tmp_path / "static" / "reader")
    return tmp_path


SAMPLE_OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf"
         prefix="se: https://standardebooks.org/vocab/1.0"
         unique-identifier="uid" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">https://impressioneditions.com/ebooks/george-webbe-dasent/burnt-njal/sir-george-webbe-dasent</dc:identifier>
    <dc:title id="title">Burnt Njal</dc:title>
    <dc:creator id="author">George Webbe Dasent</dc:creator>
    <dc:language>en-GB</dc:language>
    <dc:source>https://www.gutenberg.org/ebooks/597</dc:source>
    <dc:date>2026-06-22T17:02:32Z</dc:date>
    <dc:subject id="subject-1">Nj&#225;ll &#222;orgursson, approximately 930-1011</dc:subject>
    <meta property="se:subject">Fiction</meta>
    <meta property="se:subject">Drama</meta>
    <dc:description id="description">A short plain description.</dc:description>
    <meta id="long-description" property="se:long-description" refines="#description">
      &lt;p&gt;The rich long description.&lt;/p&gt;
    </meta>
    <meta property="se:url.encyclopedia.wikipedia">https://en.wikipedia.org/wiki/Burnt_Njal</meta>
    <meta property="se:word-count">268452</meta>
  </metadata>
</package>
"""

SAMPLE_CONFIG = {
    "pg_id": "597",
    "title": "Burnt Njal",
    "author": "George Webbe Dasent",
    "slug": "dasent-george-webbe-burnt-njal",
    "language": "en-GB",
    "book_type": "epic-poetry",
    "gutendex_subjects": ["Njáll Þorgursson, approximately 930-1011"],
}

SAMPLE_REPO = {
    "name": "burnt-njal",
    "full_name": "Impression-Editions/burnt-njal",
    "default_branch": "master",
    "pushed_at": "2026-06-25T17:50:51Z",
}

SAMPLE_RELEASES = [
    {
        "tag_name": "v1.0",
        "prerelease": False,
        "draft": False,
        "published_at": "2026-06-25T17:50:51Z",
        "created_at": "2026-06-25T17:49:51Z",
        "assets": [
            {"name": "burnt-njal.epub",
             "browser_download_url": "https://github.com/Impression-Editions/burnt-njal/releases/download/v1.0/burnt-njal.epub"},
            {"name": "burnt-njal_advanced.epub",
             "browser_download_url": "https://example/advanced.epub"},
            {"name": "burnt-njal.azw3",
             "browser_download_url": "https://example/burnt-njal.azw3"},
        ],
    }
]


# --------------------------------------------------------------------------- #
# slugify
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    ("George Webbe Dasent", "george-webbe-dasent"),
    ("Children of the Night!", "children-of-the-night"),
    ("  Multiple   Spaces ", "multiple-spaces"),
    ("Æsop's Fables", "sop-s-fables"),
    ("", ""),
    (None, ""),
])
def test_slugify(raw, expected):
    assert bc.slugify(raw) == expected


# --------------------------------------------------------------------------- #
# slugs_from_identifier
# --------------------------------------------------------------------------- #

def test_slugs_from_identifier_full():
    uid = "https://impressioneditions.com/ebooks/george-webbe-dasent/burnt-njal/sir-george-webbe-dasent"
    assert bc.slugs_from_identifier(uid) == ("george-webbe-dasent", "burnt-njal")


def test_slugs_from_identifier_no_trailing():
    uid = "https://impressioneditions.com/ebooks/edwin-arlington-robinson/children-of-the-night"
    assert bc.slugs_from_identifier(uid) == ("edwin-arlington-robinson", "children-of-the-night")


def test_slugs_from_identifier_missing():
    assert bc.slugs_from_identifier("not-a-url") == ("", "")
    assert bc.slugs_from_identifier("") == ("", "")


# --------------------------------------------------------------------------- #
# parse_opf
# --------------------------------------------------------------------------- #

def test_parse_opf_extracts_core_fields():
    opf = bc.parse_opf(SAMPLE_OPF)
    assert opf["title"] == "Burnt Njal"
    assert opf["author"] == "George Webbe Dasent"
    assert opf["language"] == "en-GB"
    assert opf["date"] == "2026-06-22T17:02:32Z"
    assert "https://www.gutenberg.org/ebooks/597" in opf["sources"]
    assert "https://en.wikipedia.org/wiki/Burnt_Njal" in opf["wikipedia_urls"]
    assert opf["word_count"] == "268,452"
    assert "Fiction" in opf["se_subjects"]
    assert "Drama" in opf["se_subjects"]


def test_parse_opf_long_description_decoded():
    opf = bc.parse_opf(SAMPLE_OPF)
    assert "<p>" in opf["long_description"]
    assert "rich long description" in opf["long_description"]


def test_parse_opf_handles_empty():
    opf = bc.parse_opf("")
    assert opf["title"] == ""
    assert opf["subjects"] == []


def test_description_for_prefers_long():
    opf = bc.parse_opf(SAMPLE_OPF)
    desc = bc.description_for(opf)
    assert "rich long description" in desc


def test_description_for_falls_back():
    assert bc.description_for({"long_description": "", "description": "plain"}) == "plain"


# --------------------------------------------------------------------------- #
# pick_downloads
# --------------------------------------------------------------------------- #

def test_pick_downloads_all_three():
    assets = SAMPLE_RELEASES[0]["assets"]
    dl = bc.pick_downloads(assets)
    assert dl["epub"].endswith("burnt-njal.epub")
    assert dl["epub_advanced"] == "https://example/advanced.epub"
    assert dl["azw3"] == "https://example/burnt-njal.azw3"
    assert "kepub" not in dl


def test_pick_downloads_advanced_not_confused_with_epub():
    assets = [
        {"name": "x.epub", "browser_download_url": "u/standard"},
        {"name": "x_advanced.epub", "browser_download_url": "u/advanced"},
    ]
    dl = bc.pick_downloads(assets)
    assert dl["epub"] == "u/standard"
    assert dl["epub_advanced"] == "u/advanced"


def test_pick_downloads_empty():
    assert bc.pick_downloads([]) == {}
    assert bc.pick_downloads(None) == {}


# --------------------------------------------------------------------------- #
# build_book_record + to_front_matter
# --------------------------------------------------------------------------- #

def test_build_book_record_uses_identifier_slugs():
    opf = bc.parse_opf(SAMPLE_OPF)
    book = bc.build_book_record(
        repo=SAMPLE_REPO, config=SAMPLE_CONFIG, opf=opf,
        downloads={"epub": "http://e", "azw3": "http://a"}, download_counts={"epub": 0},
    )
    assert book["author_slug"] == "george-webbe-dasent"
    assert book["slug"] == "burnt-njal"
    assert book["title"] == "Burnt Njal"
    assert book["author"] == "George Webbe Dasent"
    assert book["pg_id"] == "597"
    assert book["language"] == "en"
    assert book["book_type"] == "epic-poetry"
    assert "epic-poetry" in book["tags"]
    assert "Fiction" in book["tags"]
    assert book["cover"] == "/covers/george-webbe-dasent_burnt-njal.jpg"
    assert book["github_repo"] == "https://github.com/Impression-Editions/burnt-njal"
    assert book["pg_url"] == "https://www.gutenberg.org/ebooks/597"
    assert book["downloads"]["epub"] == "http://e"
    assert book["draft"] is False
    assert book["weight"] > 0  # date-derived


def test_build_book_record_falls_back_when_no_identifier():
    config = {"title": "Some Book", "author": "Jane Doe", "pg_id": 1, "book_type": "fiction"}
    repo = {"name": "some-book", "full_name": "Impression-Editions/some-book",
            "default_branch": "master", "pushed_at": "2026-01-01T00:00:00Z"}
    opf = {"identifier": "", "title": "", "author": "", "description": "",
           "long_description": "", "subjects": [], "se_subjects": [],
           "language": "", "sources": [], "date": "", "wikipedia_urls": [],
           "word_count": ""}
    book = bc.build_book_record(repo=repo, config=config, opf=opf, downloads={}, download_counts={})
    assert book["author_slug"] == "jane-doe"
    assert book["slug"] == "some-book"


def test_to_front_matter_round_trips_fields():
    opf = bc.parse_opf(SAMPLE_OPF)
    book = bc.build_book_record(
        repo=SAMPLE_REPO, config=SAMPLE_CONFIG, opf=opf, downloads={"epub": "u"}, download_counts={}
    )
    fm = bc.to_front_matter(book)
    assert fm.startswith("---\n") and fm.endswith("---\n")
    # Quoted string fields
    assert 'title: "Burnt Njal"' in fm
    assert 'author: "George Webbe Dasent"' in fm
    assert 'author_slug: "george-webbe-dasent"' in fm
    # List block
    assert "subjects:" in fm
    assert "tags:" in fm
    # Nested downloads map
    assert "downloads:" in fm
    assert "  epub: \"u\"" in fm
    # Boolean
    assert "draft: false" in fm


def test_to_front_matter_escapes_quotes():
    fm = bc.to_front_matter({"title": 'He said "hi"', "tags": ["a"]})
    assert 'title: "He said \\"hi\\""' in fm


# --------------------------------------------------------------------------- #
# Feeds
# --------------------------------------------------------------------------- #

def _book(**kw):
    base = {
        "title": "T", "author": "A", "author_slug": "a", "slug": "t",
        "date": "2026-06-01T00:00:00Z", "description": "desc",
        "cover": "/covers/a_t.jpg",
        "downloads": {"epub": "http://e", "azw3": "http://a"},
    }
    base.update(kw)
    return base


def test_render_atom_well_formed():
    xml = bc.render_atom([_book(title="My & Book")], "2026-06-01T00:00:00Z")
    assert xml.startswith("<?xml")
    assert "<feed" in xml
    assert "<entry>" in xml
    assert "&amp;" in xml  # escaped
    assert "My &amp; Book" in xml


def test_render_rss_well_formed():
    xml = bc.render_rss([_book()], "2026-06-01T00:00:00Z")
    assert "<rss" in xml
    assert "<channel>" in xml
    assert "<item>" in xml
    assert "<pubDate>" in xml  # RFC-822 date


def test_render_opds_has_acquisition_links():
    xml = bc.render_opds([_book()], "2026-06-01T00:00:00Z")
    assert "opds-spec.org/acquisition" in xml
    assert "application/epub+zip" in xml
    assert "application/x-mobipocket-ebook" in xml


def test_write_feeds_creates_files(tmp_project):
    bc.write_feeds([_book(), _book(title="Second", slug="s", author_slug="a")])
    for name in ("atom.xml", "rss.xml", "opds.xml"):
        assert (tmp_project / "static" / "feeds" / name).exists()


# --------------------------------------------------------------------------- #
# Index / author pages
# --------------------------------------------------------------------------- #

def test_write_indexes_creates_top_and_author_pages(tmp_project):
    books = [
        _book(author="Jane Doe", author_slug="jane-doe", slug="book-one"),
        _book(author="Jane Doe", author_slug="jane-doe", slug="book-two"),
    ]
    bc.write_indexes(books)
    assert (tmp_project / "content" / "ebooks" / "_index.md").exists()
    author_index = tmp_project / "content" / "ebooks" / "jane-doe" / "_index.md"
    assert author_index.exists()
    assert "Jane Doe" in author_index.read_text()


# --------------------------------------------------------------------------- #
# process_repo orchestration (network monkeypatched)
# --------------------------------------------------------------------------- #

def _patch_network(monkeypatch, *, config=SAMPLE_CONFIG, opf=SAMPLE_OPF,
                   releases=SAMPLE_RELEASES, cover=None,
                   preview=None, epub=None):
    """Replace network fetchers with deterministic in-memory fakes.

    raw_get_text returns the raw *string* body (JSON for config.json), matching
    the real implementation so json.loads()/cache writes behave identically.
    """
    # Realistic JPEG: magic bytes + payload, large enough to pass the
    # cover-validation threshold (>1000 bytes) in process_repo.
    if cover is None:
        cover = b"\xff\xd8\xff\xe0" + b"\x00" * 2000
    config_text = json.dumps(config) if config is not None else None
    monkeypatch.setattr(bc, "github_api_get", lambda path: releases)
    monkeypatch.setattr(bc, "raw_get_text", lambda url: (
        config_text if url.endswith("config.json")
        else opf if url.endswith("content.opf")
        else preview
    ))

    def _bytes(url):
        if url.lower().endswith(".epub"):
            return epub
        return cover

    monkeypatch.setattr(bc, "raw_get_bytes", _bytes)


def test_process_repo_success(tmp_project, monkeypatch):
    _patch_network(monkeypatch)
    stats = bc.Stats()
    state: dict = {}
    book = bc.process_repo(SAMPLE_REPO, stats=stats, force=False, state=state)

    assert book is not None
    assert stats.processed == 1
    assert stats.errors == 0
    # Markdown written under ebooks/{author}/{title}.md
    md = tmp_project / "content" / "ebooks" / "george-webbe-dasent" / "burnt-njal.md"
    assert md.exists()
    body = md.read_text()
    assert 'title: "Burnt Njal"' in body
    assert "downloads:" in body
    # Cover image written
    cover = tmp_project / "static" / "covers" / "george-webbe-dasent_burnt-njal.jpg"
    assert cover.exists()
    assert cover.read_bytes().startswith(b"\xff\xd8\xff\xe0")
    # has_reader defaults to absent when no EPUB is fetched
    assert book.get("has_reader") is not True
    # State recorded
    assert state["burnt-njal"]["author_slug"] == "george-webbe-dasent"


def test_process_repo_skips_without_config(tmp_project, monkeypatch):
    _patch_network(monkeypatch, config=None)
    stats = bc.Stats()
    book = bc.process_repo(SAMPLE_REPO, stats=stats, force=False, state={})
    assert book is None
    assert stats.errors == 1
    assert stats.processed == 0
    assert "burnt-njal" in stats.error_repos


def test_process_repo_skips_without_opf(tmp_project, monkeypatch):
    _patch_network(monkeypatch, opf=None)
    stats = bc.Stats()
    book = bc.process_repo(SAMPLE_REPO, stats=stats, force=False, state={})
    assert book is None
    assert stats.errors == 1


def test_process_repo_warns_but_succeeds_without_releases(tmp_project, monkeypatch):
    _patch_network(monkeypatch, releases=[])
    stats = bc.Stats()
    book = bc.process_repo(SAMPLE_REPO, stats=stats, force=False, state={})
    assert book is not None          # still produced, just no downloads
    assert stats.processed == 1
    assert book["downloads"] == {}


def test_process_repo_writes_preview_when_present(tmp_project, monkeypatch):
    _patch_network(monkeypatch, preview="<html>preview</html>")
    stats = bc.Stats()
    bc.process_repo(SAMPLE_REPO, stats=stats, force=False, state={})
    preview = tmp_project / "static" / "previews" / "george-webbe-dasent_burnt-njal.html"
    assert preview.exists()
    assert "preview" in preview.read_text()


def test_process_repo_uses_cache(tmp_project, monkeypatch):
    # Seed a fresh cache so no network is required on the second pass.
    _patch_network(monkeypatch)
    state1: dict = {}
    bc.process_repo(SAMPLE_REPO, stats=bc.Stats(), force=False, state=state1)

    # Now make the network raise — a cache hit must avoid calling it.
    def boom(url):  # noqa: ANN001
        raise AssertionError("network should not be hit when cache is fresh")
    monkeypatch.setattr(bc, "raw_get_text", boom)
    monkeypatch.setattr(bc, "raw_get_bytes", boom)
    monkeypatch.setattr(bc, "github_api_get", boom)

    stats = bc.Stats()
    book = bc.process_repo(
        SAMPLE_REPO, stats=stats, force=False, state=state1
    )
    assert book is not None
    assert stats.cached == 1


# --------------------------------------------------------------------------- #
# Date helpers
# --------------------------------------------------------------------------- #

def test_normalise_date_passthrough_iso():
    assert bc.normalise_date("2026-06-25T17:50:51Z") == "2026-06-25T17:50:51Z"


def test_normalise_date_date_only():
    assert bc.normalise_date("2026-06-25") == "2026-06-25T00:00:00Z"


def test_normalise_date_empty():
    assert bc.normalise_date("") == ""


# --------------------------------------------------------------------------- #
# Online reader (generate_reader + process_repo integration)
# --------------------------------------------------------------------------- #

def _build_test_epub() -> bytes:
    """Build a small but well-formed EPUB in memory for reader tests."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"'
            ' version="1.0">\n'
            "  <rootfiles>\n"
            '    <rootfile full-path="epub/content.opf" '
            'media-type="application/oebps-package+xml"/>\n'
            "  </rootfiles>\n"
            "</container>\n",
        )
        z.writestr(
            "epub/content.opf",
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
            'unique-identifier="uid">\n'
            '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
            '    <dc:identifier id="uid">'
            "https://impressioneditions.com/ebooks/an-author/a-book"
            "</dc:identifier>\n"
            "    <dc:title>A Book</dc:title>\n"
            "    <dc:creator>An Author</dc:creator>\n"
            "  </metadata>\n"
            "  <manifest>\n"
            '    <item href="css/core.css" id="core.css" '
            'media-type="text/css"/>\n'
            '    <item href="css/local.css" id="local.css" '
            'media-type="text/css"/>\n'
            '    <item href="css/se.css" id="se.css" '
            'media-type="text/css"/>\n'
            '    <item href="text/chapter-1.xhtml" id="chapter-1.xhtml" '
            'media-type="application/xhtml+xml"/>\n'
            '    <item href="text/chapter-2.xhtml" id="chapter-2.xhtml" '
            'media-type="application/xhtml+xml"/>\n'
            '    <item href="images/cover.jpg" id="cover.jpg" '
            'media-type="image/jpeg"/>\n'
            '    <item href="images/illustration.jpg" id="illustration.jpg" '
            'media-type="image/jpeg"/>\n'
            "  </manifest>\n"
            "  <spine>\n"
            '    <itemref idref="chapter-1.xhtml"/>\n'
            '    <itemref idref="chapter-2.xhtml"/>\n'
            "  </spine>\n"
            "</package>\n",
        )
        z.writestr("epub/css/core.css", "body{font-family:serif}\n")
        z.writestr("epub/css/local.css", "/* local */\n")
        z.writestr("epub/css/se.css", "/* se */\n")
        z.writestr(
            "epub/text/chapter-1.xhtml",
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops">\n'
            "<head><title>Chapter One</title>"
            '<link href="../css/core.css" rel="stylesheet"/></head>\n'
            '<body class="epub-type" epub:type="bodymatter">'
            '<section epub:type="chapter" id="ch1">'
            "<h2>Chapter One</h2>"
            '<p>Hello with <img src="../images/illustration.jpg" alt="x"/>.</p>'
            "</section></body>\n"
            "</html>\n",
        )
        z.writestr(
            "epub/text/chapter-2.xhtml",
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops">\n'
            "<head><title>Chapter Two</title></head>\n"
            '<body epub:type="bodymatter">'
            '<section epub:type="chapter" id="ch2">'
            "<h2>Chapter Two</h2><p>World.</p></section></body>\n"
            "</html>\n",
        )
        z.writestr("epub/images/cover.jpg", b"\xff\xd8\xff\xe0COVERJPEG")
        z.writestr("epub/images/illustration.jpg", b"\xff\xd8\xff\xe0ILLUSTRATION")
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# _parse_epub_opf
# --------------------------------------------------------------------------- #

def test_parse_epub_opf_extracts_manifest_and_spine():
    opf = (
        '<package xmlns="http://www.idpf.org/2007/opf">'
        '<manifest>'
        '<item id="core.css" href="css/core.css" media-type="text/css"/>'
        '<item id="chapter-1.xhtml" href="text/chapter-1.xhtml" '
        'media-type="application/xhtml+xml"/>'
        '<item media-type="application/xhtml+xml" '
        'href="text/chapter-2.xhtml" id="chapter-2.xhtml"/>'
        "</manifest>"
        '<spine><itemref idref="chapter-1.xhtml"/>'
        '<itemref idref="chapter-2.xhtml"/></spine>'
        "</package>"
    )
    manifest, spine = bc._parse_epub_opf(opf)
    assert set(manifest.keys()) == {"core.css", "chapter-1.xhtml", "chapter-2.xhtml"}
    assert manifest["chapter-1.xhtml"]["href"] == "text/chapter-1.xhtml"
    # Attribute order is irrelevant.
    assert manifest["chapter-2.xhtml"]["media_type"] == "application/xhtml+xml"
    assert spine == ["chapter-1.xhtml", "chapter-2.xhtml"]


# --------------------------------------------------------------------------- #
# generate_reader
# --------------------------------------------------------------------------- #

def test_generate_reader_writes_all_outputs(tmp_project):
    out = tmp_project / "static" / "reader" / "an-author_a-book"
    epub = _build_test_epub()
    metadata = {"title": "A Book", "author_slug": "an-author", "slug": "a-book"}

    ok = bc.generate_reader(epub, out, metadata)
    assert ok is True

    # Per-chapter pages.
    assert (out / "chapter-1.xhtml").exists()
    assert (out / "chapter-2.xhtml").exists()
    # Index + single page + chrome CSS.
    assert (out / "index.xhtml").exists()
    assert (out / "single-page.xhtml").exists()
    assert (out / "reader.css").exists()
    # EPUB CSS copied unchanged.
    assert (out / "core.css").read_text() == "body{font-family:serif}\n"
    assert (out / "local.css").read_text() == "/* local */\n"
    assert (out / "se.css").read_text() == "/* se */\n"
    # Images copied into images/.
    assert (out / "images" / "cover.jpg").read_bytes() == b"\xff\xd8\xff\xe0COVERJPEG"
    assert (out / "images" / "illustration.jpg").exists()


def test_generate_reader_pages_are_xhtml_with_namespace(tmp_project):
    out = tmp_project / "static" / "reader" / "an-author_a-book"
    bc.generate_reader(
        _build_test_epub(), out,
        {"title": "A Book", "author_slug": "an-author", "slug": "a-book"},
    )
    for name in ("chapter-1.xhtml", "chapter-2.xhtml", "index.xhtml",
                 "single-page.xhtml"):
        text = (out / name).read_text()
        assert text.startswith('<?xml version="1.0"')
        assert "<!DOCTYPE html>" in text
        assert 'xmlns:epub="http://www.idpf.org/2007/ops"' in text
        # All four CSS links referenced.
        assert 'href="core.css"' in text
        assert 'href="local.css"' in text
        assert 'href="se.css"' in text
        assert 'href="reader.css"' in text


def test_generate_reader_rewrites_image_paths(tmp_project):
    out = tmp_project / "static" / "reader" / "an-author_a-book"
    bc.generate_reader(
        _build_test_epub(), out,
        {"title": "A Book", "author_slug": "an-author", "slug": "a-book"},
    )
    body = (out / "chapter-1.xhtml").read_text()
    assert "../images/" not in body
    assert "images/illustration.jpg" in body


def test_generate_reader_nav_has_prev_next_contents_single(tmp_project):
    out = tmp_project / "static" / "reader" / "an-author_a-book"
    bc.generate_reader(
        _build_test_epub(), out,
        {"title": "A Book", "author_slug": "an-author", "slug": "a-book"},
    )
    ch1 = (out / "chapter-1.xhtml").read_text()
    ch2 = (out / "chapter-2.xhtml").read_text()
    # First chapter has muted previous, real next.
    assert '<span class="muted">← Previous</span>' in ch1
    assert 'href="chapter-2.xhtml">Next →' in ch1
    # Second chapter has real previous, muted next.
    assert 'href="chapter-1.xhtml">← Previous' in ch2
    assert '<span class="muted">Next →</span>' in ch2
    # Every page offers contents + single page.
    for text in (ch1, ch2):
        assert 'href="index.xhtml">Contents' in text
        assert 'href="single-page.xhtml">Single page' in text


def test_generate_reader_index_lists_chapters(tmp_project):
    out = tmp_project / "static" / "reader" / "an-author_a-book"
    bc.generate_reader(
        _build_test_epub(), out,
        {"title": "A Book", "author_slug": "an-author", "slug": "a-book"},
    )
    idx = (out / "index.xhtml").read_text()
    assert "reader-toc" in idx
    assert 'href="chapter-1.xhtml">Chapter One' in idx
    assert 'href="chapter-2.xhtml">Chapter Two' in idx
    assert 'href="single-page.xhtml"' in idx
    # Back-to-book link uses Hugo-style URL.
    assert 'href="/ebooks/an-author/a-book/"' in idx


def test_generate_reader_single_page_concatenates_bodies(tmp_project):
    out = tmp_project / "static" / "reader" / "an-author_a-book"
    bc.generate_reader(
        _build_test_epub(), out,
        {"title": "A Book", "author_slug": "an-author", "slug": "a-book"},
    )
    single = (out / "single-page.xhtml").read_text()
    assert "Chapter One" in single
    assert "Chapter Two" in single
    # Chapters separated by a hidden rule.
    assert '<hr epub:type="hidden"/>' in single


def test_generate_reader_preserves_epub_type_attributes(tmp_project):
    out = tmp_project / "static" / "reader" / "an-author_a-book"
    bc.generate_reader(
        _build_test_epub(), out,
        {"title": "A Book", "author_slug": "an-author", "slug": "a-book"},
    )
    body = (out / "chapter-1.xhtml").read_text()
    # The original epub:type attribute survives verbatim — needed for
    # namespaced CSS selectors to work in application/xhtml+xml mode.
    assert 'epub:type="chapter"' in body


def test_generate_reader_rejects_bad_zip(tmp_project):
    out = tmp_project / "static" / "reader" / "an-author_a-book"
    ok = bc.generate_reader(b"not a zip file", out, {"title": "X"})
    assert ok is False
    assert not out.exists()


def test_generate_reader_skips_when_empty_bytes(tmp_project):
    out = tmp_project / "static" / "reader" / "an-author_a-book"
    ok = bc.generate_reader(b"", out, {"title": "X"})
    assert ok is False


# --------------------------------------------------------------------------- #
# process_repo integration with reader
# --------------------------------------------------------------------------- #

def test_process_repo_generates_reader_when_epub_present(tmp_project, monkeypatch):
    epub = _build_test_epub()
    _patch_network(monkeypatch, epub=epub)
    stats = bc.Stats()
    book = bc.process_repo(SAMPLE_REPO, stats=stats, force=False, state={})

    assert book is not None
    assert book.get("has_reader") is True

    reader_dir = (
        tmp_project / "static" / "reader"
        / "george-webbe-dasent_burnt-njal"
    )
    assert (reader_dir / "index.xhtml").exists()
    assert (reader_dir / "chapter-1.xhtml").exists()
    assert (reader_dir / "reader.css").exists()
    # EPUB cached for subsequent builds.
    assert (tmp_project / ".cache" / "burnt-njal" / "reader-epub.epub").exists()

    # has_reader is serialised into the Hugo front matter.
    md = (tmp_project / "content" / "ebooks" / "george-webbe-dasent"
          / "burnt-njal.md").read_text()
    assert "has_reader: true" in md


def test_process_repo_skips_reader_when_no_epub_in_release(tmp_project, monkeypatch):
    # Release assets exist but contain only azw3, no epub.
    releases = [{
        "tag_name": "v1.0", "prerelease": False, "draft": False,
        "published_at": "2026-06-25T17:50:51Z",
        "assets": [{"name": "x.azw3", "browser_download_url": "https://x/y.azw3"}],
    }]
    _patch_network(monkeypatch, releases=releases)
    book = bc.process_repo(SAMPLE_REPO, stats=bc.Stats(), force=False, state={})
    assert book is not None
    assert "has_reader" not in book
    assert not list((tmp_project / "static" / "reader").glob("*"))


def test_process_repo_skips_reader_gracefully_on_bad_epub(tmp_project, monkeypatch):
    _patch_network(monkeypatch, epub=b"not-a-zip")
    book = bc.process_repo(SAMPLE_REPO, stats=bc.Stats(), force=False, state={})
    # Book is still produced; reader just isn't.
    assert book is not None
    assert "has_reader" not in book
