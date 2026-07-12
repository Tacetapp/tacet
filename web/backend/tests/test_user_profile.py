"""Tests for the markdown user-profile loader."""

from web.backend import user_profile as up


def test_default_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("USER_PROFILE_PATH", str(tmp_path / "nope.md"))
    assert up.load_user_profile() == up.DEFAULT_PROFILE


def test_default_when_blank(tmp_path, monkeypatch):
    p = tmp_path / "profile.md"
    p.write_text("   \n\n", encoding="utf-8")
    monkeypatch.setenv("USER_PROFILE_PATH", str(p))
    assert up.load_user_profile() == up.DEFAULT_PROFILE


def test_reads_markdown(tmp_path, monkeypatch):
    p = tmp_path / "profile.md"
    p.write_text("# My Profile\n\n## Role\nProduct manager at Acme.\n", encoding="utf-8")
    monkeypatch.setenv("USER_PROFILE_PATH", str(p))
    text = up.load_user_profile()
    assert "Product manager at Acme." in text
    assert text.startswith("# My Profile")


def test_env_path_precedence(tmp_path, monkeypatch):
    p = tmp_path / "custom.md"
    p.write_text("custom profile", encoding="utf-8")
    monkeypatch.setenv("USER_PROFILE_PATH", str(p))
    assert up.profile_path() == str(p)
    assert up.load_user_profile() == "custom profile"


def test_explicit_path_arg_overrides_env(tmp_path, monkeypatch):
    env_file = tmp_path / "env.md"
    env_file.write_text("env profile", encoding="utf-8")
    arg_file = tmp_path / "arg.md"
    arg_file.write_text("arg profile", encoding="utf-8")
    monkeypatch.setenv("USER_PROFILE_PATH", str(env_file))
    assert up.load_user_profile(str(arg_file)) == "arg profile"
