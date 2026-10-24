"""The search-and-extract pipeline.

    spec (Opus) -> search plan (Opus + tools) -> run the levels from the top down (code) -> screen (Haiku)
    -> add what the included reviews cite (code) -> screen those (Haiku)
    -> full text (code) -> extract (Haiku) -> verify (code) -> adjudicate escalations (Opus) -> review (human)
"""

from __future__ import annotations

import copy
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from itertools import zip_longest
from pathlib import Path
from typing import Any

import anthropic

from . import prompts
from .chatgpt import ChatGPTError
from .config import Settings
from .evidence import build_evidence, decisions_basis, overlap_records
from .fulltext import FullText, get_fulltext
from .llm import LLM, ModelDeclined, ModelTruncated, describe_error, usage_dict
from .openai_llm import OpenAIError
from .schemas import LEVEL_NAMES, LEVELS, PaperExtraction, SearchPlan, SearchSpec, upgrade_spec
from .sources import Record, SourceError, Sources, europepmc_pmid, europepmc_record, trial_record
from .store import Store
from .verify import Verification, escalation_reasons, in_audit_sample, verify

log = logging.getLogger(__name__)

NCT = re.compile(r"^NCT\d{8}$", re.IGNORECASE)
# Publication types that are not studies; a review's reference list is full of them.
NOT_STUDIES = {
    "Review", "Systematic Review", "Meta-Analysis", "Editorial", "Comment", "Letter", "News",
    "Guideline", "Practice Guideline", "Published Erratum", "Retraction of Publication",
}


# Errors after which every later model call fails the same way: a refused key or sign-in, a model the
# account may not use, a plan's share used up. The run stops and says why, instead of sending a
# request for every remaining record and paper.
STOPPING_CODES = frozenset({
    "signed_out", "reauth_required", "registration_rejected", "invalid_api_key", "insufficient_quota", "model_not_found",
    "subscription_sharing_usage_limit_exceeded", "subscription_sharing_user_not_eligible", "subscription_sharing_invalid_user",
    "chatpass_v2_scope_not_authorized", "chatpass_v2_invalid_authorization_context",
})


def stops_the_run(exc: BaseException) -> bool:
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError)):
        return True
    if isinstance(exc, (OpenAIError, ChatGPTError)):
        code = exc.code.replace("subscription_sharing_v2_", "subscription_sharing_")
        return code in STOPPING_CODES or (isinstance(exc, OpenAIError) and exc.status in (401, 403))
    return False


class Toolbox:
    """What the search agent can call while it explores."""

    def __init__(self, sources: Sources):
        self.sources = sources

    def count_pubmed(self, queries: list[str]) -> str:
        lines = []
        for number, query in enumerate(queries, start=1):
            try:
                hits = f"{self.sources.pubmed.search(query, 0)[0]:,}"
            except SourceError as exc:
                hits = f"error: {exc}"
            lines.append(f"#{number} {hits} | {query}")
        return "\n".join(lines)

    def preview_pubmed(self, query: str, max_results: int) -> str:
        count, ids = self.sources.pubmed.search(query, max_results)
        lines = [f"Total hits: {count}"]
        for r in self.sources.pubmed.summaries(ids):
            lines.append(f"{r.pmid} | {r.year} | {', '.join(r.pubtypes)} | {r.title}")
        return "\n".join(lines)

    def preview_europepmc(self, query: str, max_results: int) -> str:
        total, results = self.sources.europepmc.search(query, max_results, abstracts=False)
        lines = [f"Total hits: {total}"]
        for r in results:
            types = r.get("pubType") or ", ".join((r.get("pubTypeList") or {}).get("pubType") or [])
            lines.append(f"{r['id']} | {r.get('pubYear', '')} | {r['source']} | {types} | {r.get('title', '')}")
        return "\n".join(lines)

    def preview_trials(self, query: str, max_results: int) -> str:
        total, studies = self.sources.trials.search(query, max_results, with_results=True)
        lines = [f"Total studies with posted results: {total}"]
        for study in studies:
            r = trial_record(study)
            status = study.get("protocolSection", {}).get("statusModule", {}).get("overallStatus", "")
            lines.append(f"{r.ext_id} | {status} | {r.year} | {r.title}")
        return "\n".join(lines)


