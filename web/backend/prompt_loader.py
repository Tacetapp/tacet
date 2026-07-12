"""Load and render prompt templates from markdown files."""

from __future__ import annotations

import logging
import os
from glob import glob

logger = logging.getLogger(__name__)


class PromptLoader:
    """Loads .md prompt templates and renders them with str.format()."""

    def __init__(self, prompts_dir: str):
        self._dir = prompts_dir
        self._cache: dict[str, str] = {}
        self._load_all()

    def _load_all(self):
        for md_file in sorted(glob(os.path.join(self._dir, "*.md"))):
            name = os.path.splitext(os.path.basename(md_file))[0]
            with open(md_file, encoding="utf-8") as f:
                self._cache[name] = f.read()
        logger.info(f"Loaded {len(self._cache)} prompt templates: {', '.join(sorted(self._cache))}")

    def render(self, template_name: str, **kwargs) -> str:
        """Render a prompt template with the given variables."""
        template = self._cache[template_name]
        return template.format(**kwargs)

    def get_raw(self, template_name: str) -> str:
        """Return the raw template without rendering."""
        return self._cache[template_name]

    @property
    def names(self) -> list[str]:
        return sorted(self._cache.keys())

    def reload(self):
        """Reload all templates from disk (useful for dev hot-reload)."""
        self._cache.clear()
        self._load_all()
