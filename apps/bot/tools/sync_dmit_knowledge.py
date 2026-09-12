#!/usr/bin/env python3
"""Build the packaged DMIT Chinese documentation snapshot.

Run manually when the official docs change. The Bot never fetches vendor pages
while responding, so a remote page cannot modify runtime instructions.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import deque
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


DOCS_HOST = "docs.dmit.io"
SEED_URL = "https://docs.dmit.io/zh/guide/getting-started/"


class DocumentationParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self._inside_title = False
        self._main_depth = 0
        self.content_parts: list[str] = []
        self.links: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "title":
            self._inside_title = True
        if tag == "main":
            self._main_depth += 1
        elif self._main_depth:
            self._main_depth += 1
        if tag == "a" and attributes.get("href"):
            self.links.add(attributes["href"] or "")
        if self._main_depth and tag in {"p", "li", "h1", "h2", "h3", "h4", "pre", "code", "br"}:
            self.content_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._inside_title = False
        if self._main_depth:
            self._main_depth -= 1
            if tag in {"p", "li", "h1", "h2", "h3", "h4", "pre", "code", "br"}:
                self.content_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._inside_title:
            self.title_parts.append(data)
        if self._main_depth:
            self.content_parts.append(data)

    @property
    def title(self) -> str:
        return " ".join("".join(self.title_parts).split())

    @property
    def content(self) -> str:
        return "\n".join(line.strip() for line in "".join(self.content_parts).splitlines() if line.strip())


def allowed_url(raw_url: str, current_url: str) -> str | None:
    candidate, _ = urldefrag(urljoin(current_url, raw_url))
    parsed = urlparse(candidate)
    if parsed.scheme != "https" or parsed.netloc != DOCS_HOST or not parsed.path.startswith("/zh/"):
        return None
    if parsed.query or parsed.path.endswith((".png", ".jpg", ".svg", ".js", ".css", ".xml")):
        return None
    return candidate if parsed.path.endswith("/") else f"{candidate}/"


class DocsRedirectHandler(HTTPRedirectHandler):
    """Follow canonical docs redirects, including HTTP 308, without leaving the allowed origin."""

    def redirect_request(self, request, file_pointer, status, message, headers, new_url):
        target, _ = urldefrag(urljoin(request.full_url, new_url))
        if allowed_url(target, request.full_url) is None:
            raise HTTPError(request.full_url, status, "unsafe documentation redirect", headers, file_pointer)
        return super().redirect_request(request, file_pointer, status, message, headers, target)

    def http_error_308(self, request, file_pointer, status, message, headers):
        # Python 3.9 only permits 301/302/303/307 in redirect_request.  A permanent
        # 308 has the same GET redirect semantics as 307, so delegate using that code.
        return self.http_error_301(request, file_pointer, 307, message, headers)


DOCS_OPENER = build_opener(DocsRedirectHandler())
RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}
FETCH_ATTEMPTS = 4


def fetch(url: str) -> DocumentationParser:
    request = Request(url, headers={"User-Agent": "MisakaBot-DocsSync/1.0 (+offline knowledge build)"})
    for attempt in range(FETCH_ATTEMPTS):
        try:
            with DOCS_OPENER.open(request, timeout=20) as response:  # noqa: S310 - fixed official HTTPS origin
                body = response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")
            break
        except HTTPError as error:
            if error.code not in RETRYABLE_HTTP_STATUSES or attempt == FETCH_ATTEMPTS - 1:
                raise
        except URLError:
            if attempt == FETCH_ATTEMPTS - 1:
                raise
        time.sleep(0.8 * (2 ** attempt))
    parser = DocumentationParser()
    parser.feed(body)
    return parser


def sync(output: Path, max_pages: int) -> int:
    queue: deque[str] = deque([SEED_URL])
    visited: set[str] = set()
    documents: list[dict[str, str]] = []
    while queue and len(visited) < max_pages:
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        parser = fetch(url)
        if parser.content:
            documents.append({"title": parser.title or url.rsplit("/", 1)[-1], "url": url, "content": parser.content})
        for link in parser.links:
            candidate = allowed_url(link, url)
            if candidate and candidate not in visited:
                queue.append(candidate)
        time.sleep(0.15)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "source": "DMIT Docs Chinese",
                "seed_url": SEED_URL,
                "synced_at": datetime.now(timezone.utc).isoformat(),
                "document_count": len(documents),
                "documents": documents,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return len(documents)


def main() -> None:
    parser = argparse.ArgumentParser(description="Synchronize the DMIT Chinese documentation knowledge base")
    parser.add_argument("--output", type=Path, default=Path("src/misakabot/data/dmit_docs_zh.json"))
    parser.add_argument("--max-pages", type=int, default=160)
    args = parser.parse_args()
    print(f"wrote {sync(args.output, args.max_pages)} documents to {args.output}")


if __name__ == "__main__":
    main()
