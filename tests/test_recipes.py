"""The KH-F06A recipe protocol: sentinel blocks, strict JSON, limits and plain-text rendering.

Every fixture here is an assistant answer, never a user message: the protocol module is
applied only to what the model writes. The tests are grouped by what a caller can observe:
which recipes were accepted (and with which identity), what the person is shown instead,
and which family of text is kept exactly as it stood.
"""

import ast
import json
import re
from pathlib import Path

import pytest

import house_helper.core.recipes as recipes
from house_helper.core.recipes import (
    INVALID_RECIPE_NOTICE,
    MAX_RECIPE_BLOCK_LENGTH,
    MAX_RECIPES_PER_REPLY,
    RECIPE_END,
    RECIPE_START,
    TOO_MANY_RECIPES_NOTICE,
    TRUNCATED_RECIPE_NOTICE,
    Ingredient,
    InvalidRecipe,
    Prose,
    Recipe,
    RecipeIssueKind,
    parse_reply,
    render_recipe,
    render_reply,
)

TITLE = "Омлет с сыром"


def payload(**overrides):
    value = {
        "title": TITLE,
        "servings": "2 порции",
        "ingredients": [
            {"name": "яйца", "amount": "3 шт."},
            {"name": "сыр", "amount": "50 г"},
        ],
        "steps": ["Взбейте яйца.", "Обжарьте 3 минуты."],
    }
    value.update(overrides)
    return value


def block(value=None, *, raw=None):
    if raw is None:
        raw = json.dumps(payload() if value is None else value, ensure_ascii=False)
    return f"{RECIPE_START}\n{raw}\n{RECIPE_END}"


def assert_no_protocol_noise(text):
    assert RECIPE_START not in text
    assert RECIPE_END not in text
    assert '"title"' not in text
    assert '"ingredients"' not in text
    assert '"steps"' not in text


# --- ordinary answers --------------------------------------------------------------------------------


def test_an_answer_without_blocks_is_returned_unchanged():
    reply = "Просто ответ без рецепта.\n\nИ еще одна строка."

    parsed = parse_reply(reply)

    assert parsed.recipes == ()
    assert parsed.issues == ()
    assert parsed.has_recipes is False
    assert parsed.segments == (Prose(reply),)
    assert render_reply(reply) == reply


def test_an_empty_answer_is_an_empty_reply():
    parsed = parse_reply("")

    assert parsed.segments == ()
    assert parsed.recipes == ()
    assert parsed.issues == ()
    assert render_reply("") == ""


def test_whitespace_only_output_is_kept_as_it_is():
    assert parse_reply("  \n\n").segments == (Prose("  \n\n"),)
    assert render_reply("  \n\n") == "  \n\n"


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "\n",
        "Вот рецепт без разметки: омлет, 3 яйца, обжарить 3 минуты.",
        "Порции: 2\n- яйца — 3 шт.\n1. Взбейте.",
        f"Упоминание маркера {RECIPE_START} без блока не должно выглядеть как рецепт.",
    ],
)
def test_recipes_are_not_guessed_from_prose_or_from_a_lone_opening_marker(reply):
    parsed = parse_reply(reply)

    assert parsed.recipes == ()
    # A lone opening marker is protocol noise, but the words around it are not.
    if RECIPE_START in reply:
        assert parsed.issues == (RecipeIssueKind.TRUNCATED,)
        assert RECIPE_START not in render_reply(reply)
    else:
        assert parsed.issues == ()


# --- one recipe --------------------------------------------------------------------------------------


def test_one_complete_block_becomes_one_ordered_recipe():
    reply = f"Вот простой завтрак.\n{block()}\nПриятного аппетита!"

    parsed = parse_reply(reply)

    assert parsed.issues == ()
    assert parsed.has_recipes is True
    (recipe,) = parsed.recipes
    assert recipe.index == 0
    assert recipe.title == TITLE
    assert recipe.servings == "2 порции"
    assert recipe.ingredients == (
        Ingredient(name="яйца", amount="3 шт."),
        Ingredient(name="сыр", amount="50 г"),
    )
    assert recipe.steps == ("Взбейте яйца.", "Обжарьте 3 минуты.")
    assert recipe == parsed.segments[1]
    assert isinstance(parsed.segments[0], Prose)
    assert isinstance(parsed.segments[2], Prose)


