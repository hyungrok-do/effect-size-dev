"""Claude API calls, and what the model providers share.

Claude Opus 5.5 is the manager by default: it reads the problem, plans the search, adjudicates
escalated extractions and reads the statistical context of each paper's numbers. Claude Haiku 5.5
is the worker by default: it screens every record and makes the first-pass extraction of every
paper. The page may pick other Claude models for either role. openai_llm.py gives OpenAI models the
same steps. Arithmetic never happens here; the models only read, quote and select justified planning
assumptions.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Protocol, TypeVar

import anthropic
from anthropic import beta_tool, transform_schema
from pydantic import BaseModel, TypeAdapter

from . import prompts
from .config import Settings
from .fulltext import FullText
from .schemas import (
    EvidenceSummary, OverlapReport, PaperExtraction, ScreenDecision, SearchLevel, SearchPlan, SearchSpec, StudyContext,
)

# Opus and Sonnet only: when a safety classifier declines a benign request, the API retries it on
# the model Anthropic recommends for that refusal category. Haiku has no server-side fallback.
OPUS_FALLBACK: dict[str, Any] = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}


def fallback_for(model: str) -> dict[str, Any]:
    """The server-side fallback parameters for a call on this model: none for Haiku, whichever role it plays."""
    return OPUS_FALLBACK if any(f"claude-{family}-" in model for family in ("opus", "sonnet")) else {}

T = TypeVar("T", bound=BaseModel)
UsageSink = Callable[[str, str, Any], None]


class ModelDeclined(RuntimeError):
    """The model (and any fallback) declined the request."""


class ModelTruncated(RuntimeError):
    """The response hit max_tokens before the structured output was complete."""


class NoCredentials(RuntimeError):
    """Claude was asked to run without a key: none from the page, and the server has none of its own."""


NO_KEY = "Enter your Anthropic API key first, on the Agents page."


def server_credentials() -> dict[str, str]:
    """The server's own Claude credential as client arguments: ANTHROPIC_API_KEY, else
    ANTHROPIC_AUTH_TOKEN, else none. Only these two count. Left to itself the SDK also takes a profile
    saved on disk, and the page would then be told the server has no key while jobs ran on it."""
    for variable, argument in (("ANTHROPIC_API_KEY", "api_key"), ("ANTHROPIC_AUTH_TOKEN", "auth_token")):
        value = os.environ.get(variable, "").strip()
        if value:
            return {argument: value}
    return {}


def server_client() -> anthropic.Anthropic | None:
    """A client on the server's own credential, or None when it has none."""
    credential = server_credentials()
    # An explicit credential argument also keeps the SDK from looking anywhere else.
    return anthropic.Anthropic(**credential) if credential else None


def output_schema(model: type) -> dict:
    """The JSON schema of a structured output, exactly as the Anthropic SDK sends it. OpenAI's strict
    mode takes it as it is: every object is closed and every property required."""
    return transform_schema(TypeAdapter(model).json_schema())


class LLM(Protocol):
    """What the pipeline calls: ClaudeLLM or openai_llm.OpenAILLM (and the tests' fake)."""

    def document(self, fulltext: FullText, title: str) -> dict: ...

    def read_spec(self, paragraph: str, sink: UsageSink | None = None) -> SearchSpec: ...

    def plan_search(self, spec: SearchSpec, toolbox: "SearchToolbox", sink: UsageSink | None = None) -> SearchPlan: ...

    def check_overlap(self, spec: SearchSpec, records: list[dict], sink: UsageSink | None = None) -> OverlapReport: ...

    def summarize(self, spec: SearchSpec, overlap: dict, evidence: dict, sink: UsageSink | None = None) -> EvidenceSummary: ...

    def adjudicate(
        self, spec: SearchSpec, document: dict, worker: PaperExtraction | None, flags: list[str], sink: UsageSink | None = None
    ) -> PaperExtraction: ...

    def study_context(
        self, spec: SearchSpec, document: dict, extraction: PaperExtraction, sink: UsageSink | None = None
    ) -> StudyContext: ...

    def screen(self, spec: SearchSpec, record_text: str, sink: UsageSink | None = None) -> ScreenDecision: ...

    def extract(self, spec: SearchSpec, document: dict, sink: UsageSink | None = None) -> PaperExtraction: ...


