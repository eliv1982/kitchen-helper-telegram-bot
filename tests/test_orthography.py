"""The product's spelling rule: Russian text that House Helper itself writes uses "е", never "ё".

Three halves, and the line between them is the point of this file:

  * What the product writes in advance (its screens, hints, errors, mails, the bot's replies
    to commands) is written without "ё" in the source. A scan of the source keeps it so.
  * What the assistant writes is spelled by the rule as each answer is produced, before it is
    stored or delivered (web, Telegram linked to an account, and the standalone bot). Only its
    prose: what a machine reads inside the answer (web and e-mail addresses, the destination of
    a link, code) is kept exactly, so that a link still leads where it was meant to.
  * What a person wrote or sent (their messages, the conversation names made from them, their
    photos) and what was stored before the rule are never touched, on the way in or out.

The scan reads the source files; the rest runs the real application over an offline assistant
(see chat_support and telegram_support), with answers and questions that are full of "ё".
"""

import re
from pathlib import Path

import pytest
import sqlalchemy as sa

import house_helper
from house_helper.core.backend import BackendReply
from house_helper.core.chat_service import ChatService
from house_helper.core.identity import ConversationKey
from house_helper.core.orthography import replace_yo
from house_helper.core.outcomes import ChatOutcome, OutcomeStatus
from house_helper.persistence.models import Message, MessageAttachment
from house_helper.web.turn_runner import TRUNCATED_NOTE
from tests import seams
from tests.chat_support import ScriptedBackend, chat_app
from tests.helpers import FakeBackend, FakeBot, FakeMessage
from tests.photo_support import jpeg
from tests.telegram_support import MOTHER, photo_message, telegram_app

ROOT = Path(house_helper.__file__).parent.parent

YO, CAPITAL_YO = "ё", "Ё"
DIAERESIS = chr(0x0308)  # the combining mark that makes "ё" out of "е"

# A reply and a question that use the letter as people (and models) do.
QUESTION = "Как сварить свёклу? Ещё Ёлку не забыть, всё записано."
REPLY = "Свёклу варите час. Ещё понадобится Ёмкость: всё готово."
SPELLED = "Свеклу варите час. Еще понадобится Емкость: все готово."

# The address a review reproduced the defect with: respelled, it was a different address.
ADDRESS = "https://ёж.рф/manual?part=Ё"
DECOMPOSED_YO = "е" + DIAERESIS

# An answer that mixes prose with every kind of text that a machine reads: the destination of a
# link, an address, an e-mail address, code in a line and code in a block.
ANSWER_WITH_MACHINE_TEXT = (
    "Ещё одна подсказка: [Ёлочные рецепты](https://ёлка.рф/рецепты?блюдо=Ё).\n"
    f"Инструкция — {ADDRESS}, почта ёж@почта.рф.\n"
    "Команда: `ёж --всё`\n"
    "\n"
    "```\n"
    "ёж Ё\n"
    "```\n"
    "Всё готово."
)
SPELLED_ANSWER_WITH_MACHINE_TEXT = (
    "Еще одна подсказка: [Елочные рецепты](https://ёлка.рф/рецепты?блюдо=Ё).\n"
    f"Инструкция — {ADDRESS}, почта ёж@почта.рф.\n"
    "Команда: `ёж --всё`\n"
    "\n"
    "```\n"
    "ёж Ё\n"
    "```\n"
    "Все готово."
)
# What a person may write: the same address, in their own words, which stay as they typed them.
QUESTION_WITH_ADDRESS = f"Что значит Ёж по адресу {ADDRESS}? Ещё есть [Ёлка](https://ёлка.рф/Ё)."

# The two defects of the second review. An address without a scheme whose last label is on no list
# of domains; and a reference link whose label is a host name (the definition's label was spelled,
# the use of it was not, so they no longer matched).
BARE_ADDRESS_ANSWER = "Ещё рецепты: 1000.menu/cooking/search?str=свёкла — всё по ссылке."
BARE_ADDRESS_SPELLED = "Еще рецепты: 1000.menu/cooking/search?str=свёкла — все по ссылке."
REFERENCE_LINK_ANSWER = "[Рецепт][ёж.рф]\n\n[ёж.рф]: https://ёж.рф/manual?part=Ё"
REFERENCE_LINK_SPELLED = REFERENCE_LINK_ANSWER  # nothing in it is prose with the letter
# The same, with the letter in the text of the link and in the title: those are prose.
REFERENCE_LINK_WITH_PROSE_ANSWER = (
    'Ещё: [Ёлочный рецепт][ёж.рф].\n\n[ёж.рф]: https://ёж.рф/manual?part=Ё "Всё про ёлки"'
)
REFERENCE_LINK_WITH_PROSE_SPELLED = (
    'Еще: [Елочный рецепт][ёж.рф].\n\n[ёж.рф]: https://ёж.рф/manual?part=Ё "Все про елки"'
)
REPRODUCED_DEFECTS = [
    pytest.param(BARE_ADDRESS_ANSWER, BARE_ADDRESS_SPELLED, id="bare address"),
    pytest.param(REFERENCE_LINK_ANSWER, REFERENCE_LINK_SPELLED, id="reference link"),
    pytest.param(
        REFERENCE_LINK_WITH_PROSE_ANSWER,
        REFERENCE_LINK_WITH_PROSE_SPELLED,
        id="reference link with prose around it",
    ),
]


