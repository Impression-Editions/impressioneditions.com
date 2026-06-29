#!/usr/bin/env python3
"""build_catalog.py — Scan the Impression-Editions GitHub org and generate
Hugo content files, cover images, HTML previews, and syndication feeds for the
Impression Editions static website.

Data flow:
    GitHub (Impression-Editions org repos)
        -> config.json  (pipeline metadata)
        -> src/epub/content.opf  (EPUB metadata)
        -> GitHub Release assets (epub / advanced epub / azw3 download URLs)
        -> images/cover-override/cover.jpg  (cover art)
        -> {slug}.html  (single-page HTML preview, optional)
        -> Hugo content/ + static/ files

Auth: ``GITHUB_TOKEN`` environment variable (optional). Without it the
unauthenticated GitHub API is limited to 60 requests/hour; with it the limit
rises to 5000/hour.

Caching: a local ``.cache/`` directory stores fetched raw files plus a
``state.json`` recording each repo's ``pushed_at`` timestamp. On subsequent
runs, repos whose ``pushed_at`` has not changed reuse cached files. Pass
``--force`` to ignore the cache (e.g. for a clean Netlify rebuild).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import requests
except ImportError:  # pragma: no cover - exercised only without requests
    requests = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

ORG = "Impression-Editions"
API_BASE = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / ".cache"
CONTENT_EBOOKS = ROOT / "content" / "ebooks"
COVERS_DIR = ROOT / "static" / "covers"
PREVIEWS_DIR = ROOT / "static" / "previews"
FEEDS_DIR = ROOT / "static" / "feeds"
SITE_BASE_URL = "https://impressioneditions.com/"

# Exit non-zero when more than this fraction of repos fail (systemic failure).
FAILURE_THRESHOLD = 0.10

HTTP_TIMEOUT = 30


# --------------------------------------------------------------------------- #
# Logging helpers
# --------------------------------------------------------------------------- #

class Stats:
    """Lightweight run statistics container."""

    def __init__(self) -> None:
        self.processed = 0
        self.skipped = 0
        self.errors = 0
        self.cached = 0
        self.error_repos: list[str] = []

    def summary(self) -> str:
        return (
            f"Processed {self.processed} book(s), "
            f"{self.cached} from cache, "
            f"{self.skipped} skipped, "
            f"{self.errors} error(s)."
        )


def log(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr, flush=True)


def err(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Pure helpers (no network — the bulk of the testable logic)
# --------------------------------------------------------------------------- #

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str | None) -> str:
    """Return a URL-safe slug from arbitrary text.

    Examples:
        >>> slugify("George Webbe Dasent")
        'george-webbe-dasent'
        >>> slugify("Children of the Night!")
        'children-of-the-night'
    """
    if not text:
        return ""
    text = text.strip().lower()
    text = _SLUG_RE.sub("-", text)
    return text.strip("-")


def yaml_quote(value: str) -> str:
    """Escape and double-quote a string for safe YAML emission."""
    if value is None:
        return '""'
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ")
    escaped = escaped.replace("\n", " ")
    return f'"{escaped}"'


def to_front_matter(book: dict[str, Any]) -> str:
    """Serialise a book record into Hugo YAML front matter.

    Only string / int / list / bool values are supported. Lists are emitted as
    YAML block sequences. This avoids a hard dependency on PyYAML while keeping
    the output valid for arbitrary user content.
    """
    lines: list[str] = ["---"]

    def emit(key: str, value: Any) -> None:
        if isinstance(value, bool):
            lines.append(f"{key}: {str(value).lower()}")
        elif isinstance(value, int):
            lines.append(f"{key}: {value}")
        elif isinstance(value, list):
            lines.append(f"{key}:")
            if not value:
                lines.append("  []")
            else:
                for item in value:
                    lines.append(f"  - {yaml_quote(str(item))}")
        else:
            lines.append(f"{key}: {yaml_quote(str(value))}")

    for key, value in book.items():
        if key == "downloads":
            # Nested downloads map.
            lines.append("downloads:")
            if not value:
                lines.append("  {}")
            else:
                for dkey, dval in value.items():
                    lines.append(f"  {dkey}: {yaml_quote(str(dval))}")
        elif isinstance(value, dict):
            lines.append(f"{key}:")
            if not value:
                lines.append("  {}")
            else:
                for skey, sval in value.items():
                    lines.append(f"  {skey}: {yaml_quote(str(sval))}")
        else:
            emit(key, value)

    lines.append("---")
    return "\n".join(lines) + "\n"


# --- content.opf parsing --------------------------------------------------- #

_IDENT_RE = re.compile(
    r"/ebooks/(?P<author>[^/]+)/(?P<title>[^/]+?)(?:/[^/]*)?/?$", re.IGNORECASE
)
_TAG_RE = re.compile(r"<[^>]+>")


def slugs_from_identifier(identifier: str) -> tuple[str, str]:
    """Derive (author_slug, title_slug) from a content.opf dc:identifier URL.

    Falls back to ("", "") when the identifier does not match the canonical
    pattern so callers can derive slugs another way.

    Examples:
        >>> slugs_from_identifier(
        ...     "https://impressioneditions.com/ebooks/george-webbe-dasent/burnt-njal"
        ... )
        ('george-webbe-dasent', 'burnt-njal')
    """
    if not identifier:
        return "", ""
    match = _IDENT_RE.search(identifier.strip())
    if match:
        return match.group("author"), match.group("title")
    return "", ""


def _strip_tags(html: str) -> str:
    return _TAG_RE.sub("", html).strip()


def parse_opf(opf_text: str) -> dict[str, Any]:
    """Extract metadata from a Standard-Ebooks-style content.opf document.

    Uses tolerant regex parsing rather than a strict XML parser because the
    source files occasionally contain minor structural quirks. Returns a dict
    with the following keys (any may be empty when absent from the source):

        identifier, title, author, description, long_description,
        subjects (list), se_subjects (list), language, sources (list),
        date, wikipedia_urls (list), word_count
    """
    out: dict[str, Any] = {
        "identifier": "",
        "title": "",
        "author": "",
        "description": "",
        "long_description": "",
        "subjects": [],
        "se_subjects": [],
        "language": "",
        "sources": [],
        "date": "",
        "wikipedia_urls": [],
        "word_count": "",
    }
    if not opf_text:
        return out

    def find_first(pattern: str, text: str, group: int = 1) -> str:
        m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        return m.group(group).strip() if m else ""

    out["identifier"] = find_first(
        r'<dc:identifier[^>]*>(.*?)</dc:identifier>', opf_text
    )
    out["title"] = find_first(r'<dc:title[^>]*>(.*?)</dc:title>', opf_text)
    out["author"] = find_first(r'<dc:creator[^>]*>(.*?)</dc:creator>', opf_text)
    out["description"] = _clean(_find_first_decoded(
        opf_text, r'<dc:description[^>]*>(.*?)</dc:description>'
    ))
    out["long_description"] = _clean(_find_first_decoded(
        opf_text,
        r'<meta[^>]*property="se:long-description"[^>]*>(.*?)</meta>',
    ))

    # dc:language — take the first non-empty value.
    lang_match = re.search(
        r'<dc:language[^>]*>(.*?)</dc:language>', opf_text, re.IGNORECASE
    )
    if lang_match:
        out["language"] = lang_match.group(1).strip()

    # dc:source — collect all PG-style sources (de-duplicated, order preserved).
    sources: list[str] = []
    for m in re.finditer(
        r'<dc:source[^>]*>(.*?)</dc:source>', opf_text, re.IGNORECASE | re.DOTALL
    ):
        src = m.group(1).strip()
        if src and src not in sources:
            sources.append(src)
    out["sources"] = sources

    # dc:date
    date_match = re.search(
        r'<dc:date[^>]*>(.*?)</dc:date>', opf_text, re.IGNORECASE
    )
    if date_match:
        out["date"] = date_match.group(1).strip()

    # dc:subject — collect all (strip nested markup some files embed).
    subjects: list[str] = []
    for m in re.finditer(
        r'<dc:subject[^>]*>(.*?)</dc:subject>', opf_text, re.IGNORECASE | re.DOTALL
    ):
        subj = _strip_tags(m.group(1)).strip()
        if subj and subj not in subjects:
            subjects.append(subj)
    out["subjects"] = subjects

    # se:subject — high level categories (Fiction, Poetry, ...).
    # These may contain nested <dc:subject> tags or plain text.
    se_subjects: list[str] = []
    for m in re.finditer(
        r'<meta[^>]*property="se:subject"[^>]*>(.*?)</meta>',
        opf_text, re.IGNORECASE | re.DOTALL,
    ):
        inner = m.group(1)
        if "<dc:subject" in inner:
            # Extract individual dc:subject values from inside the se:subject meta
            for sm in re.finditer(r'<dc:subject[^>]*>(.*?)</dc:subject>', inner, re.IGNORECASE | re.DOTALL):
                val = _strip_tags(sm.group(1)).strip()
                if val and val not in se_subjects:
                    se_subjects.append(val)
        else:
            val = _strip_tags(inner).strip() if "<" in inner else inner.strip()
            if val and val not in se_subjects:
                se_subjects.append(val)
    out["se_subjects"] = se_subjects

    # Wikipedia links (book + author references).
    wiki: list[str] = []
    for m in re.finditer(
        r'<meta[^>]*property="se:url\.encyclopedia\.wikipedia"[^>]*>(.*?)</meta>',
        opf_text, re.IGNORECASE | re.DOTALL,
    ):
        url = m.group(1).strip()
        if url and url not in wiki:
            wiki.append(url)
    out["wikipedia_urls"] = wiki

    # Word count (optional readability metadata).
    wc_match = re.search(
        r'<meta[^>]*property="se:word-count"[^>]*>(.*?)</meta>',
        opf_text, re.IGNORECASE,
    )
    if wc_match:
        out["word_count"] = wc_match.group(1).strip()

    return out


def _clean(text: str) -> str:
    """Normalise whitespace and tidy HTML entities for human display."""
    if not text:
        return ""
    text = text.replace("\r", "\n")
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _find_first_decoded(text: str, pattern: str) -> str:
    """Return the first regex match, decoding common HTML entities."""
    m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    raw = m.group(1)
    return (
        raw.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&apos;", "'")
        .strip()
    )


def description_for(opf: dict[str, Any]) -> str:
    """Prefer the rich long-description, falling back to the plain one."""
    long_desc = opf.get("long_description", "")
    if long_desc:
        return long_desc
    return opf.get("description", "")


# --- release asset parsing ------------------------------------------------- #

def pick_downloads(assets: list[dict[str, Any]]) -> dict[str, str]:
    """Map GitHub release assets to IE's canonical download slots.

    Slots: ``epub``, ``epub_advanced``, ``azw3``. The ``_advanced.epub`` file
    is matched before generic ``.epub`` so the two are never confused.

    >>> pick_downloads([
    ...     {"name": "x.epub", "browser_download_url": "http://e"},
    ...     {"name": "x_advanced.epub", "browser_download_url": "http://ae"},
    ...     {"name": "x.azw3", "browser_download_url": "http://a"},
    ... ])
    {'epub': 'http://e', 'epub_advanced': 'http://ae', 'azw3': 'http://a'}
    """
    downloads: dict[str, str] = {}
    if not assets:
        return downloads
    for asset in assets:
        name = (asset.get("name") or "").lower()
        url = asset.get("browser_download_url") or ""
        if not url:
            continue
        if name.endswith("_advanced.epub"):
            downloads["epub_advanced"] = url
        elif name.endswith(".epub"):
            downloads["epub"] = url
        elif name.endswith(".azw3"):
            downloads["azw3"] = url
        elif name.endswith(".kepub.epub"):
            # Reserved for future KEPUB production (Phase 2+).
            downloads["kepub"] = url
    return downloads


# --- record assembly ------------------------------------------------------- #

def build_book_record(
    *,
    repo: dict[str, Any],
    config: dict[str, Any],
    opf: dict[str, Any],
    downloads: dict[str, str],
) -> dict[str, Any]:
    """Assemble the full book record written into Hugo front matter."""
    repo_name = repo.get("name", "")
    repo_full = repo.get("full_name") or f"{ORG}/{repo_name}"
    branch = repo.get("default_branch") or "master"

    # Slugs: prefer the OPF identifier (authoritative URL), else derive.
    author_slug, title_slug = slugs_from_identifier(opf.get("identifier", ""))
    title = config.get("title") or opf.get("title") or title_slug or repo_name
    author = (
        config.get("author")
        or opf.get("author")
        or (config.get("authors") or [None])[0]
        or "Unknown"
    )
    if not author_slug:
        author_slug = slugify(author)
    if not title_slug:
        title_slug = config.get("slug") or slugify(title) or repo_name

    # Dates: release > repo pushed_at > opf date, normalised to ISO 8601.
    date_raw = (
        (repo.get("_release_published_at"))
        or repo.get("pushed_at")
        or opf.get("date")
        or ""
    )
    lastmod_raw = repo.get("pushed_at") or opf.get("date") or date_raw

    pg_id = config.get("pg_id", "")
    language = (config.get("language") or opf.get("language") or "en").split("-")[0]

    # Subjects (detailed) and tags (high-level, for filtering).
    subjects = list(opf.get("subjects") or [])
    book_type = config.get("book_type") or ""
    tags = list(opf.get("se_subjects") or [])
    if book_type and book_type not in tags:
        tags.append(book_type)
    # Fold in any PG/gutendex subjects for discoverability.
    for subj in config.get("gutendex_subjects") or []:
        if subj and subj not in subjects:
            subjects.append(subj)

    pg_url = ""
    for src in opf.get("sources", []) + [
        f"https://www.gutenberg.org/ebooks/{pg_id}" if pg_id else ""
    ]:
        if "gutenberg.org" in src:
            pg_url = src
            break

    wiki_urls = opf.get("wikipedia_urls") or []
    wikipedia_url = wiki_urls[0] if wiki_urls else ""

    cover_path = f"/covers/{author_slug}_{title_slug}.jpg"
    preview_path = f"/previews/{author_slug}_{title_slug}.html"

    weight = _weight_from_date(date_raw)

    return {
        "title": title,
        "author": author,
        "author_slug": author_slug,
        "slug": title_slug,
        "date": normalise_date(date_raw),
        "lastmod": normalise_date(lastmod_raw),
        "weight": weight,
        "description": description_for(opf),
        "language": language,
        "pg_id": pg_id,
        "book_type": book_type,
        "subjects": subjects,
        "tags": tags,
        "cover": cover_path,
        "github_repo": f"https://github.com/{repo_full}",
        "pg_url": pg_url,
        "wikipedia_url": wikipedia_url,
        "downloads": downloads,
        "preview": preview_path,
        "repo_name": repo_name,
        "branch": branch,
        "word_count": opf.get("word_count", ""),
        "draft": False,
    }


def _weight_from_date(date_raw: str) -> int:
    """Higher weight = newer, so Hugo can sort newest-first by weight desc."""
    iso = normalise_date(date_raw)
    if iso and len(iso) >= 4 and iso[:4].isdigit():
        # Year-based weight keeps ordering stable within a release year.
        try:
            ymd = iso[:10].replace("-", "")
            return int(ymd) if ymd.isdigit() else int(iso[:4])
        except ValueError:
            return int(iso[:4])
    return 0


def normalise_date(raw: str) -> str:
    """Normalise a GitHub/EPUB timestamp to ISO 8601 (UTC), or return ""."""
    if not raw:
        return ""
    raw = raw.strip()
    # Already ISO (e.g. 2026-06-25T17:50:51Z) — normalise to exactly one Z.
    if re.match(r"^\d{4}-\d{2}-\d{2}T", raw):
        base = raw[:-1] if raw.endswith("Z") else raw
        return base + "Z"
    # Date only (2026-06-25).
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return f"{raw}T00:00:00Z"
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return ""


# --------------------------------------------------------------------------- #
# Network layer (monkeypatchable from tests)
# --------------------------------------------------------------------------- #

def _auth_headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "Impression-Editions-build-catalog"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get(url: str, *, binary: bool = False) -> Any:
    """GET a URL, retrying once on a secondary rate limit. Returns content."""
    if requests is None:
        raise RuntimeError(
            "The 'requests' package is required. Install with: pip install -r requirements.txt"
        )
    for attempt in range(2):
        resp = requests.get(url, headers=_auth_headers(), timeout=HTTP_TIMEOUT)
        if resp.status_code == 200:
            return resp.content if binary else resp.text
        if resp.status_code in (403, 429):
            reset = resp.headers.get("X-RateLimit-Reset")
            remaining = resp.headers.get("X-RateLimit-Remaining")
            if remaining == "0" and reset:
                wait = max(int(reset) - int(time.time()), 1)
                raise RateLimitError(wait)
            # Secondary rate limit — back off and retry once.
            if attempt == 0:
                time.sleep(5)
                continue
        if resp.status_code == 404:
            return None
        raise GitHubError(f"HTTP {resp.status_code} for {url}: {resp.text[:200]}")
    return None


class RateLimitError(RuntimeError):
    def __init__(self, wait_seconds: int) -> None:
        super().__init__(
            f"GitHub primary rate limit exhausted. Resets in ~{wait_seconds}s. "
            "Set GITHUB_TOKEN to raise the limit."
        )
        self.wait_seconds = wait_seconds


class GitHubError(RuntimeError):
    pass


def github_api_get(path: str) -> Any:
    """GET a GitHub API path (relative or absolute) and decode JSON."""
    url = path if path.startswith("http") else f"{API_BASE}{path}"
    text = _get(url)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise GitHubError(f"Invalid JSON from {url}: {exc}") from exc


def raw_get_text(url: str) -> str | None:
    return _get(url)


def raw_get_bytes(url: str) -> bytes | None:
    return _get(url, binary=True)


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #

def load_state() -> dict[str, Any]:
    state_file = CACHE_DIR / "state.json"
    if state_file.exists():
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(state: dict[str, Any]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    state_file = CACHE_DIR / "state.json"
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Per-repo processing
# --------------------------------------------------------------------------- #

def process_repo(
    repo: dict[str, Any],
    *,
    stats: Stats,
    force: bool,
    state: dict[str, Any],
) -> dict[str, Any] | None:
    """Fetch and convert a single repo into a book record.

    Returns the assembled book record on success, or ``None`` when the repo is
    skipped or errored (in which case ``stats`` is updated accordingly).
    """
    repo_name = repo.get("name", "")
    if not repo_name:
        return None

    pushed_at = repo.get("pushed_at", "")
    cached_pushed = state.get(repo_name, {}).get("pushed_at")
    cache_fresh = (not force) and cached_pushed == pushed_at

    repo_cache = CACHE_DIR / repo_name
    repo_cache.mkdir(parents=True, exist_ok=True)
    branch = repo.get("default_branch") or "master"
    repo_full = repo.get("full_name") or f"{ORG}/{repo_name}"

    def _missing_marker(filename: str) -> Path:
        """Sidecar recording a 404 so cache-fresh runs skip re-fetching."""
        return repo_cache / f".404.{filename}"

    def cached_or_fetch_text(filename: str, url: str) -> str | None:
        cache_file = repo_cache / filename
        marker = _missing_marker(filename)
        if cache_fresh:
            if cache_file.exists():
                return cache_file.read_text(encoding="utf-8")
            if marker.exists():  # previously confirmed absent
                return None
        data = raw_get_text(url)
        if data is not None:
            cache_file.write_text(data, encoding="utf-8")
            if marker.exists():
                marker.unlink()
        else:
            marker.touch()
        return data

    def cached_or_fetch_bytes(filename: str, url: str) -> bytes | None:
        cache_file = repo_cache / filename
        marker = _missing_marker(filename)
        if cache_fresh:
            if cache_file.exists():
                return cache_file.read_bytes()
            if marker.exists():
                return None
        data = raw_get_bytes(url)
        if data is not None:
            cache_file.write_bytes(data)
            if marker.exists():
                marker.unlink()
        else:
            marker.touch()
        return data

    def cached_or_fetch_json(filename: str, api_path: str) -> Any:
        cache_file = repo_cache / filename
        marker = _missing_marker(filename)
        if cache_fresh:
            if cache_file.exists():
                try:
                    return json.loads(cache_file.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    pass
            elif marker.exists():
                return None
        data = github_api_get(api_path)
        if data is not None:
            cache_file.write_text(json.dumps(data), encoding="utf-8")
            if marker.exists():
                marker.unlink()
        else:
            marker.touch()
        return data

    try:
        # --- config.json --------------------------------------------------
        config_text = cached_or_fetch_text(
            "config.json",
            f"{RAW_BASE}/{repo_full}/{branch}/config.json",
        )
        if not config_text:
            err(f"{repo_name}: missing config.json — skipping.")
            stats.errors += 1
            stats.error_repos.append(repo_name)
            return None
        config = json.loads(config_text)

        # --- content.opf --------------------------------------------------
        opf_text = cached_or_fetch_text(
            "content.opf",
            f"{RAW_BASE}/{repo_full}/{branch}/src/epub/content.opf",
        )
        if not opf_text:
            err(f"{repo_name}: missing src/epub/content.opf — skipping.")
            stats.errors += 1
            stats.error_repos.append(repo_name)
            return None
        opf = parse_opf(opf_text)

        # --- releases / downloads ----------------------------------------
        releases = cached_or_fetch_json(
            "releases.json", f"/repos/{repo_full}/releases?per_page=10"
        ) or []
        latest = _latest_release(releases)
        downloads: dict[str, str] = {}
        release_published = ""
        if latest:
            downloads = pick_downloads(latest.get("assets") or [])
            release_published = latest.get("published_at") or latest.get(
                "created_at", ""
            )
            repo["_release_published_at"] = release_published
        if not downloads:
            warn(f"{repo_name}: no usable release assets — skipping downloads.")

        # --- cover image --------------------------------------------------
        cover_bytes = None
        for cover_path in ("images/cover-override/cover.jpg", "images/cover.jpg"):
            cover_bytes = cached_or_fetch_bytes(
                "cover.jpg",
                f"{RAW_BASE}/{repo_full}/{branch}/{cover_path}",
            )
            if cover_bytes:
                break
        if not cover_bytes:
            warn(f"{repo_name}: no cover image found.")

        # --- HTML preview (optional) -------------------------------------
        preview_text = None
        candidates = [
            f"{RAW_BASE}/{repo_full}/{branch}/{repo_name}.html",
            f"{RAW_BASE}/{repo_full}/{branch}/{config.get('slug', repo_name)}.html",
        ]
        for cand in candidates:
            preview_text = cached_or_fetch_text("preview.html", cand)
            if preview_text:
                break

        # --- assemble record ---------------------------------------------
        book = build_book_record(
            repo=repo, config=config, opf=opf, downloads=downloads
        )

        # --- write outputs -----------------------------------------------
        write_book(book, cover_bytes, preview_text)

        state[repo_name] = {
            "pushed_at": pushed_at,
            "author_slug": book["author_slug"],
            "title_slug": book["slug"],
        }
        stats.processed += 1
        if cache_fresh:
            stats.cached += 1
        log(f"  ✓ {repo_name} → /ebooks/{book['author_slug']}/{book['slug']}/")
        return book

    except RateLimitError:
        raise
    except (GitHubError, ValueError, OSError) as exc:
        err(f"{repo_name}: {exc}")
        stats.errors += 1
        stats.error_repos.append(repo_name)
        return None


def _latest_release(releases: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the newest non-prerelease release, falling back to the newest."""
    if not releases:
        return None
    stable = [r for r in releases if not r.get("prerelease") and not r.get("draft")]
    pool = stable or releases
    return max(
        pool,
        key=lambda r: r.get("published_at") or r.get("created_at") or "",
    )