def test_the_plain_text_rendering_is_russian_and_keeps_conversation_and_recipe_in_order():
    reply = f"Вот простой завтрак.\n{block()}\nПриятного аппетита!"

    rendered = render_reply(reply)

    assert rendered == (
        "Вот простой завтрак.\n"
        "\n"
        f"{TITLE}\n"
        "2 порции\n"
        "\n"
        "Ингредиенты:\n"
        "- яйца — 3 шт.\n"
        "- сыр — 50 г\n"
        "\n"
        "Приготовление:\n"
        "1. Взбейте яйца.\n"
        "2. Обжарьте 3 минуты.\n"
        "\n"
        "Приятного аппетита!"
    )
    assert_no_protocol_noise(rendered)
    assert rendered.index("Вот простой завтрак.") < rendered.index(TITLE)
    assert rendered.index(TITLE) < rendered.index("Приятного аппетита!")


def test_an_inline_block_is_recognised_and_separated_from_the_prose():
    reply = f"До{block()}После."

    parsed = parse_reply(reply)
    rendered = render_reply(reply)

    (recipe,) = parsed.recipes
    assert recipe.title == TITLE
    assert rendered.startswith("До\n\n")
    assert rendered.endswith("\n\nПосле.")
    assert_no_protocol_noise(rendered)


def test_surrounding_whitespace_around_the_json_is_not_a_field():
    reply = f"Начало.\n{RECIPE_START}\n\n  {json.dumps(payload())}  \n\n{RECIPE_END}\nКонец."

    (recipe,) = parse_reply(reply).recipes

    assert recipe.title == TITLE


# --- the canonical recipe schema ---------------------------------------------------------------------


def test_servings_is_a_phrase_and_a_range_is_ordinary_text():
    value = payload(servings="2–3 порции")

    (recipe,) = parse_reply(block(value)).recipes

    assert recipe.servings == "2–3 порции"
    assert "2–3 порции" in render_recipe(recipe)


def test_null_servings_are_accepted_and_nothing_is_rendered_in_its_place():
    value = payload(servings=None)

    (recipe,) = parse_reply(block(value)).recipes

    assert recipe.servings is None
    assert render_recipe(recipe) == (
        f"{TITLE}\n"
        "\n"
        "Ингредиенты:\n"
        "- яйца — 3 шт.\n"
        "- сыр — 50 г\n"
        "\n"
        "Приготовление:\n"
        "1. Взбейте яйца.\n"
        "2. Обжарьте 3 минуты."
    )


def test_an_ingredient_amount_may_be_a_household_measure_or_a_qualitative_quantity():
    value = payload(
        ingredients=[
            {"name": "свекла", "amount": "2 шт."},
            {"name": "масло", "amount": "2 ст. л."},
            {"name": "сметана", "amount": "по вкусу"},
        ]
    )

    (recipe,) = parse_reply(block(value)).recipes

    assert [ingredient.amount for ingredient in recipe.ingredients] == [
        "2 шт.",
        "2 ст. л.",
        "по вкусу",
    ]
    rendered = render_recipe(recipe)
    assert "- масло — 2 ст. л." in rendered
    assert "- сметана — по вкусу" in rendered


# --- several recipes ---------------------------------------------------------------------------------


def test_several_recipes_keep_their_order_and_deterministic_indices():
    first = payload(title="Первый рецепт")
    second = payload(title="Второй рецепт")
    reply = f"Начало.\n{block(first)}\nМежду ними.\n{block(second)}\nКонец."

    parsed = parse_reply(reply)

    assert parsed.issues == ()
    assert [(recipe.index, recipe.title) for recipe in parsed.recipes] == [
        (0, "Первый рецепт"),
        (1, "Второй рецепт"),
    ]
    rendered = render_reply(reply)
    assert rendered.index("Начало.") < rendered.index("Первый рецепт")
    assert rendered.index("Первый рецепт") < rendered.index("Между ними.")
    assert rendered.index("Между ними.") < rendered.index("Второй рецепт")
    assert rendered.index("Второй рецепт") < rendered.index("Конец.")
    assert_no_protocol_noise(rendered)


def test_identical_blocks_in_one_answer_are_separate_occurrences():
    reply = f"{block()}\n{block()}"

    first, second = parse_reply(reply).recipes

    assert first.title == second.title
    assert first.index == 0 and second.index == 1
    assert first.recipe_id != second.recipe_id


def test_a_bad_block_does_not_consume_the_next_recipe_index():
    reply = f"До.\n{block(raw='{broken')}\nСередина.\n{block()}\nПосле."

    parsed = parse_reply(reply)

    assert parsed.issues == (RecipeIssueKind.INVALID_JSON,)
    (recipe,) = parsed.recipes
    assert recipe.index == 0
    rendered = render_reply(reply)
    assert INVALID_RECIPE_NOTICE in rendered
    assert "Середина." in rendered and "После." in rendered
    assert "{" not in rendered and "broken" not in rendered
    assert_no_protocol_noise(rendered)