# --- the rule itself ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Подключён", "Подключен"),
        ("сохранённые рецепты", "сохраненные рецепты"),
        ("ещё", "еще"),
        ("Ещё", "Еще"),
        ("ЁЖ", "ЕЖ"),
        ("Ёлка и ёлочка", "Елка и елочка"),
        ("всё, её, нём, вперёд", "все, ее, нем, вперед"),
    ],
)
def test_every_yo_becomes_ye_in_the_case_it_had(text, expected):
    assert replace_yo(text) == expected


def test_the_decomposed_form_of_the_letter_is_spelled_too():
    # "е" followed by a combining diaeresis is how some text writes "ё".
    assert replace_yo("св" + "е" + DIAERESIS + "кла") == "свекла"
    assert replace_yo("Е" + DIAERESIS + "лка") == "Елка"
    assert replace_yo("ещ" + "е" + DIAERESIS) == "еще"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Борщ с капустой, 500 г, 2 шт.",
        "Омлет из яиц: взбейте, посолите, жарьте 3 минуты.",
        "ЕЕ ее Ее",
        "plain ascii, cafe and ë",  # the Latin "ë" is another letter
        "e" + DIAERESIS,  # so is a Latin "e" with the mark
        "Йод, йогурт, и" + chr(0x0306),  # the other Russian letter made with a mark: "й"
        "🍲 \n\t «елочки»",
        "https://example.org/a?b=1#c",
    ],
)
def test_nothing_but_the_letter_is_changed(text):
    assert replace_yo(text) == text


def test_the_rule_is_idempotent_and_changes_only_the_letter():
    once = replace_yo(REPLY)

    assert once == SPELLED
    assert replace_yo(once) == once
    assert len(once) == len(REPLY)  # a letter for a letter: nothing moves, nothing is lost
    assert YO not in once and CAPITAL_YO not in once


# --- text that a machine reads is kept exactly --------------------------------------------------------
#
# Each expectation below is the whole text, with the protected span written into it letter for
# letter: the prose around the span is spelled, and the span has to come out equal to the one that
# went in (not only to still be a valid address or link).


def test_the_address_a_review_found_respelled_is_kept_exactly():
    assert replace_yo(ADDRESS) == ADDRESS
    assert replace_yo(ADDRESS) != "https://еж.рф/manual?part=Е"


@pytest.mark.parametrize(
    "address",
    [
        ADDRESS,
        "http://Ёлка.com:8080/Ё/ё?ё=Ё&б=ё#Ё",  # port, path, query and fragment
        "https://ru.wikipedia.org/wiki/Ёж_(животное)",  # parentheses in the path
        "https://ёж.рф/a%D1%91",
        "ftp://ёж.рф/Ё.txt",
        "www.ёж.рф/Ё",
        "ёж.рф/manual?part=Ё",  # no scheme
        "ёж.рф",
        "Ёлка.com",
        "1000.menu/cooking/search?str=свёкла",  # no scheme, and "menu" is on no list
        "food.recipes:8080/Ё",
        "пример.рецепты/ё#Ё",
        "example.guide?ё=Ё",
        "mailto:ёж@ёж.рф",
        "ёж@почта.рф",
        "https://ёж.рф/св" + DECOMPOSED_YO + "кла",  # the decomposed letter too
    ],
)
def test_an_address_is_kept_exactly_and_the_prose_before_and_after_it_is_spelled(address):
    text = f"Ещё один адрес: {address} — всё по нему. Ёмкость рядом."

    assert replace_yo(text) == f"Еще один адрес: {address} — все по нему. Емкость рядом."


def test_punctuation_and_quotes_around_an_address_are_not_part_of_what_is_kept_or_lost():
    text = f"Смотрите {ADDRESS}, ещё ({ADDRESS}) и «{ADDRESS}». Всё."

    assert replace_yo(text) == f"Смотрите {ADDRESS}, еще ({ADDRESS}) и «{ADDRESS}». Все."


def test_the_decomposed_letter_is_spelled_in_prose_beside_an_address_and_kept_inside_it():
    address = "https://ёж.рф/св" + DECOMPOSED_YO + "кла"
    text = f"ещ{DECOMPOSED_YO} {address} св{DECOMPOSED_YO}кла"

    assert replace_yo(text) == f"еще {address} свекла"


