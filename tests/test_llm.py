"""The Claude API layer, against a mock HTTP transport: which model and settings each step uses,
and how responses (including refusals) are read."""

import json

import anthropic
import httpx2
import pytest

from esf.config import Settings
from esf.llm import ClaudeLLM, ModelDeclined, ModelTruncated
from fixtures import SPEC, sse

DOC = {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": "PHQ-9 8.1 (4.2)"}}
EXTRACTION = {
    "study_design": "RCT", "population": "students", "groups": ["App", "Waitlist"], "matches_spec": True,
    "mismatch_reasons": [], "notes": "",
    "results": [{
        "outcome_measure": "PHQ-9", "timepoint": "12 weeks", "analysis_population": "itt", "adjusted": "unadjusted",
        "relevance": "direct", "confidence": "high", "concerns": [],
        "values": [{"group": "App", "statistic": "mean", "value": 8.1, "describes": "PHQ-9", "quote": "PHQ-9 8.1 (4.2)", "location": "Table 2"}],
    }],
}
SCREEN = {"decision": "include", "reason": "Relevant.", "study_type": "rct", "reports_relevant_outcome": "yes"}


def streamed(model, payload, stop_reason="end_turn"):
    text = json.dumps(payload)
    return sse([
        {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": model,
                                              "content": [], "stop_reason": None, "stop_sequence": None,
                                              "usage": {"input_tokens": 100, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text[:20]}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text[20:]}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": 50}},
        {"type": "message_stop"},
    ])


class FakeAPI:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append((request, body))
        kind, payload = self.responses.pop(0)
        if kind == "sse":
            return httpx2.Response(200, content=payload, headers={"content-type": "text/event-stream"})
        return httpx2.Response(200, json=payload)


def make_llm(responses):
    api = FakeAPI(responses)
    client = anthropic.Anthropic(
        api_key="test-key",
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(api)),
    )
    return ClaudeLLM(Settings(), client), api


def test_worker_steps_run_on_haiku_without_fallbacks():
    llm, api = make_llm([
        ("sse", streamed("claude-haiku-5-5", SCREEN)),
        ("sse", streamed("claude-haiku-5-5", EXTRACTION)),
    ])
    usage = []
    decision = llm.screen(SPEC, "Title: a trial", sink=lambda step, model, u: usage.append((step, model)))
    assert decision.decision == "include"
    extraction = llm.extract(SPEC, DOC)
    assert extraction.results[0].values[0].value == 8.1

    (screen_req, screen), (_, extract) = api.requests
    assert screen["model"] == extract["model"] == "claude-haiku-5-5"
    assert screen["output_config"]["effort"] == "low" and extract["output_config"]["effort"] == "medium"
    assert screen["output_config"]["format"]["type"] == "json_schema"
    assert "fallbacks" not in screen and "anthropic-beta" not in screen_req.headers
    assert screen["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert extract["messages"][0]["content"][0] == DOC
    assert usage == [("screen", "claude-haiku-5-5")]


def test_manager_steps_run_on_opus_with_fallbacks():
    llm, api = make_llm([
        ("sse", streamed("claude-opus-5-5", SPEC.model_dump())),
        ("sse", streamed("claude-opus-5-5", EXTRACTION)),
    ])
    assert llm.read_spec("A paragraph.").outcomes == ["PHQ-9"]
    llm.adjudicate(SPEC, DOC, None, ["the worker could not complete the extraction"])
    (spec_req, spec), (_, adjudicate) = api.requests
    assert spec["model"] == adjudicate["model"] == "claude-opus-5-5"
    assert spec["output_config"]["effort"] == "high" and adjudicate["output_config"]["effort"] == "xhigh"
    assert spec["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in spec_req.headers["anthropic-beta"]
    assert "None: the junior reviewer" in adjudicate["messages"][0]["content"][1]["text"]


def test_refusal_and_truncation_are_errors():
    llm, _ = make_llm([
        ("sse", streamed("claude-haiku-5-5", SCREEN, stop_reason="refusal")),
        ("sse", streamed("claude-haiku-5-5", SCREEN, stop_reason="max_tokens")),
    ])
    with pytest.raises(ModelDeclined):
        llm.screen(SPEC, "x")
    with pytest.raises(ModelTruncated):
        llm.screen(SPEC, "x")


def _message(content, stop_reason):
    return {
        "id": "msg", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": content,
        "stop_reason": stop_reason, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 10},
    }


class Toolbox:
    def __init__(self):
        self.queries = []

    def preview_pubmed(self, query, max_results):
        self.queries.append(("pubmed", query, max_results))
        return "Total hits: 12\n11111111 | 2021 | Randomized Controlled Trial | A trial"

    def preview_trials(self, query, max_results):
        self.queries.append(("trials", query, max_results))
        return "Total studies with posted results: 1"


def test_search_agent_explores_then_submits_a_plan():
    plan = {"pubmed_queries": ["mindfulness[tiab]"], "clinicaltrials_queries": ["mindfulness"], "extra_ids": ["11111111"], "summary": "S."}
    llm, api = make_llm([
        ("json", _message([
            {"type": "tool_use", "id": "t1", "name": "search_pubmed", "input": {"query": "mindfulness[tiab]", "max_results": 500}},
            {"type": "tool_use", "id": "t2", "name": "search_clinicaltrials", "input": {"query": "mindfulness"}},
        ], "tool_use")),
        ("json", _message([{"type": "tool_use", "id": "t3", "name": "submit_search_plan", "input": plan}], "tool_use")),
        ("json", _message([{"type": "text", "text": "Done."}], "end_turn")),
    ])
    toolbox = Toolbox()
    result = llm.plan_search(SPEC, toolbox)
    assert result.extra_ids == ["11111111"]
    assert toolbox.queries == [("pubmed", "mindfulness[tiab]", 50), ("trials", "mindfulness", 20)]

    first = api.requests[0][1]
    assert first["model"] == "claude-opus-5-5" and first["output_config"]["effort"] == "high"
    assert first["fallbacks"] == "default"
    assert {t["name"] for t in first["tools"]} == {"search_pubmed", "search_clinicaltrials", "submit_search_plan"}
    results = api.requests[1][1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert "Total hits: 12" in json.dumps(results[0]["content"])


def test_search_agent_without_a_plan_is_an_error():
    llm, _ = make_llm([("json", _message([{"type": "text", "text": "I give up."}], "end_turn"))])
    with pytest.raises(RuntimeError, match="without submitting"):
        llm.plan_search(SPEC, Toolbox())
