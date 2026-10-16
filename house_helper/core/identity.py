from dataclasses import dataclass


@dataclass(frozen=True)
class ConversationKey:
    """Identifies one conversation: the unit that owns a history and whose
    processing is serialized.

    `channel` names the transport the conversation lives on ("telegram") and
    `subject` identifies the conversation within that channel, in whatever form
    the channel uses -- always a string, so keys from different channels
    compare and hash the same way. Two keys are the same conversation exactly
    when both parts are equal, so the same subject on two channels never
    collides.

    This is conversation identity only. It says nothing about who the person
    is: a future application-user identity is a separate concept and must not
    be folded into this key.
    """

    channel: str
    subject: str

    def __post_init__(self) -> None:
        # A subject built from a raw channel id (an int) would silently never
        # match its str(...) twin, splitting one conversation into two. Fail
        # at construction instead.
        for name in ("channel", "subject"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(
                    f"ConversationKey.{name} must be a str, got {type(value).__name__}"
                )
            if not value:
                raise ValueError(f"ConversationKey.{name} must not be empty")
