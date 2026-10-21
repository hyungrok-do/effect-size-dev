"""Settings, read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value else default


def _int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value else default


@dataclass(frozen=True)
class Settings:
    # Opus plans and adjudicates; Haiku screens and extracts.
    manager_model: str = "claude-opus-5-5"
    worker_model: str = "claude-haiku-5-5"
    data_dir: Path = REPO_ROOT / "data"
    web_dir: Path = REPO_ROOT / "web"
    # NCBI and Unpaywall ask callers to identify themselves; Unpaywall requires it.
    contact_email: str | None = None
    ncbi_api_key: str | None = None
    # Share of papers that pass every check but still go to Opus, to measure Haiku's silent error rate.
    audit_fraction: float = 0.1
    per_query_limit: int = 100
    max_candidates: int = 300
    # After screening, the references of the included reviews are added: from this many reviews,
    # at most this many new records.
    max_reviews_followed: int = 10
    max_cited: int = 150
    max_extractions: int = 60
    worker_concurrency: int = 8
    search_max_iterations: int = 25

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            manager_model=os.environ.get("ESF_MANAGER_MODEL", cls.manager_model),
            worker_model=os.environ.get("ESF_WORKER_MODEL", cls.worker_model),
            data_dir=Path(os.environ.get("ESF_DATA_DIR", str(cls.data_dir))),
            web_dir=Path(os.environ.get("ESF_WEB_DIR", str(cls.web_dir))),
            contact_email=os.environ.get("ESF_CONTACT_EMAIL") or None,
            ncbi_api_key=os.environ.get("NCBI_API_KEY") or None,
            audit_fraction=_float("ESF_AUDIT_FRACTION", cls.audit_fraction),
            per_query_limit=_int("ESF_PER_QUERY_LIMIT", cls.per_query_limit),
            max_candidates=_int("ESF_MAX_CANDIDATES", cls.max_candidates),
            max_reviews_followed=_int("ESF_MAX_REVIEWS_FOLLOWED", cls.max_reviews_followed),
            max_cited=_int("ESF_MAX_CITED", cls.max_cited),
            max_extractions=_int("ESF_MAX_EXTRACTIONS", cls.max_extractions),
            worker_concurrency=_int("ESF_WORKER_CONCURRENCY", cls.worker_concurrency),
            search_max_iterations=_int("ESF_SEARCH_MAX_ITERATIONS", cls.search_max_iterations),
        )
