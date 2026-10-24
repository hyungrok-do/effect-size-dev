"""The web app's backend: a JSON API over the pipeline, plus the static page in web/."""

from __future__ import annotations

import csv
import html
import io
import ipaddress
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Literal

import anthropic
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .chatgpt import CALLBACK_PATH, ChatGPTError, ChatGPTSession
from .config import Settings
from .llm import LLM, ClaudeLLM
from .openai_llm import OpenAILLM
from .pipeline import Pipeline, previous_summary
from .schemas import LEVEL_NAMES, SearchSpec, upgrade_spec
from .sources import Sources, europepmc_url
from .store import RUNNING, Store

PAPER = -1  # result_index of a decision about the whole paper


def key_header() -> object:
    """The page sends the user's Anthropic API key in this header with each request that starts
    Claude work. The key is used for that job only, held in memory, and never written to disk."""
    return Header(default=None, alias="X-Anthropic-Api-Key", max_length=512)


def provider_header() -> object:
    """Which models run a job: "anthropic" (Claude, the default) or "chatgpt" (OpenAI models on the
    plan of the person signed in with ChatGPT)."""
    return Header(default=None, alias="X-Model-Provider", max_length=32)


class ModelChoice(BaseModel):
    manager_model: str = Field(min_length=1, max_length=200)
    worker_model: str = Field(min_length=1, max_length=200)


def _remote(request: Request) -> bool:
    """Whether the page was opened from another computer: ChatGPT sends the browser back to
    127.0.0.1, which there is that computer, not this server."""
    host = request.client.host if request.client else ""
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # not an address (the test client); nothing to say it is remote


def _callback_page(title: str, message: str, status: int) -> HTMLResponse:
    body = (
        '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        "<style>body{font:17px/1.5 system-ui,sans-serif;max-width:34rem;margin:15vh auto;padding:0 20px;color:#1f2328}"
        "h1{font-size:24px}a{color:#2457c5}</style>"
        f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>"
        '<p><a href="/">Back to Effect Size Finder</a></p></html>'
    )
    # The callback address carries a one-time code: keep it out of caches and referrers.
    headers = {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
    }
    return HTMLResponse(body, status_code=status, headers=headers)


class NewProject(BaseModel):
    paragraph: str = Field(min_length=20, max_length=20000)


class DecisionIn(BaseModel):
    result_index: int = Field(ge=PAPER)
    decision: Literal["include", "exclude"] | None
    reason: str = Field(default="", max_length=2000)


def effective_decision(decisions: dict[int, dict], result_index: int) -> dict | None:
    """A decision on a result wins over a decision on its whole paper."""
    return decisions.get(result_index) or decisions.get(PAPER)


def _links(paper: dict) -> dict[str, str]:
    links = {}
    if paper["source"] == "europepmc":
        links["Europe PMC"] = europepmc_url(paper["ext_id"])
    if paper.get("pmid"):
        links["PubMed"] = f"https://pubmed.ncbi.nlm.nih.gov/{paper['pmid']}/"
    if paper.get("pmcid"):
        links["PMC"] = f"https://pmc.ncbi.nlm.nih.gov/articles/{paper['pmcid']}/"
    if paper.get("doi"):
        links["DOI"] = f"https://doi.org/{paper['doi']}"
    for nct in paper.get("nct_ids") or []:
        links[nct] = f"https://clinicaltrials.gov/study/{nct}"
    return links


EXPORT_COLUMNS = [
    "paper_id", "source", "id", "doi", "title", "year", "text_used", "extracted_by", "result", "outcome_measure",
    "timepoint", "analysis_population", "adjusted", "relevance", "confidence", "group", "statistic", "value",
    "describes", "quote", "location", "quote_check", "number_in_quote", "concerns", "flags", "decision",
    "decision_reason",
]