def test_a_bare_address_whose_last_label_is_on_no_list_is_kept_exactly():
    # The review's reproduction: "menu" is no top-level domain the module knows, and the query was
    # respelled into another search.
    assert replace_yo(BARE_ADDRESS_ANSWER) == BARE_ADDRESS_SPELLED
    assert replace_yo(BARE_ADDRESS_SPELLED) == BARE_ADDRESS_SPELLED


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # In each, the letter stands where the tail of an address would be if the text were taken for one.
        pytest.param("Это всё.Ещё?Да, ёж", "Это все.Еще?Да, еж", id="no space after a full stop"),
        pytest.param(
            "Слово.Слово/всё и ещё.Ещё?всё",
            "Слово.Слово/все и еще.Еще?все",
            id="a capital letter after the full stop",
        ),
        pytest.param("Ещё 3.5/ёж и 0.5/Ё", "Еще 3.5/еж и 0.5/Е", id="decimal fractions"),
        pytest.param(
            "Ещё 2.5л/ёж и 10.12.2025/Ё",
            "Еще 2.5л/еж и 10.12.2025/Е",
            id="a last label that begins with a digit",
        ),
        pytest.param("Это т.е./всё, т.д./ёж", "Это т.е./все, т.д./еж", id="abbreviations"),
        pytest.param(
            "Пункт 3.а/ёж, 2.б/Ё", "Пункт 3.а/еж, 2.б/Е", id="a last label of a single letter"
        ),
        pytest.param(
            "Ещё ёж.menu и всё",
            "Еще еж.menu и все",
            id="a name on no list with no path, query or fragment needs its scheme",
        ),
    ],
)
def test_prose_and_numbers_with_a_slash_or_a_stop_are_not_taken_for_an_address(text, expected):
    assert replace_yo(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "[Рецепт][ёж.рф]\n\n[ёж.рф]: https://ёж.рф/manual?part=Ё",
            "[Рецепт][ёж.рф]\n\n[ёж.рф]: https://ёж.рф/manual?part=Ё",
            id="the review's reproduction",
        ),
        pytest.param(
            "Ещё: [Ёлочный рецепт][ёж.рф].\n\n[ёж.рф]: https://ёж.рф/manual?part=Ё",
            "Еще: [Елочный рецепт][ёж.рф].\n\n[ёж.рф]: https://ёж.рф/manual?part=Ё",
            id="the text is spelled, the label is kept",
        ),
        pytest.param(
            "[Ёж][ёж], ещё [Ёлка][ёж] и [Ещё][ёж].\n\n[ёж]: https://ёж.рф/Ё",
            "[Еж][ёж], еще [Елка][ёж] и [Еще][ёж].\n\n[ёж]: https://ёж.рф/Ё",
            id="the same definition used several times",
        ),
        pytest.param(
            "Ещё [ёж][] тут и [Ёж] там.\n\n[ёж]: https://ёж.рф/Ё",
            "Еще [ёж][] тут и [Ёж] там.\n\n[ёж]: https://ёж.рф/Ё",
            id="collapsed and shortcut forms: the label is also the text, and is kept",
        ),
        pytest.param(
            "[Рецепт][Мой  ЁЖ]\n\n[мой ёж]: https://ёж.рф/Ё",
            "[Рецепт][Мой  ЁЖ]\n\n[мой ёж]: https://ёж.рф/Ё",
            id="labels match without regard to case or to spaces, as in Markdown",
        ),
        pytest.param(
            "![Ёж на ёлке][ёж-картинка]\n\n[ёж-картинка]: картинки/ёж.png",
            "![Еж на елке][ёж-картинка]\n\n[ёж-картинка]: картинки/ёж.png",
            id="an image",
        ),
        pytest.param(
            "Ещё [Ёж][ёж] тут.\n\n```\n[ёж]: https://ёж.рф/Ё\n```\n",
            "Еще [Еж][еж] тут.\n\n```\n[ёж]: https://ёж.рф/Ё\n```\n",
            id="a definition inside fenced code defines nothing",
        ),
        pytest.param(
            "Ещё [Ёж][ёж] и [всё].",
            "Еще [Еж][еж] и [все].",
            id="a label that no definition names is prose",
        ),
        pytest.param(
            "[ёж](https://ёж.рф/Ё) ещё\n\n[ёж]: https://ёж.рф/Ё",
            "[еж](https://ёж.рф/Ё) еще\n\n[ёж]: https://ёж.рф/Ё",
            id="the text of an inline link is prose even where it equals a label",
        ),
        pytest.param(
            "[Рецепт][ёж.рф]\n\nЕщё абзац.\n\n"
            "[ёж.рф]: <https://ёж.рф/мой ёж> 'Всё'\n[Ёлка]: https://ёлка.рф/Ё",
            "[Рецепт][ёж.рф]\n\nЕще абзац.\n\n"
            "[ёж.рф]: <https://ёж.рф/мой ёж> 'Все'\n[Ёлка]: https://ёлка.рф/Ё",
            id="a definition after other text, with a destination in angle brackets",
        ),
    ],
)
def test_the_label_of_a_reference_link_is_kept_where_it_is_used_and_where_it_is_defined(text, expected):
    once = replace_yo(text)

    assert once == expected
    assert replace_yo(once) == once


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "[Ёлочные игрушки](https://ёлка.рф/игрушки?цвет=Ё)",
            "[Елочные игрушки](https://ёлка.рф/игрушки?цвет=Ё)",
            id="label spelled, destination kept",
        ),
        pytest.param(
            '[Ёж](https://ёж.рф/Ё "Ёжик на ёлке")',
            '[Еж](https://ёж.рф/Ё "Ежик на елке")',
            id="the title is read by a person, so it is spelled",
        ),
        pytest.param(
            "[Ёж](<https://ёж.рф/мой ёж>)",
            "[Еж](<https://ёж.рф/мой ёж>)",
            id="destination in angle brackets, spaces in it",
        ),
        pytest.param(
            "[Ёж](https://ru.wikipedia.org/wiki/Ёж_(животное))",
            "[Еж](https://ru.wikipedia.org/wiki/Ёж_(животное))",
            id="parentheses in the destination",
        ),
        pytest.param(
            "![Ёж на ёлке](картинки/ёж.png)",
            "![Еж на елке](картинки/ёж.png)",
            id="image with a relative path",
        ),
        pytest.param("[Ещё](#всё-про-ёж)", "[Еще](#всё-про-ёж)", id="anchor in the page"),
        pytest.param(
            "[ёж.рф/Ё](https://ёж.рф/Ё)",
            "[ёж.рф/Ё](https://ёж.рф/Ё)",
            id="an address as the label of its own link",
        ),
        pytest.param(
            '[Ёж][ёж]\n\n[ёж]: https://ёж.рф/Ё "Ёж"\n',
            '[Еж][ёж]\n\n[ёж]: https://ёж.рф/Ё "Еж"\n',
            id="reference link: the label is kept where it is used and where it is defined",
        ),
        pytest.param(
            "Ещё: <https://ёж.рф/Ё> и <ёж@почта.рф>.",
            "Еще: <https://ёж.рф/Ё> и <ёж@почта.рф>.",
            id="autolinks",
        ),
    ],
)
def test_the_destination_of_a_link_is_kept_exactly_and_what_a_reader_sees_is_spelled(text, expected):
    assert replace_yo(text) == expected


