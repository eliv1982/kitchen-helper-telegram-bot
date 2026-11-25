"""The one greeting every surface shows, in the words the Owner approved (F01).

Fedya speaks first on three surfaces: the Web login lead, a new empty conversation, and
Telegram /start. All three show these sentences; only the layout differs -- one lead
paragraph on the login card, three paragraphs in an empty conversation and in Telegram.
The functional help behind the «Помощь» button is a different text on purpose and is not
composed here: the greeting says who speaks and what it offers, the help says how to use it.

The browser ships no Python, so chat-logic.js keeps its own literal, and
tests/test_assistant_identity.py compares it with this module. The name comes from the
domain-neutral identity module, as everywhere else. No I/O, no provider or interface import:
the Web shell and the Telegram bot can both read it without pulling the other side in.
"""

from house_helper.core.assistant_identity import ASSISTANT_NAME

GREETING_PARTS = (
    f"Привет! Я {ASSISTANT_NAME} — твой домашний помощник.",
    "Помогу придумать, что приготовить, подобрать рецепт, заменить ингредиенты, "
    "пересчитать порции и составить список покупок по рецепту.",
    "Расскажи, что тебе нужно, — разберемся вместе.",
)

# The same words with the paragraph break Telegram and the empty conversation use.
GREETING_TEXT = "\n\n".join(GREETING_PARTS)
