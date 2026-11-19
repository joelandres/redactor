"""Private, in-memory document workspaces: one per browser session.

Nothing is written to disk or cloud storage. A workspace holds the user's uploaded PDFs and
their redacted versions until the user removes them or the workspace has been idle for
WORKSPACE_IDLE_MINUTES, whichever comes first. Workspace IDs are long random tokens kept in
an HttpOnly cookie, so one browser can never see another's documents.
"""
import secrets
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from . import config

MB = 1024 * 1024


class LimitError(Exception):
    """A memory limit would be exceeded; the message is safe to show to the user."""


@dataclass
class Redaction:
    id: str
    name: str
    pdf: bytes
    method: str
    categories: list[str]
    counts: dict[str, int]
    created: float = field(default_factory=time.time)

    def summary(self) -> dict:
        return {"id": self.id, "name": self.name, "method": self.method, "categories": self.categories,
                "counts": self.counts, "size": len(self.pdf), "created": self.created}


@dataclass
class Document:
    id: str
    name: str
    pdf: bytes
    pages: int
    created: float = field(default_factory=time.time)
    redactions: list[Redaction] = field(default_factory=list)   # newest first
    next_version: int = 1                                         # never reused

    @property
    def size(self) -> int:
        return len(self.pdf) + sum(len(r.pdf) for r in self.redactions)

    def summary(self) -> dict:
        return {"id": self.id, "name": self.name, "pages": self.pages, "size": len(self.pdf),
                "created": self.created, "redactions": [r.summary() for r in self.redactions]}

    def file(self, which: str) -> tuple[str, bytes] | None:
        """("original" or a redaction id) -> (file name, bytes)."""
        if which == "original":
            return self.name, self.pdf
        return next(((r.name, r.pdf) for r in self.redactions if r.id == which), None)


@dataclass
class Workspace:
    id: str
    documents: dict[str, Document] = field(default_factory=dict)   # insertion order = upload order
    last_used: float = field(default_factory=time.time)

    @property
    def size(self) -> int:
        return sum(d.size for d in self.documents.values())


class Workspaces:
    def __init__(self) -> None:
        self._all: dict[str, Workspace] = {}

    # ─────────────── sessions ───────────────
    def get_or_create(self, workspace_id: str | None) -> Workspace:
        self.prune()
        ws = self._all.get(workspace_id or "")
        if ws is None:
            ws = Workspace(secrets.token_urlsafe(32))
            self._all[ws.id] = ws
        ws.last_used = time.time()
        return ws

    def prune(self) -> None:
        cutoff = time.time() - config.WORKSPACE_IDLE_MINUTES * 60
        for wid in [wid for wid, ws in self._all.items() if ws.last_used < cutoff]:
            del self._all[wid]

    def total_size(self) -> int:
        return sum(ws.size for ws in self._all.values())

    def _check_room(self, ws: Workspace, extra: int) -> None:
        if ws.size + extra > config.MAX_WORKSPACE_MB * MB:
            raise LimitError(f"Your workspace is full ({config.MAX_WORKSPACE_MB} MB). "
                             "Remove some documents and try again.")
        if self.total_size() + extra > config.MAX_TOTAL_MB * MB:
            raise LimitError("The server is busy right now. Please try again in a few minutes.")

    # ─────────────── documents ───────────────
    def add_document(self, ws: Workspace, name: str, pdf: bytes, pages: int) -> Document:
        self._check_room(ws, len(pdf))
        doc = Document(secrets.token_urlsafe(12), _unique_name(name, {d.name for d in ws.documents.values()}),
                       pdf, pages)
        ws.documents[doc.id] = doc
        return doc

    def add_redaction(self, ws: Workspace, doc_id: str, pdf: bytes, method: str,
                      categories: list[str], counts: dict[str, int]) -> Redaction | None:
        """None if the document was removed while it was being redacted."""
        doc = ws.documents.get(doc_id)
        if doc is None:
            return None
        self._check_room(ws, len(pdf))
        stem = str(PurePosixPath(doc.name).with_suffix("")) + "-redacted"
        name = f"{stem}.pdf" if doc.next_version == 1 else f"{stem}-{doc.next_version}.pdf"
        doc.next_version += 1
        redaction = Redaction(secrets.token_urlsafe(12), name, pdf, method, categories, counts)
        doc.redactions.insert(0, redaction)
        return redaction


def _unique_name(name: str, taken: set[str]) -> str:
    stem, n = name[:-len(".pdf")], 1
    while name in taken:
        n += 1
        name = f"{stem}-{n}.pdf"
    return name
