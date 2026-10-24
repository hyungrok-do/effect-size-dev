"""OpenAI models through the Responses API: the same steps, prompts and schemas as ClaudeLLM.

The manager model reads the problem, plans the search, checks overlap, summarizes and adjudicates;
the worker model screens every record and extracts every paper. In the local edition the calls run
on the user's ChatGPT plan (Sign in with ChatGPT, see chatgpt.py), so every request keeps to what
plan usage accepts: not stored and streamed, the system prompt in `instructions`, no cap on output
tokens or sampling settings, and the search agent's tools grouped in a namespace. An ordinary API
key accepts the same requests. As with Claude, the models only read and quote; code does any
arithmetic.
"""

from __future__ import annotations

import json
import re
import time
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Iterator, TypeVar

import httpx
from pydantic import BaseModel

from . import prompts
from .config import Settings
from .fulltext import FullText
from .llm import ModelDeclined, ModelTruncated, SearchToolbox, UsageSink, output_schema, search_tools
from .schemas import EvidenceSummary, OverlapReport, PaperExtraction, ScreenDecision, SearchPlan, SearchSpec

API = "https://api.openai.com/v1"
TOOL_NAMESPACE = "literature"
# Reasoning efforts from least to most; a model's catalog entry may list the ones it takes.
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
MAX_SERVER_MESSAGE = 300

T = TypeVar("T", bound=BaseModel)