def export_rows(store: Store, project_id: str, included_only: bool) -> list[dict]:
    decisions = store.decisions(project_id)
    rows = []
    for paper in store.papers(project_id, status="extracted"):
        extraction = paper["extraction"] or {}
        checks = (paper["verification"] or {}).get("values", [])
        flags = (paper["verification"] or {}).get("flags", [])
        paper_decisions = decisions.get(paper["id"], {})
        for index, result in enumerate(extraction.get("results", [])):
            decision = effective_decision(paper_decisions, index)
            if included_only and (decision or {}).get("decision") != "include":
                continue
            prefix = f"result {index + 1} "
            for value_index, value in enumerate(result["values"]):
                check = checks[index][value_index] if index < len(checks) and value_index < len(checks[index]) else {}
                rows.append(
                    {
                        "paper_id": paper["id"],
                        "source": paper["source"],
                        "id": paper["ext_id"],
                        "doi": paper.get("doi") or "",
                        "title": paper["title"],
                        "year": paper.get("year") or "",
                        "text_used": paper.get("fulltext_kind") or "",
                        "extracted_by": paper.get("extracted_by") or "",
                        "result": index + 1,
                        "outcome_measure": result["outcome_measure"],
                        "timepoint": result["timepoint"],
                        "analysis_population": result["analysis_population"],
                        "adjusted": result["adjusted"],
                        "relevance": result["relevance"],
                        "confidence": result["confidence"],
                        "group": value["group"],
                        "statistic": value["statistic"],
                        "value": value["value"],
                        "describes": value["describes"],
                        "quote": value["quote"],
                        "location": value["location"],
                        "quote_check": check.get("quote", ""),
                        "number_in_quote": check.get("number_in_quote", ""),
                        "concerns": "; ".join(result["concerns"]),
                        "flags": "; ".join(f for f in flags if f.startswith(prefix)),
                        "decision": (decision or {}).get("decision", ""),
                        "decision_reason": (decision or {}).get("reason", ""),
                    }
                )
    return rows


