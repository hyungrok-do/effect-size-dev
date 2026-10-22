"""Claude API calls.

Claude Opus 5.5 is the manager: it reads the problem, plans the search and adjudicates escalated
extractions. Claude Haiku 5.5 is the worker: it screens every record and makes the first-pass
extraction of every paper. Arithmetic never happens here; Claude only reads and quotes.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Protocol, TypeVar

import anthropic
from anthropic import beta_tool
from pydantic import BaseModel

from . import prompts
from .config import Settings
from .schemas import EvidenceSummary, OverlapReport, PaperExtraction, ScreenDecision, SearchLevel, SearchPlan, SearchSpec

# Opus only: when a safety classifier declines a benign request, the API retries it on the
# model Anthropic recommends for that refusal category. Haiku has no server-side fallback.
OPUS_FALLBACK: dict[str, Any] = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}

T = TypeVar("T", bound=BaseModel)
UsageSink = Callable[[str, str, Any], None]


class ModelDeclined(RuntimeError):
    """The model (and any fallback) declined the request."""


class ModelTruncated(RuntimeError):
    """The response hit max_tokens before the structured output was complete."""


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


def _cached_system(text: str) -> list[dict]:
    # The instructions and the specification are identical across a project's calls, so cache them.
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


class ClaudeLLM:
    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None):
        self.settings = settings
        self.client = client or anthropic.Anthropic()

    # -- plumbing ---------------------------------------------------------------

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
        params: dict[str, Any] = {
            "model": self.settings.manager_model if manager else self.settings.worker_model,
            "max_tokens": max_tokens,
            "system": _cached_system(system),
            "messages": [{"role": "user", "content": content}],
            "output_config": {"effort": effort},
            "output_format": output_format,
        }
        if manager:
            params.update(OPUS_FALLBACK)
        # Streamed so long documents and high-effort thinking don't run into HTTP timeouts.
        with self.client.beta.messages.stream(**params) as stream:
            message = stream.get_final_message()
        self._record(sink, step, message)
        return self._unwrap(message, output_format)

    # -- manager (Opus) -----------------------------------------------------------

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
        runner = self.client.beta.messages.tool_runner(
            model=self.settings.manager_model,
            max_tokens=16000,
            system=_cached_system(prompts.SEARCH),
            tools=search_tools(toolbox, submitted),
            messages=[{"role": "user", "content": "Specification:\n" + spec.model_dump_json(indent=2)}],
            output_config={"effort": "high"},
            max_iterations=self.settings.search_max_iterations,
            **OPUS_FALLBACK,
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
        data = {"records": evidence["records"], "overlap": overlap, "values": evidence["values"]}
        return self._parse(
            step="summarize",
            manager=True,
            system=prompts.SUMMARIZE + "\n\nSpecification:\n" + spec.model_dump_json(indent=2),
            content=[{"type": "text", "text": "Records, the overlap check and the values:\n" + json.dumps(data, indent=1, ensure_ascii=False)}],
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

    # -- workers (Haiku) ----------------------------------------------------------

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
        return "The Anthropic API key is missing or was not accepted. Enter a valid key in the API key box and try again."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "This Anthropic API key may not use the model. Check the key's workspace and model access."
    if isinstance(exc, anthropic.RateLimitError):
        return "The Anthropic API rate limit was reached. Wait a minute and try again."
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
