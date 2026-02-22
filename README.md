# 📄 paper-mcp

An MCP server built with **FastMCP** that lets Claude (or any LLM) retrieve academic papers **by title**.  
Run it in one command with **uvx** — no manual install needed.

---

## ✨ Features

5 tools, all taking `paper_title` as the only argument:

| Tool | Returns |
|------|---------|
| `paper_get_metadata` | Title, authors, abstract, DOI, arXiv ID, citation count, TL;DR, OA status, fields of study |
| `paper_get_pdf` | Best open-access PDF URL |
| `paper_get_fulltext` | Full plain text (up to 50,000 chars) |
| `paper_get_citations` | Up to 100 papers that cite this one |
| `paper_get_references` | Up to 100 papers this one cites |

Data sources (priority order): **Semantic Scholar → arXiv → Unpaywall → Lightpanda browser via gomcp**

---

## 🚀 Quick Start

### Run without installing (uvx)

```bash
# stdio mode — for Claude Desktop / most MCP clients
uvx paper-mcp

# SSE mode — for remote or multi-client setups
uvx paper-mcp --transport sse --port 8000
```

> `uvx` downloads, installs (in an isolated env), and runs the package — zero setup.

### Install permanently

```bash
uv tool install paper-mcp
paper-mcp                        # now available globally
paper-mcp --transport sse
```

### Local development

```bash
git clone https://github.com/imnotdev25/paper-search
cd paper-search
uv sync                                 # install all deps from pyproject.toml
uv run paper-mcp                 # run directly
uv run paper-mcp --transport sse
```

---

## 🖥 Claude Desktop Config

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "papers": {
      "command": "uvx",
      "args": ["paper-mcp"]
    }
  }
}
```

No Python paths, no venv activation — `uvx` handles everything.

---

## 🌐 Browser Fallback (gomcp / Lightpanda)

For JS-rendered publisher pages, the server automatically starts a
[Lightpanda](https://lightpanda.io) headless browser via
[gomcp](https://github.com/lightpanda-io/gomcp).

**One-time setup:**
```bash
# Download gomcp binary from GitHub releases:
# https://github.com/lightpanda-io/gomcp/releases

# Then download the Lightpanda browser binary:
gomcp download
```

If `gomcp` is not installed, the server still works — browser-dependent
paths fall back to abstract/metadata gracefully.

---

## 🏗 Architecture

```
Claude (LLM)
    │  MCP (stdio or SSE)
    ▼
paper-mcp  [FastMCP, Python]
    │
    ├── Semantic Scholar API  ──  metadata, citations, references
    ├── arXiv API + HTML      ──  preprint info + full text
    ├── Unpaywall API         ──  open-access PDF by DOI
    └── gomcp SSE  ───────────── Lightpanda browser (JS fallback)
             │  CDP
             └── Lightpanda Browser (headless)
```

---

## 📦 Publishing to PyPI

```bash
# Build
uv build

# Publish (needs PyPI token)
uv publish --token $PYPI_TOKEN
```

Once on PyPI, anyone can run it with `uvx paper-mcp`.

---

## ⚙️ CLI Options

```
usage: paper-mcp [-h] [--transport {stdio,sse}] [--port PORT] [--host HOST]

options:
  --transport  stdio (default) or sse
  --port       SSE port (default: 8000)
  --host       SSE host (default: 127.0.0.1)
```

---

## 🔑 Notes

- **Semantic Scholar** free tier: ~100 req/5 min. For higher throughput, set
  `S2_API_KEY` in the environment and add it to the httpx client headers in `server.py`.
- **Unpaywall** requires a valid contact email — update `UNPAYWALL_EMAIL` in `server.py`.
- **Full text** is only available for arXiv papers (HTML renderer) and JS-rendered pages
  reachable via gomcp. Paywalled PDFs require institutional access.

---

## 📁 Project Structure

```
paper-mcp/
├── pyproject.toml                  ← packaging, entry point, deps
├── README.md
└── src/
    └── paper_mcp/
        ├── __init__.py
        └── server.py               ← all 5 FastMCP tools + main()
```