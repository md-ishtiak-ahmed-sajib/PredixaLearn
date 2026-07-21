from __future__ import annotations

import sqlite3
from dataclasses import replace

from app.core import data_migration
from app.core.config import get_settings


def test_default_runtime_migration_copies_legacy_data_without_removing_it(tmp_path, monkeypatch):
    local_app_data = tmp_path / "LocalAppData"
    legacy_root = tmp_path / "legacy-project"
    legacy_output = legacy_root / "output"
    legacy_history = legacy_root / "app" / "history"
    legacy_logs = legacy_root / "logs"
    legacy_output.mkdir(parents=True)
    legacy_history.mkdir(parents=True)
    legacy_logs.mkdir(parents=True)
    (legacy_output / "report.md").write_text("report", encoding="utf-8")
    (legacy_logs / "service.log").write_text("safe log", encoding="utf-8")
    with sqlite3.connect(legacy_history / "history.sqlite3") as connection:
        connection.execute("CREATE TABLE marker (value TEXT)")
        connection.execute("INSERT INTO marker VALUES ('copied')")

    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setattr(data_migration, "PROJECT_ROOT", legacy_root)
    target_root = local_app_data / "PredixaLearn"
    settings = replace(
        get_settings(),
        output_dir=target_root / "output",
        upload_dir=target_root / "uploads",
        history_dir=target_root / "history",
        history_db_path=target_root / "history" / "history.sqlite3",
        log_dir=target_root / "logs",
    )

    result = data_migration.migrate_legacy_runtime_data(settings)

    assert result["status"] == "completed"
    assert (settings.output_dir / "report.md").read_text(encoding="utf-8") == "report"
    assert (settings.log_dir / "service.log").read_text(encoding="utf-8") == "safe log"
    with sqlite3.connect(settings.history_db_path) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == "copied"
    assert (legacy_output / "report.md").is_file()
    assert (legacy_history / "history.sqlite3").is_file()
    assert (legacy_logs / "service.log").is_file()
    assert data_migration.migrate_legacy_runtime_data(settings) == result


def test_rebrand_migration_copies_the_previous_local_data_root_without_removing_it(
    tmp_path, monkeypatch
):
    local_app_data = tmp_path / "LocalAppData"
    prior_root = local_app_data / ("Civi" + "Scribe OCR")
    (prior_root / "output").mkdir(parents=True)
    (prior_root / "history").mkdir(parents=True)
    (prior_root / "logs").mkdir(parents=True)
    (prior_root / "output" / "prior.md").write_text("prior output", encoding="utf-8")
    (prior_root / "logs" / "prior.log").write_text("prior log", encoding="utf-8")
    with sqlite3.connect(prior_root / "history" / "history.sqlite3") as connection:
        connection.execute("CREATE TABLE marker (value TEXT)")
        connection.execute("INSERT INTO marker VALUES ('prior data')")

    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    new_root = local_app_data / "PredixaLearn"
    settings = replace(
        get_settings(),
        output_dir=new_root / "output",
        upload_dir=new_root / "uploads",
        history_dir=new_root / "history",
        history_db_path=new_root / "history" / "history.sqlite3",
        log_dir=new_root / "logs",
    )

    result = data_migration.migrate_legacy_runtime_data(settings)

    assert result["version"] == 2
    assert (settings.output_dir / "prior.md").read_text(encoding="utf-8") == "prior output"
    assert (settings.log_dir / "prior.log").read_text(encoding="utf-8") == "prior log"
    with sqlite3.connect(settings.history_db_path) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == "prior data"
    assert (prior_root / "output" / "prior.md").is_file()