class SearchToolbox(Protocol):
    def count_pubmed(self, queries: list[str]) -> str: ...

    def preview_pubmed(self, query: str, max_results: int) -> str: ...

    def preview_europepmc(self, query: str, max_results: int) -> str: ...

    def preview_trials(self, query: str, max_results: int) -> str: ...


def search_tools(toolbox: SearchToolbox, submitted: dict[str, SearchPlan]) -> list:
    """The search agent's tools. A submitted plan is stored in `submitted["plan"]`."""

    @beta_tool
    def count_pubmed(queries: list[str]) -> str:
        """Count the PubMed hits of several queries at once, like a PubMed search history.

        Use it to check each concept block alone and to compare the levels before reading titles.

        Args:
            queries: Up to 12 E-utilities queries, e.g. ['mindfulness[mh] OR mindfulness[tiab]', 'depression[mh] OR depress*[tiab]'].
        """
        return toolbox.count_pubmed(queries[:MAX_COUNTED])

    @beta_tool
    def search_pubmed(query: str, max_results: int = 20) -> str:
        """Run a PubMed search and list the top records by relevance.

        Returns the total number of hits and, for each listed record: PMID | year | publication types | title.

        Args:
            query: An E-utilities query, e.g. '"mindfulness"[tiab] AND depress*[tiab] AND randomized controlled trial[pt]'.
            max_results: How many records to list, at most 50.
        """
        return toolbox.preview_pubmed(query, max(1, min(max_results, 50)))

    @beta_tool
    def search_europepmc(query: str, max_results: int = 20) -> str:
        """Search Europe PMC, which also searches the full text of open-access articles section by section, and preprints.

        Returns the total number of hits and, for each listed record: ID | year | kind | publication types | title. Kind MED is a PubMed record (the ID is its PMID), PMC an article only in PubMed Central, PPR a preprint.

        Args:
            query: A Europe PMC query, e.g. 'mindfulness AND depress* AND (METHODS:"PHQ-9" OR RESULTS:"PHQ-9")'.
            max_results: How many records to list, at most 50.
        """
        return toolbox.preview_europepmc(query, max(1, min(max_results, 50)))

    @beta_tool
    def search_clinicaltrials(query: str, max_results: int = 20) -> str:
        """Search ClinicalTrials.gov for studies with posted results.

        Returns the total number of matching studies and, for each listed study: NCT ID | status | start year | title.

        Args:
            query: A ClinicalTrials.gov search expression, e.g. 'mindfulness AND depression'.
            max_results: How many studies to list, at most 50.
        """
        return toolbox.preview_trials(query, max(1, min(max_results, 50)))

    @beta_tool
    def submit_search_plan(
        levels: list[SearchLevel], clinicaltrials_queries: list[str], extra_ids: list[str], summary: str
    ) -> str:
        """Record the final search plan. Call this exactly once, when the levels are final.

        Args:
            levels: The levels from the top down (reviews, primary, then full_text, wider or outcome_data if used); the top records of each are screened.
            clinicaltrials_queries: The final ClinicalTrials.gov queries (studies with posted results).
            extra_ids: Specific PMIDs or NCT IDs to include regardless of the queries.
            summary: Two or three sentences on the strategy, for the methods section.
        """
        submitted["plan"] = SearchPlan(
            levels=levels,
            clinicaltrials_queries=clinicaltrials_queries,
            extra_ids=extra_ids,
            summary=summary,
        )
        return SEARCH_PLAN_RECORDED

    return [count_pubmed, search_pubmed, search_europepmc, search_clinicaltrials, submit_search_plan]


SEARCH_PLAN_RECORDED = "Recorded. Reply with a one-paragraph summary and stop."
MAX_COUNTED = 12  # queries per count_pubmed call


def context_input(extraction: PaperExtraction) -> str:
    """The extraction as the manager reads it after the document, its results numbered from 1 for
    StudyContext to refer to."""
    d = extraction.model_dump()
    numbered = {**d, "results": [{"result": i + 1, **r} for i, r in enumerate(d["results"])]}
    return prompts.CONTEXT_INPUT + json.dumps(numbered, indent=1, ensure_ascii=False)