def test_several_links_in_one_answer_each_keep_their_destination():
    text = (
        "Ещё про Ёлку: [Ёлки](https://ёлки.рф/Ё), [Свёкла](https://свёкла.рф/рецепт?ё=Ё) "
        "и [Ёж](https://ёж.рф/Ё) — всё тут."
    )

    assert replace_yo(text) == (
        "Еще про Елку: [Елки](https://ёлки.рф/Ё), [Свекла](https://свёкла.рф/рецепт?ё=Ё) "
        "и [Еж](https://ёж.рф/Ё) — все тут."
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("Команда `ёж --всё` ещё ждёт.", "Команда `ёж --всё` еще ждет.", id="inline"),
        pytest.param(
            "``код с ` и ё`` всё", "``код с ` и ё`` все", id="inline, with a backtick inside"
        ),
        pytest.param("`ёж\nЁ` ещё", "`ёж\nЁ` еще", id="inline, over two lines"),
        pytest.param("Ещё ```ёж``` всё", "Еще ```ёж``` все", id="three backticks in one line"),
        pytest.param(
            "```ёж``` ещё\nВсё", "```ёж``` еще\nВсе", id="that line opens no block"
        ),
        pytest.param(
            "Вот код:\n```python\nprint('ёж', 'Ё')\n```\nВсё.",
            "Вот код:\n```python\nprint('ёж', 'Ё')\n```\nВсе.",
            id="fenced block with a language",
        ),
        pytest.param("~~~\nёж Ё\n~~~\nещё", "~~~\nёж Ё\n~~~\nеще", id="fenced with tildes"),
        pytest.param(
            "````\n```\nёж\n```\n````\nещё",
            "````\n```\nёж\n```\n````\nеще",
            id="a shorter fence inside a longer one closes nothing",
        ),
        pytest.param(
            "1. Шаг ещё:\n   ```bash\n   echo ёж\n   ```\n2. Всё.",
            "1. Шаг еще:\n   ```bash\n   echo ёж\n   ```\n2. Все.",
            id="block inside a list",
        ),
        pytest.param(
            "> ```\n> ёж\n> ```\nещё", "> ```\n> ёж\n> ```\nеще", id="block inside a quotation"
        ),
        pytest.param(
            "Ещё код:\n```\nёж Ё\nи ещё",
            "Еще код:\n```\nёж Ё\nи ещё",
            id="a block that is never closed runs to the end",
        ),
        pytest.param(
            "Один ` апостроф, ещё ёлка.\n\nИ ещё один ` тут: всё.",
            "Один ` апостроф, еще елка.\n\nИ еще один ` тут: все.",
            id="backticks of two paragraphs do not pair up",
        ),
        pytest.param(
            "Один ` апостроф, ещё.\r\n\r\nИ ещё один ` тут: всё.",
            "Один ` апостроф, еще.\r\n\r\nИ еще один ` тут: все.",
            id="the same, with Windows line ends",
        ),
        pytest.param(
            f"Ещё `ёж` и ещё {DECOMPOSED_YO} `{DECOMPOSED_YO}`",
            f"Еще `ёж` и еще е `{DECOMPOSED_YO}`",
            id="the decomposed letter inside code is kept too",
        ),
    ],
)
def test_code_is_kept_exactly_and_the_prose_around_it_is_spelled(text, expected):
    assert replace_yo(text) == expected


