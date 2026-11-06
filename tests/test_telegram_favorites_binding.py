"""The owner a save button is clicked for, the owner it is saved for, and the owner the
confirmation is sent to are one identity, whatever the tie does in between.

The finding of the third independent review (KH-FAV-TG-02): the callback handler captured the
tie's owner (A) before saving, but the saving transaction resolved the tie again on its own.
During an A -> B transition the transaction could resolve B and save B's recipe, and the
returned title was then confirmed under the captured identity A. If the tie had gone back to A
by the time the confirmation was delivered (A -> B -> A), the delivery check saw A and B's
recipe title was sent into A's chat.

The correction: the user the click was authorized for is carried into
persistence.favorites.save_recipe_for_telegram as `expected_user_id`, the transaction resolves
the tie (with the link held) and refuses when the two are not the same, and the owner the
transaction validated is the identity the confirmation is sent under. The tests here reproduce
the review's exact interleaving (event-controlled, not sleeps), and pin the three orders a tie
can change in relative to a click, plus normal operation and a failing transaction.

The button and browsing behaviour itself is tests/test_telegram_favorites.py and
tests/test_telegram_favorites_browsing.py; the delivery-side rules are
tests/test_telegram_binding.py and tests/test_telegram_unlink_race.py.
"""

import asyncio
import json
import threading
from contextlib import contextmanager
from uuid import UUID

import pytest

from house_helper.core.backend import BackendReply
from house_helper.core.recipes import RECIPE_END, RECIPE_START, parse_reply, render_parsed_reply
from house_helper.persistence import favorites
from house_helper.persistence import telegram as store
from house_helper.persistence.models import FavoriteRecipe, TelegramLink
from house_helper.web import telegram_chat as tc
from tests.chat_support import ScriptedBackend, reached
from tests.telegram_support import MOTHER, TelegramHarness, save_buttons, telegram_app
from tests.web_support import TIMEOUT

pytestmark = pytest.mark.asyncio

# A title that appears nowhere else, so its presence in a chat is a disclosure and nothing else.
SECRET_TITLE = "Пирог сестры"
SECRET_RECIPE = {
    "title": SECRET_TITLE,
    "servings": "6 порций",
    "ingredients": [{"name": "слива", "amount": "500 г"}],
    "steps": ["Замесите тесто.", "Запеките."],
}
BORSH = {
    "title": "Борщ",
    "servings": "4 порции",
    "ingredients": [{"name": "свекла", "amount": "2 шт."}],
    "steps": ["Сварите бульон.", "Добавьте овощи."],
}

RAW_SECRET = "Вот рецепт.\n" + (
    f"{RECIPE_START}\n{json.dumps(SECRET_RECIPE, ensure_ascii=False)}\n{RECIPE_END}"
) + "\nПриятного аппетита."
RAW_BORSH = "Вот рецепт.\n" + (
    f"{RECIPE_START}\n{json.dumps(BORSH, ensure_ascii=False)}\n{RECIPE_END}"
) + "\nПриятного аппетита."


def reply(raw: str) -> BackendReply:
    """Exactly what the real backend returns: rendered text and validated recipes."""
    parsed = parse_reply(raw)
    return BackendReply(text=render_parsed_reply(parsed), recipes=parsed.recipes)


def backend_with(*raw: str) -> ScriptedBackend:
    return ScriptedBackend(*(reply(item) for item in raw))


def second_person(t: TelegramHarness) -> UUID:
    return next(iter(t.chat.extra_users.values()))


def favorites_rows(t: TelegramHarness, user_id=None) -> list[FavoriteRecipe]:
    where = [] if user_id is None else [FavoriteRecipe.user_id == user_id]
    return t.chat.rows(FavoriteRecipe, *where)


def secret_disclosed(t: TelegramHarness, since: int) -> bool:
    """True when a message sent after `since` carries the second person's recipe title."""
    return any(SECRET_TITLE in call.get("text", "") for call in t.bot.sent[since:])


async def a_button_of_the_second_person(t: TelegramHarness) -> tuple[UUID, str, object]:
    """The exact setup of the review: the account is paired to the SECOND person, receives an
    answer whose button names that person's recipe, and is handed to the first person. The
    old message (and its button) stays in the chat; the first person can click it. Returns
    (the second person's id, the button's callback data, the second person's browser)."""
    other = await t.chat.second_user()
    second = second_person(t)
    await t.pair(MOTHER, client=other)
    await t.deliver(MOTHER, "Испеки что-нибудь")
    [button] = save_buttons(t.bot.sent[-1])
    await t.service.unlink(second)
    await t.pair(MOTHER)
    assert [link.user_id for link in t.links()] == [t.user_id]
    return second, button["callback_data"], other