class OpenAIError(RuntimeError):
    """A failed request, with a message a person can act on."""

    def __init__(self, code: str, message: str, status: int | None = None, retryable: bool = False, request_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.request_id = request_id


# -- what goes into a request --------------------------------------------------------------


def strict_schema(schema: Any) -> Any:
    """A JSON schema as OpenAI's strict mode takes it: every object closed and every property
    required. An optional property becomes nullable and loses its default, so the model sends null
    for "use the default" (see without_nulls)."""
    if isinstance(schema, list):
        return [strict_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "default":
            continue
        if key in ("properties", "$defs"):
            out[key] = {name: strict_schema(sub) for name, sub in value.items()}
        else:
            out[key] = strict_schema(value)
    if "properties" in out:
        required = set(schema.get("required", []))
        for name, sub in out["properties"].items():
            if name not in required:
                out["properties"][name] = _nullable(sub)
        out["required"] = list(out["properties"])
        out["additionalProperties"] = False
    return out


def _nullable(schema: dict) -> dict:
    if isinstance(schema.get("type"), str) and "enum" not in schema:
        return {**schema, "type": [schema["type"], "null"]}
    return {"anyOf": [schema, {"type": "null"}]}


def without_nulls(arguments: dict) -> dict:
    """Tool arguments with the nulls strict mode asked for dropped, so the tool's defaults apply."""
    return {key: value for key, value in arguments.items() if value is not None}


def function_tools(tools: Iterable[dict]) -> list[dict]:
    """Claude tool definitions ({name, description, input_schema}) as strict OpenAI function tools."""
    return [
        {
            "type": "function",
            "name": tool["name"],
            "description": tool["description"],
            "parameters": strict_schema(tool["input_schema"]),
            "strict": True,
        }
        for tool in tools
    ]


def pdf_name(title: str) -> str:
    stem = re.sub(r"[^\w .-]+", "", title)[:80].strip(" .")
    return f"{stem or 'document'}.pdf"


def input_content(content: Any, files: bool = True) -> list[dict]:
    """Claude message content (a string, or text and document blocks) as Responses input parts.

    A PDF goes as a file, or, with files=False, as its text layer (the `text` that document()
    keeps with it)."""
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    parts = []
    for block in content:
        kind = block.get("type")
        if kind == "text":
            parts.append({"type": "input_text", "text": block["text"]})
        elif kind == "document":
            source, title = block["source"], block.get("title") or "Document"
            if source["type"] == "base64" and source.get("media_type") == "application/pdf":
                if files:
                    parts.append({"type": "input_file", "filename": pdf_name(title), "file_data": f"data:application/pdf;base64,{source['data']}"})
                else:
                    parts.append({"type": "input_text", "text": f"Document: {title} (the text layer of its PDF)\n\n{block.get('text', '')}"})
            else:
                parts.append({"type": "input_text", "text": f"Document: {title}\n\n{source['data']}"})
        else:
            raise ValueError(f"Unsupported content block: {kind}")
    return parts


def effort_for(requested: str, levels: list[str] | None) -> str:
    """The requested reasoning effort, or the nearest one below it that the model takes."""
    known = [level for level in levels or [] if level in EFFORTS]
    if not known or requested in known or requested not in EFFORTS:
        return requested
    rank = EFFORTS.index
    lower = [level for level in known if rank(level) <= rank(requested)]
    return max(lower, key=rank) if lower else min(known, key=rank)


def usage_of(usage: dict | None) -> SimpleNamespace:
    """OpenAI usage in the fields the store keeps for Claude. OpenAI counts cached input inside
    input_tokens; Claude counts it apart, so it is moved to cache_read_input_tokens."""
    usage = usage or {}
    cached = (usage.get("input_tokens_details") or {}).get("cached_tokens") or 0
    return SimpleNamespace(
        input_tokens=max(0, (usage.get("input_tokens") or 0) - cached),
        output_tokens=usage.get("output_tokens") or 0,
        cache_read_input_tokens=cached,
        cache_creation_input_tokens=0,
    )


# -- errors ----------------------------------------------------------------------------------

# Codes OpenAI uses for plan usage and common API failures: (message, retryable).
KNOWN_ERRORS: dict[str, tuple[str, bool]] = {
    "subscription_sharing_user_not_eligible": (
        "This ChatGPT account can't lend its plan to apps. Plan usage needs ChatGPT Plus or Pro.", False),
    "subscription_sharing_usage_limit_exceeded": (
        "This app has used its share of your ChatGPT plan for now. Check your usage and the app's limit in ChatGPT Settings.", False),
    "subscription_sharing_usage_unavailable": ("ChatGPT could not check your plan's usage. Try again shortly.", True),
    "subscription_sharing_unsupported_capability": ("ChatGPT plan usage does not support part of this request.", False),
    "subscription_sharing_route_not_supported": ("ChatGPT plan usage does not support this kind of request.", False),
    "subscription_sharing_invalid_user": ("ChatGPT could not confirm this account's plan. Sign in with ChatGPT again.", False),
    "subscription_sharing_user_unavailable": ("Your ChatGPT account is unavailable for the moment. Try again shortly.", True),
    "chatpass_v2_scope_not_authorized": ("The ChatGPT sign-in does not allow this request. Sign in again and allow plan usage.", False),
    "invalid_api_key": ("OpenAI did not accept the API key.", False),
    "model_not_found": ("This model is not available to this account. Choose another model.", False),
    "insufficient_quota": ("The OpenAI account has run out of credit. Check its billing.", False),
    "rate_limit_exceeded": ("The OpenAI rate limit was reached. Wait a minute and try again.", True),
    "server_error": ("OpenAI had a server error. Try again shortly.", True),
}


def api_error(body: Any, status: int | None = None, request_id: str | None = None) -> OpenAIError:
    detail = body if isinstance(body, dict) else {}
    for _ in range(4):
        if isinstance(detail.get("error"), dict):
            detail = detail["error"]
        elif isinstance(detail.get("detail"), dict):
            detail = detail["detail"]
        else:
            break
    code = detail.get("code") if isinstance(detail.get("code"), str) else None
    if code is None and isinstance(detail.get("error"), str):
        code = detail["error"]  # OAuth style: {"error": "invalid_token"}
    code = code or (detail.get("type") if isinstance(detail.get("type"), str) else None) or "api_error"
    server = detail.get("message") if isinstance(detail.get("message"), str) else ""
    server = server[:MAX_SERVER_MESSAGE] + ("…" if len(server) > MAX_SERVER_MESSAGE else "")
    known = KNOWN_ERRORS.get(code) or KNOWN_ERRORS.get(code.replace("subscription_sharing_v2_", "subscription_sharing_"))
    if known:
        message, retryable = known
        if code.endswith("unsupported_capability") and isinstance(detail.get("param"), str):
            message = f"{message[:-1]} ({detail['param']})."
    elif status == 401:
        message, retryable = "OpenAI did not accept the sign-in or the API key.", False
    elif status == 403:
        message, retryable = "OpenAI refused the request. Check that this account may use the model.", False
    elif status == 429:
        message, retryable = "OpenAI asked to slow down. Wait a minute and try again.", True
    elif status is not None and status >= 500:
        message, retryable = "OpenAI is unavailable for the moment. Try again shortly.", True
    else:
        message, retryable = "OpenAI could not complete the request.", False
    if server and not known:
        message = f"{message} OpenAI said: {server}"
    suffix = f" ({status or 'error'} {code}{f', request {request_id}' if request_id else ''})"
    return OpenAIError(code, message + suffix, status, retryable, request_id)


# -- the HTTP client -------------------------------------------------------------------------


def sse_events(lines: Iterable[str]) -> Iterator[dict]:
    """The JSON events of a server-sent event stream."""
    data: list[str] = []
    for line in lines:
        if line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
        elif line == "" and data:
            payload = "\n".join(data)
            data = []
            if payload.strip() and payload.strip() != "[DONE]":
                yield json.loads(payload)
    if data and "".join(data).strip() not in ("", "[DONE]"):
        yield json.loads("\n".join(data))


def read_stream(lines: Iterable[str], request_id: str | None = None) -> dict:
    """The finished response from a Responses stream: its output items, status and usage."""
    items: list[dict] = []
    for event in sse_events(lines):
        kind = event.get("type")
        if kind == "response.output_item.done" and isinstance(event.get("item"), dict):
            items.append(event["item"])
        elif kind in ("response.completed", "response.incomplete"):
            response = dict(event.get("response") or {})
            response["output"] = items or response.get("output") or []
            return response
        elif kind == "response.failed":
            response = event.get("response") or {}
            raise api_error({"error": response.get("error") or {}}, None, request_id)
        elif kind == "error":
            raise api_error(event, None, request_id)
    raise OpenAIError("stream_interrupted", "The answer from OpenAI stopped before it finished. Try again.", retryable=True, request_id=request_id)


class Responses:
    """POST /responses, streamed, with retries for the failures worth retrying."""

    def __init__(
        self,
        token: Callable[[], str],
        client: httpx.Client | None = None,
        base_url: str = API,
        retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.token = token
        # Long documents and high reasoning effort can take minutes before the stream ends.
        self.client = client or httpx.Client(timeout=httpx.Timeout(60.0, read=600.0), follow_redirects=False)
        self.base_url = base_url.rstrip("/")
        self.retries = retries
        self.sleep = sleep

    def create(self, body: dict) -> dict:
        for attempt in range(self.retries + 1):
            try:
                return self._once(body)
            except OpenAIError as exc:
                if not exc.retryable or attempt == self.retries:
                    raise
            except httpx.TransportError as exc:
                if attempt == self.retries:
                    raise OpenAIError("network_error", f"Could not reach OpenAI ({exc.__class__.__name__}). Check the connection and try again.", retryable=True) from exc
            self.sleep(min(2.0**attempt, 8.0))
        raise AssertionError("unreachable")

    def _once(self, body: dict) -> dict:
        headers = {"Authorization": f"Bearer {self.token()}", "Accept": "text/event-stream", "Content-Type": "application/json"}
        with self.client.stream("POST", f"{self.base_url}/responses", headers=headers, content=json.dumps(body)) as response:
            request_id = response.headers.get("x-request-id")
            if response.status_code >= 400:
                response.read()
                try:
                    detail = response.json()
                except ValueError:
                    detail = None
                raise api_error(detail, response.status_code, request_id)
            return read_stream(response.iter_lines(), request_id)


# -- the steps -------------------------------------------------------------------------------


def output_text(response: dict) -> str:
    """The text of a finished response; a refusal or an early stop raises."""
    if response.get("status") == "incomplete":
        reason = (response.get("incomplete_details") or {}).get("reason") or "incomplete"
        if reason == "content_filter":
            raise ModelDeclined("declined (content filter)")
        raise ModelTruncated(f"the response stopped early ({reason})")
    text = []
    for item in response.get("output") or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if part.get("type") == "refusal":
                raise ModelDeclined(f"declined ({(part.get('refusal') or 'no reason given')[:200]})")
            if part.get("type") == "output_text":
                text.append(part.get("text") or "")
    return "".join(text)


class OpenAILLM:
    """The ClaudeLLM interface on OpenAI models."""

    def __init__(
        self,
        settings: Settings,
        token: Callable[[], str],
        manager_model: str | None = None,
        worker_model: str | None = None,
        client: httpx.Client | None = None,
        reasoning_levels: Callable[[str], list[str] | None] | None = None,
        namespaced_tools: bool = True,
    ):
        self.settings = settings
        self.manager_model = manager_model or settings.openai_manager_model
        self.worker_model = worker_model or settings.openai_worker_model
        self.responses = Responses(token, client)
        self.reasoning_levels = reasoning_levels or (lambda model: None)
        self.namespaced_tools = namespaced_tools
        # Plan usage may not take PDF files; after the first refusal, PDFs go as their text layer.
        self.files = True

    # -- plumbing ---------------------------------------------------------------------------

    @staticmethod
    def _record(sink: UsageSink | None, step: str, model: str, response: dict) -> None:
        if sink is not None and response.get("usage") is not None:
            sink(step, response.get("model") or model, usage_of(response["usage"]))

    def _body(self, model: str, system: str, input_items: list, effort: str) -> dict:
        body = {"model": model, "instructions": system, "input": input_items, "store": False, "stream": True}
        levels = self.reasoning_levels(model)
        if levels != []:  # a catalog entry that lists no levels: a model that takes no reasoning setting
            body["reasoning"] = {"effort": effort_for(effort, levels)}
        return body

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
    ) -> T:
        model = self.manager_model if manager else self.worker_model
        while True:
            files = self.files
            if not files and _has_pdf(content) and not _pdf_text(content).strip():
                raise OpenAIError(
                    "pdf_not_readable",
                    "ChatGPT plan usage does not take PDF files, and this PDF has no text layer to send instead. "
                    "Upload a PDF with selectable text, or use the Claude API or the OpenAI API for it.",
                )
            body = self._body(model, system, [{"role": "user", "content": input_content(content, files)}], effort)
            body["text"] = {"format": {"type": "json_schema", "name": output_format.__name__, "schema": output_schema(output_format), "strict": True}}
            try:
                response = self.responses.create(body)
                break
            except OpenAIError as exc:
                if files and exc.code.endswith("unsupported_capability") and _has_pdf(content):
                    self.files = False
                    continue
                raise
        self._record(sink, step, model, response)
        return output_format.model_validate_json(output_text(response))

    def document(self, fulltext: FullText, title: str) -> dict:
        """The paper for extract() and adjudicate(): Claude's document block, with a PDF's text layer
        kept beside it for when plan usage won't take the file."""
        block = fulltext.document_block(title)
        if fulltext.pdf is not None:
            block["text"] = fulltext.text
        return block

    # -- manager ----------------------------------------------------------------------------

    def read_spec(self, paragraph: str, sink: UsageSink | None = None) -> SearchSpec:
        return self._parse(step="spec", manager=True, system=prompts.SPEC, content=paragraph, output_format=SearchSpec, effort="high", sink=sink)

    def plan_search(self, spec: SearchSpec, toolbox: SearchToolbox, sink: UsageSink | None = None) -> SearchPlan:
        submitted: dict[str, SearchPlan] = {}
        tools = {tool.name: tool for tool in search_tools(toolbox, submitted)}
        definitions = function_tools(tool.to_dict() for tool in tools.values())
        if self.namespaced_tools:
            # Plan usage takes function tools only inside a namespace; an API key takes either.
            definitions = [{"type": "namespace", "name": TOOL_NAMESPACE, "description": "Literature searches for planning the search.", "tools": definitions}]
        model = self.manager_model
        items: list = [{"role": "user", "content": [{"type": "input_text", "text": "Specification:\n" + spec.model_dump_json(indent=2)}]}]
        for _ in range(self.settings.search_max_iterations):
            body = self._body(model, prompts.SEARCH, items, "high")
            body["tools"] = definitions
            # Not stored, so the reasoning comes back encrypted to send with the next turn.
            body["include"] = ["reasoning.encrypted_content"]
            response = self.responses.create(body)
            self._record(sink, "search", model, response)
            output = response.get("output") or []
            output_text(response)  # a refusal or an early stop raises
            items.extend(output)
            calls = [item for item in output if item.get("type") == "function_call"]
            if not calls:
                break
            for call in calls:
                items.append({"type": "function_call_output", "call_id": call["call_id"], "output": _run_tool(tools, call)})
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
        text = f"Junior reviewer's extraction:\n{junior}\n\nFlags from the automatic checks:\n" + "\n".join(f"- {f}" for f in flags)
        return self._parse(
            step="adjudicate",
            manager=True,
            system=prompts.ADJUDICATE + "\n\nSpecification:\n" + spec.model_dump_json(indent=2),
            content=[document, {"type": "text", "text": text}],
            output_format=PaperExtraction,
            effort="xhigh",
            sink=sink,
        )

    # -- workers ----------------------------------------------------------------------------

    def screen(self, spec: SearchSpec, record_text: str, sink: UsageSink | None = None) -> ScreenDecision:
        return self._parse(
            step="screen",
            manager=False,
            system=prompts.SCREEN + "\n\nSpecification:\n" + spec.model_dump_json(indent=2),
            content=record_text,
            output_format=ScreenDecision,
            effort="low",
            sink=sink,
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


def _pdf_text(content: Any) -> str:
    return "".join(block.get("text", "") for block in content if block.get("type") == "document")


def _has_pdf(content: Any) -> bool:
    return not isinstance(content, str) and any(
        block.get("type") == "document" and block["source"]["type"] == "base64" for block in content
    )


def _run_tool(tools: dict, call: dict) -> str:
    """Run one function call; a failure goes back to the model as text, as Claude's tool runner does."""
    name = call.get("name") or ""
    # A call inside the namespace carries the namespace in its own field; strip it if it is in the name too.
    tool = tools.get(name) or tools.get(re.sub(rf"^{TOOL_NAMESPACE}[.:/_]+", "", name))
    if tool is None:
        return f"Error: Tool '{name}' not found"
    try:
        arguments = json.loads(call.get("arguments") or "{}")
        return str(tool.call(without_nulls(arguments) if isinstance(arguments, dict) else arguments))
    except Exception as exc:
        return f"Error: {exc}"
