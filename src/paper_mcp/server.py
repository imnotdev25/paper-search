#!/usr/bin/env python3
"""
Academic Paper Search MCP Server
=================================
Retrieves academic papers by title using:
  - Semantic Scholar API  (metadata, citations, references)
  - arXiv API / HTML      (full text for arXiv preprints)
  - Unpaywall             (open-access PDF URLs via DOI)
  - gomcp / Lightpanda    (browser fallback for JS-rendered pages)

Most tools accept paper_title (str); the DOI→metadata tool accepts doi (str).

Run via uvx:
  uvx paper-search-mcp                    # stdio (default)
  uvx paper-search-mcp --transport sse    # SSE on port 8000

Run locally with uv:
  uv run paper-search-mcp
  uv run paper-search-mcp --transport sse
"""

import argparse
import asyncio
import json
import re
import subprocess
import urllib.parse
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx
from fastmcp import Client, Context, FastMCP
from pydantic import BaseModel, ConfigDict, Field

# ── Constants ─────────────────────────────────────────────────────────────────

S2_BASE = "https://api.semanticscholar.org/graph/v1"
ARXIV_API = "https://export.arxiv.org/api/query"
UNPAYWALL_BASE = "https://api.unpaywall.org/v2"
UNPAYWALL_EMAIL = "research@example.com"  # Required by Unpaywall ToS
GOMCP_SSE_URL = "http://localhost:8081"   # Default gomcp SSE address

S2_PAPER_FIELDS = (
    "paperId,externalIds,title,abstract,year,authors,venue,"
    "publicationVenue,referenceCount,citationCount,openAccessPdf,"
    "fieldsOfStudy,tldr,publicationTypes,publicationDate,journal,isOpenAccess"
)

# ── Lifespan ──────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(server: FastMCP):
    """
    Starts gomcp (Lightpanda browser) as a background SSE subprocess,
    creates a shared httpx.AsyncClient, and tears everything down on exit.
    If gomcp is not installed the server still works — browser tools degrade
    gracefully to returning the abstract / metadata instead.
    """
    browser_proc: Optional[subprocess.Popen] = None
    browser_available = False

    try:
        browser_proc = subprocess.Popen(
            ["gomcp", "sse"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        await asyncio.sleep(1.5)
        if browser_proc.poll() is None:
            browser_available = True
    except FileNotFoundError:
        browser_available = False

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        follow_redirects=True,
        headers={"User-Agent": "PaperSearchMCP/1.0 (academic-research-bot)"},
    ) as http:
        yield {"http": http, "browser_available": browser_available}

    if browser_proc and browser_proc.poll() is None:
        browser_proc.terminate()
        try:
            browser_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            browser_proc.kill()


# ── Server ────────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "paper_search_mcp",
    lifespan=lifespan,
)

# ── Shared input model ────────────────────────────────────────────────────────


class PaperInput(BaseModel):
    """Shared input accepted by every tool in this server."""

    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_assignment=True,
        extra="forbid",
    )

    paper_title: str = Field(
        ...,
        description=(
            "Full or partial title of the academic paper to search for. "
            "Example: 'Attention Is All You Need'"
        ),
        min_length=3,
        max_length=500,
    )


# ── Private helpers ───────────────────────────────────────────────────────────

# Semantic Scholar official status-code descriptions
_S2_STATUS: dict[int, str] = {
    200: "OK — request successful.",
    400: "Bad Request — the server could not understand your request. Check your parameters.",
    401: "Unauthorized — not authenticated or credentials are invalid.",
    403: "Forbidden — server understood the request but refused it; you lack permission.",
    404: "Not Found — the requested resource or endpoint does not exist.",
    429: "Too Many Requests — rate limit exceeded; backing off before retry.",
    500: "Internal Server Error — something went wrong on Semantic Scholar's side.",
}

_S2_MAX_RETRIES = 10    # maximum retry attempts on 429 / 5xx
_S2_MAX_WAIT    = 15.0  # seconds — exponential back-off ceiling