def write_book(
    book: dict[str, Any],
    cover_bytes: bytes | None,
    preview_text: str | None,
) -> None:
    """Write the Hugo markdown, cover, and preview files for one book."""
    author_dir = CONTENT_EBOOKS / book["author_slug"]
    author_dir.mkdir(parents=True, exist_ok=True)

    # Drop book-internal keys not needed in front matter.
    front = {k: v for k, v in book.items() if k not in ("branch", "repo_name")}
    md = to_front_matter(front)
    (author_dir / f"{book['slug']}.md").write_text(md, encoding="utf-8")

    if cover_bytes:
        COVERS_DIR.mkdir(parents=True, exist_ok=True)
        (COVERS_DIR / f"{book['author_slug']}_{book['slug']}.jpg").write_bytes(
            cover_bytes
        )

    if preview_text:
        PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)
        (PREVIEWS_DIR / f"{book['author_slug']}_{book['slug']}.html").write_text(
            preview_text, encoding="utf-8"
        )


# --------------------------------------------------------------------------- #
# Index / author pages
# --------------------------------------------------------------------------- #

def write_indexes(books: list[dict[str, Any]]) -> None:
    """Generate the /ebooks grid index and per-author index pages."""
    CONTENT_EBOOKS.mkdir(parents=True, exist_ok=True)

    ebooks_index = (
        "---\n"
        "title: \"Ebooks\"\n"
        "description: \"Browse the Impression Editions catalog.\"\n"
        "layout: \"list\"\n"
        "---\n\n"
        "Browse our growing catalog of carefully produced ebooks.\n"
    )
    (CONTENT_EBOOKS / "_index.md").write_text(ebooks_index, encoding="utf-8")

    # Group by author for author index pages.
    authors: dict[str, list[dict[str, Any]]] = {}
    for book in books:
        authors.setdefault(book["author_slug"], []).append(book)

    for author_slug, author_books in authors.items():
        author_name = author_books[0].get("author", author_slug)
        author_dir = CONTENT_EBOOKS / author_slug
        author_dir.mkdir(parents=True, exist_ok=True)
        index_md = (
            "---\n"
            f"title: {yaml_quote(author_name)}\n"
            f"author_slug: {yaml_quote(author_slug)}\n"
            f"description: {yaml_quote('Books by ' + author_name)}\n"
            "layout: \"list\"\n"
            "---\n\n"
            f"All ebooks by {author_name}.\n"
        )
        (author_dir / "_index.md").write_text(index_md, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Feeds (Atom / RSS / OPDS)
# --------------------------------------------------------------------------- #

def iso_to_rfc822(iso: str) -> str:
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
    except (ValueError, TypeError):
        return ""


def write_feeds(books: list[dict[str, Any]]) -> None:
    """Generate Atom, RSS, and OPDS feeds from the newest books."""
    FEEDS_DIR.mkdir(parents=True, exist_ok=True)
    newest = sorted(
        books,
        key=lambda b: b.get("date", "") or "",
        reverse=True,
    )[:20]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (FEEDS_DIR / "atom.xml").write_text(
        render_atom(newest, now), encoding="utf-8"
    )
    (FEEDS_DIR / "rss.xml").write_text(
        render_rss(newest, now), encoding="utf-8"
    )
    (FEEDS_DIR / "opds.xml").write_text(
        render_opds(newest, now), encoding="utf-8"
    )


def _book_url(book: dict[str, Any]) -> str:
    return f"{SITE_BASE_URL}ebooks/{book['author_slug']}/{book['slug']}/"


def _xml_escape(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _strip_for_feed(text: str) -> str:
    return _TAG_RE.sub("", text or "").strip()


def render_atom(books: list[dict[str, Any]], updated: str) -> str:
    entries = []
    for book in books:
        url = _book_url(book)
        date = book.get("date", "") or updated
        summary = _xml_escape(_strip_for_feed(book.get("description", "")))[:500]
        entries.append(
            "<entry>\n"
            f"  <id>{_xml_escape(url)}</id>\n"
            f"  <title>{_xml_escape(book.get('title', ''))}</title>\n"
            f"  <link href=\"{_xml_escape(url)}\"/>\n"
            f"  <updated>{_xml_escape(date)}</updated>\n"
            f"  <author><name>{_xml_escape(book.get('author', ''))}</name></author>\n"
            f"  <summary>{summary}</summary>\n"
            "</entry>"
        )
    body = "\n".join(entries)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom">\n'
        f"  <id>{_xml_escape(SITE_BASE_URL)}</id>\n"
        f"  <title>Impression Editions — New Releases</title>\n"
        f'  <link href="{_xml_escape(SITE_BASE_URL)}feeds/atom.xml" rel="self"/>\n'
        f'  <link href="{_xml_escape(SITE_BASE_URL)}"/>\n'
        f"  <updated>{_xml_escape(updated)}</updated>\n"
        f"{body}\n"
        "</feed>\n"
    )


def render_rss(books: list[dict[str, Any]], now: str) -> str:
    items = []
    for book in books:
        url = _book_url(book)
        date = iso_to_rfc822(book.get("date", "")) or now
        desc = _xml_escape(_strip_for_feed(book.get("description", "")))[:500]
        items.append(
            "    <item>\n"
            f"      <title>{_xml_escape(book.get('title', ''))}</title>\n"
            f"      <link>{_xml_escape(url)}</link>\n"
            f"      <guid isPermaLink=\"true\">{_xml_escape(url)}</guid>\n"
            f"      <pubDate>{_xml_escape(date)}</pubDate>\n"
            f"      <description>{desc}</description>\n"
            "    </item>"
        )
    body = "\n".join(items)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<rss version="2.0">\n'
        "  <channel>\n"
        "    <title>Impression Editions — New Releases</title>\n"
        f"    <link>{_xml_escape(SITE_BASE_URL)}</link>\n"
        "    <description>Free, carefully produced ebooks.</description>\n"
        f"    <lastBuildDate>{_xml_escape(iso_to_rfc822(now))}</lastBuildDate>\n"
        f"{body}\n"
        "  </channel>\n"
        "</rss>\n"
    )