# --- exact interleavings -------------------------------------------------------------------


class Seam:
    """The bot's database with a place to stand: the test can hold a named task before its Nth
    database call, and run an action (on the event loop) right after that call returns, whether
    it returned or raised. Calls are told apart by task name, so only the click under test is
    counted."""

    def __init__(self, real) -> None:
        self._real = real
        self._calls: dict[str, int] = {}
        self._holds: dict[tuple[str, int], tuple[asyncio.Event, asyncio.Event]] = {}
        self._after: dict[tuple[str, int], object] = {}

    def hold_before(self, task: str, nth: int) -> tuple[asyncio.Event, asyncio.Event]:
        """(arrived, release): the Nth call of `task` waits for `release` before it starts."""
        self._holds[(task, nth)] = (asyncio.Event(), asyncio.Event())
        return self._holds[(task, nth)]

    def after(self, task: str, nth: int, action) -> None:
        self._after[(task, nth)] = action

    async def _through(self, method: str, work, **options):
        name = asyncio.current_task().get_name()
        number = self._calls[name] = self._calls.get(name, 0) + 1
        gate = self._holds.get((name, number))
        if gate is not None:
            gate[0].set()
            await gate[1].wait()
        try:
            return await getattr(self._real, method)(work, **options)
        finally:
            action = self._after.get((name, number))
            if action is not None:
                action()

    async def run(self, work, **options):
        return await self._through("run", work, **options)

    async def run_to_completion(self, work, **options):
        return await self._through("run_to_completion", work, **options)

    def __getattr__(self, name):
        return getattr(self._real, name)


def interpose(t: TelegramHarness) -> Seam:
    seam = Seam(t.service._database)
    t.service._database = seam
    return seam


def move_owner(t: TelegramHarness, user_id: UUID):
    """An action for the seam: the same Telegram account becomes `user_id`'s, straight in the
    database, as an unlink followed by a pairing would make it. A stored link's binding cannot
    be edited (the ORM refuses it), so the row is replaced."""

    def move() -> None:
        with t.chat.base.factory() as db, db.begin():
            link = db.query(TelegramLink).filter(TelegramLink.telegram_user_id == MOTHER).one()
            made = link.created_at
            db.delete(link)
            db.flush()
            db.add(TelegramLink(user_id=user_id, telegram_user_id=MOTHER, created_at=made))

    return move


def gate_save_favorite(monkeypatch):
    """persistence.favorites.save_favorite waits, on its worker thread, INSIDE the saving
    transaction, right before the favorite is written. Returns (entered, proceed)."""
    entered, proceed = threading.Event(), threading.Event()
    real = favorites.save_favorite

    def gated(*args, **kwargs):
        entered.set()
        assert proceed.wait(TIMEOUT), "the test never let the save go on"
        return real(*args, **kwargs)

    monkeypatch.setattr(favorites, "save_favorite", gated)
    return entered, proceed


def gate_store_unlink(monkeypatch):
    """persistence.telegram.unlink waits, on its worker thread, before it does anything.
    Returns (entered, proceed)."""
    entered, proceed = threading.Event(), threading.Event()
    real = store.unlink

    def gated(session, user_id, **options):
        entered.set()
        assert proceed.wait(TIMEOUT), "the test never let the unlink go on"
        return real(session, user_id, **options)

    monkeypatch.setattr(store, "unlink", gated)
    return entered, proceed


@contextmanager
def always_released(held: list):
    """Whatever the test does, every gate in `held` is opened on the way out: a test that
    fails with a worker still held would otherwise hang the shutdown and hide its own failure
    under a time-out."""
    try:
        yield held
    finally:
        for gate in held:
            gate.set()


async def yield_to_the_loop(rounds: int = 30) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


# --- A: the review's A -> B -> A interleaving ---------------------------------------------------