async def _s2_request(
    http: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs,
) -> httpx.Response:
    """
    Central Semantic Scholar HTTP wrapper with:
      • Full status-code descriptions for 200/400/401/403/404/429/500.
      • Exponential back-off retry for 429 and 5xx responses.
        - Up to _S2_MAX_RETRIES (10) attempts.
        - Wait = min(2^attempt, _S2_MAX_WAIT) seconds (hard-capped at 15 s).
        - Respects the Retry-After response header when present on 429.
      • Raises httpx.HTTPStatusError with an enriched message on final failure.
    """
    last_exc: Optional[httpx.HTTPStatusError] = None

    for attempt in range(_S2_MAX_RETRIES + 1):
        r = await http.request(method, url, **kwargs)

        if r.status_code == 200:
            return r

        status_msg = _S2_STATUS.get(r.status_code, f"HTTP {r.status_code} — unexpected status.")

        # Non-retryable client errors — surface immediately with clear message
        if r.status_code in (400, 401, 403, 404):
            raise httpx.HTTPStatusError(
                f"Semantic Scholar {r.status_code}: {status_msg}",
                request=r.request,
                response=r,
            )

        # 429 or 5xx — retryable with exponential back-off
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == _S2_MAX_RETRIES:
                last_exc = httpx.HTTPStatusError(
                    f"Semantic Scholar {r.status_code}: {status_msg} "
                    f"(gave up after {_S2_MAX_RETRIES} retries)",
                    request=r.request,
                    response=r,
                )
                break

            # Honour Retry-After when the server provides it
            retry_after = r.headers.get("Retry-After", "")
            if retry_after.isdigit():
                wait = min(float(retry_after), _S2_MAX_WAIT)
            else:
                wait = min(2.0 ** attempt, _S2_MAX_WAIT)

            await asyncio.sleep(wait)
            continue

        # Any other unexpected status
        r.raise_for_status()

    if last_exc:
        raise last_exc
    raise RuntimeError("Unexpected exit from _s2_request retry loop")


async def _s2_search(http: httpx.AsyncClient, title: str) -> Optional[dict]:
    """Search Semantic Scholar by title; return best-match paper dict."""
    r = await _s2_request(
        http, "GET", f"{S2_BASE}/paper/search",
        params={"query": title, "limit": 1, "fields": S2_PAPER_FIELDS},
    )
    items = r.json().get("data", [])
    return items[0] if items else None


async def _arxiv_search(http: httpx.AsyncClient, title: str) -> Optional[dict]:
    """Search arXiv; return minimal dict with arxiv_id, pdf_url, summary."""
    r = await http.get(
        ARXIV_API,
        params={
            "search_query": f'ti:"{title}"',
            "max_results": 1,
            "sortBy": "relevance",
        },
    )
    r.raise_for_status()
    text = r.text
    if "<entry>" not in text:
        return None

    def _grab(tag: str) -> str:
        s = text.find(f"<{tag}>")
        e = text.find(f"</{tag}>")
        return text[s + len(tag) + 2 : e].strip() if s != -1 and e != -1 else ""

    raw_id = _grab("id")
    arxiv_id = raw_id.split("/abs/")[-1] if "/abs/" in raw_id else None
    base_id = arxiv_id.split("v")[0] if arxiv_id and "v" in arxiv_id else arxiv_id

    return {
        "title": _grab("title"),
        "summary": _grab("summary"),
        "arxiv_id": base_id,
        "pdf_url": f"https://arxiv.org/pdf/{base_id}" if base_id else None,
        "html_url": f"https://arxiv.org/html/{base_id}" if base_id else None,
    }


async def _unpaywall_pdf(http: httpx.AsyncClient, doi: str) -> Optional[str]:
    """Resolve open-access PDF URL via Unpaywall (requires DOI)."""
    encoded = urllib.parse.quote(doi, safe="")
    r = await http.get(
        f"{UNPAYWALL_BASE}/{encoded}", params={"email": UNPAYWALL_EMAIL}
    )
    if r.status_code != 200:
        return None
    best = r.json().get("best_oa_location") or {}
    return best.get("url_for_pdf") or best.get("url")


