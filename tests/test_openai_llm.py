"""The OpenAI layer against a mock Responses API: what each step sends (within what ChatGPT plan
usage accepts), how answers, refusals, early stops and errors are read, the search agent's tool
loop, and a whole search run on OpenAI models."""

import base64
import json

import httpx
import pytest

from esf import prompts
from esf.config import Settings
from esf.fulltext import FullText
from esf.llm import ModelDeclined, ModelTruncated, context_input, describe_error, output_schema, search_tools
from esf.openai_llm import OpenAIError, OpenAILLM, at_most, effort_for, function_tools, input_content, strict_schema
from esf.pipeline import Pipeline
from esf.schemas import EvidenceSummary, OverlapReport, PaperExtraction, ScreenDecision, SearchSpec, StudyContext
from esf.sources import Sources
from esf.store import Store
from fixtures import (
    EUROPEPMC_QUERY,
    PLAN,
    SPEC,
    FakeLLM,
    FakeOpenAI,
    LiteratureAPIs,
    function_call_item,
    message_item,
    mock_client,
    openai_sse,
    reasoning_item,
)
from test_llm import EXTRACTION, SCREEN, Toolbox

# Fields plan usage rejects; no request may carry them.
UNSUPPORTED = {"background", "conversation", "max_output_tokens", "max_tool_calls", "metadata", "moderation", "prompt",
               "prompt_cache_retention", "safety_identifier", "temperature", "top_logprobs", "top_p", "truncation", "user",
               "previous_response_id"}


