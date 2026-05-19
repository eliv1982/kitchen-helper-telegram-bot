import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.7"))
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "600"))


def validate_config() -> list[str]:
    """Return list of missing required settings."""
    errors: list[str] = []
    if not BOT_TOKEN:
        errors.append("BOT_TOKEN is not set")
    if not OPENAI_API_KEY:
        errors.append("OPENAI_API_KEY is not set")
    return errors
