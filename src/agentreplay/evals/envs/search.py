"""Literal token search over a checked-in synthetic 500-document corpus."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, ClassVar

from .base import Environment, EnvironmentError, tool_schema

CORPUS_PATH = Path(__file__).parents[1] / "data" / "corpus.jsonl"


def tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


class Search(Environment):
    schemas: ClassVar[list[dict[str, Any]]] = [
        tool_schema(
            "search",
            "All query tokens must occur; rank by document ID. Empty queries fail.",
            {
                "query": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            ["query"],
        ),
        tool_schema(
            "get_document",
            "Read one document by ID.",
            {"id": {"type": "string"}},
            ["id"],
        ),
    ]

    def __init__(self, seed: int = 0) -> None:
        self.documents = [
            json.loads(line)
            for line in CORPUS_PATH.read_text(encoding="utf-8").splitlines()
        ]
        if len(self.documents) != 500 or len({d["id"] for d in self.documents}) != 500:
            raise EnvironmentError("corpus must contain 500 unique documents")
        self.reset(seed)

    def reset(self, seed: int) -> None:
        self.seed = seed

    def snapshot(self) -> dict[str, Any]:
        return {"documents": copy.deepcopy(self.documents)}

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        terms = tokens(query)
        if not terms:
            raise EnvironmentError("query must contain a word or number")
        return [
            copy.deepcopy(d)
            for d in sorted(self.documents, key=lambda d: d["id"])
            if terms <= tokens(d["title"] + " " + d["text"])
        ][:limit]

    def get_document(self, id: str) -> dict[str, Any]:
        for document in self.documents:
            if document["id"] == id:
                return copy.deepcopy(document)
        raise EnvironmentError(f"unknown document {id!r}")
