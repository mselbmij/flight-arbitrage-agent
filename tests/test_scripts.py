"""Phase 6: scripts/ (ARCHITECTURE.md section 7: cron, backup). Shell syntax, the crontab lines,
and backup_db.sh run for real against a tmp tree (the script cd's to its own repo root, so it is
copied into <tmp>/scripts/ and everything it touches is under <tmp>)."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from src.db import Database
from tests.conftest import REPO, make_run

SCRIPTS = REPO / "scripts"
SH = shutil.which("sh") or "/bin/sh"
SH_FILES = sorted(SCRIPTS.glob("*.sh"))
TOOLS = ("dirname", "sed", "tail", "mkdir", "date", "rm", "mv", "ls", "sort", "python3")


def test_the_expected_scripts_exist_and_are_executable():
    assert {p.name for p in SH_FILES} == {"backup_db.sh", "install.sh"}
    for p in SH_FILES:
        assert os.access(p, os.X_OK), p


@pytest.mark.parametrize("script", SH_FILES, ids=lambda p: p.name)
def test_shell_syntax(script: Path):
    subprocess.run(["sh", "-n", str(script)], check=True)
    assert "set -eu" in script.read_text()
    assert script.read_text().startswith("#!/bin/sh")


def test_crontab_example_has_the_section_7_lines():
    lines = [
        ln for ln in (SCRIPTS / "crontab.example").read_text().splitlines() if ln[:1].isdigit()
    ]
    poll = next(ln for ln in lines if "fares poll" in ln)
    assert poll.startswith("50 6,17 * * *") and "sleep" in poll and "--trigger cron" in poll
    assert "RANDOM" not in poll  # cron runs /bin/sh; $RANDOM is a bashism
    backup = next(ln for ln in lines if "backup_db.sh" in ln)
    assert backup.startswith("0 3 * * *")
    health = next(ln for ln in lines if "fares health" in ln)
    assert health.startswith("0 9,21 * * *")
    assert len(lines) == 3


# -- backup_db.sh --------------------------------------------------------------------------- #


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPTS / "backup_db.sh", tmp_path / "scripts" / "backup_db.sh")
    (tmp_path / "data").mkdir()
    with Database(tmp_path / "data" / "fares.db") as d:
        d.migrate()
        d.start_run(make_run("run-1"))
        d.start_run(make_run("run-2"))
    return tmp_path


def run_backup(root: Path, *, path: str | None = None, **env: str):
    full = {k: v for k, v in os.environ.items() if k not in ("FARES_DB_PATH", "BACKUP_KEEP")}
    full.update(env)
    if path is not None:
        full["PATH"] = path
    return subprocess.run(
        [SH, str(root / "scripts" / "backup_db.sh")], env=full, capture_output=True, text=True
    )


def backups(root: Path) -> list[Path]:
    return sorted((root / "data" / "backups").glob("fares-*.db"))


def run_count(db: Path) -> int:
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    finally:
        con.close()


def no_sqlite3_path(root: Path) -> str:
    """A PATH with the tools the script needs and no `sqlite3` CLI: forces the Python fallback."""
    bin_dir = root / "bin"
    bin_dir.mkdir()
    for tool in TOOLS:
        found = shutil.which(tool)
        assert found, tool
        (bin_dir / tool).symlink_to(found)
    return str(bin_dir)


def test_backup_writes_a_timestamped_copy_with_all_the_rows(tree: Path):
    res = run_backup(tree)
    assert res.returncode == 0, res.stderr
    [b] = backups(tree)
    assert b.name.startswith("fares-") and len(b.name) == len("fares-20260101-0000.db")
    assert run_count(b) == 2
    assert not list((tree / "data" / "backups").glob("*.part"))


def test_backup_falls_back_to_python_when_the_sqlite3_cli_is_missing(tree: Path):
    res = run_backup(tree, path=no_sqlite3_path(tree))
    assert res.returncode == 0, res.stderr
    [b] = backups(tree)
    assert run_count(b) == 2


def test_backup_keeps_the_newest_14(tree: Path):
    bdir = tree / "data" / "backups"
    bdir.mkdir()
    for i in range(16):
        (bdir / f"fares-2020010{i // 10}-{i:04d}.db").write_bytes(b"old")
    res = run_backup(tree)
    assert res.returncode == 0, res.stderr
    kept = [p.name for p in backups(tree)]
    assert len(kept) == 14
    assert kept[-1] > "fares-2021"  # sorted ascending: today's backup is the last kept
    assert "fares-20200100-0000.db" not in kept and "fares-20200100-0001.db" not in kept


def test_backup_keep_is_configurable(tree: Path):
    bdir = tree / "data" / "backups"
    bdir.mkdir()
    for i in range(5):
        (bdir / f"fares-2020010{i}-0000.db").write_bytes(b"old")
    assert run_backup(tree, BACKUP_KEEP="2").returncode == 0
    assert len(backups(tree)) == 2


def test_backup_fails_non_zero_when_the_database_is_missing(tmp_path: Path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPTS / "backup_db.sh", tmp_path / "scripts" / "backup_db.sh")
    res = run_backup(tmp_path)
    assert res.returncode != 0 and "no database" in res.stderr
    assert backups(tmp_path) == []


def test_backup_fails_non_zero_and_leaves_no_partial_file_on_a_corrupt_database(tree: Path):
    (tree / "data" / "fares.db").write_bytes(b"this is not a sqlite database" * 100)
    res = run_backup(tree, path=no_sqlite3_path(tree))
    assert res.returncode != 0
    assert backups(tree) == []
    assert not list((tree / "data" / "backups").glob("*.part"))


def test_backup_honours_fares_db_path_from_the_environment(tree: Path):
    shutil.move(tree / "data" / "fares.db", tree / "data" / "other.db")
    assert run_backup(tree).returncode != 0
    assert run_backup(tree, FARES_DB_PATH="data/other.db").returncode == 0
    assert run_count(backups(tree)[0]) == 2


def test_backup_reads_fares_db_path_from_dot_env_when_unset(tree: Path):
    shutil.move(tree / "data" / "fares.db", tree / "data" / "other.db")
    (tree / ".env").write_text('NTFY_TOPIC=x\nFARES_DB_PATH="data/other.db"   # live\n')
    res = run_backup(tree)
    assert res.returncode == 0, res.stderr
    assert run_count(backups(tree)[0]) == 2


def test_backup_dir_is_configurable(tree: Path):
    res = run_backup(tree, BACKUP_DIR="elsewhere")
    assert res.returncode == 0, res.stderr
    assert len(list((tree / "elsewhere").glob("fares-*.db"))) == 1