async def _arxiv_fulltext(http: httpx.AsyncClient, arxiv_id: str) -> Optional[str]:
    """Fetch plain-text content from arXiv's HTML5 renderer (up to 50k chars)."""
    url = f"https://arxiv.org/html/{arxiv_id}"
    try:
        r = await http.get(url)
        if r.status_code != 200 or "<article" not in r.text:
            return None
        text = re.sub(
            r"<(script|style)[^>]*>.*?</(script|style)>",
            "",
            r.text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text[:50_000].strip()
    except Exception:
        return None


async def _browser_fetch(title: str) -> Optional[str]:
    """
    Use gomcp / Lightpanda browser to fetch visible page text from a
    Google Scholar search for the paper title.
    """
    try:
        async with Client(GOMCP_SSE_URL) as browser:
            tools = {t.name for t in await browser.list_tools()}
            query_url = (
                "https://scholar.google.com/scholar?q="
                + urllib.parse.quote(title)
            )
            for nav_name in ("browser_navigate", "navigate", "goto"):
                if nav_name in tools:
                    await browser.call_tool(nav_name, {"url": query_url})
                    break
            else:
                return None
            await asyncio.sleep(2)
            for text_name in ("browser_text", "get_text", "extract_text", "content"):
                if text_name in tools:
                    result = await browser.call_tool(text_name, {})
                    return str(result.data)[:20_000]
    except Exception:
        return None
    return None


def _normalise_paper(paper: dict) -> dict:
    """Flatten a Semantic Scholar paper dict into clean metadata."""
    ext = paper.get("externalIds") or {}
    journal = paper.get("journal") or {}
    return {
        "title": paper.get("title", ""),
        "abstract": paper.get("abstract", ""),
        "year": paper.get("year"),
        "authors": [a.get("name", "") for a in (paper.get("authors") or [])],
        "venue": paper.get("venue") or journal.get("name") or "",
        "doi": ext.get("DOI"),
        "arxiv_id": ext.get("ArXiv"),
        "semantic_scholar_id": paper.get("paperId"),
        "citation_count": paper.get("citationCount"),
        "reference_count": paper.get("referenceCount"),
        "fields_of_study": paper.get("fieldsOfStudy") or [],
        "publication_types": paper.get("publicationTypes") or [],
        "publication_date": paper.get("publicationDate"),
        "is_open_access": paper.get("isOpenAccess", False),
        "open_access_pdf": (paper.get("openAccessPdf") or {}).get("url"),
        "tldr": (paper.get("tldr") or {}).get("text"),
    }


def _api_error(e: Exception) -> str:
    """Format API/network errors into structured, actionable JSON.

    For Semantic Scholar errors the message already contains the official
    status-code description from _S2_STATUS (injected by _s2_request).
    """
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        # _s2_request embeds the human-readable description in str(e); use it
        # directly so callers always see the full official S2 message.
        detail = str(e)
        # Supplement with a next-step hint for common codes
        hints: dict[int, str] = {
            400: "Double-check query parameters and try again.",
            401: "Provide a valid Semantic Scholar API key via the S2_API_KEY env var.",
            403: "Your account does not have access to this endpoint.",
            404: "The paper or resource was not found — try a different title or DOI.",
            429: f"Rate limit reached. The server was retried {_S2_MAX_RETRIES} times "
                 f"with back-off up to {_S2_MAX_WAIT}s. Wait a minute, then retry.",
            500: "Semantic Scholar is experiencing server-side issues. Try again later.",
        }
        return json.dumps({
            "error": detail,
            "status_code": code,
            "hint": hints.get(code, "See https://api.semanticscholar.org for API status."),
        })
    if isinstance(e, httpx.TimeoutException):
        return json.dumps({
            "error": "Request timed out.",
            "hint": "The upstream API is slow. Try again shortly.",
        })
    return json.dumps({"error": f"{type(e).__name__}: {e}"})


# ── Tools ─────────────────────────────────────────────────────────────────────


@mcp.tool(
    name="paper_get_metadata",
    annotations={
        "title": "Get Paper Metadata",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def paper_get_metadata(params: PaperInput, ctx: Context) -> str:
    """Return comprehensive metadata for an academic paper searched by title.

    Queries Semantic Scholar first (richest metadata), then falls back to arXiv.

    Returns JSON with: title, authors, abstract, year, venue, doi, arxiv_id,
    semantic_scholar_id, citation_count, reference_count, fields_of_study,
    publication_types, publication_date, is_open_access, open_access_pdf, tldr.

    Args:
        params (PaperInput): { paper_title: str }

    Returns:
        str: JSON-encoded metadata dict or { "error": "..." }.
    """
    http: httpx.AsyncClient = ctx.lifespan_context["http"]
    try:
        await ctx.info(f"[metadata] Searching: {params.paper_title!r}")
        paper = await _s2_search(http, params.paper_title)
        if paper:
            return json.dumps({**_normalise_paper(paper), "source": "semantic_scholar"}, indent=2)

        await ctx.info("[metadata] Falling back to arXiv…")
        arxiv = await _arxiv_search(http, params.paper_title)
        if arxiv:
            return json.dumps({**arxiv, "source": "arxiv"}, indent=2)

        return json.dumps({"error": "Paper not found. Check the title spelling."})
    except Exception as e:
        return _api_error(e)


@mcp.tool(
    name="paper_get_pdf",
    annotations={
        "title": "Get Paper PDF URL",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def paper_get_pdf(params: PaperInput, ctx: Context) -> str:
    """Find the best open-access PDF URL for a paper by title.

    Source priority: Semantic Scholar OA → arXiv PDF → Unpaywall → gomcp browser.

    Returns JSON with: pdf_url, source, title, year. May include browser_excerpt
    if gomcp fallback was used.

    Args:
        params (PaperInput): { paper_title: str }

    Returns:
        str: JSON-encoded result or { "error": "..." }.
    """
    http: httpx.AsyncClient = ctx.lifespan_context["http"]
    browser_available: bool = ctx.lifespan_context["browser_available"]
    try:
        paper = await _s2_search(http, params.paper_title)
        base: dict[str, Any] = {}

        if paper:
            meta = _normalise_paper(paper)
            base = {"title": meta["title"], "year": meta["year"]}

            if meta["open_access_pdf"]:
                return json.dumps({**base, "pdf_url": meta["open_access_pdf"], "source": "semantic_scholar"}, indent=2)
            if meta["arxiv_id"]:
                return json.dumps({**base, "pdf_url": f"https://arxiv.org/pdf/{meta['arxiv_id']}", "source": "arxiv"}, indent=2)
            if meta["doi"]:
                await ctx.info("[pdf] Trying Unpaywall…")
                url = await _unpaywall_pdf(http, meta["doi"])
                if url:
                    return json.dumps({**base, "pdf_url": url, "source": "unpaywall"}, indent=2)

        if browser_available:
            await ctx.info("[pdf] Trying gomcp browser fallback…")
            excerpt = await _browser_fetch(params.paper_title)
            if excerpt:
                return json.dumps({**base, "pdf_url": None, "source": "browser_gomcp",
                                   "note": "PDF URL not auto-resolved; browser excerpt included.",
                                   "browser_excerpt": excerpt[:2_000]}, indent=2)

        return json.dumps({"error": "No open-access PDF found.",
                           "title": base.get("title", params.paper_title),
                           "suggestion": "The paper may be paywalled. Try your institution's access."})
    except Exception as e:
        return _api_error(e)


@mcp.tool(
    name="paper_get_fulltext",
    annotations={
        "title": "Get Paper Full Text",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def paper_get_fulltext(params: PaperInput, ctx: Context) -> str:
    """Retrieve the full text of a paper by title (up to 50,000 characters).

    Fetch order: arXiv HTML5 renderer → gomcp browser → abstract-only fallback.

    Returns JSON with: title, abstract, tldr, fulltext (str|null),
    fulltext_length (int), source.

    Args:
        params (PaperInput): { paper_title: str }

    Returns:
        str: JSON-encoded result or { "error": "..." }.
    """
    http: httpx.AsyncClient = ctx.lifespan_context["http"]
    browser_available: bool = ctx.lifespan_context["browser_available"]
    try:
        result: dict[str, Any] = {}
        arxiv_id: Optional[str] = None

        paper = await _s2_search(http, params.paper_title)
        if paper:
            meta = _normalise_paper(paper)
            result = {"title": meta["title"], "abstract": meta["abstract"], "tldr": meta["tldr"]}
            arxiv_id = meta["arxiv_id"]

        if arxiv_id:
            await ctx.info(f"[fulltext] Fetching arXiv HTML for {arxiv_id}…")
            fulltext = await _arxiv_fulltext(http, arxiv_id)
            if fulltext:
                return json.dumps({**result, "fulltext": fulltext,
                                   "fulltext_length": len(fulltext), "source": "arxiv_html"}, indent=2)

        if not result:
            arxiv = await _arxiv_search(http, params.paper_title)
            if arxiv and arxiv.get("arxiv_id"):
                arxiv_id = arxiv["arxiv_id"]
                result = {"title": arxiv.get("title", ""), "abstract": arxiv.get("summary", ""), "tldr": None}
                fulltext = await _arxiv_fulltext(http, arxiv_id)
                if fulltext:
                    return json.dumps({**result, "fulltext": fulltext,
                                       "fulltext_length": len(fulltext), "source": "arxiv_html"}, indent=2)

        if browser_available:
            await ctx.info("[fulltext] Trying gomcp browser…")
            text = await _browser_fetch(params.paper_title)
            if text:
                return json.dumps({**result, "fulltext": text,
                                   "fulltext_length": len(text), "source": "browser_gomcp"}, indent=2)

        if result.get("abstract"):
            return json.dumps({**result, "fulltext": None, "source": "abstract_only",
                               "note": "Full text unavailable; abstract returned."}, indent=2)

        return json.dumps({"error": "Full text not found.", "title": params.paper_title})
    except Exception as e:
        return _api_error(e)


@mcp.tool(
    name="paper_get_citations",
    annotations={
        "title": "Get Papers That Cite This Paper",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def paper_get_citations(params: PaperInput, ctx: Context) -> str:
    """Return up to 100 papers that cite the given paper (forward citations).

    Returns JSON with: paper_title, total_citations, returned,
    citations (list of {title, authors, year, venue, doi, arxiv_id, citation_count}).

    Args:
        params (PaperInput): { paper_title: str }

    Returns:
        str: JSON-encoded citations or { "error": "..." }.
    """
    http: httpx.AsyncClient = ctx.lifespan_context["http"]
    try:
        paper = await _s2_search(http, params.paper_title)
        if not paper:
            return json.dumps({"error": "Paper not found. Try a different title."})

        r = await http.get(
            f"{S2_BASE}/paper/{paper['paperId']}/citations",
            params={"fields": "title,authors,year,venue,externalIds,citationCount", "limit": 100},
        )
        r.raise_for_status()

        def _fmt(item: dict) -> dict:
            p = item.get("citingPaper", {})
            ext = p.get("externalIds") or {}
            return {
                "title": p.get("title", ""),
                "authors": [a.get("name", "") for a in (p.get("authors") or [])],
                "year": p.get("year"),
                "venue": p.get("venue", ""),
                "doi": ext.get("DOI"),
                "arxiv_id": ext.get("ArXiv"),
                "citation_count": p.get("citationCount"),
            }

        citations = [_fmt(i) for i in r.json().get("data", [])]
        return json.dumps({"paper_title": paper.get("title"), "total_citations": paper.get("citationCount"),
                           "returned": len(citations), "citations": citations}, indent=2)
    except Exception as e:
        return _api_error(e)


@mcp.tool(
    name="paper_get_references",
    annotations={
        "title": "Get Paper Reference List",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def paper_get_references(params: PaperInput, ctx: Context) -> str:
    """Return the bibliography of a paper — the papers it cites (backward citations).

    Returns JSON with: paper_title, total_references, returned,
    references (list of {title, authors, year, venue, doi, arxiv_id}).

    Args:
        params (PaperInput): { paper_title: str }

    Returns:
        str: JSON-encoded references or { "error": "..." }.
    """
    http: httpx.AsyncClient = ctx.lifespan_context["http"]
    try:
        paper = await _s2_search(http, params.paper_title)
        if not paper:
            return json.dumps({"error": "Paper not found. Try a different title."})

        r = await http.get(
            f"{S2_BASE}/paper/{paper['paperId']}/references",
            params={"fields": "title,authors,year,venue,externalIds", "limit": 100},
        )
        r.raise_for_status()

        def _fmt(item: dict) -> dict:
            p = item.get("citedPaper", {})
            ext = p.get("externalIds") or {}
            return {
                "title": p.get("title", ""),
                "authors": [a.get("name", "") for a in (p.get("authors") or [])],
                "year": p.get("year"),
                "venue": p.get("venue", ""),
                "doi": ext.get("DOI"),
                "arxiv_id": ext.get("ArXiv"),
            }

        references = [_fmt(i) for i in r.json().get("data", [])]
        return json.dumps({"paper_title": paper.get("title"), "total_references": paper.get("referenceCount"),
                           "returned": len(references), "references": references}, indent=2)
    except Exception as e:
        return _api_error(e)


# ── DOI input model ───────────────────────────────────────────────────────────


class DoiInput(BaseModel):
    """Input model for DOI-based lookups."""

    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_assignment=True,
        extra="forbid",
    )

    doi: str = Field(
        ...,
        description=(
            "The Digital Object Identifier (DOI) of the paper. "
            "Accepts bare DOI or full URL form. "
            "Examples: '10.1145/3442188.3445922'  or  'https://doi.org/10.1145/3442188.3445922'"
        ),
        min_length=5,
        max_length=300,
    )


def _normalise_doi(doi: str) -> str:
    """Strip URL prefix so we always work with a bare DOI like '10.xxxx/...'."""
    doi = doi.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi.org/", "doi:"):
        if doi.lower().startswith(prefix):
            doi = doi[len(prefix):]
            break
    return doi


async def _crossref_metadata(http: httpx.AsyncClient, doi: str) -> Optional[dict]:
    """
    Fetch paper metadata from the Crossref REST API.
    Returns a normalised dict on success, None if not found.
    Crossref is the canonical DOI registry — very high coverage.
    """
    encoded = urllib.parse.quote(doi, safe="")
    r = await http.get(
        f"https://api.crossref.org/works/{encoded}",
        headers={"User-Agent": "PaperSearchMCP/1.0 (mailto:research@example.com)"},
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()

    msg = r.json().get("message", {})

    def _name(author: dict) -> str:
        given = author.get("given", "")
        family = author.get("family", "")
        return f"{given} {family}".strip() if given or family else author.get("name", "")

    # Published date (prefer print, fall back to online / created)
    date_parts = (
        (msg.get("published-print") or msg.get("published-online") or msg.get("created") or {})
        .get("date-parts", [[]])[0]
    )
    pub_date = "-".join(str(p) for p in date_parts) if date_parts else None
    year = date_parts[0] if date_parts else None

    # Journal / venue
    container = (msg.get("container-title") or [""])[0]

    # ISSN
    issn_list = msg.get("ISSN") or []

    # Funder info
    funders = [f.get("name", "") for f in (msg.get("funder") or [])]

    return {
        "title": (msg.get("title") or [""])[0],
        "doi": msg.get("DOI"),
        "abstract": msg.get("abstract"),           # Not always present in Crossref
        "authors": [_name(a) for a in (msg.get("author") or [])],
        "year": year,
        "publication_date": pub_date,
        "venue": container,
        "publisher": msg.get("publisher"),
        "issn": issn_list,
        "volume": msg.get("volume"),
        "issue": msg.get("issue"),
        "page": msg.get("page"),
        "type": msg.get("type"),                   # e.g. "journal-article"
        "subjects": msg.get("subject") or [],
        "funders": funders,
        "reference_count": msg.get("reference-count"),
        "is_referenced_by_count": msg.get("is-referenced-by-count"),
        "license": [(lic.get("URL") or "") for lic in (msg.get("license") or [])],
        "url": msg.get("URL"),
    }


async def _s2_by_doi(http: httpx.AsyncClient, doi: str) -> Optional[dict]:
    """Look up a paper on Semantic Scholar using its DOI (with retry)."""
    encoded = urllib.parse.quote(doi, safe="")
    try:
        r = await _s2_request(
            http, "GET", f"{S2_BASE}/paper/DOI:{encoded}",
            params={"fields": S2_PAPER_FIELDS},
        )
        return r.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None   # DOI simply not in S2 — non-fatal
        raise


async def _unpaywall_full(http: httpx.AsyncClient, doi: str) -> Optional[dict]:
    """
    Fetch the full Unpaywall record for a DOI.
    Returns OA status, best PDF URL, and all OA locations.
    """
    encoded = urllib.parse.quote(doi, safe="")
    r = await http.get(
        f"{UNPAYWALL_BASE}/{encoded}",
        params={"email": UNPAYWALL_EMAIL},
    )
    if r.status_code != 200:
        return None
    data = r.json()
    best = data.get("best_oa_location") or {}
    return {
        "is_oa": data.get("oa_status") != "closed",
        "oa_status": data.get("oa_status"),          # "gold", "green", "bronze", "closed"…
        "best_pdf_url": best.get("url_for_pdf") or best.get("url"),
        "best_host_type": best.get("host_type"),      # "publisher" | "repository"
        "oa_locations": [
            {
                "url": loc.get("url_for_pdf") or loc.get("url"),
                "host_type": loc.get("host_type"),
                "version": loc.get("version"),         # "publishedVersion", "acceptedVersion"…
                "license": loc.get("license"),
            }
            for loc in (data.get("oa_locations") or [])
            if loc.get("url_for_pdf") or loc.get("url")
        ],
    }


# ── Tool 6: DOI → Metadata ────────────────────────────────────────────────────


@mcp.tool(
    name="doi_get_metadata",
    annotations={
        "title": "Resolve DOI to Full Paper Metadata",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def doi_get_metadata(params: DoiInput, ctx: Context) -> str:
    """Resolve a DOI to comprehensive paper metadata from multiple authoritative sources.

    This is the reverse of paper_get_metadata: given a DOI you already know,
    retrieve everything about the paper without needing its title.

    Data is merged from three complementary sources (all queried in parallel):
      1. Crossref  — canonical DOI registry; best for bibliographic data,
                     publisher, volume/issue/page, funders, license.
      2. Semantic Scholar — adds citation count, references, fields of study,
                            TL;DR, and open-access PDF link.
      3. Unpaywall — adds detailed open-access status and every known PDF URL.

    Returns a merged JSON object with:
        doi, title, abstract, authors, year, publication_date,
        venue, publisher, volume, issue, page, type, subjects,
        issn, funders, license,
        citation_count, reference_count, is_referenced_by_count,
        fields_of_study, tldr, publication_types,
        is_open_access, oa_status, open_access_pdf,
        oa_locations (list of all known PDF URLs with host_type / version / license),
        arxiv_id, semantic_scholar_id,
        sources (list of which APIs responded successfully)

    Args:
        params (DoiInput): { doi: str }
            Accepts bare DOI or full URL, e.g.:
              "10.1145/3442188.3445922"
              "https://doi.org/10.1145/3442188.3445922"

    Returns:
        str: JSON-encoded metadata dict or { "error": "..." }.
    """
    http: httpx.AsyncClient = ctx.lifespan_context["http"]

    doi = _normalise_doi(params.doi)
    await ctx.info(f"[doi_get_metadata] Resolving DOI: {doi}")

    try:
        # ── Query all three sources in parallel ───────────────────────────────
        crossref_task    = asyncio.create_task(_crossref_metadata(http, doi))
        s2_task          = asyncio.create_task(_s2_by_doi(http, doi))
        unpaywall_task   = asyncio.create_task(_unpaywall_full(http, doi))

        crossref, s2_raw, unpaywall = await asyncio.gather(
            crossref_task, s2_task, unpaywall_task, return_exceptions=True
        )

        # Treat exceptions from any source as None (non-fatal)
        if isinstance(crossref, Exception):
            await ctx.warning(f"[doi_get_metadata] Crossref error: {crossref}")
            crossref = None
        if isinstance(s2_raw, Exception):
            await ctx.warning(f"[doi_get_metadata] Semantic Scholar error: {s2_raw}")
            s2_raw = None
        if isinstance(unpaywall, Exception):
            await ctx.warning(f"[doi_get_metadata] Unpaywall error: {unpaywall}")
            unpaywall = None

        if not crossref and not s2_raw:
            return json.dumps({
                "error": "DOI not found in Crossref or Semantic Scholar.",
                "doi": doi,
                "suggestion": "Check the DOI is correct. Preprints may only exist on arXiv.",
            })

        s2 = _normalise_paper(s2_raw) if s2_raw else {}

        # ── Merge — prefer Crossref for bibliographic fields, S2 for impact ──
        sources = []
        merged: dict[str, Any] = {"doi": doi}

        if crossref:
            sources.append("crossref")
            merged.update({
                "title":            crossref.get("title") or s2.get("title"),
                "abstract":         crossref.get("abstract") or s2.get("abstract"),
                "authors":          crossref.get("authors") or s2.get("authors", []),
                "year":             crossref.get("year") or s2.get("year"),
                "publication_date": crossref.get("publication_date") or s2.get("publication_date"),
                "venue":            crossref.get("venue") or s2.get("venue"),
                "publisher":        crossref.get("publisher"),
                "volume":           crossref.get("volume"),
                "issue":            crossref.get("issue"),
                "page":             crossref.get("page"),
                "type":             crossref.get("type"),
                "subjects":         crossref.get("subjects", []),
                "issn":             crossref.get("issn", []),
                "funders":          crossref.get("funders", []),
                "license":          crossref.get("license", []),
                "url":              crossref.get("url"),
                "is_referenced_by_count": crossref.get("is_referenced_by_count"),
            })
        else:
            merged.update({
                "title":            s2.get("title"),
                "abstract":         s2.get("abstract"),
                "authors":          s2.get("authors", []),
                "year":             s2.get("year"),
                "publication_date": s2.get("publication_date"),
                "venue":            s2.get("venue"),
            })

        if s2_raw:
            sources.append("semantic_scholar")
            merged.update({
                "semantic_scholar_id": s2.get("semantic_scholar_id"),
                "arxiv_id":            s2.get("arxiv_id"),
                "citation_count":      s2.get("citation_count"),
                "reference_count":     s2.get("reference_count") or crossref.get("reference_count") if crossref else s2.get("reference_count"),
                "fields_of_study":     s2.get("fields_of_study", []),
                "publication_types":   s2.get("publication_types", []),
                "tldr":                s2.get("tldr"),
                # S2 OA PDF as starting point (may be overridden by Unpaywall below)
                "is_open_access":      s2.get("is_open_access", False),
                "open_access_pdf":     s2.get("open_access_pdf"),
            })

        if unpaywall:
            sources.append("unpaywall")
            merged.update({
                "is_open_access":  unpaywall.get("is_oa", merged.get("is_open_access", False)),
                "oa_status":       unpaywall.get("oa_status"),
                "open_access_pdf": unpaywall.get("best_pdf_url") or merged.get("open_access_pdf"),
                "oa_locations":    unpaywall.get("oa_locations", []),
            })

        merged["sources"] = sources
        return json.dumps(merged, indent=2)

    except Exception as e:
        return _api_error(e)


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    """CLI entry point — called by `uvx paper-search-mcp` or `uv run paper-search-mcp`."""
    parser = argparse.ArgumentParser(
        prog="paper-search-mcp",
        description="Academic Paper Search MCP Server (FastMCP + Lightpanda browser)",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport mode: 'stdio' (default, for Claude Desktop) or 'sse' (remote/multi-client)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for SSE transport (default: 8000)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for SSE transport (default: 127.0.0.1)",
    )
    args = parser.parse_args()

    if args.transport == "sse":
        mcp.run(transport="sse", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

