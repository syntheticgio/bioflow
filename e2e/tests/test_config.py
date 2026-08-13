from e2e.backend import config


def test_defaults_when_no_file(tmp_path, monkeypatch):
    monkeypatch.delenv("BIOFLOW_BASE_URL", raising=False)
    monkeypatch.delenv("BIOFLOW_PROFILE", raising=False)
    cfg = config.load(str(tmp_path))
    assert cfg.base_url == "http://localhost:8000"
    assert cfg.profile == ""
    assert cfg.cleanup is False


def test_env_override_wins_over_file(tmp_path, monkeypatch):
    config.save(str(tmp_path), config.Config(base_url="http://file.example"))
    monkeypatch.setenv("BIOFLOW_BASE_URL", "http://env.example")
    cfg = config.load(str(tmp_path))
    assert cfg.base_url == "http://env.example"


def test_partial_file_fills_missing_from_defaults(tmp_path):
    (tmp_path / "config.json").write_text('{"base_url": "http://x.example"}')
    cfg = config.load(str(tmp_path))
    assert cfg.base_url == "http://x.example"
    assert cfg.profile == ""
    assert cfg.cleanup is False


def test_save_then_load_round_trips(tmp_path):
    cfg = config.Config(base_url="http://a.example", profile="abc123", cleanup=True)
    config.save(str(tmp_path), cfg)
    assert config.load(str(tmp_path)) == cfg


def test_corrupt_file_falls_back_to_defaults(tmp_path):
    (tmp_path / "config.json").write_text("{not json")
    cfg = config.load(str(tmp_path))
    assert cfg.base_url == "http://localhost:8000"


def test_data_dir_is_created():
    d = config.data_dir()
    assert d.endswith("/.hermes/plugins/bioflow-e2e/data")
