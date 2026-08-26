"""Private implementation package for `wt serve` — the local run viewer.

Split mirrors `_report`:
    data.py    — read-only loaders over runs/ artifacts and discovered packs
    server.py  — stdlib ThreadingHTTPServer, JSON endpoints, SSE live tail
    page.py    — the self-contained viewer page (inline CSS/JS, no external
                 requests)

Everything here is read-only by construction: no handler writes under the
runs/ directory (or anywhere else), and the only HTTP method served is GET.
"""
