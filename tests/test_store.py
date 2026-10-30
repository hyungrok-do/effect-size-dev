import os
import sqlite3
import stat

import pytest

from esf.store import Store


def test_an_older_database_gains_the_new_columns(tmp_path):
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE projects (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, paragraph TEXT NOT NULL, spec TEXT,
            status TEXT NOT NULL, progress TEXT, error TEXT, search_plan TEXT);
        CREATE TABLE papers (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, source TEXT NOT NULL,
            ext_id TEXT NOT NULL, pmid TEXT, pmcid TEXT, doi TEXT, nct_ids TEXT, title TEXT, year TEXT, journal TEXT,
            pubtypes TEXT, abstract TEXT, found_by TEXT, status TEXT NOT NULL, screen TEXT, fulltext_kind TEXT,
            fulltext_url TEXT, extraction TEXT, extracted_by TEXT, verification TEXT, escalation TEXT, error TEXT,
            UNIQUE (project_id, source, ext_id));
        INSERT INTO projects VALUES ('p1', '2026-01-01T00:00:00+00:00', 'An older search.', NULL, 'done', NULL, NULL, NULL);
    """)
    db.commit()
    db.close()

    store = Store(path)
    assert store.project("p1")["search_log"] is None
    store.update_project("p1", search_log=[{"level": "primary", "hits": 4}])
    store.add_paper("p1", source="pubmed", ext_id="1", found_by=["x"], found_in=["primary"], status="candidate")
    assert store.project("p1")["search_log"] == [{"level": "primary", "hits": 4}]
    assert store.papers("p1")[0]["found_in"] == ["primary"]
    # The manager's reading of a paper's context: none for a paper extracted before it, kept as JSON after.
    paper = store.papers("p1")[0]
    assert paper["context"] is None
    store.update_paper(paper["id"], context={"status": "error", "error": "overloaded"})
    assert store.paper(paper["id"])["context"] == {"status": "error", "error": "overloaded"}
    Store(path)  # opening it again changes nothing
    assert "context" in {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(papers)")}


def test_an_older_specification_is_upgraded():
    from esf.schemas import SearchSpec, upgrade_spec

    old = {
        "question": "Q?", "population": "Students", "intervention": "App", "comparator": "Waitlist",
        "outcomes": ["PHQ-9", "GAD-7"], "timepoint": "12 weeks", "design": "parallel_rct", "outcome_type": "continuous",
        "statistics_needed": ["Mean and SD of PHQ-9 per arm"], "stated_values": [], "inclusion_criteria": ["RCTs"],
        "exclusion_criteria": ["Animals"], "search_terms": ["mindfulness", "PHQ-9"], "open_questions": ["Which scale?"],
    }
    spec = SearchSpec.model_validate(upgrade_spec(old))
    assert spec.population.description == "Students" and spec.comparator.kind == "other"
    assert [(o.name, o.role, o.type, o.timepoint) for o in spec.outcomes] == [
        ("PHQ-9", "primary", "continuous", "12 weeks"), ("GAD-7", "secondary", "continuous", "12 weeks"),
    ]
    assert spec.statistics_needed[0].purpose == "Mean and SD of PHQ-9 per arm" and spec.statistics_needed[0].outcome == "PHQ-9"
    assert spec.search_terms[0].terms == ["mindfulness", "PHQ-9"]
    assert upgrade_spec(spec.model_dump()) == spec.model_dump()  # a current one is left as it is


def test_usage_since_a_search_started_keeps_the_reading_of_the_problem(tmp_path, monkeypatch):
    store = Store(tmp_path / "test.sqlite3")
    project_id = store.create_project("A search run twice, on two providers.")
    tokens = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    for at, step, model in (("2026-01-01T00:00:00+00:00", "spec", "claude-opus-5-5"),
                            ("2026-01-01T00:01:00+00:00", "screen", "claude-haiku-5-5"),
                            ("2026-01-02T00:01:00+00:00", "screen", "gpt-6-luna")):
        monkeypatch.setattr("esf.store.now", lambda at=at: at)
        store.add_usage(project_id, step, model, tokens)
    assert {(u["step"], u["model"]) for u in store.usage(project_id)} == {
        ("spec", "claude-opus-5-5"), ("screen", "claude-haiku-5-5"), ("screen", "gpt-6-luna")}
    # The second search started on the 2nd: the first one's workers did none of its work.
    assert [(u["step"], u["model"], u["calls"]) for u in store.usage(project_id, since="2026-01-02T00:00:00+00:00")] == [
        ("spec", "claude-opus-5-5", 1), ("screen", "gpt-6-luna", 1)]


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX")
def test_the_data_folder_is_its_owners_alone(tmp_path):
    Store(tmp_path / "data" / "esf.sqlite3")
    assert stat.S_IMODE((tmp_path / "data").stat().st_mode) == 0o700
    # A folder made before (by an older version), open to everyone, is closed to them.
    for kept in ("esf.sqlite3", "chatgpt.json", "esf.sqlite3-wal", ".chatgpt.json.x1y2.tmp", "projects"):
        older = tmp_path / f"older-{kept}"
        older.mkdir()
        if kept == "projects":
            (older / kept).mkdir()
        else:
            (older / kept).write_text("")
        older.chmod(0o755)
        Store(older / "esf.sqlite3")
        assert stat.S_IMODE(older.stat().st_mode) == 0o700, kept


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX")
def test_a_folder_that_is_not_the_apps_own_keeps_its_mode(tmp_path, caplog, monkeypatch):
    # ESF_DATA_DIR may name a home folder, or one shared with a group: its mode is its owner's to choose.
    for mode in (0o755, 0o770):
        shared = tmp_path / f"shared-{mode:o}"
        shared.mkdir()
        (shared / "notes.txt").write_text("someone else's files")
        shared.chmod(mode)
        # Once the app has put its database there, the folder is still not the app's alone: it stays as it is
        # at every start, and every start says so.
        for start in range(2):
            caplog.clear()
            Store(shared / "esf.sqlite3")
            assert stat.S_IMODE(shared.stat().st_mode) == mode, start
            assert "open to other users of this computer" in caplog.text and str(shared) in caplog.text
    # A group-shared folder keeps its setgid bit too.
    group = tmp_path / "group"
    group.mkdir()
    (group / "notes.txt").write_text("")
    group.chmod(0o2770)
    for _ in range(2):
        Store(group / "esf.sqlite3")
    assert stat.S_IMODE(group.stat().st_mode) == 0o2770
    # The default data folder is the app's own, whatever it holds.
    default = tmp_path / "default"
    default.mkdir()
    (default / "notes.txt").write_text("")
    default.chmod(0o755)
    monkeypatch.setattr("esf.store.Settings.data_dir", default)
    Store(default / "esf.sqlite3")
    assert stat.S_IMODE(default.stat().st_mode) == 0o700
