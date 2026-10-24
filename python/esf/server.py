"""The web app's backend: a JSON API over the pipeline, plus the static page in web/."""

from __future__ import annotations

import csv
import hmac
import html
import io
import ipaddress
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Literal

import anthropic
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .chatgpt import CALLBACK_PATH, JOB_REFRESH_WAIT, ChatGPTError, ChatGPTSession
from .config import MODEL_CHOICES, Settings
from .llm import LLM, NO_KEY, ClaudeLLM, server_credentials
from .openai_llm import OpenAILLM
from .pipeline import Pipeline, previous_summary
from .schemas import LEVEL_NAMES, SearchSpec, upgrade_spec
from .sources import Sources, europepmc_url
from .store import RUNNING, Store

PAPER = -1  # result_index of a decision about the whole paper


# What runs a job, as the page's X-Model-Provider header says: Claude with an Anthropic API key (the
# default), OpenAI's models with an OpenAI API key, or OpenAI's models on the plan of the person
# signed in with ChatGPT.
PROVIDERS = ("anthropic", "openai", "chatgpt")
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}")


@dataclass(frozen=True)
class Agents:
    """A job's provider, the page's key for it (if it sent one) and the models for its roles (as the
    page chose them, else the provider's defaults; on a ChatGPT plan the session's choice is used),
    and whether the request came from another computer."""

    provider: str
    key: str | None
    manager_model: str | None
    worker_model: str | None
    remote: bool = False


MAX_KEY_LENGTH = 512


def agents_headers(
    request: Request,
    x_model_provider: str | None = Header(default=None, max_length=32),
    # No max_length on the keys: FastAPI's error for it would quote the whole key back to the page.
    x_anthropic_api_key: str | None = Header(default=None),
    x_openai_api_key: str | None = Header(default=None),
    x_manager_model: str | None = Header(default=None),
    x_worker_model: str | None = Header(default=None),
) -> Agents:
    """The headers the page sends with each request that starts work on the models. Each vendor's key
    has its own header and only that vendor's is read; it is used for that job only, held in memory,
    and never written to disk."""
    for sent in (x_anthropic_api_key, x_openai_api_key):
        if sent is None:
            continue
        if len(sent) > MAX_KEY_LENGTH:
            raise HTTPException(400, "The API key is too long. Paste only the key itself.")
        # Over plain HTTP from another computer the key has crossed the network readable: refused, so
        # that setup fails visibly instead of running on keys anyone on the network could copy.
        if sent.strip() and request.url.scheme != "https" and _remote(request):
            raise HTTPException(400, "This server takes API keys from other computers only over HTTPS.")
    provider = x_model_provider or "anthropic"
    if provider not in PROVIDERS:
        raise HTTPException(400, "Unknown model provider.")
    if provider == "chatgpt":
        return Agents(provider, None, None, None, _remote(request))
    models = {}
    for role, value in (("manager", x_manager_model), ("worker", x_worker_model)):
        value = (value or "").strip()
        if value and not MODEL_NAME.fullmatch(value):
            raise HTTPException(400, f"The {role} model's name is not valid.")
        models[role] = value or None
    key = x_anthropic_api_key if provider == "anthropic" else x_openai_api_key
    return Agents(provider, (key or "").strip() or None, models["manager"], models["worker"], _remote(request))


class ModelChoice(BaseModel):
    manager_model: str = Field(min_length=1, max_length=200)
    worker_model: str = Field(min_length=1, max_length=200)


# Headers a reverse proxy adds; behind one, every visitor reaches the server from 127.0.0.1.
FORWARDING_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded", "x-forwarded-host", "x-forwarded-proto")
LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}


def _remote(request: Request) -> bool:
    """Whether the page was opened from another computer: ChatGPT sends the browser back to
    127.0.0.1, which there is that computer, not this server. A request through a proxy, from a peer
    that is not a loopback address (or no address at all, as on a Unix socket behind a proxy), or to a
    name other than this computer's own, counts as remote."""
    if any(header in request.headers for header in FORWARDING_HEADERS):
        return True
    peer = request.client.host if request.client else ""
    try:
        if not ipaddress.ip_address(peer).is_loopback:
            return True
    except ValueError:
        return True  # nothing shows the request came from this computer
    return _host_name(request.headers.get("host", "")) not in LOOPBACK_NAMES