def test_five_independent_recipes_are_all_accepted_with_zero_based_indices():
    values = [
        payload(
            title=f"Рецепт {number}",
            servings=f"{number} порции",
            ingredients=[{"name": f"продукт {number}", "amount": "по вкусу"}],
            steps=[f"Шаг {number}."],
        )
        for number in range(1, 6)
    ]
    reply = "\n".join(block(value) for value in values)

    parsed = parse_reply(reply)

    assert parsed.issues == ()
    assert [recipe.index for recipe in parsed.recipes] == [0, 1, 2, 3, 4]
    assert [recipe.title for recipe in parsed.recipes] == [
        "Рецепт 1",
        "Рецепт 2",
        "Рецепт 3",
        "Рецепт 4",
        "Рецепт 5",
    ]
    assert len({recipe.recipe_id for recipe in parsed.recipes}) == 5
    rendered = render_reply(reply)
    assert all(f"Рецепт {number}" in rendered for number in range(1, 6))
    assert TOO_MANY_RECIPES_NOTICE not in rendered
    assert_no_protocol_noise(rendered)


def test_the_per_reply_limit_keeps_the_first_five_and_reports_the_rest():
    assert MAX_RECIPES_PER_REPLY == 5
    blocks = "\n".join(block(payload(title=f"Рецепт {number}")) for number in range(1, 8))
    reply = f"Начало.\n{blocks}\nКонец."

    parsed = parse_reply(reply)

    assert [recipe.index for recipe in parsed.recipes] == [0, 1, 2, 3, 4]
    assert [recipe.title for recipe in parsed.recipes] == [
        f"Рецепт {number}" for number in range(1, 6)
    ]
    assert parsed.issues == (RecipeIssueKind.TOO_MANY,) * 2
    rendered = render_reply(reply)
    assert TOO_MANY_RECIPES_NOTICE in rendered
    assert "Рецепт 6" not in rendered and "Рецепт 7" not in rendered
    assert rendered.index("Рецепт 5") < rendered.index(TOO_MANY_RECIPES_NOTICE)
    assert rendered.endswith("Конец.")
    assert_no_protocol_noise(rendered)


# --- malformed blocks --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "{",
        "not json",
        '{"title": "x", "servings": 2, "ingredients": [], "steps": [],}',
        '{"title": "x", "servings": 2, "ingredients": [], "steps": []} trailing',
        '{"title": "x", "servings": NaN, "ingredients": [{"name": "a", "quantity": "b"}], '
        '"steps": ["c"]}',
        '{"title": "x", "servings": Infinity, "ingredients": [{"name": "a", '
        '"quantity": "b"}], "steps": ["c"]}',
        '{"title": "first", "title": "second", "servings": 2, '
        '"ingredients": [{"name": "a", "quantity": "b"}], "steps": ["c"]}',
    ],
)
def test_invalid_json_is_reported_and_never_shown_as_a_recipe(raw):
    reply = f"До.\n{block(raw=raw)}\nПосле."

    parsed = parse_reply(reply)

    assert parsed.recipes == ()
    assert parsed.has_recipes is False
    assert parsed.issues == (RecipeIssueKind.INVALID_JSON,)
    assert isinstance(parsed.segments[1], InvalidRecipe)
    assert parsed.segments[1].kind is RecipeIssueKind.INVALID_JSON
    rendered = render_reply(reply)
    assert INVALID_RECIPE_NOTICE in rendered
    assert "До." in rendered and "После." in rendered
    assert_no_protocol_noise(rendered)