def render_opds(books: list[dict[str, Any]], now: str) -> str:
    entries = []
    for book in books:
        url = _book_url(book)
        updated = book.get("date", "") or now
        title = _xml_escape(book.get("title", ""))
        author = _xml_escape(book.get("author", ""))
        acquisitions = []
        dl = book.get("downloads") or {}
        for label, mime, rel in (
            ("epub", "application/epub+zip", "http://opds-spec.org/acquisition"),
            ("epub_advanced", "application/epub+zip",
             "http://opds-spec.org/acquisition"),
            ("azw3", "application/x-mobipocket-ebook",
             "http://opds-spec.org/acquisition"),
        ):
            if dl.get(label):
                acquisitions.append(
                    f'    <link rel="{rel}" '
                    f'href="{_xml_escape(dl[label])}" type="{mime}"/>'
                )
        acq = "\n".join(acquisitions)
        entries.append(
            "  <entry>\n"
            f"    <title>{title}</title>\n"
            f"    <id>{_xml_escape(url)}</id>\n"
            f"    <updated>{_xml_escape(updated)}</updated>\n"
            f"    <author><name>{author}</name></author>\n"
            f'    <link rel="http://opds-spec.org/image" '
            f'href="{_xml_escape(SITE_BASE_URL.rstrip("/") + book.get("cover", ""))}" type="image/jpeg"/>\n'
            f'    <link rel="alternate" href="{_xml_escape(url)}" type="text/html"/>\n'
            f"{acq}\n"
            "  </entry>"
        )
    body = "\n".join(entries)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom">\n'
        '  <id>urn:uuid:impression-editions:catalog</id>\n'
        "  <title>Impression Editions Catalog</title>\n"
        f"  <updated>{_xml_escape(now)}</updated>\n"
        f'  <link rel="self" href="{_xml_escape(SITE_BASE_URL)}feeds/opds.xml" '
        'type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>\n'
        f'  <link rel="start" href="{_xml_escape(SITE_BASE_URL)}feeds/opds.xml" '
        'type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>\n'
        f"{body}\n"
        "</feed>\n"
    )


