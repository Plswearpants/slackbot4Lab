"""Load an authoring-time skill file into the answering prompt.

The file is a standard ``SKILL.md`` — YAML frontmatter plus a Markdown body — so
it stays readable as a skill in its own right while the body is what actually
reaches the model. Frontmatter is stripped rather than sent: ``name`` and
``description`` exist to help a human or a tool find the skill, and spending
prompt tokens on them would buy nothing.

Reloaded when the file's mtime changes, so editing the skill takes effect on the
next question instead of requiring a restart. Iterating on prompt guidance is
exactly the kind of thing you want a tight loop on.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)


def strip_frontmatter(text: str) -> str:
    return _FRONTMATTER_RE.sub("", text, count=1).strip()


class Skill:
    """A skill file whose body is injected into the system prompt."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._body = ""
        self._mtime: float | None = None
        self.reload()

    def reload(self) -> bool:
        """Re-read if the file changed. True when the body changed."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            if self._body:
                logger.warning("Skill file %s disappeared; continuing without it", self.path)
            self._body, self._mtime = "", None
            return False

        if self._mtime == mtime:
            return False

        try:
            body = strip_frontmatter(self.path.read_text())
        except OSError:
            logger.warning("Could not read skill %s", self.path, exc_info=True)
            return False

        changed = body != self._body
        self._body, self._mtime = body, mtime
        if changed:
            logger.info("Loaded skill %s (%d chars)", self.path.name, len(body))
        return changed

    @property
    def body(self) -> str:
        self.reload()
        return self._body

    def __bool__(self) -> bool:
        return bool(self.body)
