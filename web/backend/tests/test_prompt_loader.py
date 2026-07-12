"""Tests for PromptLoader."""

import os
import tempfile
import pytest
from web.backend.prompt_loader import PromptLoader


@pytest.fixture
def loader(tmp_path):
    """Create a PromptLoader with test prompt files."""
    (tmp_path / "greeting.md").write_text("Hello, {name}! You are a {role}.")
    (tmp_path / "simple.md").write_text("No variables here.")
    return PromptLoader(str(tmp_path))


def test_load_discovers_files(loader):
    assert sorted(loader.names) == ["greeting", "simple"]


def test_render_fills_variables(loader):
    result = loader.render("greeting", name="Alice", role="developer")
    assert result == "Hello, Alice! You are a developer."


def test_render_no_variables(loader):
    result = loader.render("simple")
    assert result == "No variables here."


def test_render_missing_template(loader):
    with pytest.raises(KeyError):
        loader.render("nonexistent")


def test_render_missing_variable(loader):
    with pytest.raises(KeyError):
        loader.render("greeting", name="Alice")  # missing 'role'


def test_get_raw(loader):
    assert "{name}" in loader.get_raw("greeting")


def test_reload(tmp_path):
    (tmp_path / "test.md").write_text("version 1")
    loader = PromptLoader(str(tmp_path))
    assert loader.render("test") == "version 1"
    (tmp_path / "test.md").write_text("version 2")
    loader.reload()
    assert loader.render("test") == "version 2"


def test_real_prompts_load():
    """Verify the actual prompts directory loads correctly."""
    prompts_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "prompts")
    loader = PromptLoader(prompts_dir)
    expected = {"soul", "analysis", "rolling-summary", "chat-insight", "curation", "summary"}
    assert expected.issubset(set(loader.names))
