from pathlib import Path


def test_state_dir_keeps_shipped_skills_and_adds_user_skills(monkeypatch, tmp_path):
    from core import runtime_paths

    monkeypatch.setenv("LIMEBOT_STATE_DIR", str(tmp_path / "state"))
    assert runtime_paths.get_state_dir() == (tmp_path / "state").resolve()
    assert runtime_paths.get_config_file() == (tmp_path / "state" / "limebot.json").resolve()
    assert runtime_paths.get_skills_dir() == (tmp_path / "state" / "skills").resolve()
    assert runtime_paths.PROJECT_DIR / "skills" in runtime_paths.get_skill_dirs()
    assert runtime_paths.get_skills_dir() in runtime_paths.get_skill_dirs()


def test_default_state_dir_remains_project_root(monkeypatch):
    from core import runtime_paths

    monkeypatch.delenv("LIMEBOT_STATE_DIR", raising=False)
    assert runtime_paths.get_state_dir() == runtime_paths.PROJECT_DIR
    assert runtime_paths.get_skills_dir() == (
        runtime_paths.PROJECT_DIR / ".limebot" / "skills"
    )
    assert runtime_paths.get_skill_dirs() == [
        Path(runtime_paths.PROJECT_DIR / "skills"),
        runtime_paths.PROJECT_DIR / ".limebot" / "skills",
    ]