# --------------------------------------------------------------------------- #
# Repo discovery
# --------------------------------------------------------------------------- #

def fetch_all_repos() -> list[dict[str, Any]]:
    """Fetch every repo in the org, following pagination."""
    repos: list[dict[str, Any]] = []
    url: str | None = f"{API_BASE}/orgs/{ORG}/repos?per_page=100&type=all"
    while url:
        data = github_api_get(url)
        if data is None:
            break
        repos.extend(data)
        url = _next_link(url)
    # Only keep repos that look like book repos (have a config.json). We don't
    # filter strictly here — process_repo handles missing metadata — but we do
    # exclude obvious non-book repos such as this website's own repo.
    return [r for r in repos if r.get("name", "").lower() not in {
        "ie-website", "impression-editions.github.io", ".github"
    }]


def _next_link(current_url: str) -> str | None:
    """Follow the GitHub API Link header to the next page."""
    if requests is None:
        return None
    resp = requests.head(current_url, headers=_auth_headers(), timeout=HTTP_TIMEOUT)
    link = resp.headers.get("Link", "")
    match = re.search(r'<([^>]+)>;\s*rel="next"', link)
    return match.group(1) if match else None


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def run(force: bool = False) -> Stats:
    """Run the full catalog build. Returns the run statistics."""
    if requests is None:
        raise RuntimeError(
            "The 'requests' package is required. "
            "Install with: pip install -r requirements.txt"
        )

    for directory in (CONTENT_EBOOKS, COVERS_DIR, PREVIEWS_DIR, FEEDS_DIR, CACHE_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    stats = Stats()
    state = load_state()

    log(f"Scanning GitHub org '{ORG}'...")
    try:
        repos = fetch_all_repos()
    except RateLimitError:
        raise
    except GitHubError as exc:
        err(f"Failed to list repos: {exc}")
        return stats

    log(f"Found {len(repos)} repos.")
    if not repos:
        warn("No repos found. Set GITHUB_TOKEN if this org has private repos.")
        return stats

    books: list[dict[str, Any]] = []
    for repo in sorted(repos, key=lambda r: r.get("name", "")):
        book = process_repo(
            repo, stats=stats, force=force, state=state
        )
        if book:
            books.append(book)

    write_indexes(books)
    write_feeds(books)
    save_state(state)

    log(stats.summary())
    if stats.error_repos:
        log("Failed repos: " + ", ".join(stats.error_repos))
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate the Impression Editions Hugo catalog from GitHub."
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Ignore the cache and re-fetch every repo.",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Process at most N repos (0 = no limit). Useful for testing.",
    )
    args = parser.parse_args(argv)

    stats = run(force=args.force)
    total = stats.processed + stats.errors + stats.skipped
    if total > 0 and stats.errors / total > FAILURE_THRESHOLD:
        err(
            f"Failure rate {stats.errors}/{total} exceeds {FAILURE_THRESHOLD:.0%} "
            "threshold — aborting build."
        )
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RateLimitError as exc:
        err(str(exc))
        raise SystemExit(2)
