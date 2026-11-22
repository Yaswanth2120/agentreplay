"""Virtual relative POSIX paths only; tools never access the host filesystem."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, ClassVar

from .base import Environment, EnvironmentError, tool_schema

PATH = {"type": "string", "minLength": 1}


def safe_path(path: str) -> str:
    parts = path.split("/")
    if (
        "\x00" in path
        or path.startswith("/")
        or "\\" in path
        or any(p in {"", ".", ".."} for p in parts)
    ):
        raise EnvironmentError("path must be a normalized relative POSIX file path")
    return str(PurePosixPath(path))


class Files(Environment):
    schemas: ClassVar[list[dict[str, Any]]] = [
        tool_schema(
            "read", "Read a virtual UTF-8 text file.", {"path": PATH}, ["path"]
        ),
        tool_schema(
            "write",
            "Create or replace a virtual text file.",
            {"path": PATH, "content": {"type": "string"}},
            ["path", "content"],
        ),
        tool_schema("list", "List virtual paths in sorted order.", {}, []),
        tool_schema(
            "grep",
            "Find case-sensitive literal text; line numbers start at one.",
            {"text": {"type": "string", "minLength": 1}},
            ["text"],
        ),
    ]

    def __init__(self, seed: int = 0) -> None:
        self.reset(seed)

    def reset(self, seed: int) -> None:
        self.contents = {
            "notes/readme.txt": f"Fixture seed {seed}\nPreserve this file.\n"
        }

    def snapshot(self) -> dict[str, Any]:
        return dict(self.contents)

    def read(self, path: str) -> str:
        path = safe_path(path)
        if path not in self.contents:
            raise EnvironmentError(f"missing file {path!r}")
        return self.contents[path]

    def write(self, path: str, content: str) -> str:
        path = safe_path(path)
        if any(
            p.startswith(path + "/") or path.startswith(p + "/") for p in self.contents
        ):
            raise EnvironmentError("file conflicts with a virtual directory")
        self.contents[path] = content
        return content

    def list(self) -> list[str]:
        return sorted(self.contents)

    def grep(self, text: str) -> list[dict[str, Any]]:
        return [
            {"path": path, "line": i, "text": line}
            for path in sorted(self.contents)
            for i, line in enumerate(self.contents[path].splitlines(), 1)
            if text in line
        ]