def test_the_tags_of_html_are_kept_exactly_and_what_stands_between_them_is_spelled():
    opening, image = '<a href="https://ёж.рф/Ё" title="Ё">', '<img src="ё.png" alt="Ёж"/>'

    assert replace_yo(f"{opening}Ёж</a> ещё {image}") == f"{opening}Еж</a> еще {image}"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Если x<y, то ещё ёлка, а y>z — всё.", "Если x<y, то еще елка, а y>z — все."),
        ("<ещё> (ещё) [всё] {всё}", "<еще> (еще) [все] {все}"),
        ("Это всё.Ещё одно.", "Это все.Еще одно."),  # a full stop is not enough to make a domain
        ("Ещё 5.Ёлка и 2.Ёж", "Еще 5.Елка и 2.Еж"),
        ("Ёж@ ещё @ёж", "Еж@ еще @еж"),  # an at sign is not enough to make an address
    ],
)
def test_prose_that_only_resembles_machine_text_is_still_spelled(text, expected):
    assert replace_yo(text) == expected


def test_an_answer_that_mixes_prose_with_machine_text_is_spelled_once_and_only_in_the_prose():
    once = replace_yo(ANSWER_WITH_MACHINE_TEXT)

    assert once == SPELLED_ANSWER_WITH_MACHINE_TEXT
    assert replace_yo(once) == once
    assert len(once) == len(ANSWER_WITH_MACHINE_TEXT)


# --- what the product writes in advance --------------------------------------------------------------

# The one place the letter stays: the instruction the assistant is given is the model's input, not
# a text anyone is shown, and its wording is pinned by tests/test_kitchen_prompt.py.
NOT_SHOWN_TO_ANYONE = {Path("house_helper/core/kitchen_prompt.py")}
SCANNED_SUFFIXES = {".py", ".js", ".css", ".html", ".svg", ".txt", ".json"}

_FORBIDDEN = re.compile(f"[{YO}{CAPITAL_YO}]|[еЕ]{DIAERESIS}")


def _shown_source_files():
    files = [ROOT / "bot.py"]
    files += sorted(
        path
        for path in (ROOT / "house_helper").rglob("*")
        if path.is_file() and path.suffix in SCANNED_SUFFIXES and "__pycache__" not in path.parts
    )
    return [path for path in files if path.relative_to(ROOT) not in NOT_SHOWN_TO_ANYONE]


def test_the_scan_covers_the_files_that_carry_the_texts_people_read():
    scanned = {path.relative_to(ROOT).as_posix() for path in _shown_source_files()}

    assert {
        "bot.py",
        "house_helper/web/telegram_chat.py",  # the bot's replies linked to an account
        "house_helper/web/diagnostics.py",  # the API's error messages
        "house_helper/web/email_pages.py",
        "house_helper/web/invite_pages.py",
        "house_helper/web/mailer.py",  # the e-mail with the sign-in link
        "house_helper/web/ui.py",
        "house_helper/web/static/app.js",
        "house_helper/web/static/chat-logic.js",  # statuses, hints, labels of the page
        "house_helper/core/messages_ru.py",
    } <= scanned
    assert "house_helper/core/kitchen_prompt.py" not in scanned


def test_no_text_the_product_shows_is_written_with_yo():
    offenders = {}
    for path in _shown_source_files():
        found = [
            f"{number}: {line.strip()[:100]}"
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if _FORBIDDEN.search(line)
        ]
        if found:
            offenders[path.relative_to(ROOT).as_posix()] = found

    assert offenders == {}


# --- the assistant's answers: web ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_new_web_answer_is_spelled_before_it_is_stored_and_the_question_is_kept(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(REPLY)) as chat:
        conversation = await chat.create()

        sent = await chat.send(conversation, QUESTION, "k-1")
        replay = await chat.send(conversation, QUESTION, "k-1")
        history = (await chat.history(conversation)).json()["messages"]
        listed = (await chat.client.get("/api/conversations")).json()["conversations"]
        stored = [(m.role, m.content) for m in chat.messages(conversation)]

    assert sent.status_code == 201
    body = sent.json()
    assert body["user_message"]["text"] == QUESTION  # as the person typed it
    assert body["assistant_message"]["text"] == SPELLED
    assert replay.json()["assistant_message"]["text"] == SPELLED  # a retry replays the stored answer
    assert [(m["role"], m["text"]) for m in history] == [("user", QUESTION), ("assistant", SPELLED)]
    assert stored == [("user", QUESTION), ("assistant", SPELLED)]
    assert [c["title"] for c in listed] == [QUESTION]  # named by the person's own words


