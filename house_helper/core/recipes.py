"""The recipe protocol: delimited JSON blocks in one assistant answer, and their plain text.

An answer may carry zero or more recipes. Each recipe is one complete block:

    <<<RECIPE>>>
    {"title": "Борщ", "servings": "4 порции",
     "ingredients": [{"name": "Свекла", "amount": "2 шт."}],
     "steps": ["Подготовить ингредиенты."]}
    <<<END_RECIPE>>>

The sentinels and the JSON are for a machine only. Everything around them is ordinary
conversation and stays in its place. A block is recognised by its sentinels alone: recipes
are never guessed from prose, however much a paragraph looks like one. The JSON is strict --
one object with exactly these four keys, no duplicate keys, no NaN or Infinity, and no
control characters in the text fields. Ingredients and steps are ordered lists and keep
their order. At most MAX_RECIPES_PER_REPLY recipes are accepted from one answer; further
blocks are reported, not parsed, and a malformed or incomplete block never becomes a
Recipe.

Two fields are prose rather than numbers. `servings` is a short human phrase -- "4 порции",
or a range such as "2–3 порции" -- and may be null when the model does not know it; nothing
here invents a number for it. An ingredient's `amount` is likewise free text, so household
measures ("2 ст. л.") and qualitative quantities ("по вкусу") are ordinary values. Both are
still bounded, single-line, and free of control characters and protocol markers.

What a person is shown is plain Russian text: the conversation with each valid block
replaced by a readable recipe, in the order in which the blocks stood. An invalid block is
replaced by a short notice that says it could not be read -- the raw JSON is never shown,
and no notice ever claims that a recipe was parsed. A marker without its partner is
protocol noise and is removed. A truncated block (its opening sentinel is the last thing
in the answer) is reported as truncated and is not rendered. An answer with no blocks is
returned unchanged, apart from the product's spelling rule.

Spelling: the model's answer is spelled by house_helper.core.orthography.replace_yo, as
every answer is when it is produced, before anything is stored. Rendering never spells
again: a recipe that a person edits later is rendered exactly as it was entered. That is
also why parse_reply is the only place that touches the text of the model.

Identity: Recipe.index is the zero-based position of the recipe among the accepted recipes
of its answer. Recipe.recipe_id is a hash of the protocol version, the index and the
validated fields, so parsing the same answer twice produces the same identities; nothing
depends on time or on a random source. It is a content fingerprint, not a name: editing any
field produces a different one. A stored recipe is identified by where it came from --
(message_id, position) -- and recipe_id is never a database key.

The module is pure: standard library and the spelling rule only. It knows nothing about
OpenAI, the database, the web application or Telegram, and makes no outside calls.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias

from house_helper.core.orthography import replace_yo

RECIPE_START = "<<<RECIPE>>>"
RECIPE_END = "<<<END_RECIPE>>>"

# A version of this wire format, mixed into every recipe identity. A future incompatible
# format must change it, so that identities made under the two formats cannot be confused.
PROTOCOL_VERSION = 1

# Per-answer and per-field limits. They bound the work a single answer can cause and keep
# one recipe readable: the shared prompt asks for no more than five suggestions.
MAX_RECIPES_PER_REPLY = 5
MAX_RECIPE_BLOCK_LENGTH = 20_000
MAX_TITLE_LENGTH = 200
MAX_SERVINGS_LENGTH = 100
MAX_INGREDIENTS = 100
MAX_INGREDIENT_NAME_LENGTH = 200
MAX_AMOUNT_LENGTH = 100
MAX_STEPS = 50
MAX_STEP_LENGTH = 1000

# Shown in place of a block that could not be trusted. A notice is deliberately not a claim
# that the recipe worked: the only claim made here is that it did not.
INVALID_RECIPE_NOTICE = "Рецепт не удалось разобрать, он не показан."
TRUNCATED_RECIPE_NOTICE = "Рецепт оборван, он не показан."
TOO_MANY_RECIPES_NOTICE = (
    "В одном ответе больше рецептов, чем можно показать, лишние не показаны."
)

_EXPECTED_RECIPE_KEYS = frozenset({"title", "servings", "ingredients", "steps"})
_EXPECTED_INGREDIENT_KEYS = frozenset({"name", "amount"})

# Control characters are never part of a recipe field: they are not prose, and a title or a
# step is one line of text. The C1 range is included because Python strings may hold it.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class RecipeIssueKind(str, Enum):
    """Why part of an answer was not accepted as recipes. The values are stable for callers."""

    INVALID_JSON = "invalid_json"
    INVALID_SCHEMA = "invalid_schema"
    TRUNCATED = "truncated"
    TOO_MANY = "too_many"
    STRAY_END = "stray_end"


@dataclass(frozen=True)
class Ingredient:
    """One line of the ingredient list, in the order the model gave it.

    `amount` is what the model wrote, so it may be a household measure ("2 ст. л.") or a
    qualitative quantity ("по вкусу").
    """

    name: str
    amount: str


@dataclass(frozen=True)
class Recipe:
    """A validated recipe from one answer.

    `index` is zero-based and counts only accepted recipes, in document order. `servings`
    is the phrase the model gave, or None when it named none. `recipe_id` is derived from
    the content, so it changes whenever the content does; it is not a persistent key.
    """

    index: int
    title: str
    servings: str | None
    ingredients: tuple[Ingredient, ...]
    steps: tuple[str, ...]

    @property
    def recipe_id(self) -> str:
        canonical = json.dumps(
            {
                "protocol": PROTOCOL_VERSION,
                "index": self.index,
                "title": self.title,
                "servings": self.servings,
                "ingredients": [
                    {"name": ingredient.name, "amount": ingredient.amount}
                    for ingredient in self.ingredients
                ],
                "steps": list(self.steps),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"recipe-{digest[:16]}"


@dataclass(frozen=True)
class Prose:
    """Ordinary conversation, exactly as it stood apart from its spelling."""

    text: str


@dataclass(frozen=True)
class InvalidRecipe:
    """A block that was not accepted. It keeps the reason, never the raw content."""

    kind: RecipeIssueKind


Segment: TypeAlias = Prose | Recipe | InvalidRecipe


@dataclass(frozen=True)
class ParsedReply:
    """One answer cut into ordered segments, plus its valid recipes and its problems.

    `segments` is what rendering walks: prose, accepted recipes and rejected blocks, in
    the order in which they stood in the answer. `recipes` is the same accepted recipes
    in that order; `issues` names every rejected block and every stray marker.
    """

    segments: tuple[Segment, ...]
    recipes: tuple[Recipe, ...]
    issues: tuple[RecipeIssueKind, ...]

    @property
    def has_recipes(self) -> bool:
        return bool(self.recipes)


class _InvalidJson(ValueError):
    pass


class _InvalidSchema(ValueError):
    pass


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidJson(f"duplicate key {key!r}")
        result[key] = value
    return result


def _no_constants(name: str) -> None:
    # NaN and Infinity are Python's json extensions, not JSON. They are never accepted.
    raise _InvalidJson(f"non-standard constant {name}")


def _text(value: object, *, max_length: int, field: str) -> str:
    if not isinstance(value, str):
        raise _InvalidSchema(f"{field} must be a string")
    value = value.strip()
    if not value:
        raise _InvalidSchema(f"{field} must not be empty")
    if len(value) > max_length:
        raise _InvalidSchema(f"{field} is too long")
    if _CONTROL_CHARACTERS.search(value):
        raise _InvalidSchema(f"{field} contains a control character")
    if RECIPE_START in value or RECIPE_END in value:
        raise _InvalidSchema(f"{field} contains a protocol marker")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _InvalidSchema(f"{field} is not valid Unicode text") from error
    return value


def _ingredient(value: object) -> Ingredient:
    if not isinstance(value, dict) or set(value) != _EXPECTED_INGREDIENT_KEYS:
        raise _InvalidSchema("an ingredient must have exactly name and amount")
    name = _text(value["name"], max_length=MAX_INGREDIENT_NAME_LENGTH, field="name")
    amount = _text(value["amount"], max_length=MAX_AMOUNT_LENGTH, field="amount")
    return Ingredient(name=replace_yo(name), amount=replace_yo(amount))


def _servings(value: object) -> str | None:
    """The servings phrase, or None when the model did not know it.

    The protocol carries what a person will read, so a JSON number is not accepted: only a
    phrase such as "4 порции" or "2–3 порции". Nothing derives a missing value.
    """
    if value is None:
        return None
    return _text(value, max_length=MAX_SERVINGS_LENGTH, field="servings")


def _recipe_from_json(value: object, index: int) -> Recipe:
    if not isinstance(value, dict) or set(value) != _EXPECTED_RECIPE_KEYS:
        raise _InvalidSchema(
            "a recipe must have exactly title, servings, ingredients and steps"
        )

    title = _text(value["title"], max_length=MAX_TITLE_LENGTH, field="title")
    servings = _servings(value["servings"])

    raw_ingredients = value["ingredients"]
    if not isinstance(raw_ingredients, list) or not 1 <= len(raw_ingredients) <= MAX_INGREDIENTS:
        raise _InvalidSchema("ingredients must be a non-empty list within the limit")

    raw_steps = value["steps"]
    if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= MAX_STEPS:
        raise _InvalidSchema("steps must be a non-empty list within the limit")

    return Recipe(
        index=index,
        title=replace_yo(title),
        servings=None if servings is None else replace_yo(servings),
        ingredients=tuple(_ingredient(item) for item in raw_ingredients),
        steps=tuple(
            replace_yo(_text(step, max_length=MAX_STEP_LENGTH, field="step"))
            for step in raw_steps
        ),
    )


def _find_block_end(text: str, start: int) -> int:
    """The first end sentinel after `start` that is not inside a JSON string literal.

    A sentinel may stand in a value -- a title, for example -- and it must not close the
    block there. Such a value is rejected by _text afterwards; the framing delimiter is the
    one outside a string. A raw newline ends a string for recovery: JSON strings cannot
    contain one, so a line of sentinels is always outside a malformed string too.
    """
    in_string = False
    escaped = False
    index = start
    while index < len(text):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"' or character == "\n":
                in_string = False
        elif character == '"':
            in_string = True
        elif text.startswith(RECIPE_END, index):
            return index
        index += 1
    return -1


def _decode_block(raw: str, index: int) -> tuple[Recipe | None, RecipeIssueKind | None]:
    """The recipe in `raw`, or None and the reason it could not be built."""
    if len(raw) > MAX_RECIPE_BLOCK_LENGTH:
        return None, RecipeIssueKind.INVALID_SCHEMA
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_no_constants,
        )
    except (_InvalidJson, json.JSONDecodeError, RecursionError):
        return None, RecipeIssueKind.INVALID_JSON
    try:
        return _recipe_from_json(value, index), None
    except _InvalidSchema:
        return None, RecipeIssueKind.INVALID_SCHEMA


def parse_reply(text: str) -> ParsedReply:
    """Cut one assistant answer into ordered segments and validate every complete block.

    Unpaired markers are removed from the prose and reported. A block whose opening
    sentinel has no closing sentinel is treated as truncated: everything from the opening
    sentinel to the end of the answer is protocol content that cannot be trusted, so it is
    replaced by InvalidRecipe and never rendered. Text before it is kept.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")

    segments: list[Segment] = []
    recipes: list[Recipe] = []
    issues: list[RecipeIssueKind] = []
    prose_parts: list[str] = []

    def flush_prose() -> None:
        if not prose_parts:
            return
        prose = "".join(prose_parts)
        prose_parts.clear()
        if prose:
            segments.append(Prose(replace_yo(prose)))

    pos = 0
    while True:
        start_at = text.find(RECIPE_START, pos)
        end_at = text.find(RECIPE_END, pos)

        if end_at != -1 and (start_at == -1 or end_at < start_at):
            prose_parts.append(text[pos:end_at])
            issues.append(RecipeIssueKind.STRAY_END)
            pos = end_at + len(RECIPE_END)
            continue

        if start_at == -1:
            prose_parts.append(text[pos:])
            break

        prose_parts.append(text[pos:start_at])
        flush_prose()

        close_at = _find_block_end(text, start_at + len(RECIPE_START))
        if close_at == -1:
            issues.append(RecipeIssueKind.TRUNCATED)
            segments.append(InvalidRecipe(RecipeIssueKind.TRUNCATED))
            pos = len(text)
            break

        raw = text[start_at + len(RECIPE_START) : close_at]
        if len(recipes) >= MAX_RECIPES_PER_REPLY:
            kind = RecipeIssueKind.TOO_MANY
            segment: Segment = InvalidRecipe(kind)
        else:
            recipe, failure = _decode_block(raw, len(recipes))
            if recipe is not None:
                recipes.append(recipe)
                segments.append(recipe)
                pos = close_at + len(RECIPE_END)
                continue
            if failure is None:
                raise RuntimeError("a rejected block must name its reason")
            kind = failure
            segment = InvalidRecipe(kind)

        issues.append(kind)
        segments.append(segment)
        pos = close_at + len(RECIPE_END)

    flush_prose()
    return ParsedReply(tuple(segments), tuple(recipes), tuple(issues))


