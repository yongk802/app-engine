from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


class InvalidKnowledgePackError(ValueError):
    pass


ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}


@dataclass(frozen=True)
class MisconceptionRule:
    pattern: str
    correction: str


@dataclass(frozen=True)
class KnowledgeEntry:
    id: str
    app_id: str
    title: str
    keywords: tuple[str, ...]
    fact: str
    source_note: str
    misconceptions: tuple[MisconceptionRule, ...]


@dataclass(frozen=True)
class Misconception:
    entry_id: str
    correction: str
    matched_text: str


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9][a-z0-9+.-]*", text.casefold()))


class KnowledgeBase:
    def __init__(self, entries: tuple[KnowledgeEntry, ...]):
        self.entries = entries

    @classmethod
    def load(cls, path: Path) -> "KnowledgeBase":
        try:
            raw = json.loads(Path(path).read_text())
            entries = tuple(KnowledgeEntry(
                id=item["id"], app_id=item["app_id"], title=item["title"],
                keywords=tuple(item["keywords"]), fact=item["fact"],
                source_note=item["source_note"],
                misconceptions=tuple(MisconceptionRule(rule["pattern"], rule["correction"])
                                     for rule in item.get("misconceptions", [])),
            ) for item in raw["entries"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise InvalidKnowledgePackError(str(exc)) from exc
        if not entries or len({entry.id for entry in entries}) != len(entries):
            raise InvalidKnowledgePackError("knowledge entries must be non-empty with unique IDs")
        for entry in entries:
            if not all((entry.id, entry.app_id, entry.title, entry.keywords, entry.fact, entry.source_note)):
                raise InvalidKnowledgePackError(f"incomplete knowledge entry: {entry.id}")
            for rule in entry.misconceptions:
                try:
                    re.compile(rule.pattern)
                except re.error as exc:
                    raise InvalidKnowledgePackError(f"invalid pattern in {entry.id}: {exc}") from exc
        return cls(entries)

    def retrieve(self, app_id: str, question: str, limit: int = 4) -> tuple[KnowledgeEntry, ...]:
        query = _tokens(question)
        ranked = []
        for entry in self.entries:
            if entry.app_id != app_id:
                continue
            terms = _tokens(" ".join(entry.keywords))
            score = len(query & terms)
            if score:
                ranked.append((score, entry.id, entry))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return tuple(item[2] for item in ranked[:max(0, limit)])


def build_grounding_context(entries: tuple[KnowledgeEntry, ...]) -> str:
    if not entries:
        return ""
    lines = ["REVIEWED LOCAL REFERENCE — treat these facts as authoritative:"]
    for entry in entries:
        lines.append(f"- {entry.title}: {entry.fact} (Source note: {entry.source_note})")
    lines.append("If the reference does not answer the question, say so instead of guessing.")
    return "\n".join(lines)


def detect_misconceptions(answer: str, entries: tuple[KnowledgeEntry, ...]) -> tuple[Misconception, ...]:
    found = []
    for entry in entries:
        for rule in entry.misconceptions:
            match = re.search(rule.pattern, answer, flags=re.IGNORECASE)
            if match:
                found.append(Misconception(entry.id, rule.correction, match.group(0)))
    return tuple(found)


def extract_visible_answer(chunks: list[str]) -> list[str]:
    combined = "".join(chunks).strip()
    try:
        parsed = json.loads(combined)
    except (json.JSONDecodeError, TypeError):
        return chunks
    answer = parsed.get("answer") if isinstance(parsed, dict) else None
    return [answer.strip()] if isinstance(answer, str) and answer.strip() else chunks