class Scripted:
    """Answers each request with the next scripted (status, body) and records what was sent."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append((request, body))
        status, payload = self.responses.pop(0)
        if isinstance(payload, bytes):
            return httpx.Response(status, content=payload, headers={"content-type": "text/event-stream"})
        return httpx.Response(status, json=payload)


def answer(payload, model="gpt-6-luna", **options):
    return 200, openai_sse([message_item(json.dumps(payload))], model, **options)


def make_llm(handler, **options):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    llm = OpenAILLM(Settings(), lambda: "plan-token", client=client, **options)
    llm.responses.sleep = lambda seconds: None
    return llm


def check_plan_usage_rules(body):
    assert body["store"] is False and body["stream"] is True
    assert not UNSUPPORTED & set(body), UNSUPPORTED & set(body)
    assert all(item.get("role") != "system" for item in body["input"])
    assert body["instructions"]


def test_worker_steps_run_on_the_worker_model():
    api = Scripted([answer(SCREEN), answer(EXTRACTION)])
    llm = make_llm(api)
    usage = []
    assert llm.screen(SPEC, "Title: a trial", sink=lambda step, model, u: usage.append((step, model, u))).decision == "include"
    document = {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": "PHQ-9 8.1 (4.2)"}, "title": "A trial"}
    assert llm.extract(SPEC, document).results[0].values[0].value == 8.1

    (screen_request, screen), (_, extract) = api.requests
    assert screen_request.headers["authorization"] == "Bearer plan-token"
    assert screen_request.url == "https://api.openai.com/v1/responses"
    assert "openai-organization" not in screen_request.headers and "openai-project" not in screen_request.headers
    for body in (screen, extract):
        check_plan_usage_rules(body)
        assert body["model"] == "gpt-6-luna"
    assert screen["reasoning"] == {"effort": "low"} and extract["reasoning"] == {"effort": "medium"}
    assert screen["text"]["format"] == {"type": "json_schema", "name": "ScreenDecision", "schema": output_schema(ScreenDecision), "strict": True}
    assert screen["instructions"].startswith("You screen records") and "Specification:" in screen["instructions"]
    assert screen["input"] == [{"role": "user", "content": [{"type": "input_text", "text": "Title: a trial"}]}]
    assert extract["input"][0]["content"][0] == {"type": "input_text", "text": "Document: A trial\n\nPHQ-9 8.1 (4.2)"}
    # OpenAI counts cached input inside input_tokens; the store keeps it apart, as Claude reports it.
    step, model, u = usage[0]
    assert (step, model) == ("screen", "gpt-6-luna")
    assert (u.input_tokens, u.cache_read_input_tokens, u.output_tokens, u.cache_creation_input_tokens) == (100, 20, 30, 0)


def test_manager_steps_run_on_the_manager_model_at_the_effort_it_takes():
    api = Scripted([answer(SPEC.model_dump(), "gpt-6.1-sol"), answer(EXTRACTION, "gpt-6.1-sol")])
    llm = make_llm(api, reasoning_levels=lambda model: ["low", "medium", "high"] if model == "gpt-6.1-sol" else None)
    assert llm.read_spec("A paragraph.").outcomes[0].measure == "PHQ-9"
    llm.adjudicate(SPEC, {"type": "text", "text": "the paper"}, None, ["the worker could not complete the extraction"])
    (_, spec), (_, adjudicate) = api.requests
    assert spec["model"] == adjudicate["model"] == "gpt-6.1-sol"
    # Adjudication asks for xhigh; this model's catalog entry tops out at high.
    assert spec["reasoning"] == {"effort": "high"} and adjudicate["reasoning"] == {"effort": "high"}
    assert "None: the junior reviewer" in adjudicate["input"][0]["content"][1]["text"]
    assert effort_for("xhigh", None) == "xhigh" and effort_for("medium", ["high", "xhigh"]) == "high"


@pytest.mark.parametrize("plan", [True, False])
def test_the_manager_reads_the_context_from_the_document_then_the_numbered_extraction(plan):
    extraction = PaperExtraction.model_validate(EXTRACTION)
    reading = FakeLLM().study_context(SPEC, {"title": "A trial"}, extraction)
    api = Scripted([answer(reading.model_dump(), "gpt-6.1-sol")])
    llm = make_llm(api, namespaced_tools=plan)
    document = {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": "PHQ-9 8.1 (4.2)"}, "title": "A trial"}
    usage = []
    assert llm.study_context(SPEC, document, extraction, sink=lambda step, model, u: usage.append((step, model))) == reading
    [(_, body)] = api.requests
    check_plan_usage_rules(body)
    assert body["model"] == "gpt-6.1-sol" and body["reasoning"] == {"effort": "high"}
    assert body["text"]["format"] == {"type": "json_schema", "name": "StudyContext", "schema": output_schema(StudyContext), "strict": True}
    assert body["instructions"] == prompts.CONTEXT + "\n\nSpecification:\n" + SPEC.model_dump_json(indent=2)
    assert body["input"] == [{"role": "user", "content": [
        {"type": "input_text", "text": "Document: A trial\n\nPHQ-9 8.1 (4.2)"},
        {"type": "input_text", "text": context_input(extraction)},
    ]}]
    assert context_input(extraction).startswith(prompts.CONTEXT_INPUT + '{\n "study_design": "RCT"')
    assert '"results": [\n  {\n   "result": 1,\n   "outcome_measure": "PHQ-9"' in context_input(extraction)
    assert usage == [("context", "gpt-6.1-sol")]


def test_the_summary_is_given_the_estimands_of_the_results():
    summary = {"overview": "One trial.", "needs": [], "overlap": "", "caveats": [], "gaps": []}
    api = Scripted([answer(summary, "gpt-6.1-sol")])
    evidence = {"records": [{"key": "R1", "context": {"design": "RCT", "sample_size": "", "sample_size_found": False, "notes": ""}}],
                "estimands": [{"key": "E1", "record": "R1", "result": "R1:0"}],
                "values": [{"key": "V1", "result": "R1:0", "estimand": "E1"}]}
    make_llm(api).summarize(SPEC, {"groups": [], "notes": ""}, evidence)
    [(_, body)] = api.requests
    assert body["model"] == "gpt-6.1-sol" and body["instructions"].startswith(prompts.SUMMARIZE)
    [part] = body["input"][0]["content"]
    header, data = part["text"].split("\n", 1)
    assert header == "Records, the estimands of their results, the overlap check and the values:"
    assert list(json.loads(data)) == ["records", "estimands", "overlap", "values"]
    assert json.loads(data)["estimands"] == evidence["estimands"] and json.loads(data)["records"] == evidence["records"]


@pytest.mark.parametrize("plan", [True, False])
def test_an_effort_above_high_is_capped_on_the_plan_when_the_model_levels_are_unknown(plan):
    api = Scripted([answer(EXTRACTION, "gpt-6.1-sol"), answer(SPEC.model_dump(), "gpt-6.1-sol"), answer(SCREEN)])
    llm = make_llm(api, namespaced_tools=plan)  # no catalog: no model's levels are known
    llm.adjudicate(SPEC, {"type": "text", "text": "the paper"}, None, ["a flag"])
    llm.read_spec("A paragraph.")
    llm.screen(SPEC, "x")
    efforts = [body["reasoning"]["effort"] for _, body in api.requests]
    # An API key keeps the effort asked for, as before.
    assert efforts == (["high", "high", "low"] if plan else ["xhigh", "high", "low"])
    # A catalog entry that lists xhigh gets it on the plan too.
    api = Scripted([answer(EXTRACTION, "gpt-6.1-sol")])
    make_llm(api, reasoning_levels=lambda model: ["low", "high", "xhigh"]).adjudicate(SPEC, {"type": "text", "text": "p"}, None, [])
    assert api.requests[0][1]["reasoning"] == {"effort": "xhigh"}
    assert at_most("max", "high") == "high" and at_most("medium", "high") == "medium" and at_most("other", "high") == "other"


def test_a_pdf_goes_as_a_file_or_as_its_text_layer_when_plan_usage_refuses_files():
    pdf = FullText(kind="oa_pdf", text="PHQ-9 9.0 (4.0)", url="https://example.org/a.pdf", pdf=b"%PDF-1.4 fake")
    refused = (400, {"error": {"code": "subscription_sharing_unsupported_capability", "param": "input[0].content[0]", "message": "no files"}})
    api = Scripted([refused, answer(EXTRACTION), answer(EXTRACTION)])
    llm = make_llm(api)
    document = llm.document(pdf, "Brief mindfulness: a trial")
    llm.extract(SPEC, document)
    llm.extract(SPEC, document)
    first, second, third = (body["input"][0]["content"][0] for _, body in api.requests)
    assert first == {"type": "input_file", "filename": "Brief mindfulness a trial.pdf",
                     "file_data": "data:application/pdf;base64," + base64.b64encode(b"%PDF-1.4 fake").decode()}
    assert second["type"] == third["type"] == "input_text" and second["text"].endswith("PHQ-9 9.0 (4.0)")
    # Claude's block is untouched apart from the text kept beside it.
    assert document["source"]["type"] == "base64" and document["text"] == "PHQ-9 9.0 (4.0)"


def test_refusals_and_early_stops_are_errors():
    refusal = {"type": "message", "role": "assistant", "content": [{"type": "refusal", "refusal": "I can't help with that."}]}
    api = Scripted([
        (200, openai_sse([refusal], "gpt-6-luna")),
        answer(SCREEN, status="incomplete", incomplete={"reason": "max_output_tokens"}),
        answer(SCREEN, status="incomplete", incomplete={"reason": "content_filter"}),
    ])
    llm = make_llm(api)
    with pytest.raises(ModelDeclined, match="can't help"):
        llm.screen(SPEC, "x")
    with pytest.raises(ModelTruncated, match="max_output_tokens"):
        llm.screen(SPEC, "x")
    with pytest.raises(ModelDeclined):
        llm.screen(SPEC, "x")


def test_plan_limits_are_not_retried_and_are_explained():
    api = Scripted([(429, {"error": {"code": "subscription_sharing_usage_limit_exceeded", "message": "limit"}})])
    with pytest.raises(OpenAIError) as caught:
        make_llm(api).screen(SPEC, "x")
    assert len(api.requests) == 1
    assert "ChatGPT Settings" in describe_error(caught.value) and "429" in str(caught.value)


@pytest.mark.parametrize("streamed", [False, True])
def test_responses_auth_errors_update_the_chatgpt_session(tmp_path, streamed):
    from test_chatgpt import signed_in

    session, _, _ = signed_in(tmp_path)
    if streamed:
        payload = b'event: error\ndata: {"type":"error","code":"invalid_token"}\n\n'
        response = (200, payload)
    else:
        response = (401, {"error": {"code": "invalid_token"}})
    api = Scripted([response])
    llm = OpenAILLM(Settings(), session.access_token, client=httpx.Client(transport=httpx.MockTransport(api)),
                    on_error=session.handle_api_error)
    with pytest.raises(OpenAIError) as caught:
        llm.screen(SPEC, "a paper")
    assert caught.value.code == "invalid_token" and len(api.requests) == 1
    assert session.state()["status"] == "reauth_required"
    assert "Sign in with ChatGPT again" in session.state()["error"]


def test_server_errors_and_cut_streams_are_retried():
    failed = openai_sse([], "gpt-6-luna").replace(b"response.completed", b"response.failed").replace(
        b'"status": "completed"', b'"status": "failed", "error": {"code": "server_error", "message": "boom"}')
    cut = openai_sse([message_item("{")], "gpt-6-luna").split(b"event: response.completed")[0]
    api = Scripted([(500, {"error": {"message": "oops"}}), (200, failed), (200, cut), answer(SCREEN)])
    assert make_llm(api).screen(SPEC, "x").decision == "include"
    assert len(api.requests) == 4

    api = Scripted([(200, cut)] * 4)
    with pytest.raises(OpenAIError, match="stopped before it finished"):
        make_llm(api).screen(SPEC, "x")


def test_a_bad_request_keeps_openais_own_words():
    api = Scripted([(400, {"error": {"type": "invalid_request_error", "code": "invalid_value", "param": "model", "message": "Unknown model gpt-x."}})])
    with pytest.raises(OpenAIError, match="Unknown model gpt-x") as caught:
        make_llm(api).screen(SPEC, "x")
    assert caught.value.code == "invalid_value" and not caught.value.retryable


def test_search_agent_calls_tools_in_a_namespace_then_submits_a_plan():
    plan = PLAN.model_dump()
    many = [f"q{i}" for i in range(20)]
    api = Scripted([
        (200, openai_sse([
            reasoning_item(),
            function_call_item("count_pubmed", {"queries": many}, "c1"),
            function_call_item("search_pubmed", {"query": "mindfulness[tiab]", "max_results": None}, "c2"),
            function_call_item("search_clinicaltrials", {"query": "mindfulness", "max_results": 500}, "c3"),
            function_call_item("search_everything", {"query": "x"}, "c4"),
        ], "gpt-6.1-sol")),
        (200, openai_sse([function_call_item("submit_search_plan", {**plan, "levels": [{"level": "everything", "query": "q", "why": ""}]}, "c5")], "gpt-6.1-sol")),
        (200, openai_sse([function_call_item("submit_search_plan", plan, "c6")], "gpt-6.1-sol")),
        (200, openai_sse([message_item("Searched from the top down.")], "gpt-6.1-sol")),
    ])
    toolbox = Toolbox()
    usage = []
    result = make_llm(api).plan_search(SPEC, toolbox, sink=lambda step, model, u: usage.append(step))
    assert result == PLAN
    assert usage == ["search"] * 4
    # At most 12 counts per call; a null max_results means the default; previews capped at 50 records.
    assert toolbox.queries == [("count", tuple(many[:12])), ("pubmed", "mindfulness[tiab]", 20), ("trials", "mindfulness", 50)]

    bodies = [body for _, body in api.requests]
    first = bodies[0]
    check_plan_usage_rules(first)
    assert first["model"] == "gpt-6.1-sol" and first["reasoning"] == {"effort": "high"}
    assert first["include"] == ["reasoning.encrypted_content"]
    (namespace,) = first["tools"]
    assert namespace["type"] == "namespace" and namespace["name"] == "literature"
    assert namespace["tools"] == function_tools(t.to_dict() for t in search_tools(Toolbox(), {}))
    assert all(tool["strict"] for tool in namespace["tools"])
    assert "top down" in first["instructions"]

    # Not stored: each turn sends the whole conversation, the reasoning included.
    second = bodies[1]["input"]
    assert second[1] == reasoning_item() and second[2]["call_id"] == "c1"
    calls = [item for item in second if item.get("type") == "function_call"]
    assert [item["call_id"] for item in calls] == ["c1", "c2", "c3", "c4"]
    assert all("id" not in item and "status" not in item and item["namespace"] == "literature" for item in calls)
    outputs = {item["call_id"]: item["output"] for item in second if item.get("type") == "function_call_output"}
    assert "#12 12 | q11" in outputs["c1"] and "Total hits: 12" in outputs["c2"]
    assert outputs["c4"] == "Error: Tool 'search_everything' not found"
    rejected = next(item["output"] for item in bodies[2]["input"] if item.get("call_id") == "c5" and item["type"] == "function_call_output")
    assert rejected.startswith("Error: Invalid arguments")
    assert len(bodies[3]["input"]) == len(bodies[2]["input"]) + 2


@pytest.mark.parametrize("namespaced", [True, False])
def test_replayed_turns_carry_no_server_ids(namespaced):
    # Nothing is stored, so the ids of earlier messages and calls would point at nothing.
    count = function_call_item("count_pubmed", {"queries": ["q"]}, "c1")
    submit = function_call_item("submit_search_plan", PLAN.model_dump(), "c2")
    api = Scripted([
        (200, openai_sse([reasoning_item(), message_item("Counting first."), count], "gpt-6.1-sol")),
        (200, openai_sse([submit], "gpt-6.1-sol")),
        (200, openai_sse([message_item("Done.")], "gpt-6.1-sol")),
    ])
    make_llm(api, namespaced_tools=namespaced).plan_search(SPEC, Toolbox())
    last = api.requests[2][1]["input"]
    assert last[1] == reasoning_item()  # whole: its id and encrypted_content carry the reasoning
    assert last[2] == {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Counting first.", "annotations": []}]}
    without_ids = [{key: value for key, value in item.items() if key not in ("id", "status")} for item in (count, submit)]
    assert [item for item in last if item.get("type") == "function_call"] == without_ids
    assert [item["call_id"] for item in last if item.get("type") == "function_call_output"] == ["c1", "c2"]


def test_a_search_on_a_model_without_reasoning_settings_asks_for_no_reasoning_back():
    api = Scripted([(200, openai_sse([function_call_item("submit_search_plan", PLAN.model_dump(), "c1")], "gpt-6.1-sol")),
                    (200, openai_sse([message_item("Done.")], "gpt-6.1-sol"))])
    make_llm(api, reasoning_levels=lambda model: []).plan_search(SPEC, Toolbox())
    for _, body in api.requests:
        assert "reasoning" not in body and "include" not in body


def test_search_agent_without_a_plan_is_an_error():
    api = Scripted([(200, openai_sse([message_item("I give up.")], "gpt-6.1-sol"))])
    with pytest.raises(RuntimeError, match="without submitting"):
        make_llm(api).plan_search(SPEC, Toolbox())


def test_tools_can_go_without_a_namespace_for_an_api_key():
    api = Scripted([(200, openai_sse([function_call_item("submit_search_plan", PLAN.model_dump(), "c1")], "gpt-6.1-sol")),
                    (200, openai_sse([message_item("Done.")], "gpt-6.1-sol"))])
    make_llm(api, namespaced_tools=False).plan_search(SPEC, Toolbox())
    assert [tool["type"] for tool in api.requests[0][1]["tools"]] == ["function"] * 5


def test_strict_schemas():
    # The structured outputs already meet strict mode, so they go as Claude gets them.
    for model in (SearchSpec, ScreenDecision, PaperExtraction, StudyContext, OverlapReport, EvidenceSummary):
        assert strict_schema(output_schema(model)) == output_schema(model), model.__name__
    schema = {"type": "object", "properties": {"q": {"type": "string"}, "n": {"type": "integer", "default": 20},
                                               "level": {"$ref": "#/$defs/L"}},
              "required": ["q"], "$defs": {"L": {"type": "object", "properties": {"x": {"enum": ["a", "b"], "type": "string"}}}}}
    assert strict_schema(schema) == {
        "type": "object",
        "properties": {"q": {"type": "string"}, "n": {"type": ["integer", "null"]}, "level": {"anyOf": [{"$ref": "#/$defs/L"}, {"type": "null"}]}},
        "required": ["q", "n", "level"],
        "additionalProperties": False,
        "$defs": {"L": {"type": "object", "properties": {"x": {"anyOf": [{"enum": ["a", "b"], "type": "string"}, {"type": "null"}]}},
                        "required": ["x"], "additionalProperties": False}},
    }
    with pytest.raises(ValueError):
        input_content([{"type": "image", "source": {}}])


def test_a_whole_search_runs_on_openai_models(tmp_path):
    settings = Settings(data_dir=tmp_path, contact_email="test@example.com", audit_fraction=0.0, worker_concurrency=2)
    store = Store(tmp_path / "test.sqlite3")
    api = FakeOpenAI()
    llm = OpenAILLM(settings, lambda: "plan-token", client=httpx.Client(transport=httpx.MockTransport(api)))
    pipeline = Pipeline(store, llm, Sources(settings, client=mock_client(LiteratureAPIs())), settings)
    project_id = store.create_project("We plan a randomized trial of a mindfulness app versus waitlist for PHQ-9 at 12 weeks.")
    pipeline.read_spec(project_id)
    pipeline.run(project_id)
    project = store.project(project_id)
    assert project["status"] == "done", project["error"]
    assert [lv["level"] for lv in project["search_plan"]["levels"]] == [lv.level for lv in PLAN.levels]

    papers = {p["ext_id"]: p for p in store.papers(project_id)}
    assert papers["33333333"]["status"] == "screened_out"
    assert sum(p["status"] == "extracted" for p in papers.values()) == 8
    assert papers["44444444"]["extracted_by"] == "manager"
    # The search agent's tools ran on the real toolbox (the null max_results took the default).
    assert api.tool_outputs["c1"].startswith("#1 4 | mindfulness[tiab]")
    assert "| PPR | preprint |" in api.tool_outputs["c2"] and EUROPEPMC_QUERY
    assert api.llm.calls.count("screen") == 9 and api.llm.calls.count("extract") == 8 and api.llm.calls.count("search-turn") == 3
    # The manager read every extracted paper's context, the PDF's too, and named the model it ran on.
    assert api.llm.calls.count("context") == 8
    contexts = {key: p["context"] for key, p in papers.items() if p["status"] == "extracted"}
    assert all(c["status"] == "done" and c["model"] == "gpt-6.1-sol" for c in contexts.values())
    assert contexts["11111111"]["checks"] == {"sample_size": None, "results": ["exact"]}
    pdf = [p for _, body in api.requests if body.get("text", {}).get("format", {}).get("name") == "StudyContext"
           for p in body["input"][0]["content"] if p["type"] == "input_file"]
    assert len(pdf) == 1 and pdf[0]["filename"].startswith("Brief mindfulness")

    # The open-access PDF went as a file.
    files = [p for _, body in api.requests if "text" in body for p in body["input"][0]["content"] if p["type"] == "input_file"]
    assert files and files[0]["filename"].startswith("Brief mindfulness")
    for _, body in api.requests:
        check_plan_usage_rules(body)
    models = {(u["step"], u["model"]) for u in store.usage(project_id)}
    assert ("screen", "gpt-6-luna") in models and ("spec", "gpt-6.1-sol") in models and ("adjudicate", "gpt-6.1-sol") in models
    assert ("context", "gpt-6.1-sol") in models

    pipeline.check_overlap(project_id)
    pipeline.summarize(project_id)
    summary = store.project(project_id)["summary"]
    assert summary["status"] == "done" and summary["evidence"]["estimands"]
    assert summary["result"]["needs"][0]["estimands"].startswith("[V")


def test_a_used_up_plan_stops_the_run(tmp_path):
    """When the plan's share runs out, the run stops and says so, instead of sending a request for every
    remaining record and finishing as if nothing happened."""
    settings = Settings(data_dir=tmp_path, contact_email="test@example.com", audit_fraction=0.0, worker_concurrency=1)
    store = Store(tmp_path / "test.sqlite3")
    api = FakeOpenAI()
    screened = []

    def handler(request):
        body = json.loads(request.content)
        if body.get("text", {}).get("format", {}).get("name") == "ScreenDecision":
            screened.append(body)
            if len(screened) > 2:
                return httpx.Response(429, json={"error": {"code": "subscription_sharing_usage_limit_exceeded", "message": "limit"}})
        return api(request)

    llm = OpenAILLM(settings, lambda: "plan-token", client=httpx.Client(transport=httpx.MockTransport(handler)))
    llm.responses.sleep = lambda seconds: None
    pipeline = Pipeline(store, llm, Sources(settings, client=mock_client(LiteratureAPIs())), settings)
    project_id = store.create_project("We plan a randomized trial of a mindfulness app versus waitlist for PHQ-9 at 12 weeks.")
    pipeline.read_spec(project_id)
    pipeline.run(project_id)
    project = store.project(project_id)
    assert project["status"] == "error" and "ChatGPT Settings" in project["error"]
    assert len(screened) == 3  # one refused request, then no more
    statuses = [p["status"] for p in store.papers(project_id)]
    assert statuses.count("candidate") == len(statuses) - 2 and "unsure" not in statuses


def test_a_model_without_reasoning_levels_gets_no_reasoning_setting():
    api = Scripted([answer(SCREEN)])
    make_llm(api, reasoning_levels=lambda model: []).screen(SPEC, "x")
    assert "reasoning" not in api.requests[0][1]


def test_a_pdf_without_a_text_layer_is_not_sent_empty():
    scanned = FullText(kind="oa_pdf", text="", url="https://example.org/scan.pdf", pdf=b"%PDF-1.4 scanned")
    refused = (400, {"error": {"code": "subscription_sharing_unsupported_capability", "message": "no files"}})
    api = Scripted([refused])
    llm = make_llm(api)
    with pytest.raises(OpenAIError, match="no text layer"):
        llm.extract(SPEC, llm.document(scanned, "A scanned trial"))
    assert len(api.requests) == 1
