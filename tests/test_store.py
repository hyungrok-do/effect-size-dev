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