async def test_the_reviews_a_to_b_to_a_click_saves_nothing_and_discloses_no_title(tmp_path):
    """The exact scenario of the finding: the click is authorized as the first person, the tie
    becomes the second person's before the saving transaction runs (so the old code saved the
    second person's recipe), and it is the first person's again by the time the confirmation is
    considered (so the old delivery check passed). Nothing of the second person's recipe may
    reach the first person, and no favorite may be created."""
    async with telegram_app(tmp_path, backend_with(RAW_SECRET)) as t:
        second, data, other = await a_button_of_the_second_person(t)
        before = len(t.bot.sent)

        seam = interpose(t)
        arrived, release = seam.hold_before("click", 2)  # after authorization, before the save
        seam.after("click", 2, move_owner(t, t.user_id))  # B -> A, right after the save
        with always_released([release]):
            clicking = asyncio.create_task(t.click(MOTHER, data), name="click")
            await asyncio.wait_for(arrived.wait(), TIMEOUT)  # authorized as the first person
            await t.service.unlink(t.user_id)  # A -> B, while the click waits for its save
            await t.pair(MOTHER, client=other)
            assert [link.user_id for link in t.links()] == [second]
            release.set()
            await asyncio.wait_for(clicking, TIMEOUT)
        await t.settle()

        # Not saved for either person, by this click.
        assert t.chat.rows(FavoriteRecipe) == []
        assert (await t.chat.client.get("/api/favorites")).json()["favorites"] == []
        assert (await other.get("/api/favorites")).json()["favorites"] == []

        # No success, and no recipe title of the second person in the first person's chat.
        assert all(entry["text"] not in (tc.SAVED, tc.ALREADY_SAVED) for entry in t.bot.answered)
        assert t.bot.answered[-1]["text"] == tc.RECIPE_UNAVAILABLE
        assert not secret_disclosed(t, before)


# --- B: the tie changes before the transactional authorization ----------------------------------


async def test_a_stale_authorized_owner_cannot_save_under_the_newly_linked_owner(tmp_path):
    """The click is authorized as the first person, the tie then becomes the second person's
    before the saving transaction runs and stays there. The transaction resolves the second
    person; the save must not be made under that identity (nor under the stale one)."""
    async with telegram_app(tmp_path, backend_with(RAW_SECRET)) as t:
        second, data, other = await a_button_of_the_second_person(t)
        before = len(t.bot.sent)

        seam = interpose(t)
        arrived, release = seam.hold_before("click", 2)
        with always_released([release]):
            clicking = asyncio.create_task(t.click(MOTHER, data), name="click")
            await asyncio.wait_for(arrived.wait(), TIMEOUT)
            await t.service.unlink(t.user_id)  # A -> B, and it stays B
            await t.pair(MOTHER, client=other)
            assert [link.user_id for link in t.links()] == [second]
            release.set()
            await asyncio.wait_for(clicking, TIMEOUT)
        await t.settle()

        assert t.chat.rows(FavoriteRecipe) == []  # no favorite under the new owner
        assert (await other.get("/api/favorites")).json()["favorites"] == []
        assert all(entry["text"] not in (tc.SAVED, tc.ALREADY_SAVED) for entry in t.bot.answered)
        assert t.bot.answered[-1]["text"] == tc.RECIPE_UNAVAILABLE
        assert not secret_disclosed(t, before)


# --- C: the tie changes while the saving transaction is open ------------------------------------


async def test_the_tie_cannot_change_inside_the_save_and_the_confirmation_is_withheld(tmp_path, monkeypatch):
    """The guarantees around a save that is already under way: the tie cannot be taken apart in
    the middle of the transaction that resolves it (the open save holds the database's writer
    lock; PostgreSQL holds the link row: telegram.enrolled_user_for_telegram(hold=True)), and
    when it is taken apart right after the save the confirmation is withheld by the locked
    delivery instead of being sent to whoever the account belongs to now. The save itself was
    made for the owner the transaction resolved, which is the owner the click was authorized
    for."""
    async with telegram_app(tmp_path, backend_with(RAW_BORSH)) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Борщ?")
        [button] = save_buttons(t.bot.sent[-1])
        before = len(t.bot.sent)

        entered_save, proceed_save = gate_save_favorite(monkeypatch)
        entered_unlink, proceed_unlink = gate_store_unlink(monkeypatch)
        with always_released([proceed_save, proceed_unlink]):
            clicking = asyncio.create_task(
                t.click(MOTHER, button["callback_data"]), name="click"
            )
            await reached(entered_save)  # the transaction is open; its owner (the first person) is resolved
            unlinking = asyncio.ensure_future(t.service.unlink(t.user_id))
            await reached(entered_unlink)  # the unlink holds the binding lock; its delete waits below
            proceed_unlink.set()
            await yield_to_the_loop()
            assert not unlinking.done()  # it cannot remove the tie under the open save
            assert [link.user_id for link in t.links()] == [t.user_id]

            proceed_save.set()
            await asyncio.wait_for(asyncio.wait({clicking, unlinking}), TIMEOUT)
        await t.settle()

        # The save went to the owner the transaction resolved inside the open unit ...
        [kept] = t.chat.rows(FavoriteRecipe)
        assert (kept.user_id, kept.title) == (t.user_id, "Борщ")
        assert t.bot.answered[-1]["text"] == tc.SAVED
        # ... the unlink committed after it, and the confirmation was never sent.
        assert unlinking.result() is True and t.links() == []
        assert not any(
            call.get("text", "").startswith("Рецепт «Борщ»") for call in t.bot.sent[before:]
        )