_NOTICES = {
    RecipeIssueKind.INVALID_JSON: INVALID_RECIPE_NOTICE,
    RecipeIssueKind.INVALID_SCHEMA: INVALID_RECIPE_NOTICE,
    RecipeIssueKind.TRUNCATED: TRUNCATED_RECIPE_NOTICE,
    RecipeIssueKind.TOO_MANY: TOO_MANY_RECIPES_NOTICE,
}


def render_recipe(recipe: Recipe) -> str:
    """One recipe as plain Russian text, no protocol markers and no JSON.

    Nothing here rewrites the fields: an edited recipe is shown as it was edited. The
    servings phrase stands on its own line, so "2–3 порции" does not become "Порции: 2–3
    порции"; an unknown number of servings is left out rather than guessed.
    """
    lines = [recipe.title]
    if recipe.servings is not None:
        lines.append(recipe.servings)
    lines.extend(["", "Ингредиенты:"])
    lines.extend(f"- {item.name} — {item.amount}" for item in recipe.ingredients)
    lines.extend(["", "Приготовление:"])
    lines.extend(
        f"{number}. {step}" for number, step in enumerate(recipe.steps, start=1)
    )
    return "\n".join(lines)


def render_parsed_reply(parsed: ParsedReply) -> str:
    """The answer as plain text: prose and recipes in their original order."""
    out = ""
    after_block = False

    for segment in parsed.segments:
        if isinstance(segment, Prose):
            text = segment.text
            if after_block:
                text = text.lstrip("\n")
                if not text:
                    continue
                out += "\n\n" + text
                after_block = False
            else:
                out += text
            continue

        block = (
            render_recipe(segment)
            if isinstance(segment, Recipe)
            else _NOTICES[segment.kind]
        )
        if out:
            out = out.rstrip("\n")
            if out:
                out += "\n\n"
        out += block
        after_block = True

    return out


def render_reply(text: str) -> str:
    """Parse one assistant answer and return it as plain text for a person."""
    return render_parsed_reply(parse_reply(text))


__all__ = [
    "INVALID_RECIPE_NOTICE",
    "MAX_RECIPES_PER_REPLY",
    "PROTOCOL_VERSION",
    "RECIPE_END",
    "RECIPE_START",
    "TOO_MANY_RECIPES_NOTICE",
    "TRUNCATED_RECIPE_NOTICE",
    "Ingredient",
    "InvalidRecipe",
    "ParsedReply",
    "Prose",
    "Recipe",
    "RecipeIssueKind",
    "Segment",
    "parse_reply",
    "render_parsed_reply",
    "render_recipe",
    "render_reply",
]