def summary_input(overlap: dict, evidence: dict) -> str:
    """What the summary is given (see evidence.build_evidence), with the overlap check."""
    data = {"records": evidence["records"], "estimands": evidence["estimands"], "overlap": overlap, "values": evidence["values"]}
    return "Records, the estimands of their results, the overlap check and the values:\n" + json.dumps(data, indent=1, ensure_ascii=False)


def _cached_system(text: str) -> list[dict]:
    # The instructions and the specification are identical across a project's calls, so cache them.
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


class ClaudeLLM:
    def __init__(
        self,
        settings: Settings,
        client: anthropic.Anthropic | None = None,
        manager_model: str | None = None,
        worker_model: str | None = None,
    ):
        """Without a client, the server's own credential (server_client); without that, client is None
        and every call fails at once with NO_KEY."""
        self.settings = settings
        self.client = client if client is not None else server_client()
        self.manager_model = manager_model or settings.manager_model
        self.worker_model = worker_model or settings.worker_model

    # -- plumbing ---------------------------------------------------------------

    def _api(self) -> anthropic.Anthropic:
        if self.client is None:
            raise NoCredentials(NO_KEY)
        return self.client

    @staticmethod
    def _record(sink: UsageSink | None, step: str, message: Any) -> None:
        if sink is not None and getattr(message, "usage", None) is not None:
            sink(step, message.model, message.usage)

    @staticmethod
    def _unwrap(message: Any, output_format: type[T]) -> T:
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            raise ModelDeclined(f"declined ({getattr(details, 'category', None) or 'no category'})")
        if message.stop_reason == "max_tokens":
            raise ModelTruncated("the response hit max_tokens")
        parsed = getattr(message, "parsed_output", None)
        if parsed is not None:
            return parsed
        text = next((b.text for b in message.content if b.type == "text"), "")
        return output_format.model_validate_json(text)

    def _parse(
        self,
        *,
        step: str,
        manager: bool,
        system: str,
        content: Any,
        output_format: type[T],
        effort: str,
        sink: UsageSink | None,
        max_tokens: int = 64000,
    ) -> T:
        model = self.manager_model if manager else self.worker_model
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": _cached_system(system),
            "messages": [{"role": "user", "content": content}],
            "output_config": {"effort": effort},
            "output_format": output_format,
            **fallback_for(model),
        }
        # Streamed so long documents and high-effort thinking don't run into HTTP timeouts.
        with self._api().beta.messages.stream(**params) as stream:
            message = stream.get_final_message()
        self._record(sink, step, message)
        return self._unwrap(message, output_format)

    def document(self, fulltext: FullText, title: str) -> dict:
        """The paper for extract(), adjudicate() and study_context(): a document block, the PDF itself
        when there is one."""
        return fulltext.document_block(title)

    # -- manager (Opus by default) ------------------------------------------------

    def read_spec(self, paragraph: str, sink: UsageSink | None = None) -> SearchSpec:
        return self._parse(
            step="spec",
            manager=True,
            system=prompts.SPEC,
            content=paragraph,
            output_format=SearchSpec,
            effort="high",
            sink=sink,
        )

    def plan_search(self, spec: SearchSpec, toolbox: SearchToolbox, sink: UsageSink | None = None) -> SearchPlan:
        submitted: dict[str, SearchPlan] = {}
        runner = self._api().beta.messages.tool_runner(
            model=self.manager_model,
            max_tokens=16000,
            system=_cached_system(prompts.SEARCH),
            tools=search_tools(toolbox, submitted),
            messages=[{"role": "user", "content": "Specification:\n" + spec.model_dump_json(indent=2)}],
            output_config={"effort": "high"},
            max_iterations=self.settings.search_max_iterations,
            **fallback_for(self.manager_model),
        )
        for message in runner:
            self._record(sink, "search", message)
            if message.stop_reason == "refusal":
                raise ModelDeclined("the search agent's request was declined")
        if "plan" not in submitted:
            raise RuntimeError("The search agent stopped without submitting a search plan.")
        return submitted["plan"]

    def check_overlap(self, spec: SearchSpec, records: list[dict], sink: UsageSink | None = None) -> OverlapReport:
        return self._parse(
            step="overlap",
            manager=True,
            system=prompts.OVERLAP + "\n\nSpecification:\n" + spec.model_dump_json(indent=2),
            content=[{"type": "text", "text": "Records:\n" + json.dumps(records, indent=1, ensure_ascii=False)}],
            output_format=OverlapReport,
            effort="high",
            sink=sink,
        )

    def summarize(self, spec: SearchSpec, overlap: dict, evidence: dict, sink: UsageSink | None = None) -> EvidenceSummary:
        return self._parse(
            step="summarize",
            manager=True,
            system=prompts.SUMMARIZE + "\n\nSpecification:\n" + spec.model_dump_json(indent=2),
            content=[{"type": "text", "text": summary_input(overlap, evidence)}],
            output_format=EvidenceSummary,
            effort="high",
            sink=sink,
        )

    def adjudicate(
        self,
        spec: SearchSpec,
        document: dict,
        worker: PaperExtraction | None,
        flags: list[str],
        sink: UsageSink | None = None,
    ) -> PaperExtraction:
        junior = worker.model_dump_json(indent=1) if worker else "None: the junior reviewer could not complete it."
        text = (
            f"Junior reviewer's extraction:\n{junior}\n\n"
            "Flags from the automatic checks:\n" + "\n".join(f"- {f}" for f in flags)
        )
        return self._parse(
            step="adjudicate",
            manager=True,
            system=prompts.ADJUDICATE + "\n\nSpecification:\n" + spec.model_dump_json(indent=2),
            content=[document, {"type": "text", "text": text}],
            output_format=PaperExtraction,
            effort="xhigh",
            sink=sink,
        )

    def study_context(
        self, spec: SearchSpec, document: dict, extraction: PaperExtraction, sink: UsageSink | None = None
    ) -> StudyContext:
        return self._parse(
            step="context",
            manager=True,
            system=prompts.CONTEXT + "\n\nSpecification:\n" + spec.model_dump_json(indent=2),
            content=[document, {"type": "text", "text": context_input(extraction)}],
            output_format=StudyContext,
            effort="high",
            sink=sink,
        )

    # -- workers (Haiku by default) -----------------------------------------------

    def screen(self, spec: SearchSpec, record_text: str, sink: UsageSink | None = None) -> ScreenDecision:
        return self._parse(
            step="screen",
            manager=False,
            system=prompts.SCREEN + "\n\nSpecification:\n" + spec.model_dump_json(indent=2),
            content=record_text,
            output_format=ScreenDecision,
            effort="low",
            sink=sink,
            max_tokens=8000,
        )

    def extract(self, spec: SearchSpec, document: dict, sink: UsageSink | None = None) -> PaperExtraction:
        return self._parse(
            step="extract",
            manager=False,
            system=prompts.EXTRACT + "\n\nSpecification:\n" + spec.model_dump_json(indent=2),
            content=[document, {"type": "text", "text": "Extract the statistics relevant to the specification."}],
            output_format=PaperExtraction,
            effort="medium",
            sink=sink,
        )


def describe_error(exc: BaseException) -> str:
    """A message a person can act on."""
    if isinstance(exc, anthropic.AuthenticationError) or (
        isinstance(exc, TypeError) and "authentication method" in str(exc)
    ):
        return "The Anthropic API key is missing or was not accepted. Enter a valid key in the API key box, on the Agents page, and try again."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "This Anthropic API key may not use the model. Check the key's workspace and model access."
    if isinstance(exc, anthropic.RateLimitError):
        return "The Anthropic API rate limit was reached. Wait a minute and try again."
    if isinstance(exc, anthropic.APIStatusError) and (exc.status_code == 402 or "credit balance" in str(exc).lower()):
        return "The Anthropic account has run out of credit. Add credit under Plans & Billing in the Anthropic Console."
    return str(exc)


def usage_dict(usage: Any) -> dict[str, int]:
    return {
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }


def dumps(model: BaseModel | None) -> str | None:
    return json.dumps(model.model_dump()) if model is not None else None