def _loopback_only(request: Request) -> None:
    """The ChatGPT sign-in is the person's at this computer: other computers may not use or see it."""
    if _remote(request):
        raise HTTPException(403, "Your ChatGPT sign-in can be used only in a browser on the computer that runs the server.")


def _host_name(host: str) -> str:
    """The name in a Host header, without its port: "127.0.0.1:8000" -> "127.0.0.1", "[::1]:8000" -> "::1"."""
    host = host.strip().lower()
    if host.startswith("["):
        return host[1:host.index("]")] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _own_page(request: Request, host: str, allowed_hosts: set[str]) -> bool:
    """Whether a request that changes something comes from the server's own page. The browser's
    Sec-Fetch-Site says so against the address the person sees, so it holds behind a proxy too; without
    it, the Origin's host must be the Host the request came to (or a name in ESF_ALLOWED_HOSTS)."""
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site in ("same-origin", "none")
    origin = request.headers.get("origin")
    if origin is None:
        return True  # not a browser's cross-site request: those carry an Origin
    origin_host = origin.split("://", 1)[-1].lower()
    return origin_host == host.lower() or _host_name(origin_host) in allowed_hosts - {"localhost"}


# The header that carries the access key `esf serve` puts in the page's address (#key=...), and what a
# request without it is told. Loopback is no boundary between people: other users of this computer, its
# other programs, and a proxy in front of the server all reach it from 127.0.0.1.
ACCESS_HEADER = "x-esf-token"
NEEDS_ACCESS_KEY = "Open the address that esf serve printed in the terminal; it carries this server's access key."


def _has_access(request: Request, access_token: str | None) -> bool:
    """Whether a request may go on: with an access key, every /api request must carry it. The page's
    own files hold no data and need none, nor does ChatGPT's return to /auth/callback, which its state
    and PKCE verifier tie to a sign-in that was started with the key."""
    # The path as the routes match it: without the prefix a proxy may serve the app under (root_path).
    path, root = request.scope["path"], request.scope.get("root_path", "")
    if root and path.startswith(root + "/"):
        path = path[len(root):]
    if access_token is None or not (path == "/api" or path.startswith("/api/")):
        return True
    given = request.headers.get(ACCESS_HEADER, "")
    return hmac.compare_digest(given.encode("utf-8", "replace"), access_token.encode("utf-8"))


def _is_address(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


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
    "ci_level", "describes", "quote", "location", "quote_check", "number_in_quote", "concerns", "flags", "decision",
    "decision_reason",
]


# A spreadsheet runs a cell that starts with one of these as a formula, so such text (a quote from a
# paper, a title, a reason typed on the page) gets a leading apostrophe. A number written as text, such
# as "-0.42", stays as it is.
FORMULA_START = re.compile(r"[=+\-@\t\r]")
PLAIN_NUMBER = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?", re.ASCII)


def csv_cell(value: object) -> object:
    if isinstance(value, str) and FORMULA_START.match(value) and not PLAIN_NUMBER.fullmatch(value):
        return "'" + value
    return value


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
                        "ci_level": value.get("ci_level") if value.get("ci_level") is not None else "",
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


def plan_llm(settings: Settings, session: ChatGPTSession) -> OpenAILLM:
    """OpenAI's models on the signed-in ChatGPT plan, with the models chosen for the sign-in."""
    chosen = session.state()

    def token() -> str:
        # A job waits longer than a page's request for a renewal ChatGPT doesn't allow yet, rather than
        # fail every record meanwhile; past that, the run stops (see stops_the_run).
        return session.access_token(max_wait=JOB_REFRESH_WAIT)

    return OpenAILLM(
        settings,
        token,
        manager_model=chosen["manager_model"],
        worker_model=chosen["worker_model"],
        reasoning_levels=session.reasoning_levels,
        on_error=session.handle_api_error,
    )