def create_app(
    settings: Settings | None = None,
    llm: LLM | None = None,
    sources: Sources | None = None,
    store: Store | None = None,
    llm_for_key: Callable[[str], LLM] | None = None,
    chatgpt: ChatGPTSession | None = None,
    llm_for_chatgpt: Callable[[ChatGPTSession], LLM] | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    store = store or Store(settings.data_dir / "esf.sqlite3")
    store.interrupt_running()
    pipeline = Pipeline(store, llm or ClaudeLLM(settings), sources or Sources(settings), settings)
    llm_for_key = llm_for_key or (lambda key: ClaudeLLM(settings, anthropic.Anthropic(api_key=key)))
    chatgpt = chatgpt or ChatGPTSession(
        settings.data_dir / "chatgpt.json",
        manager_model=settings.openai_manager_model,
        worker_model=settings.openai_worker_model,
    )

    def plan_llm(session: ChatGPTSession) -> LLM:
        chosen = session.state()
        return OpenAILLM(
            settings,
            session.access_token,
            manager_model=chosen["manager_model"],
            worker_model=chosen["worker_model"],
            reasoning_levels=session.reasoning_levels,
        )

    llm_for_chatgpt = llm_for_chatgpt or plan_llm

    def pipeline_for(api_key: str | None, provider: str | None = None) -> Pipeline:
        """The models a job runs on: OpenAI's on the signed-in ChatGPT plan; or Claude, with a key from
        the page, or without one the server's own credentials."""
        if (provider or "anthropic") == "chatgpt":
            status = chatgpt.state()["status"]
            if status != "connected":
                again = status == "reauth_required"
                raise HTTPException(409, "Your ChatGPT sign-in has expired. Sign in with ChatGPT again, in Setup."
                                    if again else "Sign in with ChatGPT first, in Setup.")
            return pipeline.using(llm_for_chatgpt(chatgpt))
        if provider not in (None, "anthropic"):
            raise HTTPException(400, "Unknown model provider.")
        api_key = (api_key or "").strip()
        return pipeline.using(llm_for_key(api_key)) if api_key else pipeline
    jobs = ThreadPoolExecutor(max_workers=4, thread_name_prefix="esf-job")
    busy: set[str] = set()
    busy_lock = threading.Lock()

    app = FastAPI(title="Effect Size Finder")
    app.state.pipeline = pipeline
    app.state.store = store
    app.state.chatgpt = chatgpt

    def submit(key: str, fn: Callable[[], None]) -> None:
        with busy_lock:
            if key in busy:
                raise HTTPException(409, "This is already running.")
            busy.add(key)

        def job() -> None:
            try:
                fn()
            finally:
                with busy_lock:
                    busy.discard(key)

        jobs.submit(job)

    def get_project(project_id: str) -> dict:
        project = store.project(project_id)
        if project is None:
            raise HTTPException(404, "No such project.")
        return project

    def get_paper(project_id: str, paper_id: int) -> dict:
        paper = store.paper(paper_id)
        if paper is None or paper["project_id"] != project_id:
            raise HTTPException(404, "No such paper.")
        return paper

    @app.get("/api/config")
    def config() -> dict:
        return {
            "manager_model": settings.manager_model,
            "worker_model": settings.worker_model,
            "openai_manager_model": settings.openai_manager_model,
            "openai_worker_model": settings.openai_worker_model,
            "contact_email_set": bool(settings.contact_email),
            "server_has_key": bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")),
            "max_candidates": settings.max_candidates,
            "max_extractions": settings.max_extractions,
        }

    # -- Sign in with ChatGPT ------------------------------------------------------------------

    @app.get("/api/chatgpt")
    def chatgpt_state(request: Request, refresh: bool = False) -> dict:
        """The sign-in, the plan's models and the chosen ones; never a token."""
        if refresh and chatgpt.state()["status"] == "connected":
            try:
                chatgpt.refresh_models()
            except ChatGPTError:
                pass  # the session records what went wrong
        return {**chatgpt.state(), "remote": _remote(request)}

    @app.post("/api/chatgpt/sign-in")
    def chatgpt_sign_in(request: Request) -> dict:
        """The address of ChatGPT's sign-in page, which comes back to this server's /auth/callback."""
        if _remote(request):
            raise HTTPException(400, "Sign in with ChatGPT works only in a browser on the computer that runs the server.")
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        return {"url": chatgpt.start(port)}

    @app.get(CALLBACK_PATH, response_class=HTMLResponse)
    def chatgpt_callback(request: Request) -> HTMLResponse:
        try:
            chatgpt.finish(request.url.query, request.headers.get("host", ""))
        except ChatGPTError as exc:
            return _callback_page("ChatGPT sign-in did not finish", str(exc), 400)
        return _callback_page("Signed in with ChatGPT", "Return to Effect Size Finder; this tab can be closed.", 200)

    @app.put("/api/chatgpt/models")
    def chatgpt_models(body: ModelChoice) -> dict:
        try:
            chatgpt.set_models(body.manager_model, body.worker_model)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return chatgpt.state()

    @app.post("/api/chatgpt/sign-out")
    def chatgpt_sign_out() -> dict:
        chatgpt.sign_out()
        return chatgpt.state()

    # -- projects -------------------------------------------------------------------------------

    @app.get("/api/projects")
    def list_projects() -> list[dict]:
        return store.projects()

    @app.post("/api/projects", status_code=201)
    def create_project(
        body: NewProject, x_anthropic_api_key: str | None = key_header(), x_model_provider: str | None = provider_header()
    ) -> dict:
        job = pipeline_for(x_anthropic_api_key, x_model_provider)
        project_id = store.create_project(body.paragraph.strip())
        store.update_project(project_id, status="reading")
        submit(project_id, lambda: job.read_spec(project_id))
        return {"id": project_id}

    @app.get("/api/projects/{project_id}")
    def project(project_id: str) -> dict:
        project = get_project(project_id)
        project["counts"] = store.status_counts(project_id)
        project["usage"] = store.usage(project_id)
        project["level_names"] = LEVEL_NAMES
        return project

    @app.put("/api/projects/{project_id}/spec")
    def update_spec(project_id: str, spec: SearchSpec) -> dict:
        project = get_project(project_id)
        if project["status"] in RUNNING:
            raise HTTPException(409, "Wait until the current step finishes.")
        store.update_project(project_id, spec=spec.model_dump(), status="spec_ready")
        return {"ok": True}

    @app.post("/api/projects/{project_id}/run", status_code=202)
    def run(project_id: str, x_anthropic_api_key: str | None = key_header(), x_model_provider: str | None = provider_header()) -> dict:
        project = get_project(project_id)
        if not project["spec"]:
            raise HTTPException(400, "Read the problem first.")
        if project["status"] in RUNNING:
            raise HTTPException(409, "This project is already running.")
        if (project.get("summary") or {}).get("status") == "running":
            raise HTTPException(409, "Wait until the summary is written.")
        job = pipeline_for(x_anthropic_api_key, x_model_provider)
        store.update_project(project_id, status="searching")
        submit(project_id, lambda: job.run(project_id))
        return {"ok": True}

    @app.post("/api/projects/{project_id}/summary", status_code=202)
    def summarize(project_id: str, x_anthropic_api_key: str | None = key_header(), x_model_provider: str | None = provider_header()) -> dict:
        project = get_project(project_id)
        if project["status"] in RUNNING:
            raise HTTPException(409, "Wait until the search finishes.")
        if (project.get("summary") or {}).get("status") == "running":
            raise HTTPException(409, "The summary is already being written.")
        job = pipeline_for(x_anthropic_api_key, x_model_provider)
        store.update_project(project_id, summary={"status": "running", "previous": previous_summary(project.get("summary"))})
        submit(f"summary-{project_id}", lambda: job.summarize(project_id))
        return {"ok": True}

    @app.get("/api/projects/{project_id}/papers")
    def papers(project_id: str) -> list[dict]:
        get_project(project_id)
        decisions = store.decisions(project_id)
        out = []
        for paper in store.papers(project_id):
            paper["decisions"] = {str(k): v for k, v in decisions.get(paper["id"], {}).items()}
            paper["links"] = _links(paper)
            out.append(paper)
        return out

    @app.post("/api/projects/{project_id}/papers/{paper_id}/extract", status_code=202)
    def extract(
        project_id: str, paper_id: int, x_anthropic_api_key: str | None = key_header(), x_model_provider: str | None = provider_header()
    ) -> dict:
        project = get_project(project_id)
        get_paper(project_id, paper_id)
        if project["status"] in RUNNING:
            raise HTTPException(409, "Wait until the current run finishes.")
        spec = SearchSpec.model_validate(upgrade_spec(project["spec"]))
        job = pipeline_for(x_anthropic_api_key, x_model_provider)
        store.clear_decisions(paper_id)
        store.update_paper(paper_id, status="fetching", error=None)
        submit(f"paper-{paper_id}", lambda: job.extract_paper(project_id, spec, paper_id))
        return {"ok": True}

    @app.get("/api/projects/{project_id}/papers/{paper_id}/authors")
    def authors(project_id: str, paper_id: int) -> dict:
        get_paper(project_id, paper_id)
        return {"authors": pipeline.author_list(paper_id)}

    @app.put("/api/projects/{project_id}/papers/{paper_id}/decisions")
    def decide(project_id: str, paper_id: int, body: DecisionIn) -> dict:
        paper = get_paper(project_id, paper_id)
        results = (paper["extraction"] or {}).get("results", [])
        if body.result_index != PAPER and body.result_index >= len(results):
            raise HTTPException(400, "No such result.")
        store.set_decision(paper_id, body.result_index, body.decision, body.reason)
        return {"ok": True}

    @app.get("/api/projects/{project_id}/papers/{paper_id}/source", response_class=PlainTextResponse)
    def source(project_id: str, paper_id: int) -> str:
        get_paper(project_id, paper_id)
        text = pipeline.source_text(project_id, paper_id)
        if text is None:
            raise HTTPException(404, "No text was retrieved for this paper.")
        return text

    @app.get("/api/projects/{project_id}/export.csv")
    def export_csv(project_id: str, included_only: bool = False) -> Response:
        get_project(project_id)
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=EXPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(export_rows(store, project_id, included_only))
        name = f"effect-sizes-{project_id}{'-included' if included_only else ''}.csv"
        return Response(
            buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.get("/api/projects/{project_id}/export.json")
    def export_json(project_id: str) -> JSONResponse:
        project = get_project(project_id)
        body = {
            "project": {k: project[k] for k in ("id", "created_at", "paragraph", "spec", "search_plan", "search_log", "overlap", "summary")},
            "papers": papers(project_id),
            "usage": store.usage(project_id),
        }
        return JSONResponse(body, headers={"Content-Disposition": f'attachment; filename="effect-sizes-{project_id}.json"'})

    if settings.web_dir.is_dir():
        # The page shares the browser edition's specification panel (site/lib/spec-panel.js).
        site_lib = settings.web_dir.parent / "site" / "lib"
        if site_lib.is_dir():
            app.mount("/lib", StaticFiles(directory=site_lib), name="site-lib")
        app.mount("/", StaticFiles(directory=settings.web_dir, html=True), name="web")
    return app