@pytest.mark.asyncio
async def test_the_assistant_is_shown_the_question_exactly_as_written(tmp_path):
    backend = ScriptedBackend(REPLY, "Ещё минуту.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        await chat.send(conversation, QUESTION, "k-1")
        await chat.send(conversation, "А ещё?", "k-2")

    assert backend.requests[0][0] == [{"role": "user", "content": QUESTION}]
    assert backend.requests[1][0] == [
        {"role": "user", "content": QUESTION},
        {"role": "assistant", "content": SPELLED},  # the answer as it was stored: spelled
        {"role": "user", "content": "А ещё?"},
    ]


@pytest.mark.asyncio
async def test_an_answer_cut_off_at_the_cap_is_spelled_and_keeps_its_note(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(BackendReply(text=REPLY, truncated=True))) as chat:
        conversation = await chat.create()

        sent = await chat.send(conversation, QUESTION, "k-1")

    assert sent.json()["assistant_message"]["text"] == SPELLED + TRUNCATED_NOTE


@pytest.mark.asyncio
async def test_an_answer_stored_before_the_rule_is_shown_and_given_to_the_assistant_as_it_was(tmp_path):
    older = "Свёкла — ещё один вариант: борщ. Ёмкость не нужна."
    backend = ScriptedBackend("Первый ответ.", "Теперь проще.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        await chat.send(conversation, "Как сварить борщ?", "k-1")
        [assistant] = [m for m in chat.messages(conversation) if m.role == "assistant"]
        with chat.base.factory() as db:  # an answer from before the rule, as an older release stored it
            db.execute(sa.update(Message).where(Message.id == assistant.id).values(content=older))
            db.commit()

        shown = (await chat.history(conversation)).json()["messages"]
        await chat.send(conversation, "А проще?", "k-2")
        after = (await chat.history(conversation)).json()["messages"]
        stored = chat.messages(conversation)[1].content

    assert [m["text"] for m in shown] == ["Как сварить борщ?", older]
    assert [m["text"] for m in after][:2] == ["Как сварить борщ?", older]  # still, after a new turn
    assert stored == older
    assert backend.requests[1][0][1] == {"role": "assistant", "content": older}  # history is not respelled
    assert [m["text"] for m in after][-1] == "Теперь проще."


@pytest.mark.asyncio
async def test_a_web_answer_keeps_its_addresses_links_and_code_and_the_question_is_kept(tmp_path):
    backend = ScriptedBackend(ANSWER_WITH_MACHINE_TEXT, "Ещё минуту.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()

        sent = await chat.send(conversation, QUESTION_WITH_ADDRESS, "k-1")
        replay = await chat.send(conversation, QUESTION_WITH_ADDRESS, "k-1")
        await chat.send(conversation, "А ещё?", "k-2")
        history = (await chat.history(conversation)).json()["messages"]
        stored = [(m.role, m.content) for m in chat.messages(conversation)]

    assert sent.status_code == 201
    body = sent.json()
    assert body["user_message"]["text"] == QUESTION_WITH_ADDRESS  # as the person typed it
    assert body["assistant_message"]["text"] == SPELLED_ANSWER_WITH_MACHINE_TEXT
    assert replay.json()["assistant_message"]["text"] == SPELLED_ANSWER_WITH_MACHINE_TEXT
    assert [(m["role"], m["text"]) for m in history[:2]] == [
        ("user", QUESTION_WITH_ADDRESS),
        ("assistant", SPELLED_ANSWER_WITH_MACHINE_TEXT),
    ]
    assert stored[:2] == [
        ("user", QUESTION_WITH_ADDRESS),
        ("assistant", SPELLED_ANSWER_WITH_MACHINE_TEXT),
    ]
    assert backend.requests[1][0][:2] == [  # what the assistant is shown on the next turn
        {"role": "user", "content": QUESTION_WITH_ADDRESS},
        {"role": "assistant", "content": SPELLED_ANSWER_WITH_MACHINE_TEXT},
    ]


# --- the assistant's answers: Telegram linked to an account ---------------------------------------------


@pytest.mark.asyncio
async def test_a_telegram_answer_is_spelled_and_the_question_is_kept_in_the_shared_history(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend(REPLY)) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, QUESTION)
        [conversation] = t.conversations()
        history = (await t.chat.history(str(conversation.id))).json()["messages"]

    assert t.bot.texts(MOTHER)[-1] == SPELLED
    assert [(m["role"], m["text"]) for m in history] == [("user", QUESTION), ("assistant", SPELLED)]
    assert t.chat.backend.requests[0][0] == [{"role": "user", "content": QUESTION}]


@pytest.mark.asyncio
async def test_a_web_exchange_is_copied_to_telegram_with_the_question_kept_and_the_answer_spelled(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend(REPLY)) as t:
        await t.pair(MOTHER)
        conversation = await t.chat.create()
        sent = await t.chat.send(conversation, QUESTION, "web-1")
        await t.settle()

    assert sent.status_code == 201
    assert t.bot.texts(MOTHER)[-2:] == ["Вы (на сайте):\n" + QUESTION, SPELLED]


@pytest.mark.asyncio
async def test_a_photo_and_its_caption_are_kept_exactly_and_only_the_answer_is_spelled(tmp_path):
    picture = jpeg(5000, "svekla")
    caption = "Что сделать из свёклы? Ещё есть Ёж на фото."
    async with telegram_app(tmp_path, ScriptedBackend(REPLY)) as t:
        await t.pair(MOTHER)
        t.bot.files["p-1"] = ("photos/p-1.jpg", picture)
        t.chat.clock.advance(seconds=1)
        await t.deliver_update(photo_message(t.next_id(), MOTHER, "p-1", caption=caption))
        [stored] = t.chat.rows(MessageAttachment)
        [conversation] = t.conversations()
        messages = [(m.role, m.content) for m in t.chat.messages(str(conversation.id))]

    assert bytes(stored.data) == picture
    assert messages == [("user", caption), ("assistant", SPELLED)]
    assert t.bot.texts(MOTHER)[-1] == SPELLED


@pytest.mark.asyncio
async def test_a_telegram_answer_keeps_its_addresses_links_and_code(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend(ANSWER_WITH_MACHINE_TEXT)) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, QUESTION_WITH_ADDRESS)
        [conversation] = t.conversations()
        history = (await t.chat.history(str(conversation.id))).json()["messages"]

    assert t.bot.texts(MOTHER)[-1] == SPELLED_ANSWER_WITH_MACHINE_TEXT
    assert [(m["role"], m["text"]) for m in history] == [
        ("user", QUESTION_WITH_ADDRESS),
        ("assistant", SPELLED_ANSWER_WITH_MACHINE_TEXT),
    ]
    assert t.chat.backend.requests[0][0] == [{"role": "user", "content": QUESTION_WITH_ADDRESS}]


@pytest.mark.asyncio
async def test_a_photo_caption_with_an_address_is_kept_and_so_is_the_address_in_the_answer(tmp_path):
    picture = jpeg(5000, "adres")
    async with telegram_app(tmp_path, ScriptedBackend(ANSWER_WITH_MACHINE_TEXT)) as t:
        await t.pair(MOTHER)
        t.bot.files["p-2"] = ("photos/p-2.jpg", picture)
        t.chat.clock.advance(seconds=1)
        await t.deliver_update(
            photo_message(t.next_id(), MOTHER, "p-2", caption=QUESTION_WITH_ADDRESS)
        )
        [stored] = t.chat.rows(MessageAttachment)
        [conversation] = t.conversations()
        messages = [(m.role, m.content) for m in t.chat.messages(str(conversation.id))]

    assert bytes(stored.data) == picture
    assert messages == [
        ("user", QUESTION_WITH_ADDRESS),
        ("assistant", SPELLED_ANSWER_WITH_MACHINE_TEXT),
    ]
    assert t.bot.texts(MOTHER)[-1] == SPELLED_ANSWER_WITH_MACHINE_TEXT


# --- the standalone bot ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_standalone_bot_service_spells_the_answer_and_keeps_the_question():
    backend = FakeBackend(reply=REPLY)
    key = ConversationKey("telegram", "orthography-1")

    async with ChatService(backend).turn(key) as turn:
        outcome = await turn.send(QUESTION)

    assert outcome == ChatOutcome(OutcomeStatus.REPLIED, reply=SPELLED)
    assert backend.requests[0][0] == [{"role": "user", "content": QUESTION}]
    assert seams.history_of(key) == [
        {"role": "user", "content": QUESTION},
        {"role": "assistant", "content": SPELLED},  # the history the next question sees
    ]


@pytest.mark.asyncio
async def test_the_standalone_bot_delivers_the_spelled_answer(monkeypatch):
    async def fake_ai(messages, user_id):
        return REPLY

    seams.install_ai(monkeypatch, fake_ai)
    message = FakeMessage(user_id=4242, text=QUESTION)

    await seams.process_text(message, FakeBot())

    message.answer.assert_awaited_once_with(SPELLED)
    assert seams.history(4242) == [
        {"role": "user", "content": QUESTION},
        {"role": "assistant", "content": SPELLED},
    ]


@pytest.mark.asyncio
async def test_the_standalone_bot_service_keeps_addresses_links_and_code_in_the_answer():
    backend = FakeBackend(reply=ANSWER_WITH_MACHINE_TEXT)
    key = ConversationKey("telegram", "orthography-2")

    async with ChatService(backend).turn(key) as turn:
        outcome = await turn.send(QUESTION_WITH_ADDRESS)

    assert outcome == ChatOutcome(OutcomeStatus.REPLIED, reply=SPELLED_ANSWER_WITH_MACHINE_TEXT)
    assert backend.requests[0][0] == [{"role": "user", "content": QUESTION_WITH_ADDRESS}]
    assert seams.history_of(key) == [
        {"role": "user", "content": QUESTION_WITH_ADDRESS},
        {"role": "assistant", "content": SPELLED_ANSWER_WITH_MACHINE_TEXT},
    ]


@pytest.mark.asyncio
async def test_the_standalone_bot_delivers_the_answer_with_its_addresses_links_and_code_kept(
    monkeypatch,
):
    async def fake_ai(messages, user_id):
        return ANSWER_WITH_MACHINE_TEXT

    seams.install_ai(monkeypatch, fake_ai)
    message = FakeMessage(user_id=4244, text=QUESTION_WITH_ADDRESS)

    await seams.process_text(message, FakeBot())

    message.answer.assert_awaited_once_with(SPELLED_ANSWER_WITH_MACHINE_TEXT)
    assert seams.history(4244) == [
        {"role": "user", "content": QUESTION_WITH_ADDRESS},
        {"role": "assistant", "content": SPELLED_ANSWER_WITH_MACHINE_TEXT},
    ]


# --- the two defects of the second review, through every way an answer is produced ----------------------
#
# Web and Telegram linked to an account share one generation path (the turn runner); the standalone
# bot has its own (ChatService). Each is checked at the three places an answer ends up: what the
# caller is given, the stored history, and what is delivered.


@pytest.mark.asyncio
@pytest.mark.parametrize(("answer", "spelled"), REPRODUCED_DEFECTS)
async def test_a_web_answer_with_a_bare_address_or_a_reference_link_is_stored_and_replayed_exactly(
    tmp_path, answer, spelled
):
    backend = ScriptedBackend(answer, "Ещё минуту.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()

        sent = await chat.send(conversation, QUESTION_WITH_ADDRESS, "k-1")
        replay = await chat.send(conversation, QUESTION_WITH_ADDRESS, "k-1")
        await chat.send(conversation, "А ещё?", "k-2")
        history = (await chat.history(conversation)).json()["messages"]
        stored = [(m.role, m.content) for m in chat.messages(conversation)]

    assert sent.status_code == 201
    assert sent.json()["user_message"]["text"] == QUESTION_WITH_ADDRESS
    assert sent.json()["assistant_message"]["text"] == spelled
    assert replay.json()["assistant_message"]["text"] == spelled
    assert [(m["role"], m["text"]) for m in history[:2]] == [
        ("user", QUESTION_WITH_ADDRESS),
        ("assistant", spelled),
    ]
    assert stored[:2] == [("user", QUESTION_WITH_ADDRESS), ("assistant", spelled)]
    assert backend.requests[1][0][:2] == [  # what the assistant is shown on the next turn
        {"role": "user", "content": QUESTION_WITH_ADDRESS},
        {"role": "assistant", "content": spelled},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("answer", "spelled"), REPRODUCED_DEFECTS)
async def test_a_telegram_answer_with_a_bare_address_or_a_reference_link_is_delivered_exactly(
    tmp_path, answer, spelled
):
    async with telegram_app(tmp_path, ScriptedBackend(answer)) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, QUESTION_WITH_ADDRESS)
        [conversation] = t.conversations()
        history = (await t.chat.history(str(conversation.id))).json()["messages"]
        stored = [(m.role, m.content) for m in t.chat.messages(str(conversation.id))]

    assert t.bot.texts(MOTHER)[-1] == spelled
    assert [(m["role"], m["text"]) for m in history] == [
        ("user", QUESTION_WITH_ADDRESS),
        ("assistant", spelled),
    ]
    assert stored == [("user", QUESTION_WITH_ADDRESS), ("assistant", spelled)]


@pytest.mark.asyncio
@pytest.mark.parametrize(("answer", "spelled"), REPRODUCED_DEFECTS)
async def test_the_standalone_service_gives_and_stores_a_bare_address_or_a_reference_link_exactly(
    answer, spelled
):
    backend = FakeBackend(reply=answer)
    key = ConversationKey("telegram", "orthography-3")

    async with ChatService(backend).turn(key) as turn:
        outcome = await turn.send(QUESTION_WITH_ADDRESS)

    assert outcome == ChatOutcome(OutcomeStatus.REPLIED, reply=spelled)
    assert backend.requests[0][0] == [{"role": "user", "content": QUESTION_WITH_ADDRESS}]
    assert seams.history_of(key) == [
        {"role": "user", "content": QUESTION_WITH_ADDRESS},
        {"role": "assistant", "content": spelled},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("answer", "spelled"), REPRODUCED_DEFECTS)
async def test_the_standalone_bot_delivers_a_bare_address_or_a_reference_link_exactly(
    monkeypatch, answer, spelled
):
    async def fake_ai(messages, user_id):
        return answer

    seams.install_ai(monkeypatch, fake_ai)
    message = FakeMessage(user_id=4245, text=QUESTION_WITH_ADDRESS)

    await seams.process_text(message, FakeBot())

    message.answer.assert_awaited_once_with(spelled)
    assert seams.history(4245) == [
        {"role": "user", "content": QUESTION_WITH_ADDRESS},
        {"role": "assistant", "content": spelled},
    ]


@pytest.mark.asyncio
async def test_the_standalone_bots_own_greeting_is_written_without_yo():
    message = FakeMessage(user_id=4243, text="/start")

    await seams.process_start_command(message)

    (greeting,), _ = message.answer.call_args
    assert replace_yo(greeting) == greeting
