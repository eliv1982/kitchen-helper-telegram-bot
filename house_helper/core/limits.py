"""Limits shared by every channel, in a module that imports nothing.

The Telegram bot (context_manager, chat_service) and the web chat (house_helper.web)
read the same numbers from here, so the two cannot drift apart. The old names
(context_manager.MAX_HISTORY_MESSAGES, chat_service.DEFAULT_MAX_INPUT_LENGTH) are
kept and re-export these.
"""

# The most characters one incoming message may have, independent of any transport's
# own limit (Telegram's is ~4096). Keeps a single request's cost, and the history it
# adds, predictable whatever a user sends.
MAX_INPUT_LENGTH = 2000

# How many messages of a conversation the AI is shown at most. Telegram keeps this
# many completed messages and sends them with the pending one on top (so up to one
# more); the web chat sends this many INCLUDING the message being answered (see
# house_helper.persistence.conversations.build_context). Must stay even for the
# Telegram trimming: it drops whole user/assistant pairs from the front.
MAX_HISTORY_MESSAGES = 20

# The default cap on the length of one answer, in tokens (MAX_TOKENS in the
# environment overrides it for both channels). It was 600, which cuts a complete
# recipe in Russian off in the middle; Cyrillic text costs roughly one token per
# three characters, so 1000 tokens are about 3000 characters, still inside
# Telegram's 4096-character message.
DEFAULT_MAX_OUTPUT_TOKENS = 1000
