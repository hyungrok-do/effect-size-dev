"""The Claude API layer, against a mock HTTP transport: which model and settings each step uses,
and how responses (including refusals) are read."""

import json

import anthropic
import httpx2
import pytest

from esf.config import Settings
from esf.llm import NO_KEY, ClaudeLLM, ModelDeclined, ModelTruncated, NoCredentials, describe_error, server_credentials
from fixtures import SPEC, streamed

# Every variable the Anthropic SDK reads a credential from, besides a profile on disk.
CREDENTIAL_VARIABLES = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "ANTHROPIC_CONFIG_DIR", "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID", "ANTHROPIC_IDENTITY_TOKEN", "ANTHROPIC_IDENTITY_TOKEN_FILE",
)


def profile_on_disk(home, monkeypatch):
    """A person's saved Anthropic sign-in, in a home folder of its own, as the SDK finds it unasked; and
    no credential in the environment."""
    config = home / ".config" / "anthropic"
    (config / "configs").mkdir(parents=True)
    (config / "credentials").mkdir()
    (config / "configs" / "default.json").write_text(json.dumps({"authentication": {"type": "user_oauth"}}))
    (config / "credentials" / "default.json").write_text(json.dumps({"access_token": "sk-ant-oat01-PROFILE", "expires_at": 4102444800}))
    monkeypatch.setenv("HOME", str(home))
    for name in CREDENTIAL_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    assert anthropic.Anthropic().credentials is not None  # the SDK left to itself would use it

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
        if kind == "401":
            return httpx2.Response(401, json=payload)
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
    assert llm.read_spec("A paragraph.").outcomes[0].measure == "PHQ-9"
    llm.adjudicate(SPEC, DOC, None, ["the worker could not complete the extraction"])
    (spec_req, spec), (_, adjudicate) = api.requests
    assert spec["model"] == adjudicate["model"] == "claude-opus-5-5"
    assert spec["output_config"]["effort"] == "high" and adjudicate["output_config"]["effort"] == "xhigh"
    assert spec["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in spec_req.headers["anthropic-beta"]
    assert "None: the junior reviewer" in adjudicate["messages"][0]["content"][1]["text"]


def test_the_chosen_models_run_with_the_fallback_their_own_model_takes():
    api = FakeAPI([
        ("sse", streamed("claude-haiku-5-5", SPEC.model_dump())),
        ("sse", streamed("claude-sonnet-5-5", SCREEN)),
    ])
    client = anthropic.Anthropic(api_key="test-key", max_retries=0, http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(api)))
    llm = ClaudeLLM(Settings(), client, manager_model="claude-haiku-5-5", worker_model="claude-sonnet-5-5")
    llm.read_spec("A paragraph.")
    llm.screen(SPEC, "Title: a trial")
    (spec_req, spec), (screen_req, screen) = api.requests
    # Haiku has no server-side fallback, even as the manager; Sonnet has one, even as a worker.
    assert spec["model"] == "claude-haiku-5-5" and "fallbacks" not in spec and "anthropic-beta" not in spec_req.headers
    assert screen["model"] == "claude-sonnet-5-5" and screen["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in screen_req.headers["anthropic-beta"]


def test_refusal_and_truncation_are_errors():
    llm, _ = make_llm([
        ("sse", streamed("claude-haiku-5-5", SCREEN, stop_reason="refusal")),
        ("sse", streamed("claude-haiku-5-5", SCREEN, stop_reason="max_tokens")),
    ])
    with pytest.raises(ModelDeclined):
        llm.screen(SPEC, "x")
    with pytest.raises(ModelTruncated):
        llm.screen(SPEC, "x")


def test_a_rejected_key_is_explained():
    llm, api = make_llm([("401", {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})])
    with pytest.raises(anthropic.AuthenticationError) as caught:
        llm.screen(SPEC, "x")
    assert "API key box" in describe_error(caught.value)
    headers = api.requests[0][0].headers
    assert "test-key" in headers.get("x-api-key", "") + headers.get("authorization", "")


def test_the_servers_own_client_takes_only_the_two_documented_variables(tmp_path, monkeypatch):
    profile_on_disk(tmp_path, monkeypatch)
    llm = ClaudeLLM(Settings())
    assert llm.client is None and server_credentials() == {}
    # Every step fails at once, asking for a key: there is no client to call anything with.
    for step in (lambda: llm.read_spec("A paragraph."), lambda: llm.screen(SPEC, "x"), lambda: llm.plan_search(SPEC, Toolbox())):
        with pytest.raises(NoCredentials) as caught:
            step()
        assert describe_error(caught.value) == NO_KEY == "Enter your Anthropic API key first, on the Agents page."

    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", " sk-ant-oat01-SERVER ")
    client = ClaudeLLM(Settings()).client
    assert (client.api_key, client.auth_token, client.credentials) == (None, "sk-ant-oat01-SERVER", None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-SERVER")  # the API key comes first, as in the SDK
    client = ClaudeLLM(Settings()).client
    assert (client.api_key, client.auth_token, client.credentials) == ("sk-ant-api03-SERVER", None, None)
    # A client given to it is used as it is.
    given = anthropic.Anthropic(api_key="sk-ant-given")
    assert ClaudeLLM(Settings(), given).client is given


def _message(content, stop_reason):
    return {
        "id": "msg", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": content,
        "stop_reason": stop_reason, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 10},
    }


class Toolbox:
    def __init__(self):
        self.queries = []

    def count_pubmed(self, queries):
        self.queries.append(("count", tuple(queries)))
        return "\n".join(f"#{i} 12 | {q}" for i, q in enumerate(queries, start=1))

    def preview_pubmed(self, query, max_results):
        self.queries.append(("pubmed", query, max_results))
        return "Total hits: 12\n11111111 | 2021 | Randomized Controlled Trial | A trial"

    def preview_trials(self, query, max_results):
        self.queries.append(("trials", query, max_results))
        return "Total studies with posted results: 1"


def test_search_agent_explores_then_submits_a_plan():
    plan = {
        "levels": [
            {"level": "reviews", "query": "mindfulness[tiab] AND meta-analysis[pt]", "why": "Pooled effects."},
            {"level": "primary", "query": "mindfulness[tiab] AND randomized[tiab]", "why": "Trials."},
        ],
        "clinicaltrials_queries": ["mindfulness"], "extra_ids": ["11111111"], "summary": "S.",
    }
    many = [f"q{i}" for i in range(20)]
    llm, api = make_llm([
        ("json", _message([
            {"type": "tool_use", "id": "t0", "name": "count_pubmed", "input": {"queries": many}},
            {"type": "tool_use", "id": "t1", "name": "search_pubmed", "input": {"query": "mindfulness[tiab]", "max_results": 500}},
            {"type": "tool_use", "id": "t2", "name": "search_clinicaltrials", "input": {"query": "mindfulness"}},
        ], "tool_use")),
        ("json", _message([{"type": "tool_use", "id": "t3", "name": "submit_search_plan", "input": plan}], "tool_use")),
        ("json", _message([{"type": "text", "text": "Done."}], "end_turn")),
    ])
    toolbox = Toolbox()
    result = llm.plan_search(SPEC, toolbox)
    assert [(lv.level, lv.query) for lv in result.levels] == [(lv["level"], lv["query"]) for lv in plan["levels"]]
    assert result.extra_ids == ["11111111"]
    # At most 12 counts per call; previews capped at 50 records.
    assert toolbox.queries == [("count", tuple(many[:12])), ("pubmed", "mindfulness[tiab]", 50), ("trials", "mindfulness", 20)]

    first = api.requests[0][1]
    assert first["model"] == "claude-opus-5-5" and first["output_config"]["effort"] == "high"
    assert first["fallbacks"] == "default"
    assert [t["name"] for t in first["tools"]] == [
        "count_pubmed", "search_pubmed", "search_europepmc", "search_clinicaltrials", "submit_search_plan",
    ]
    assert "top down" in first["system"][0]["text"]
    results = api.requests[1][1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t0", "t1", "t2"]
    assert "#12 12 | q11" in json.dumps(results[0]["content"])
    assert "Total hits: 12" in json.dumps(results[1]["content"])


def test_a_plan_with_an_unknown_level_goes_back_to_the_agent():
    bad = {"levels": [{"level": "everything", "query": "q", "why": ""}], "clinicaltrials_queries": [], "extra_ids": [], "summary": ""}
    good = {**bad, "levels": [{"level": "primary", "query": "q", "why": ""}]}
    llm, api = make_llm([
        ("json", _message([{"type": "tool_use", "id": "t1", "name": "submit_search_plan", "input": bad}], "tool_use")),
        ("json", _message([{"type": "tool_use", "id": "t2", "name": "submit_search_plan", "input": good}], "tool_use")),
        ("json", _message([{"type": "text", "text": "Done."}], "end_turn")),
    ])
    assert llm.plan_search(SPEC, Toolbox()).levels[0].level == "primary"
    error = api.requests[1][1]["messages"][-1]["content"][0]
    assert error["is_error"] is True


def test_search_agent_without_a_plan_is_an_error():
    llm, _ = make_llm([("json", _message([{"type": "text", "text": "I give up."}], "end_turn"))])
    with pytest.raises(RuntimeError, match="without submitting"):
        llm.plan_search(SPEC, Toolbox())