@pytest.mark.parametrize(
    "value",
    [
        {
            "servings": "2 порции",
            "ingredients": [{"name": "a", "amount": "b"}],
            "steps": ["c"],
        },
        {**payload(), "extra": True},
        payload(title=""),
        payload(title="   "),
        payload(title=5),
        payload(title="x" * 201),
        payload(title="x\ty"),
        payload(servings=2),
        payload(servings=2.0),
        payload(servings=True),
        payload(servings=""),
        payload(servings="   "),
        payload(servings="x" * 101),
        payload(servings="2\tпорции"),
        payload(ingredients=[]),
        payload(ingredients=[{"name": "a"}]),
        payload(ingredients=[{"name": "a", "amount": "b", "unit": "c"}]),
        payload(ingredients=[{"name": "a", "quantity": "b"}]),
        payload(ingredients=["3 яйца"]),
        payload(ingredients=[{"name": "", "amount": "b"}]),
        payload(ingredients=[{"name": "a", "amount": ""}]),
        payload(ingredients=[{"name": "a", "amount": "b"}] * 101),
        payload(steps=[]),
        payload(steps=["ok", 5]),
        payload(steps=[""]),
        payload(steps=["Взбейте\nяйца"]),
        payload(steps=["ok"] * 51),
    ],
)
def test_json_that_is_not_the_exact_schema_is_reported(value):
    reply = f"До.\n{block(value)}\nПосле."

    parsed = parse_reply(reply)

    assert parsed.recipes == ()
    assert parsed.issues == (RecipeIssueKind.INVALID_SCHEMA,)
    rendered = render_reply(reply)
    assert INVALID_RECIPE_NOTICE in rendered
    assert_no_protocol_noise(rendered)


@pytest.mark.parametrize("raw", ["[]", '"just a string"', "null", "2", "true"])
def test_valid_json_that_is_not_an_object_is_a_schema_failure(raw):
    parsed = parse_reply(f"До.\n{block(raw=raw)}\nПосле.")

    assert parsed.recipes == ()
    assert parsed.issues == (RecipeIssueKind.INVALID_SCHEMA,)
    assert INVALID_RECIPE_NOTICE in render_reply(f"До.\n{block(raw=raw)}\nПосле.")


@pytest.mark.parametrize(
    "value",
    [
        payload(title=f"Омлет {RECIPE_START}"),
        payload(title=f"Омлет {RECIPE_END}"),
        payload(ingredients=[{"name": "яйца", "amount": RECIPE_END}]),
        payload(steps=[f"Взбейте {RECIPE_START} яйца."]),
    ],
)
def test_a_protocol_marker_inside_a_field_is_a_schema_failure(value):
    # The marker must not close the block early and leave a JSON remnant on screen, and it
    # must never be rendered as part of a recipe.
    raw = json.dumps(value, ensure_ascii=False)
    reply = f"До.\n{RECIPE_START}\n{raw}\n{RECIPE_END}\nПосле."

    parsed = parse_reply(reply)
    rendered = render_reply(reply)

    assert parsed.recipes == ()
    assert parsed.issues == (RecipeIssueKind.INVALID_SCHEMA,)
    assert INVALID_RECIPE_NOTICE in rendered
    assert "До." in rendered and "После." in rendered
    assert_no_protocol_noise(rendered)
    assert '"steps"' not in rendered and '"ingredients"' not in rendered


def test_a_block_over_the_size_limit_is_rejected_before_json_is_read():
    raw = json.dumps({"title": "x", "padding": "x" * MAX_RECIPE_BLOCK_LENGTH})

    parsed = parse_reply(f"До.{block(raw=raw)}После.")

    assert parsed.recipes == ()
    assert parsed.issues == (RecipeIssueKind.INVALID_SCHEMA,)
    assert_no_protocol_noise(render_reply(f"До.{block(raw=raw)}После."))


# --- incomplete and truncated output -----------------------------------------------------------------


def test_a_truncated_block_is_reported_not_parsed_and_does_not_crash():
    reply = f"Начало.\n{RECIPE_START}\n{json.dumps(payload())}"

    parsed = parse_reply(reply)
    rendered = render_reply(reply)

    assert parsed.recipes == ()
    assert parsed.issues == (RecipeIssueKind.TRUNCATED,)
    assert parsed.segments[1].kind is RecipeIssueKind.TRUNCATED
    assert "Начало." in rendered
    assert TRUNCATED_RECIPE_NOTICE in rendered
    assert_no_protocol_noise(rendered)


def test_after_an_unclosed_marker_the_rest_is_untrusted_protocol_content():
    reply = f"Начало.\n{RECIPE_START}\n{json.dumps(payload())}\nЭто обычный текст после."

    rendered = render_reply(reply)

    assert "Начало." in rendered
    assert "Это обычный текст после." not in rendered
    assert TRUNCATED_RECIPE_NOTICE in rendered
    assert_no_protocol_noise(rendered)


def test_a_stray_closing_marker_is_removed_and_the_words_around_it_stay():
    reply = "До маркера. " + RECIPE_END + " После маркера."

    parsed = parse_reply(reply)
    rendered = render_reply(reply)

    assert parsed.recipes == ()
    assert parsed.issues == (RecipeIssueKind.STRAY_END,)
    assert rendered == "До маркера.  После маркера."
    assert_no_protocol_noise(rendered)


