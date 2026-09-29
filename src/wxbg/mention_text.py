"""Build the user's requested literal @username text without using remarks."""

from .policy import AdapterError, validate_text


def build_at_username_text(username: str, text: str = "") -> str:
    if (type(username) is not str or not 1 <= len(username) <= 128
            or username != username.strip() or "@" in username
            or "\ufffc" in username
            or any(ord(char) < 32 or ord(char) == 127 for char in username)):
        raise AdapterError("invalid_username")
    if type(text) is not str or (text and not text.strip()):
        raise AdapterError("invalid_text")
    validate_text(text, allow_empty=True)
    rendered = "@" + username + ((" " + text) if text else "")
    validate_text(rendered)
    return rendered