# --- D: normal operation ------------------------------------------------------------------------


async def test_a_normal_click_saves_confirms_is_idempotent_and_opens_the_list(tmp_path):
    async with telegram_app(tmp_path, backend_with(RAW_BORSH)) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Борщ?")
        [button] = save_buttons(t.bot.sent[-1])
        assert button["text"] == tc.SAVE_BUTTON

        await t.click(MOTHER, button["callback_data"])

        [kept] = favorites_rows(t)
        assert (kept.user_id, kept.title, kept.source_recipe_index) == (t.user_id, "Борщ", 0)
        assert t.bot.answered[-1]["text"] == tc.SAVED
        confirmation = t.bot.sent[-1]
        assert confirmation["text"] == "Рецепт «Борщ» сохранен в избранное."
        assert save_buttons(confirmation) == [
            {"text": tc.FAVORITES_OPEN, "callback_data": "fl:0"}
        ]

        # A repeated click is the same favorite, answered as such, and adds no message.
        sent = len(t.bot.sent)
        await t.click(MOTHER, button["callback_data"])
        assert t.bot.answered[-1]["text"] == tc.ALREADY_SAVED
        assert len(t.bot.sent) == sent
        assert len(favorites_rows(t)) == 1

        # The confirmation's button opens the list, and the list opens the recipe.
        await t.click(MOTHER, "fl:0")
        [item] = save_buttons(t.bot.sent[-1])
        assert item["text"] == "Борщ"
        await t.click(MOTHER, item["callback_data"])
        assert t.bot.sent[-1]["text"].startswith("Борщ")
        assert "Ингредиенты" in t.bot.sent[-1]["text"]


# --- E: a failing transaction -------------------------------------------------------------------


async def test_a_failure_inside_the_saving_transaction_rolls_back_and_confirms_nothing(
    tmp_path, monkeypatch
):
    """The row is written, then the unit fails: the whole transaction is taken back, the click
    is answered with the failure notice, nothing is confirmed, and a later click still works."""
    async with telegram_app(tmp_path, backend_with(RAW_BORSH)) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Борщ?")
        [button] = save_buttons(t.bot.sent[-1])
        sent = len(t.bot.sent)

        real = favorites.save_favorite
        writes: list[favorites.SavedFavorite] = []

        def explode(*args, **kwargs):
            writes.append(real(*args, **kwargs))
            raise RuntimeError("the unit failed after the write")

        monkeypatch.setattr(favorites, "save_favorite", explode)
        await t.click(MOTHER, button["callback_data"])

        assert len(writes) == 1 and writes[0].created  # the insert really happened inside the unit
        assert t.chat.rows(FavoriteRecipe) == []  # ... and was rolled back with it
        assert t.bot.answered[-1]["text"] == tc.SAVE_FAILED
        assert len(t.bot.sent) == sent  # no confirmation
        assert not any("сохранен в избранное" in call.get("text", "") for call in t.bot.sent)

        # The database is not left in a failed state: the same button works when the unit does.
        monkeypatch.undo()
        await t.click(MOTHER, button["callback_data"])
        [kept] = favorites_rows(t)
        assert (kept.user_id, kept.title) == (t.user_id, "Борщ")
        assert t.bot.answered[-1]["text"] == tc.SAVED
        assert t.bot.sent[-1]["text"] == "Рецепт «Борщ» сохранен в избранное."
