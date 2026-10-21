import sqlite3

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
    Store(path)  # opening it again changes nothing