def test_a_stray_closing_marker_does_not_prevent_a_later_block_from_being_read():
    reply = f"До {RECIPE_END} и рецепт:\n{block()}"

    parsed = parse_reply(reply)

    assert parsed.issues == (RecipeIssueKind.STRAY_END,)
    (recipe,) = parsed.recipes
    assert recipe.title == TITLE
    assert_no_protocol_noise(render_reply(reply))


@pytest.mark.parametrize(
    "reply",
    [
        "",
        RECIPE_START,
        RECIPE_END,
        RECIPE_START + RECIPE_END,
        RECIPE_END + RECIPE_START,
        f"{RECIPE_START}\n{RECIPE_START}\n{{}}{RECIPE_END}{RECIPE_END}",
        f"До.\n{block(raw='{')}\nПосле.",
        f"До.\n{RECIPE_START}\n{{",
        "{{{{}}}}",
    ],
)
def test_pathological_answers_never_crash_rendering(reply):
    parsed = parse_reply(reply)
    rendered = render_reply(reply)

    assert isinstance(rendered, str)
    assert rendered == render_reply(reply)
    assert_no_protocol_noise(rendered)
    for recipe in parsed.recipes:
        assert recipe.index in range(len(parsed.recipes))


# --- identity ----------------------------------------------------------------------------------------


def test_recipe_identity_is_deterministic_for_the_same_answer():
    reply = f"Вот:\n{block()}"

    first = parse_reply(reply).recipes[0]
    second = parse_reply(reply).recipes[0]

    assert first.recipe_id == second.recipe_id
    assert re.fullmatch(r"recipe-[0-9a-f]{16}", first.recipe_id)
    assert first.recipe_id == parse_reply(reply).recipes[0].recipe_id


def test_recipe_identity_changes_with_the_content():
    changed = parse_reply(block(payload(title="Другой омлет"))).recipes[0]
    original = parse_reply(block()).recipes[0]
    same_content = parse_reply(block(payload(title="Другой омлет"))).recipes[0]

    assert changed.recipe_id == same_content.recipe_id
    assert changed.recipe_id != original.recipe_id


def test_identity_is_the_same_after_the_spelling_rule_has_been_applied():
    with_yo = parse_reply(block(payload(title="Свёкла тушёная", steps=["Тушите свёклу."])))
    already_spelled = parse_reply(
        block(payload(title="Свекла тушеная", steps=["Тушите свеклу."]))
    )

    assert with_yo.recipes[0].title == already_spelled.recipes[0].title
    assert with_yo.recipes[0].recipe_id == already_spelled.recipes[0].recipe_id


# --- spelling and edits ------------------------------------------------------------------------------


def test_the_model_text_is_spelled_once_but_machine_readable_spans_are_kept():
    reply = (
        "Ещё рецепт: https://ёж.рф/свёкла — всё по ссылке.\n"
        + block(
            payload(
                title="Свёкла тушёная",
                ingredients=[{"name": "свёкла", "amount": "2 шт."}],
                steps=["Тушите свёклу."],
            )
        )
    )

    parsed = parse_reply(reply)
    recipe = parsed.recipes[0]

    assert parsed.segments[0].text.startswith(
        "Еще рецепт: https://ёж.рф/свёкла — все по ссылке."
    )
    assert recipe.title == "Свекла тушеная"
    assert recipe.ingredients == (Ingredient(name="свекла", amount="2 шт."),)
    assert recipe.steps == ("Тушите свеклу.",)
    assert "Свекла тушеная" in render_reply(reply)


def test_a_personally_edited_recipe_is_rendered_exactly_as_edited():
    edited = Recipe(
        index=0,
        title="Свёкла тушёная",
        servings="2 порции",
        ingredients=(Ingredient(name="свёкла", amount="2 шт."),),
        steps=("Тушите свёклу.",),
    )

    rendered = render_recipe(edited)

    assert rendered == (
        "Свёкла тушёная\n"
        "2 порции\n"
        "\n"
        "Ингредиенты:\n"
        "- свёкла — 2 шт.\n"
        "\n"
        "Приготовление:\n"
        "1. Тушите свёклу."
    )
    assert "ё" in rendered


# --- boundaries --------------------------------------------------------------------------------------


def test_the_module_depends_only_on_the_standard_library_and_the_spelling_rule():
    path = Path(recipes.__file__)
    modules = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)

    assert modules == {
        "__future__",
        "hashlib",
        "json",
        "re",
        "dataclasses",
        "enum",
        "typing",
        "house_helper.core.orthography",
    }