def _record_text(paper: dict) -> str:
    kind = "Registry record" if paper["source"] == "clinicaltrials" else "Article"
    parts = [
        f"{kind}: {paper['title']}",
        f"Year: {paper.get('year') or 'unknown'}; source: {paper.get('journal') or ''}",
        f"Publication types: {', '.join(paper.get('pubtypes') or []) or 'unknown'}",
    ]
    if "full_text" in (paper.get("found_in") or []):
        parts.append(prompts.FOUND_BY_FULL_TEXT)
    parts += ["", paper.get("abstract") or "(no abstract available)"]
    return "\n".join(parts)


def previous_summary(summary: dict | None) -> dict | None:
    """The finished summary to keep while another is written: the summary itself if it is done, or
    the one a running summary kept."""
    summary = summary or {}
    if summary.get("status") == "done":
        return {k: v for k, v in summary.items() if k != "last_error"}
    return summary.get("previous")


class Pipeline:
    def __init__(self, store: Store, llm: LLM, sources: Sources, settings: Settings):
        self.store = store
        self.llm = llm
        self.sources = sources
        self.settings = settings

    def using(self, llm: LLM) -> "Pipeline":
        """The same pipeline calling the models through another client: one holding a user's API key,
        or OpenAI models on a ChatGPT plan."""
        clone = copy.copy(self)
        clone.llm = llm
        return clone

    def _sink(self, project_id: str):
        def sink(step: str, model: str, usage: Any) -> None:
            self.store.add_usage(project_id, step, model, usage_dict(usage))

        return sink

    def _progress(self, project_id: str, stage: str, done: int = 0, total: int = 0) -> None:
        self.store.update_project(project_id, progress={"stage": stage, "done": done, "total": total})

    def _text_path(self, project_id: str, paper_id: int) -> Path:
        return self.settings.data_dir / "projects" / project_id / f"paper-{paper_id}.txt"

    def source_text(self, project_id: str, paper_id: int) -> str | None:
        path = self._text_path(project_id, paper_id)
        return path.read_text(encoding="utf-8") if path.exists() else None

    # -- step 1: read the problem ----------------------------------------------

    def read_spec(self, project_id: str) -> None:
        project = self.store.project(project_id)
        assert project is not None
        self.store.update_project(project_id, status="reading", error=None)
        try:
            spec = self.llm.read_spec(project["paragraph"], self._sink(project_id))
        except Exception as exc:  # surfaced in the UI
            log.exception("reading the problem failed")
            self.store.update_project(project_id, status="error", error=f"Reading the problem failed. {describe_error(exc)}")
            return
        self.store.update_project(project_id, status="spec_ready", spec=spec.model_dump())

    # -- step 2: search, screen, extract ---------------------------------------

    def run(self, project_id: str) -> None:
        project = self.store.project(project_id)
        assert project is not None and project["spec"]
        spec = SearchSpec.model_validate(upgrade_spec(project["spec"]))
        self.store.clear_papers(project_id)
        self.store.update_project(project_id, error=None, search_plan=None, search_log=None, overlap=None, summary=None, progress=None)
        try:
            self.store.update_project(project_id, status="searching")
            self._progress(project_id, "planning the search")
            plan = self.llm.plan_search(spec, Toolbox(self.sources), self._sink(project_id))
            self.store.update_project(project_id, search_plan=plan.model_dump())
            self._collect(project_id, plan)

            self.store.update_project(project_id, status="screening")
            self._screen_all(project_id, spec)
            self._follow_reviews(project_id)
            self._screen_all(project_id, spec)

            self.store.update_project(project_id, status="extracting")
            self._extract_all(project_id, spec)
            try:
                self._progress(project_id, "checking for overlap")
                self.check_overlap(project_id)
            except Exception as exc:  # the extractions stand without it; the summary tries again
                log.exception("the overlap check failed")
                self.store.update_project(project_id, overlap={"error": describe_error(exc)})
        except Exception as exc:
            log.exception("pipeline failed")
            self.store.update_project(project_id, status="error", error=describe_error(exc))
            return
        self.store.update_project(project_id, status="done")
        self._progress(project_id, "done")

    def _collect(self, project_id: str, plan: SearchPlan) -> None:
        """Run the levels from the top down, then the registry queries, and store the candidates in that
        order. Each search's hits and how many records it added go to the project's search log."""
        limit = self.settings.per_query_limit
        levels = sorted((lv for lv in plan.levels if lv.query.strip()), key=lambda lv: LEVELS.index(lv.level))
        queries = [("europepmc" if lv.level == "full_text" else "pubmed", lv.level, lv.query) for lv in levels]
        queries += [("clinicaltrials", "registry", q) for q in plan.clinicaltrials_queries]
        # Where each record was found, in the order first found; `first` is the index of the search that
        # found it first. Papers are keyed "pmid:<PMID>", or by their Europe PMC ID when not in PubMed
        # (`record` then holds them, as PubMed can't supply them); registry records by NCT ID.
        papers: dict[str, dict] = {}
        trials: dict[str, dict] = {}
        entries: list[dict] = []
        failed: list[str] = []

        def found(table: dict, key: str, level: str, label: str, index: int, record: Any = None) -> None:
            item = table.setdefault(key, {"found_by": [], "found_in": [], "first": index, "record": record})
            item["found_by"].append(label)
            if level not in item["found_in"]:
                item["found_in"].append(level)

        for index, (source, level, query) in enumerate(queries):
            self._progress(project_id, "running the search", index, len(queries))
            label = f"{LEVEL_NAMES[level]}: {query}"
            entry: dict[str, Any] = {"level": level, "source": source, "query": query}
            try:
                if source == "pubmed":
                    hits, ids = self.sources.pubmed.search(query, limit)
                    for pmid in ids:
                        found(papers, f"pmid:{pmid}", level, label, index)
                    entry.update(hits=hits, taken=len(ids))
                elif source == "europepmc":
                    hits, results = self.sources.europepmc.search(query, limit)
                    for result in results:
                        pmid = europepmc_pmid(result)
                        if pmid:
                            found(papers, f"pmid:{pmid}", level, label, index)
                        else:
                            found(papers, str(result["id"]), level, label, index, europepmc_record(result))
                    entry.update(hits=hits, taken=len(results))
                else:
                    hits, studies = self.sources.trials.search(query, limit, with_results=True)
                    for record in map(trial_record, studies):
                        found(trials, record.ext_id, level, label, index, record)
                    entry.update(hits=hits, taken=len(studies))
            except SourceError as exc:
                failed.append(f"{label} ({exc})")
                entry["error"] = str(exc)
            entries.append(entry)
            # Saved as each search returns, so the page can show the levels filling in.
            self.store.update_project(project_id, search_log=entries)
        if failed:
            if len(failed) == len(queries):
                raise SourceError("Every search query failed: " + "; ".join(failed))
            self.store.update_project(project_id, search_plan={**plan.model_dump(), "failed_queries": failed})

        named = LEVEL_NAMES["named"]
        for extra in plan.extra_ids:
            extra = extra.strip()
            if NCT.match(extra):
                study = self.sources.trials.study(extra.upper())
                if study:
                    found(trials, extra.upper(), "named", named, len(entries), trial_record(study))
            elif extra.isdigit():
                found(papers, f"pmid:{extra}", "named", named, len(entries))
        if any(item["first"] == len(entries) for item in [*papers.values(), *trials.values()]):
            entries.append({"level": "named", "source": "pubmed", "query": ", ".join(plan.extra_ids), "hits": None, "taken": None})

        keys = list(papers)[: self.settings.max_candidates]
        pmid_list = [key.removeprefix("pmid:") for key in keys if key.startswith("pmid:")]
        records: list[Record] = self.sources.pubmed.fetch(pmid_list) if pmid_list else []
        by_pmid = {f"pmid:{r.pmid}": r for r in records}
        articles = [(by_pmid.get(key) or papers[key]["record"], papers[key]) for key in keys]
        articles = [(record, item) for record, item in articles if record is not None]
        registry = [(item["record"], item) for item in trials.values()]
        # Interleave the two sources so the cap on candidates keeps the top of each.
        merged = [item for pair in zip_longest(articles, registry) for item in pair if item is not None]
        added = 0
        for record, item in merged:
            if added >= self.settings.max_candidates:
                break
            if self._add(project_id, record, item["found_by"], item["found_in"]):
                added += 1
                entry = entries[item["first"]]
                entry["added"] = entry.get("added", 0) + 1
        for entry in entries:
            if "error" not in entry:
                entry.setdefault("added", 0)
        self.store.update_project(project_id, search_log=entries)
        self._progress(project_id, "running the search", len(queries), len(queries))

    def _follow_reviews(self, project_id: str) -> None:
        """The step down from the reviews: add the studies cited by the reviews that passed screening."""
        papers = self.store.papers(project_id)
        reviews = [
            p
            for p in papers
            if p["source"] == "pubmed"
            and p["status"] == "screened_in"
            and (p.get("screen") or {}).get("study_type") == "systematic_review_or_meta_analysis"
        ][: self.settings.max_reviews_followed]
        entry: dict[str, Any] = {"level": "cited", "source": "pubmed", "reviews": len(reviews), "cited": 0, "already": 0}
        known = {p["pmid"]: p for p in papers if p.get("pmid")}
        cited: dict[str, list[str]] = {}
        # Which reviews cite each record, new or already found: the overlap check needs to know.
        citing: dict[str, list[str]] = {}
        failed: list[str] = []
        for number, review in enumerate(reviews, start=1):
            self._progress(project_id, "reading the reviews' references", number - 1, len(reviews))
            try:
                refs = self.sources.references(review["pmid"])
            except Exception as exc:  # one unreadable reference list never stops the search
                failed.append(f"PMID {review['pmid']} ({describe_error(exc)})")
                continue
            entry["cited"] += len(refs)
            label = f"{LEVEL_NAMES['cited']}: {review['title']} (PMID {review['pmid']})"
            for pmid in refs:
                citing.setdefault(pmid, []).append(review["pmid"])
                if pmid in known:
                    entry["already"] += 1
                else:
                    cited.setdefault(pmid, []).append(label)
        for pmid, reviews_citing in citing.items():
            if pmid in known:
                paper = known[pmid]
                self.store.update_paper(paper["id"], cited_by=sorted({*(paper.get("cited_by") or []), *reviews_citing}))
        # Fetch more than the cap: reference lists are full of reviews, guidelines and methods papers.
        ids = list(cited)[: self.settings.max_cited * 3]
        try:
            fetched = self.sources.pubmed.fetch(ids) if ids else []
        except Exception as exc:
            fetched = []
            failed.append(f"fetching the {len(ids)} cited records ({describe_error(exc)})")
        records = [r for r in fetched if not NOT_STUDIES & set(r.pubtypes)]
        added = 0
        for record in records:
            if added >= self.settings.max_cited:
                break
            if self._add(project_id, record, cited[record.pmid], ["cited"], citing[record.pmid]):
                added += 1
        entry.update(added=added, not_studies=len(fetched) - len(records))
        if failed:
            entry["failed"] = failed
        project = self.store.project(project_id)
        assert project is not None
        log_entries = [e for e in project.get("search_log") or [] if e["level"] != "cited"]
        self.store.update_project(project_id, search_log=[*log_entries, entry])
        self._progress(project_id, "reading the reviews' references", len(reviews), len(reviews))

    def _add(
        self, project_id: str, record: Record, found_by: list[str], found_in: list[str], cited_by: list[str] | None = None
    ) -> bool:
        return (
            self.store.add_paper(
                project_id,
                source=record.source,
                ext_id=record.ext_id,
                pmid=record.pmid,
                pmcid=record.pmcid,
                doi=record.doi,
                nct_ids=record.nct_ids,
                title=record.title,
                authors=record.authors,
                author_list=record.author_list,
                year=record.year,
                journal=record.journal,
                pubtypes=record.pubtypes,
                abstract=record.abstract,
                found_by=found_by,
                found_in=found_in,
                cited_by=cited_by or [],
                status="candidate",
            )
            is not None
        )

    def _screen_all(self, project_id: str, spec: SearchSpec) -> None:
        papers = self.store.papers(project_id, status="candidate")
        sink = self._sink(project_id)
        done = 0
        self._progress(project_id, "screening", 0, len(papers))
        stop = _Stop()

        def screen(paper: dict) -> None:
            if stop.error is not None:
                return  # left as a candidate: the run stops
            try:
                decision = self.llm.screen(spec, _record_text(paper), sink)
                status = {"include": "screened_in", "exclude": "screened_out", "unsure": "unsure"}[decision.decision]
                self.store.update_paper(paper["id"], status=status, screen=decision.model_dump())
            except Exception as exc:
                if stops_the_run(exc):
                    stop.set(exc)
                    return
                # Never drop a record silently: an unscreened record goes forward as "unsure".
                self.store.update_paper(
                    paper["id"],
                    status="unsure",
                    screen={"decision": "unsure", "reason": f"Screening failed ({describe_error(exc)}); review by hand."},
                )

        with ThreadPoolExecutor(self.settings.worker_concurrency) as pool:
            for _ in pool.map(screen, papers):
                done += 1
                self._progress(project_id, "screening", done, len(papers))
        stop.raise_if_set()

    def _extract_all(self, project_id: str, spec: SearchSpec) -> None:
        queue = self.store.papers(project_id, status="screened_in") + self.store.papers(project_id, status="unsure")
        overflow = queue[self.settings.max_extractions :]
        queue = queue[: self.settings.max_extractions]
        for paper in overflow:
            self.store.update_paper(paper["id"], error="Not extracted: over the per-project limit; use Extract.")
        done = 0
        self._progress(project_id, "extracting", 0, len(queue))
        stop = _Stop()

        def extract(paper: dict) -> None:
            if stop.error is None:
                self.extract_paper(project_id, spec, paper["id"], stop=stop)

        with ThreadPoolExecutor(self.settings.worker_concurrency) as pool:
            for _ in pool.map(extract, queue):
                done += 1
                self._progress(project_id, "extracting", done, len(queue))
        stop.raise_if_set()

    # -- across papers: overlap and the summary ---------------------------------

    def check_overlap(self, project_id: str) -> None:
        """Opus looks across the extracted records for reports that would be counted twice."""
        project = self.store.project(project_id)
        assert project is not None
        spec = SearchSpec.model_validate(upgrade_spec(project["spec"]))
        records = overlap_records(self.store.papers(project_id))
        report: dict = {"groups": [], "notes": ""}
        if len(records) > 1:
            report = self.llm.check_overlap(spec, records, self._sink(project_id)).model_dump()
        self.store.update_project(project_id, overlap={"report": report, "records": [r["key"] for r in records], "input": records})

    def summarize(self, project_id: str) -> None:
        """Opus describes, for each effect size the plan needs, the values the investigator kept: how they
        compare and where they share participants. It does not combine them or recommend one. If writing
        it again fails, the previous summary stays, with the error."""
        previous = previous_summary((self.store.project(project_id) or {}).get("summary"))
        self.store.update_project(project_id, summary={"status": "running", "previous": previous})
        try:
            project = self.store.project(project_id)
            assert project is not None
            spec = SearchSpec.model_validate(upgrade_spec(project["spec"]))
            papers = self.store.papers(project_id)
            overlap = project.get("overlap") or {}
            if "report" not in overlap or overlap.get("input") != overlap_records(papers):
                self.check_overlap(project_id)
                overlap = (self.store.project(project_id) or {})["overlap"]
            decisions = self.store.decisions(project_id)
            evidence = build_evidence(papers, decisions, overlap["report"])
            if not evidence["values"]:
                raise ValueError("There is nothing to summarize: no extracted result is kept.")
            result = self.llm.summarize(spec, overlap["report"], evidence, self._sink(project_id))
            self.store.update_project(project_id, summary={
                "status": "done", "result": result.model_dump(), "evidence": evidence,
                "basis": decisions_basis(papers, decisions),
            })
        except Exception as exc:
            log.exception("the summary failed")
            error = describe_error(exc)
            self.store.update_project(project_id, summary={**previous, "last_error": error} if previous else {"status": "error", "error": error})

    # -- one paper ------------------------------------------------------------

    def author_list(self, paper_id: int) -> list[dict]:
        """A paper's authors with their affiliations. A record stored before they were kept gets them
        from its source now (PubMed, Europe PMC or ClinicalTrials.gov); an empty list if it can't."""
        paper = self.store.paper(paper_id)
        assert paper is not None
        if paper.get("author_list"):
            return paper["author_list"]
        record: Record | None = None
        try:
            if paper["source"] == "clinicaltrials":
                study = self.sources.trials.study(paper["ext_id"])
                record = trial_record(study) if study else None
            elif paper.get("pmid"):
                record = next(iter(self.sources.pubmed.fetch([paper["pmid"]])), None)
            else:
                kind = "PPR" if paper["ext_id"].startswith("PPR") else "PMC"
                _, results = self.sources.europepmc.search(f"EXT_ID:{paper['ext_id']} AND SRC:{kind}", 5)
                found = next((r for r in results if str(r.get("id")) == paper["ext_id"]), None)
                record = europepmc_record(found) if found else None
        except SourceError:
            log.exception("could not fetch the authors of paper %s", paper_id)
        if record is None or not record.author_list:
            return []
        self.store.update_paper(paper_id, author_list=record.author_list, authors=paper.get("authors") or record.authors)
        return record.author_list

    def extract_paper(self, project_id: str, spec: SearchSpec, paper_id: int, stop: "_Stop | None" = None) -> None:
        """Extract one paper. In a run (`stop` given), an error that would fail every paper stops the run."""
        try:
            self._extract_paper(project_id, spec, paper_id)
        except Exception as exc:
            log.exception("extracting paper %s failed", paper_id)
            self.store.update_paper(paper_id, status="error", error=f"Extraction failed. {describe_error(exc)}")
            if stop is not None and stops_the_run(exc):
                stop.set(exc)

    def _extract_paper(self, project_id: str, spec: SearchSpec, paper_id: int) -> None:
        paper = self.store.paper(paper_id)
        assert paper is not None
        sink = self._sink(project_id)
        self.store.update_paper(paper_id, status="fetching", error=None)
        try:
            fulltext = get_fulltext(paper, self.sources)
        except Exception as exc:
            self.store.update_paper(paper_id, status="no_fulltext", error=f"Could not get the text: {exc}")
            return
        if fulltext is None:
            self.store.update_paper(paper_id, status="no_fulltext", error="No open full text or abstract found.")
            return
        path = self._text_path(project_id, paper_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(fulltext.text, encoding="utf-8")
        self.store.update_paper(paper_id, status="extracting", fulltext_kind=fulltext.kind, fulltext_url=fulltext.url)

        document = self.llm.document(fulltext, paper["title"] or paper["ext_id"])
        quotes_reliable = fulltext.kind != "oa_pdf" and bool(fulltext.text.strip())
        worker, report, worker_error = self._worker_pass(spec, document, fulltext, quotes_reliable, sink)
        reasons = escalation_reasons(worker, report)
        if worker_error:
            reasons = [worker_error]
        audited = not reasons and in_audit_sample(f"{project_id}:{paper_id}", self.settings.audit_fraction)
        if audited:
            reasons = ["random audit of an extraction that passed every check"]

        final, final_report, extracted_by = worker, report, "worker"
        if reasons:
            try:
                final = self.llm.adjudicate(spec, document, worker, reasons, sink)
                final_report = verify(final, fulltext.text, quotes_reliable)
                extracted_by = "manager"
            except Exception as exc:
                if stops_the_run(exc):
                    raise
                if worker is None:
                    self.store.update_paper(
                        paper_id,
                        status="needs_manual",
                        escalation={"reasons": reasons, "audit": audited},
                        error=f"Neither model completed the extraction. {describe_error(exc)}",
                    )
                    return
                reasons.append(f"manager review failed: {describe_error(exc)}")

        assert final is not None and final_report is not None
        self.store.update_paper(
            paper_id,
            status="extracted",
            extraction=final.model_dump(),
            extracted_by=extracted_by,
            extracted_model=getattr(self.llm, f"{extracted_by}_model", None),
            verification=final_report.to_dict(),
            escalation={"reasons": reasons, "audit": audited} if reasons else None,
        )

    def _worker_pass(
        self, spec: SearchSpec, document: dict, fulltext: FullText, quotes_reliable: bool, sink
    ) -> tuple[PaperExtraction | None, Verification | None, str | None]:
        try:
            worker = self.llm.extract(spec, document, sink)
        except ModelDeclined as exc:
            return None, None, f"the worker {exc}"
        except ModelTruncated:
            return None, None, "the worker's extraction was cut off before it finished"
        except Exception as exc:
            if stops_the_run(exc):
                raise
            return None, None, f"the worker failed: {describe_error(exc)}"
        return worker, verify(worker, fulltext.text, quotes_reliable), None


class _Stop:
    """The first error that stops a batch of model calls; the calls not yet started are skipped."""

    def __init__(self):
        self._lock = threading.Lock()
        self.error: BaseException | None = None

    def set(self, error: BaseException) -> None:
        with self._lock:
            if self.error is None:
                self.error = error

    def raise_if_set(self) -> None:
        if self.error is not None:
            raise self.error