def create_app(
    settings: Settings | None = None,
    llm: LLM | None = None,
    sources: Sources | None = None,
    store: Store | None = None,
    llm_for_key: Callable[[str, str, str], LLM] | None = None,
    chatgpt: ChatGPTSession | None = None,
    llm_for_chatgpt: Callable[[ChatGPTSession], LLM] | None = None,
    llm_for_openai: Callable[[str, str, str], LLM] | None = None,
    access_token: str | None = None,
) -> FastAPI:
    """`llm_for_key` and `llm_for_openai` make the models for a job from (key, manager model, worker
    model): Claude's with an Anthropic API key, OpenAI's with an OpenAI API key. With `access_token`
    (`esf serve` makes one per launch), every /api request must carry it in X-ESF-Token; without it, as
    in the tests or an app that embeds this one, the API is open to whoever reaches it."""
    settings = settings or Settings.from_env()
    store = store or Store(settings.data_dir / "esf.sqlite3")
    store.interrupt_running()
    pipeline = Pipeline(store, llm or ClaudeLLM(settings), sources or Sources(settings), settings)
    llm_for_key = llm_for_key or (
        lambda key, manager, worker: ClaudeLLM(settings, anthropic.Anthropic(api_key=key), manager, worker)
    )
    # An API key takes plain function tools; only plan usage needs them in a namespace.
    llm_for_openai = llm_for_openai or (
        lambda key, manager, worker: OpenAILLM(settings, lambda: key, manager, worker, namespaced_tools=False)
    )
    chatgpt = chatgpt or ChatGPTSession(
        settings.data_dir / "chatgpt.json",
        manager_model=settings.openai_manager_model,
        worker_model=settings.openai_worker_model,
    )
    llm_for_chatgpt = llm_for_chatgpt or (lambda session: plan_llm(settings, session))

    def pipeline_for(agents: Agents) -> Pipeline:
        """The models a job runs on: OpenAI's on the signed-in ChatGPT plan; OpenAI's with a key from
        the page or the server's; or Claude, with a key from the page, or without one the server's own
        credentials. A key provider's models are the page's choice, else the configured ones."""
        if agents.provider == "chatgpt":
            if agents.remote:
                raise HTTPException(403, "Your ChatGPT plan can be used only from the computer that runs the server.")
            chosen = chatgpt.state()
            if chosen["status"] != "connected":
                again = chosen["status"] == "reauth_required"
                raise HTTPException(409, "Your ChatGPT sign-in has expired. Sign in with ChatGPT again, on the Agents page."
                                    if again else "Sign in with ChatGPT first, on the Agents page.")
            # Every call would fail on a model the plan does not offer.
            offered = {model["slug"] for model in chosen["models"]}
            for role, label in (("manager", "manager"), ("worker", "workers'")):
                if offered and chosen[f"{role}_model"] not in offered:
                    raise HTTPException(409, f"Your ChatGPT plan does not offer {chosen[f'{role}_model']}. "
                                        f"Choose the {label} model on the Agents page.")
            return pipeline.using(llm_for_chatgpt(chatgpt))
        if agents.provider == "openai":
            key = agents.key or os.environ.get("OPENAI_API_KEY", "").strip()
            if not key:
                raise HTTPException(409, "Enter your OpenAI API key first, on the Agents page.")
            manager = agents.manager_model or settings.openai_manager_model
            return pipeline.using(llm_for_openai(key, manager, agents.worker_model or settings.openai_worker_model))
        manager = agents.manager_model or settings.manager_model
        worker = agents.worker_model or settings.worker_model
        if agents.key:
            return pipeline.using(llm_for_key(agents.key, manager, worker))
        if not isinstance(pipeline.llm, ClaudeLLM):
            return pipeline  # a stand-in for Claude (the tests') runs as it is
        # The server's own credential (ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN only), if it has one.
        if pipeline.llm.client is None:
            raise HTTPException(409, NO_KEY)
        if (manager, worker) == (settings.manager_model, settings.worker_model):
            return pipeline
        return pipeline.using(ClaudeLLM(settings, pipeline.llm.client, manager, worker))

    jobs = ThreadPoolExecutor(max_workers=4, thread_name_prefix="esf-job")
    busy: set[str] = set()
    busy_lock = threading.Lock()

    app = FastAPI(title="Effect Size Finder")
    app.state.pipeline = pipeline
    app.state.store = store
    app.state.chatgpt = chatgpt
    allowed_hosts = {"localhost", *(name.lower() for name in settings.allowed_hosts)}

    @app.middleware("http")
    async def own_pages_only(request: Request, call_next: Callable) -> Response:
        """The server answers only to its address, localhost or a name in ESF_ALLOWED_HOSTS, so a page
        that rebinds its own DNS name to this computer can't reach it. A request that changes
        something must come from the server's own page: another site may send a form post, which
        needs no CORS preflight, but its browser marks it as cross-site or gives its own origin."""
        host = request.headers.get("host", "")
        name = _host_name(host)
        if not (name in allowed_hosts or _is_address(name)):
            return JSONResponse({"detail": "This server answers only to its address or localhost; ESF_ALLOWED_HOSTS lists other names."}, status_code=403)
        if not _has_access(request, access_token):
            # The code tells the page it is the access check, so it says how to get in instead of failing.
            return JSONResponse({"detail": NEEDS_ACCESS_KEY, "code": "access_key"}, status_code=401)
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and not _own_page(request, host, allowed_hosts):
            return JSONResponse({"detail": "Only this server's own page may make changes."}, status_code=403)
        response = await call_next(request)
        # Nor may another site frame the page and have the person click in it.
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        return response

    @app.exception_handler(ChatGPTError)
    async def chatgpt_failed(request: Request, exc: ChatGPTError) -> JSONResponse:
        """A step of the sign-in that failed (the sign-in service unreachable, the settings not saved), with its message."""
        status = 503 if exc.retryable else 500 if exc.code == "storage_failed" else 502
        return JSONResponse({"detail": str(exc)}, status_code=status)

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
            "model_choices": {provider: list(models) for provider, models in MODEL_CHOICES.items()},
            "contact_email_set": bool(settings.contact_email),
            # Whether Claude can run without a key from the page: on the client the server built from its
            # own credential, or, for a stand-in for Claude, on the variables that client is built from.
            "server_has_key": pipeline.llm.client is not None if isinstance(pipeline.llm, ClaudeLLM) else bool(server_credentials()),
            "server_has_openai_key": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
            "max_candidates": settings.max_candidates,
            "max_extractions": settings.max_extractions,
        }

    # -- Sign in with ChatGPT ------------------------------------------------------------------

    @app.get("/api/chatgpt")
    def chatgpt_state(request: Request, refresh: bool = False) -> dict:
        """The sign-in, the plan's models and the chosen ones; never a token. Another computer learns
        only whether someone is signed in."""
        if _remote(request):
            return {"status": chatgpt.state()["status"], "remote": True}
        if refresh and chatgpt.state()["status"] == "connected":
            try:
                chatgpt.refresh_models()
            except ChatGPTError:
                pass  # the session records what went wrong
        return {**chatgpt.state(), "remote": _remote(request)}

    def sign_in_url(request: Request, new_registration: bool = False) -> dict:
        if _remote(request):
            raise HTTPException(400, "Sign in with ChatGPT works only in a browser on the computer that runs the server.")
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        return {"url": chatgpt.start(port, new_registration=new_registration)}

    @app.post("/api/chatgpt/sign-in")
    def chatgpt_sign_in(request: Request) -> dict:
        """The address of ChatGPT's sign-in page, which comes back to this server's /auth/callback. After
        ChatGPT did not grant use of the plan, the session has it show its permission screen again."""
        return sign_in_url(request)

    @app.post("/api/chatgpt/another-account")
    def chatgpt_another_account(request: Request) -> dict:
        """The page's "Use another ChatGPT account": a sign-in on a new registration of this app, since
        ChatGPT ties each registration to one account. The current sign-in stays until the new one ends."""
        return sign_in_url(request, new_registration=True)

    @app.get(CALLBACK_PATH, response_class=HTMLResponse)
    def chatgpt_callback(request: Request) -> HTMLResponse:
        try:
            chatgpt.finish(request.url.query, request.headers.get("host", ""))
        except ChatGPTError as exc:
            return _callback_page("ChatGPT sign-in did not finish", str(exc), 400)
        return _callback_page("Signed in with ChatGPT", "Return to Effect Size Finder; this tab can be closed.", 200)

    @app.put("/api/chatgpt/models")
    def chatgpt_models(request: Request, body: ModelChoice) -> dict:
        _loopback_only(request)
        try:
            chatgpt.set_models(body.manager_model, body.worker_model)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {**chatgpt.state(), "remote": False}

    @app.post("/api/chatgpt/cancel")
    def chatgpt_cancel(request: Request) -> dict:
        """Gives up the sign-ins waiting for ChatGPT to send the browser back."""
        _loopback_only(request)
        chatgpt.cancel()
        return {**chatgpt.state(), "remote": False}

    @app.post("/api/chatgpt/sign-out")
    def chatgpt_sign_out(request: Request) -> dict:
        _loopback_only(request)
        chatgpt.sign_out()
        return {**chatgpt.state(), "remote": False}

    # -- projects -------------------------------------------------------------------------------

    @app.get("/api/projects")
    def list_projects() -> list[dict]:
        return store.projects()

    @app.post("/api/projects", status_code=201)
    def create_project(body: NewProject, agents: Agents = Depends(agents_headers)) -> dict:
        job = pipeline_for(agents)
        project_id = store.create_project(body.paragraph.strip())
        store.update_project(project_id, status="reading")
        submit(project_id, lambda: job.read_spec(project_id))
        return {"id": project_id}

    @app.get("/api/projects/{project_id}")
    def project(project_id: str) -> dict:
        project = get_project(project_id)
        project["counts"] = store.status_counts(project_id)
        project["usage"] = store.usage(project_id)
        # The models of this search, for naming who did what; usage above also counts earlier searches.
        project["run_usage"] = store.usage(project_id, since=project.get("run_started_at"))
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
    def run(project_id: str, agents: Agents = Depends(agents_headers)) -> dict:
        project = get_project(project_id)
        if not project["spec"]:
            raise HTTPException(400, "Read the problem first.")
        if project["status"] in RUNNING:
            raise HTTPException(409, "This project is already running.")
        if (project.get("summary") or {}).get("status") == "running":
            raise HTTPException(409, "Wait until the summary is written.")
        job = pipeline_for(agents)
        store.update_project(project_id, status="searching")
        submit(project_id, lambda: job.run(project_id))
        return {"ok": True}

    @app.post("/api/projects/{project_id}/summary", status_code=202)
    def summarize(project_id: str, agents: Agents = Depends(agents_headers)) -> dict:
        project = get_project(project_id)
        if project["status"] in RUNNING:
            raise HTTPException(409, "Wait until the search finishes.")
        if (project.get("summary") or {}).get("status") == "running":
            raise HTTPException(409, "The summary is already being written.")
        job = pipeline_for(agents)
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
    def extract(project_id: str, paper_id: int, agents: Agents = Depends(agents_headers)) -> dict:
        project = get_project(project_id)
        get_paper(project_id, paper_id)
        if project["status"] in RUNNING:
            raise HTTPException(409, "Wait until the current run finishes.")
        spec = SearchSpec.model_validate(upgrade_spec(project["spec"]))
        job = pipeline_for(agents)
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
        for row in export_rows(store, project_id, included_only):
            writer.writerow({column: csv_cell(value) for column, value in row.items()})
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
