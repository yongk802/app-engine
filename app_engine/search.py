"""App category vocabulary and catalog search.

Every app declares a ``description`` and one or more ``categories`` drawn from
:data:`CATEGORIES` (primary category first). :func:`search_apps` ranks app
listings by how well a free-text query matches their label, id, categories,
description, and author, so every host (the standalone launcher, Atrium) finds
and orders apps the same way.

Dependency-free and pure: callers pass plain listing mappings (the same dicts
the hosts already serialize), never filesystem paths.
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class AppCategory:
    """One entry in the shared category vocabulary."""

    id: str
    label: str
    description: str


CATEGORIES: tuple[AppCategory, ...] = (
    AppCategory("games", "Games", "Arcade, action, sports, and card games, plus simulations and toys."),
    AppCategory("learning", "Learning", "Courses, interactive lessons, tutors, practice drills, and training simulators."),
    AppCategory("ai", "AI & Models", "Language models, machine learning, and AI-powered assistants."),
    AppCategory("developer", "Developer", "Programming, algorithms, editors, and local development or model stacks."),
    AppCategory("security", "Security", "Cybersecurity practice, capture-the-flag challenges, and hardening."),
    AppCategory("science", "Science & Math", "Mathematics, physics, and quantum computing."),
    AppCategory("creative", "Creative & Media", "Video, film, music, audio, images, and design."),
    AppCategory("productivity", "Productivity", "Calendars, notes, memory, email, and dictation."),
    AppCategory("business", "Business & Marketing", "Marketing, partners, revenue, websites, and business content."),
    AppCategory("news", "News & Info", "News digests, feeds, and weather."),
    AppCategory("home", "Home & Life", "Cooking, housing, gifts, and everyday household decisions."),
    AppCategory("wellness", "Wellness", "Exercise, stretching, mindfulness, and reflection."),
    AppCategory("system", "System", "Host administration: containers, builds, policies, cameras, and remote browsing."),
)

CATEGORY_IDS = frozenset(category.id for category in CATEGORIES)
_BY_ID = {category.id: category for category in CATEGORIES}
_BY_LABEL = {category.label.casefold(): category.id for category in CATEGORIES}

# ``category:games`` / ``cat:games`` inside a free-text query filters instead
# of matching text, so a single search box can do both.
_FILTER_TOKEN = re.compile(r"^(?:category|cat):(.+)$")
_WORD = re.compile(r"[a-z0-9]+")


def category_id(value: str) -> str:
    """Resolve a category id or display label to its id (lowercased)."""
    text = str(value or "").strip().casefold()
    return _BY_LABEL.get(text, text)


def category_label(value: str) -> str:
    """Display label for a category id; unknown ids are title-cased."""
    known = _BY_ID.get(value)
    return known.label if known else value.replace("-", " ").replace("_", " ").title()


def parse_query(query: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split a query into lowercase text terms and ``category:`` filters."""
    terms: list[str] = []
    categories: list[str] = []
    for token in str(query or "").casefold().split():
        match = _FILTER_TOKEN.match(token)
        if match:
            categories.append(category_id(match.group(1)))
        else:
            terms.append(token)
    return tuple(terms), tuple(categories)


def _app_categories(app: Mapping) -> list[str]:
    raw = app.get("categories") or ()
    if not isinstance(raw, (list, tuple)):
        return []
    return [c.strip().casefold() for c in raw if isinstance(c, str) and c.strip()]


def _text(app: Mapping, key: str) -> str:
    value = app.get(key, "")
    return value.casefold() if isinstance(value, str) else ""


def _starts_word(term: str, text: str) -> bool:
    return any(word.startswith(term) for word in _WORD.findall(text))


def _term_score(term: str, app: Mapping, categories: list[str]) -> int:
    """Best single-field match for one term; 0 when it matches nowhere."""
    label = _text(app, "label")
    app_id = _text(app, "id")
    description = _text(app, "description")
    category_labels = [category_label(c).casefold() for c in categories]
    if label == term:
        return 100
    if label.startswith(term):
        return 80
    if _starts_word(term, label):
        return 60
    if app_id.startswith(term):
        return 50
    if term in categories or term in category_labels:
        return 40
    if term in label:
        return 30
    if any(c.startswith(term) for c in categories) or any(_starts_word(term, c) for c in category_labels):
        return 25
    if _starts_word(term, description):
        return 20
    # Mid-word matches only for longer terms, so "ai" doesn't hit "email".
    if len(term) >= 4 and (term in description or term in app_id):
        return 10
    if _starts_word(term, _text(app, "author")):
        return 5
    return 0


def search_apps(
    apps: Iterable[Mapping],
    query: str = "",
    categories: Iterable[str] = (),
) -> list[Mapping]:
    """Filter and rank app listings.

    Every text term must match somewhere (AND); apps must carry at least one of
    the requested categories (OR), whether passed explicitly or as
    ``category:`` tokens in ``query``. With no text terms, the input order is
    preserved, so hosts keep their own default ordering.
    """
    terms, query_categories = parse_query(query)
    wanted = {category_id(c) for c in (*categories, *query_categories) if str(c or "").strip()}
    ranked: list[tuple[int, int, Mapping]] = []
    for index, app in enumerate(apps):
        app_categories = _app_categories(app)
        if wanted and wanted.isdisjoint(app_categories):
            continue
        score = 0
        for term in terms:
            term_score = _term_score(term, app, app_categories)
            if not term_score:
                break
            score += term_score
        else:
            ranked.append((-score, index, app))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [app for _, _, app in ranked]


def category_counts(apps: Iterable[Mapping]) -> list[dict[str, object]]:
    """The vocabulary with per-category app counts, for filter controls.

    Categories outside the vocabulary that apps still declare are appended so
    a host's own tags stay filterable.
    """
    counts: Counter[str] = Counter()
    for app in apps:
        counts.update(set(_app_categories(app)))
    listing: list[dict[str, object]] = [
        {"id": c.id, "label": c.label, "description": c.description, "count": counts[c.id]}
        for c in CATEGORIES
    ]
    listing.extend(
        {"id": extra, "label": category_label(extra), "description": "", "count": counts[extra]}
        for extra in sorted(set(counts) - CATEGORY_IDS)
    )
    return listing


__all__ = [
    "AppCategory",
    "CATEGORIES",
    "CATEGORY_IDS",
    "category_counts",
    "category_id",
    "category_label",
    "parse_query",
    "search_apps",
]
