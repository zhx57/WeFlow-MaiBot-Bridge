from pathlib import Path

import pytest

from weflow_maibot_bridge.config import ConfigError, load_config


def test_config_resolves_paths_and_env_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[weflow]
access_token = "file-secret"
[bridge]
bot_nicknames = ["Mai"]
[media]
directory = "runtime/media"
[storage]
database = "runtime/state.db"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("WEFLOW_ACCESS_TOKEN", "environment-secret")
    config = load_config(path)
    assert config.weflow.access_token == "environment-secret"
    assert config.media.directory == (tmp_path / "runtime/media").resolve()
    assert config.storage.database == (tmp_path / "runtime/state.db").resolve()
    assert config.bridge.bot_nicknames == ("Mai",)


def test_config_rejects_unknown_and_invalid_mode(tmp_path: Path) -> None:
    unknown = tmp_path / "unknown.toml"
    unknown.write_text('[weflow]\naccess_token="x"\nnope=1\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="未知字段"):
        load_config(unknown)
    invalid = tmp_path / "invalid.toml"
    invalid.write_text('[weflow]\naccess_token="x"\n[bridge]\ngroup_mode="sometimes"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="group_mode"):
        load_config(invalid)
    wrong_type = tmp_path / "wrong-type.toml"
    wrong_type.write_text('[weflow]\naccess_token="x"\n[bridge]\nqueue_size="many"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="类型错误"):
        load_config(wrong_type)
