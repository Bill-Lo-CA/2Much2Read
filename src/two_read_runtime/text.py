from __future__ import annotations

import unicodedata

# Newline and tab are the only control characters any renderer here wants to keep: they are layout
# in a Discord code block and in the agenda table. Callers that render a single line pass keep=""
# instead.
SAFE_WHITESPACE = "\n\t"


def is_inert(character: str, *, keep: str = SAFE_WHITESPACE) -> bool:
    """One definition of text that cannot steer a terminal or a Discord renderer.

    Three call sites need this and three copies had drifted: the Discord sanitiser and the agenda
    cell agreed, and the live progress reporter did not - it rejected only ord < 32 and DEL, which
    lets the whole C1 range through. U+009B is the C1 CSI and most terminal emulators act on it
    exactly as they act on ESC [, so a Gmail subject or a Hacker News title - both of which reach
    the progress line by design - could move the cursor and repaint the screen.

    Cf covers the bidirectional overrides and zero-width joiners, which reorder or hide text
    without being control characters at all.

    Callers keep their own assembly, because what they do with a rejected character differs: the
    two renderers drop it, and the progress line substitutes a space so words do not run together.
    """
    if character in keep:
        return True
    code = ord(character)
    return code >= 0x20 and not 0x7F <= code <= 0x9F and unicodedata.category(character) != "Cf"
