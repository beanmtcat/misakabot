"""Reviewed local retrieval for vendor documentation."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib.resources import files
from typing import TypedDict


DMIT_GETTING_STARTED_URL = "https://docs.dmit.io/zh/guide/getting-started"
_DMIT_DATA_FILE = "data/dmit_docs_zh.json"
_ALIAS_NOTE = "在本群语境中，“大妈”是 DMIT 的常用简称。"


class DmitDocument(TypedDict):
    title: str
    url: str
    content: str


def _terms(text: str) -> set[str]:
    """Use short Chinese n-grams plus Latin words for dependency-free retrieval."""
    terms = {item.casefold() for item in re.findall(r"[a-zA-Z0-9_./-]{2,}", text)}
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        terms.add(run)
        terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return terms


@lru_cache(maxsize=1)
def dmit_documents() -> tuple[DmitDocument, ...]:
    """Load the packaged Chinese DMIT Docs snapshot produced by the sync tool."""
    resource = files("misakabot").joinpath(_DMIT_DATA_FILE)
    payload = json.loads(resource.read_text(encoding="utf-8"))
    documents = payload.get("documents", [])
    if not isinstance(documents, list):
        return ()
    return tuple(
        {"title": str(item["title"]), "url": str(item["url"]), "content": str(item["content"])}
        for item in documents
        if isinstance(item, dict) and {"title", "url", "content"} <= item.keys()
    )


def dmit_knowledge_for_query(query: str, limit: int = 3, chunk_size: int = 1800) -> tuple[str, ...]:
    """Return relevant, source-linked chunks rather than the whole document set."""
    query_terms = _terms(query.replace("大妈", "DMIT"))
    ranked: list[tuple[int, DmitDocument]] = []
    for document in dmit_documents():
        title = document["title"].casefold()
        content = document["content"].casefold()
        score = sum(20 for term in query_terms if term in title)
        score += sum(min(content.count(term), 3) * 3 for term in query_terms if term in content)
        if score:
            ranked.append((score, document))
    ranked.sort(key=lambda item: item[0], reverse=True)
    selected = ranked[:limit]
    if not selected:
        return (
            f"{_ALIAS_NOTE}\nDMIT 官方中文文档已收录，但未找到与当前问题直接对应的条目。"
            f"请以官方文档或工单为准：{DMIT_GETTING_STARTED_URL}",
        )
    return tuple(
        f"{_ALIAS_NOTE}\n[DMIT 官方文档：{document['title']}]\n"
        f"来源：{document['url']}\n{document['content'][:chunk_size]}"
        for _, document in selected
    )
