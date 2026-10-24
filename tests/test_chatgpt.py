import base64
import hashlib
import json
import os
import secrets
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlencode

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from esf.chatgpt import (
    APP_NAME, ISSUER, PENDING_SECONDS, REGISTRATION_ENTRY, RESOURCE, SCOPES, ChatGPTError, ChatGPTSession, redirect_uri,
)

PORT = 8765
HOST = f"127.0.0.1:{PORT}"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWK = {**json.loads(RSAAlgorithm.to_jwk(KEY.public_key())), "kid": "key-1", "alg": "RS256", "use": "sig"}

CATALOG = {"models": [
    {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1 Sol", "visibility": "list",
     "supported_reasoning_levels": [{"effort": "low", "description": "Fast"}, {"effort": "high", "description": "Thorough"}]},
    {"slug": "gpt-6-luna", "display_name": "GPT-6 Luna", "visibility": "list", "supported_reasoning_levels": ["minimal", "low", "medium"]},
    {"slug": "gpt-6-mini", "display_name": "GPT-6 Mini", "visibility": "list"},
    {"slug": "internal-eval", "display_name": "Internal", "visibility": "hide"},
    {"slug": "no-name", "visibility": "list"},
    {"slug": "", "display_name": "No slug", "visibility": "list"},
    "not a model",
]}


def challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


class FakeOpenAI:
    """httpx mock transport handler for OpenAI's sign-in service and model catalog, with a stand-in
    for the browser leg of the sign-in (authorize)."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.authorizations: list[dict[str, str]] = []
        self.token_forms: list[dict[str, str]] = []
        self.revocations: list[dict[str, str]] = []
        self.codes: dict[str, dict[str, str]] = {}
        self.client_id = "app_Test123"
        self.subject = "user-ada"
        self.access, self.refresh = "at-1", "rt-1"
        self.scope = SCOPES
        self.discovery = {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/oauth/authorize",
            "token_endpoint": f"{ISSUER}/oauth/token",
            "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
            "revocation_endpoint": f"{ISSUER}/oauth/revoke",
        }
        self.jwks: dict = {"keys": [JWK]}
        self.jwks_status = 200
        self.revoke_status = 200
        self.catalog: object = CATALOG
        self.catalog_status = 200
        self.offline = False
        self.offline_paths: set[str] = set()
        self.token_reply: tuple[int, dict] | None = None  # replaces the code exchange's reply
        self.token_changes: dict = {}  # merged into the code exchange's reply; None removes a field
        self.id_token_options: dict = {}  # for the code exchange's id_token
        self.refresh_reply: tuple[int, dict] | None = None  # replaces a refresh's reply
        self.refresh_id_token: dict | None = None  # when set, refreshes return an id_token with these options
        self.refresh_delay = 0.0
        self.refreshes = 0  # refresh requests, failed ones included
        self.rotations = 1  # tokens are named after this: at-1, rt-1, then at-2, rt-2, ...

    def id_token(self, nonce=None, sub=None, aud=None, iss=ISSUER, key=KEY, kid="key-1", exp_in=3600, **claims) -> str:
        now = int(time.time())  # PyJWT checks exp and iat against the real clock
        payload = {"iss": iss, "aud": aud or self.client_id, "sub": sub or self.subject, "iat": now, "exp": now + exp_in,
                   "email": "ada@example.com", "name": "Ada Lovelace", **claims}
        if nonce is not None:
            payload["nonce"] = nonce
        return jwt.encode(payload, key, algorithm="RS256", headers={"kid": kid})

    def authorize(self, url: str, **changes: str | None) -> str:
        """What the browser does: sign in at the authorize URL and return the callback's query string."""
        parsed = httpx.URL(url)
        assert (parsed.scheme, parsed.host, parsed.path) == ("https", "auth.openai.com", "/oauth/authorize")
        params = dict(parsed.params)
        self.authorizations.append(params)
        registering = params["client_id"] == REGISTRATION_ENTRY
        if not registering:
            assert params["client_id"] == self.client_id
        code = secrets.token_urlsafe(8)
        self.codes[code] = params
        query = {"code": code, "state": params["state"], **({"client_id": self.client_id} if registering else {})}
        query.update(changes)
        return urlencode({k: v for k, v in query.items() if v is not None})

    def _token(self, form: dict[str, str]) -> httpx.Response:
        self.token_forms.append(form)
        if form["grant_type"] == "authorization_code":
            started = self.codes.pop(form["code"], None)
            if self.token_reply:
                return httpx.Response(self.token_reply[0], json=self.token_reply[1])
            if (started is None or form["client_id"] != self.client_id or form["redirect_uri"] != started["redirect_uri"]
                    or form["resource"] != RESOURCE or challenge(form["code_verifier"]) != started["code_challenge"]):
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad code."})
            body = {"access_token": self.access, "token_type": "Bearer", "expires_in": 3600, "refresh_token": self.refresh,
                    "scope": self.scope, "id_token": self.id_token(**{"nonce": started["nonce"], **self.id_token_options})}
            body.update(self.token_changes)
            return httpx.Response(200, json={k: v for k, v in body.items() if v is not None})
        if form["grant_type"] == "refresh_token":
            self.refreshes += 1
            time.sleep(self.refresh_delay)
            if self.refresh_reply:
                return httpx.Response(self.refresh_reply[0], json=self.refresh_reply[1])
            if form["refresh_token"] != self.refresh or form["client_id"] != self.client_id or form["resource"] != RESOURCE:
                return httpx.Response(400, json={"error": "refresh_token_reused"})
            self.rotations += 1
            self.access, self.refresh = f"at-{self.rotations}", f"rt-{self.rotations}"
            # No scope: a refresh may leave it out when it is unchanged.
            body = {"access_token": self.access, "token_type": "bearer", "expires_in": 3600, "refresh_token": self.refresh}
            if self.refresh_id_token is not None:
                body["id_token"] = self.id_token(**self.refresh_id_token)
            return httpx.Response(200, json=body)
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.offline or request.url.path in self.offline_paths:
            raise httpx.ConnectError("offline", request=request)
        url = request.url
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()} if request.method == "POST" else {}
        if url.host == "auth.openai.com":
            if url.path == "/.well-known/openid-configuration":
                return httpx.Response(200, json=self.discovery)
            if url.path == "/.well-known/jwks.json":
                return httpx.Response(self.jwks_status, json=self.jwks)
            if url.path == "/oauth/token" and request.method == "POST":
                return self._token(form)
            if url.path == "/oauth/revoke" and request.method == "POST":
                self.revocations.append(form)
                return httpx.Response(self.revoke_status)
        if url.host == "api.openai.com" and url.path == "/v1/models":
            if request.headers.get("authorization") != f"Bearer {self.access}":
                return httpx.Response(401, json={"error": {"code": "invalid_token", "message": "Bad token."}})
            return httpx.Response(self.catalog_status, json=self.catalog)
        return httpx.Response(404, text="not found")


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def new_session(tmp_path, api=None, clock=None):
    api = api or FakeOpenAI()
    clock = clock or Clock()
    # The client follows redirects on purpose: the session must turn that off for its own requests.
    client = httpx.Client(transport=httpx.MockTransport(api), follow_redirects=True)
    return ChatGPTSession(tmp_path / "settings" / "chatgpt.json", client=client, clock=clock), api, clock


def signed_in(tmp_path):
    session, api, clock = new_session(tmp_path)
    session.finish(api.authorize(session.start(PORT)), HOST)
    return session, api, clock


def saved(tmp_path) -> dict:
    return json.loads((tmp_path / "settings" / "chatgpt.json").read_text())


# -- start --------------------------------------------------------------------------


def test_the_first_sign_in_registers_the_app_and_later_ones_reuse_its_client_id(tmp_path):
    session, api, _ = new_session(tmp_path)
    url = session.start(PORT)
    assert session.state()["status"] == "disconnected" and session.state()["pending"] is True
    params = dict(httpx.URL(url).params)
    assert params["client_id"] == REGISTRATION_ENTRY and params["agent_name_hint"] == APP_NAME
    assert params["redirect_uri"] == redirect_uri(PORT) == "http://127.0.0.1:8765/auth/callback"
    assert params["response_type"] == "code" and params["scope"] == SCOPES and params["resource"] == RESOURCE
    assert params["code_challenge_method"] == "S256" and len(params["state"]) >= 43 and len(params["nonce"]) >= 43

    session.finish(api.authorize(url), HOST)
    exchange = api.token_forms[0]
    assert exchange["grant_type"] == "authorization_code" and exchange["client_id"] == api.client_id
    assert challenge(exchange["code_verifier"]) == params["code_challenge"]
    assert exchange["redirect_uri"] == params["redirect_uri"] and exchange["resource"] == RESOURCE
    assert "client_secret" not in exchange
    assert all(r.headers["accept"] == "application/json" for r in api.requests)

    again = dict(httpx.URL(session.start(9000)).params)
    assert again["client_id"] == api.client_id and "agent_name_hint" not in again
    assert again["redirect_uri"] == "http://127.0.0.1:9000/auth/callback"
    assert again["state"] != params["state"] and again["nonce"] != params["nonce"]
    assert again["code_challenge"] != params["code_challenge"]

    # A later process reads the registration from the settings file.
    later, _, _ = new_session(tmp_path, api)
    assert dict(httpx.URL(later.start(PORT)).params)["client_id"] == api.client_id


def test_discovery_is_checked_and_cached_only_once_it_succeeds(tmp_path):
    session, api, _ = new_session(tmp_path)
    for bad in ({"issuer": "https://auth.example.com"}, {"token_endpoint": "https://evil.example/oauth/token"},
                {"jwks_uri": "https://auth.openai.com.evil.example/jwks"}, {"revocation_endpoint": None}):
        api.discovery = {**FakeOpenAI().discovery, **bad}
        with pytest.raises(ChatGPTError) as caught:
            session.start(PORT)
        assert caught.value.code == "discovery_failed" and caught.value.retryable
    api.discovery = FakeOpenAI().discovery
    session.start(PORT)
    session.start(PORT)
    fetched = [r for r in api.requests if r.url.path == "/.well-known/openid-configuration"]
    assert len(fetched) == 5  # four failures, then one success that is reused


def test_start_needs_a_valid_port(tmp_path):
    session, _, _ = new_session(tmp_path)
    for port in (0, 65536, True, "8765"):
        with pytest.raises(ValueError):
            session.start(port)


# -- finish -------------------------------------------------------------------------


def test_sign_in_connects_and_keeps_no_token_on_disk(tmp_path):
    session, api, _ = signed_in(tmp_path)
    state = session.state()
    assert state["status"] == "connected" and state["error"] is None
    assert (state["name"], state["email"]) == ("Ada Lovelace", "ada@example.com")
    assert saved(tmp_path) == {"client_id": api.client_id}  # the models are saved only once chosen
    path = tmp_path / "settings" / "chatgpt.json"
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in path.parent.iterdir()] == ["chatgpt.json"]  # no temp file left behind
    text = path.read_text() + json.dumps(state)
    assert "at-1" not in text and "rt-1" not in text and "eyJ" not in text
    # Only listed models with a slug and a display name; reasoning levels in either shape.
    assert state["models"] == [
        {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1 Sol", "reasoning_levels": ["low", "high"]},
        {"slug": "gpt-6-luna", "display_name": "GPT-6 Luna", "reasoning_levels": ["minimal", "low", "medium"]},
        {"slug": "gpt-6-mini", "display_name": "GPT-6 Mini", "reasoning_levels": None},
    ]
    assert session.reasoning_levels("gpt-6-luna") == ["minimal", "low", "medium"]
    assert session.reasoning_levels("gpt-6-mini") is None and session.reasoning_levels("unknown") is None
    assert session.access_token() == "at-1"


def test_an_unexpected_catalog_leaves_the_model_list_empty(tmp_path):
    session, api, _ = new_session(tmp_path)
    api.catalog = {"object": "list", "data": [{"id": "gpt-6"}]}
    session.finish(api.authorize(session.start(PORT)), HOST)
    assert session.state()["status"] == "connected" and session.state()["models"] == []
    api.catalog = CATALOG
    assert [m["slug"] for m in session.refresh_models()] == ["gpt-6.1-sol", "gpt-6-luna", "gpt-6-mini"]


def test_a_catalog_that_fails_to_load_does_not_undo_the_sign_in(tmp_path):
    session, api, _ = new_session(tmp_path)
    api.catalog_status = 503
    session.finish(api.authorize(session.start(PORT)), HOST)
    state = session.state()
    assert state["status"] == "connected" and state["models"] == []
    assert state["error"].startswith("You are signed in, but the list of ChatGPT models could not be loaded.")
    assert session.access_token() == "at-1"
    api.catalog_status = 200
    assert len(session.refresh_models()) == 3 and session.state()["error"] is None


def test_a_callback_from_elsewhere_leaves_the_sign_in_pending(tmp_path):
    session, api, _ = new_session(tmp_path)
    query = api.authorize(session.start(PORT))
    for bad_query, host in (
        (query, "localhost:8765"),
        (query, "127.0.0.1:9999"),
        (query, "evil.example"),
        (query.replace("state=", "state=x"), HOST),
        ("&".join(p for p in query.split("&") if not p.startswith("state=")), HOST),
        (query + "&state=another", HOST),
        ("", HOST),
    ):
        with pytest.raises(ChatGPTError):
            session.finish(bad_query, host)
        assert session.state()["pending"] is True and session.state()["error"] is None
    session.finish(query, HOST)
    assert session.state()["status"] == "connected"

    with pytest.raises(ChatGPTError) as caught:
        session.finish(query, HOST)  # replayed
    assert caught.value.code == "no_sign_in"
    assert session.state()["status"] == "connected" and session.state()["error"] is None
    assert len(api.token_forms) == 1


def test_a_sign_in_expires(tmp_path):
    session, api, clock = new_session(tmp_path)
    query = api.authorize(session.start(PORT))
    clock.now += PENDING_SECONDS + 1
    assert session.state()["status"] == "disconnected"
    with pytest.raises(ChatGPTError) as caught:
        session.finish(query, HOST)
    assert caught.value.code == "sign_in_expired" and session.state()["error"] == str(caught.value)
    assert api.token_forms == []


def test_a_new_sign_in_keeps_the_old_one_usable_until_it_succeeds(tmp_path):
    session, api, clock = signed_in(tmp_path)
    first = api.authorize(session.start(PORT))
    second_url = session.start(PORT)  # started again (another click, a closed tab): the first stays valid
    assert session.state()["status"] == "connected" and session.state()["pending"] is True and session.access_token() == "at-1"
    session.finish(first, HOST)  # whichever finishes first wins...
    assert session.state()["pending"] is False
    with pytest.raises(ChatGPTError) as caught:
        session.finish(api.authorize(second_url), HOST)  # ...and the other is no longer waiting
    assert caught.value.code == "no_sign_in"
    # A sign-in that never finishes leaves the old one usable.
    third = session.start(PORT)
    clock.now += PENDING_SECONDS + 1
    assert session.state()["status"] == "connected" and session.state()["pending"] is False and session.access_token() == "at-1"
    with pytest.raises(ChatGPTError):
        session.finish(api.authorize(third), HOST)
    assert session.state()["status"] == "connected"


def test_at_most_a_few_sign_ins_wait_at_once(tmp_path):
    session, api, _ = new_session(tmp_path)
    urls = [session.start(PORT) for _ in range(4)]
    with pytest.raises(ChatGPTError) as caught:
        session.finish(api.authorize(urls[0]), HOST)  # the oldest of four was dropped
    assert caught.value.code == "wrong_state"
    session.finish(api.authorize(urls[1]), HOST)
    assert session.state()["status"] == "connected"


def test_cancel_drops_waiting_sign_ins(tmp_path):
    session, api, _ = new_session(tmp_path)
    url = session.start(PORT)
    session.cancel()
    assert session.state()["pending"] is False
    with pytest.raises(ChatGPTError) as caught:
        session.finish(api.authorize(url), HOST)
    assert caught.value.code == "no_sign_in" and session.state()["status"] == "disconnected"


def test_signing_out_during_the_code_exchange_wins(tmp_path):
    session, api, _ = new_session(tmp_path)
    query = api.authorize(session.start(PORT))
    exchange = api._token

    def slow_exchange(form):
        session.sign_out()  # the person signs out while the code is being exchanged
        return exchange(form)

    api._token = slow_exchange
    with pytest.raises(ChatGPTError) as caught:
        session.finish(query, HOST)
    assert caught.value.code == "canceled"
    assert session.state()["status"] == "disconnected"
    with pytest.raises(ChatGPTError):
        session.access_token()
    assert api.revocations and api.revocations[-1]["token"] == "rt-1"  # the tokens it got were revoked


def test_a_rejected_registration_is_forgotten(tmp_path):
    session, api, _ = signed_in(tmp_path)
    api.token_reply = (401, {"error": "invalid_client"})
    with pytest.raises(ChatGPTError) as caught:
        session.finish(api.authorize(session.start(PORT)), HOST)
    assert caught.value.code == "registration_rejected" and "register" in str(caught.value)
    assert saved(tmp_path)["client_id"] is None
    # The tokens held from the earlier sign-in were issued to that registration: they go too.
    assert session.state()["status"] == "reauth_required"
    with pytest.raises(ChatGPTError) as caught:
        session.access_token()
    assert caught.value.code == "reauth_required"
    params = dict(httpx.URL(session.start(PORT)).params)
    assert params["client_id"] == REGISTRATION_ENTRY and params["agent_name_hint"] == APP_NAME

    session, api, clock = signed_in(tmp_path / "refresh")
    api.refresh_reply = (401, {"error": "invalid_client"})
    clock.now += 3600
    with pytest.raises(ChatGPTError) as caught:
        session.access_token()
    assert caught.value.code == "registration_rejected" and session.state()["status"] == "reauth_required"
    assert dict(httpx.URL(session.start(PORT)).params)["client_id"] == REGISTRATION_ENTRY


def test_a_server_on_port_80_can_finish_sign_in(tmp_path):
    session, api, _ = new_session(tmp_path)
    url = session.start(80)
    assert dict(httpx.URL(url).params)["redirect_uri"] == "http://127.0.0.1:80/auth/callback"
    session.finish(api.authorize(url), "127.0.0.1")  # a browser leaves the default port out of Host
    assert session.state()["status"] == "connected"


def test_a_canceled_sign_in(tmp_path):
    session, api, _ = new_session(tmp_path)
    query = api.authorize(session.start(PORT), code=None, error="access_denied")
    with pytest.raises(ChatGPTError) as caught:
        session.finish(query, HOST)
    assert caught.value.code == "access_denied" and "canceled" in str(caught.value)
    state = session.state()
    assert state["status"] == "disconnected" and state["error"] == str(caught.value)
    with pytest.raises(ChatGPTError) as again:
        session.finish(query, HOST)  # the matched attempt was used up
    assert again.value.code == "no_sign_in"


@pytest.mark.parametrize("changes", [
    {"client_id": None},  # registration returned no client ID
    {"client_id": REGISTRATION_ENTRY},
    {"client_id": "has spaces"},
    {"client_id": "x" * 201},
])
def test_the_issued_client_id_is_checked(tmp_path, changes):
    session, api, _ = new_session(tmp_path)
    with pytest.raises(ChatGPTError) as caught:
        session.finish(api.authorize(session.start(PORT), **changes), HOST)
    assert caught.value.code == "registration_incomplete"
    assert not (tmp_path / "settings" / "chatgpt.json").exists() and api.token_forms == []


def test_a_client_id_that_differs_from_the_saved_one_is_refused(tmp_path):
    session, api, _ = signed_in(tmp_path)
    with pytest.raises(ChatGPTError) as caught:
        session.finish(api.authorize(session.start(PORT), client_id="app_Other"), HOST)
    assert caught.value.code == "registration_incomplete" and saved(tmp_path)["client_id"] == api.client_id
    # A callback that carries the saved client ID is fine.
    session.finish(api.authorize(session.start(PORT), client_id=api.client_id), HOST)
    assert session.state()["status"] == "connected"


def test_the_registration_is_saved_before_the_code_exchange(tmp_path):
    session, api, _ = new_session(tmp_path)
    api.token_reply = (500, {"error": "server_error"})
    with pytest.raises(ChatGPTError) as caught:
        session.finish(api.authorize(session.start(PORT)), HOST)
    assert caught.value.retryable and session.state()["status"] == "disconnected"
    assert saved(tmp_path)["client_id"] == api.client_id
    params = dict(httpx.URL(session.start(PORT)).params)
    assert params["client_id"] == api.client_id and "agent_name_hint" not in params


def test_without_plan_usage_the_sign_in_fails(tmp_path):
    session, api, _ = new_session(tmp_path)
    api.scope = "openid profile email offline_access resource.invoke"
    with pytest.raises(ChatGPTError) as caught:
        session.finish(api.authorize(session.start(PORT)), HOST)
    assert caught.value.code == "plan_not_granted" and "Plus or Pro" in str(caught.value)
    state = session.state()
    assert state["status"] == "disconnected" and state["error"] == str(caught.value)
    with pytest.raises(ChatGPTError):
        session.access_token()


@pytest.mark.parametrize("changes", [
    {"refresh_token": None}, {"access_token": None}, {"token_type": "mac"}, {"expires_in": 0}, {"expires_in": "3600"},
    {"scope": None}, {"id_token": None}, {"id_token": ""},
])
def test_an_incomplete_token_reply_is_refused(tmp_path, changes):
    session, api, _ = new_session(tmp_path)
    api.token_changes = changes
    with pytest.raises(ChatGPTError) as caught:
        session.finish(api.authorize(session.start(PORT)), HOST)
    assert caught.value.code in ("invalid_token_response", "invalid_id_token") and not caught.value.retryable
    assert session.state()["status"] == "disconnected"


@pytest.mark.parametrize("options", [
    {"nonce": "another-nonce"},
    {"aud": "app_Other"},
    {"key": OTHER_KEY},  # the listed key ID, but signed with another key
    {"iss": "https://auth.example.com"},
    {"exp_in": -60},
    {"azp": "app_Other"},
])
def test_an_id_token_that_does_not_verify_is_refused(tmp_path, options):
    session, api, _ = new_session(tmp_path)
    api.id_token_options = options
    with pytest.raises(ChatGPTError) as caught:
        session.finish(api.authorize(session.start(PORT)), HOST)
    assert caught.value.code == "invalid_id_token" and not caught.value.retryable
    assert session.state()["status"] == "disconnected"


@pytest.mark.parametrize("outage", ["down", "empty", "offline"])
def test_an_unreachable_key_service_is_a_retryable_error(tmp_path, outage):
    session, api, _ = new_session(tmp_path)
    query = api.authorize(session.start(PORT))
    if outage == "down":
        api.jwks_status = 503
    elif outage == "empty":
        api.jwks = {"keys": []}
    else:
        api.offline_paths.add("/.well-known/jwks.json")
    with pytest.raises(ChatGPTError) as caught:
        session.finish(query, HOST)
    assert caught.value.code == "identity_check_unavailable" and caught.value.retryable
    assert session.state()["status"] == "disconnected"


# -- access_token -------------------------------------------------------------------


def test_access_token_needs_a_sign_in(tmp_path):
    session, _, _ = new_session(tmp_path)
    with pytest.raises(ChatGPTError) as caught:
        session.access_token()
    assert caught.value.code == "signed_out" and str(caught.value) == "Sign in with ChatGPT first."


def test_access_token_refreshes_only_near_expiry_and_keeps_the_rotated_token(tmp_path):
    session, api, clock = signed_in(tmp_path)
    clock.now += 3600 - 121
    assert session.access_token() == "at-1" and api.refreshes == 0
    clock.now += 2
    assert session.access_token() == "at-2" and api.refreshes == 1
    assert api.token_forms[-1] == {"grant_type": "refresh_token", "client_id": api.client_id, "refresh_token": "rt-1",
                                   "resource": RESOURCE}
    assert session.access_token() == "at-2" and api.refreshes == 1
    clock.now += 3600
    assert session.access_token() == "at-3" and api.token_forms[-1]["refresh_token"] == "rt-2"
    assert session.state()["status"] == "connected"
    assert [m["slug"] for m in session.refresh_models()] == ["gpt-6.1-sol", "gpt-6-luna", "gpt-6-mini"]


def test_many_threads_cause_one_refresh(tmp_path):
    session, api, clock = signed_in(tmp_path)
    api.refresh_delay = 0.05
    clock.now += 3600
    barrier = threading.Barrier(8)

    def call(_):
        barrier.wait()
        return session.access_token()

    with ThreadPoolExecutor(8) as pool:
        tokens = list(pool.map(call, range(8)))
    assert tokens == ["at-2"] * 8 and api.refreshes == 1


def test_a_refreshed_identity_for_the_same_account_is_accepted(tmp_path):
    session, api, clock = signed_in(tmp_path)
    api.refresh_id_token = {"email": "ada@new.example"}
    clock.now += 3600
    assert session.access_token() == "at-2" and session.state()["email"] == "ada@new.example"


def test_a_refreshed_identity_for_another_account_is_refused(tmp_path):
    session, api, clock = signed_in(tmp_path)
    api.refresh_id_token = {"sub": "user-someone-else"}
    clock.now += 3600
    with pytest.raises(ChatGPTError) as caught:
        session.access_token()
    assert caught.value.code == "account_mismatch"
    assert session.state()["status"] == "reauth_required"
    with pytest.raises(ChatGPTError) as again:
        session.access_token()
    assert again.value.code == "reauth_required"


@pytest.mark.parametrize("status, body", [
    (400, {"error": "invalid_grant", "error_description": "Refresh token expired."}),
    (400, {"error": "refresh_token_reused"}),
    (401, {"error": {"code": "refresh_token_expired", "message": "Expired."}}),
    (401, {"error": {"code": "refresh_token_invalidated", "message": "Revoked."}}),
])
def test_a_refused_refresh_asks_for_a_new_sign_in(tmp_path, status, body):
    session, api, clock = signed_in(tmp_path)
    api.refresh_reply = (status, body)
    clock.now += 3600
    with pytest.raises(ChatGPTError) as caught:
        session.access_token()
    assert not caught.value.retryable and "Sign in with ChatGPT again" in str(caught.value)
    state = session.state()
    assert state["status"] == "reauth_required" and state["error"] == str(caught.value)
    assert state["email"] == "ada@example.com"  # so the page can say which account
    with pytest.raises(ChatGPTError) as again:
        session.access_token()
    assert again.value.code == "reauth_required"
    # Signing in again restores the session.
    session.finish(api.authorize(session.start(PORT)), HOST)
    assert session.state()["status"] == "connected"


@pytest.mark.parametrize("failure", ["server", "offline"])
def test_a_refresh_that_fails_for_now_keeps_the_tokens(tmp_path, failure):
    session, api, clock = signed_in(tmp_path)
    clock.now += 3600
    if failure == "server":
        api.refresh_reply = (503, {"error": "temporarily_unavailable"})
    else:
        api.offline = True
    with pytest.raises(ChatGPTError) as caught:
        session.access_token()
    assert caught.value.retryable and session.state()["status"] == "connected"
    assert session.state()["error"] == str(caught.value)
    api.refresh_reply, api.offline = None, False
    assert session.access_token() == "at-2" and api.token_forms[-1]["refresh_token"] == "rt-1"
    assert session.state()["error"] is None


def test_a_key_service_outage_during_refresh_keeps_the_rotated_tokens(tmp_path):
    session, api, clock = signed_in(tmp_path)
    api.refresh_id_token = {}
    api.jwks_status = 503
    clock.now += 3600
    with pytest.raises(ChatGPTError) as caught:
        session.access_token()
    assert caught.value.code == "identity_check_unavailable" and caught.value.retryable
    assert session.state()["status"] == "connected" and api.refreshes == 1
    api.jwks_status = 200
    # The used-up refresh token is not sent again; the received rotation is verified and used.
    assert session.access_token() == "at-2" and api.refreshes == 1


def test_a_kept_rotation_that_has_expired_is_renewed_with_its_refresh_token(tmp_path):
    session, api, clock = signed_in(tmp_path)
    api.refresh_id_token = {}
    api.jwks_status = 503
    clock.now += 3600
    with pytest.raises(ChatGPTError):
        session.access_token()
    clock.now += 7200  # the outage outlasts the rotated access token too
    api.jwks_status = 200
    assert session.access_token() == "at-3" and api.token_forms[-1]["refresh_token"] == "rt-2"
    assert session.state()["status"] == "connected" and api.refreshes == 2


def test_a_kept_rotation_is_checked_as_of_when_it_arrived(tmp_path):
    session, api, clock = signed_in(tmp_path)
    api.refresh_id_token = {"exp_in": -300}  # expired five minutes ago...
    api.jwks_status = 503
    clock.now += 3600
    with pytest.raises(ChatGPTError):
        session.access_token()
    session._rotation.received_at -= 600  # ...but it arrived ten minutes ago, during the outage
    api.jwks_status = 200
    assert session.access_token() == "at-2"

    session, api, clock = signed_in(tmp_path / "fresh")
    api.refresh_id_token = {"exp_in": -300}  # expired before it arrived: refused
    clock.now += 3600
    with pytest.raises(ChatGPTError) as caught:
        session.access_token()
    assert caught.value.code == "invalid_id_token" and session.state()["status"] == "reauth_required"


def test_a_refresh_without_a_new_refresh_token_is_refused(tmp_path):
    session, api, clock = signed_in(tmp_path)
    clock.now += 3600
    api.refresh_reply = (200, {"access_token": "at-2", "token_type": "Bearer", "expires_in": 3600})
    with pytest.raises(ChatGPTError) as caught:
        session.access_token()
    assert caught.value.code == "invalid_token_response" and session.state()["status"] == "reauth_required"


# -- sign_out -----------------------------------------------------------------------


def test_sign_out_revokes_the_refresh_token_and_keeps_the_registration(tmp_path):
    session, api, clock = signed_in(tmp_path)
    session.set_models("gpt-6-luna", "gpt-6-mini")
    clock.now += 3600
    session.access_token()  # rotates to rt-2
    session.sign_out()
    assert api.revocations == [{"token": "rt-2", "token_type_hint": "refresh_token", "client_id": api.client_id}]
    state = session.state()
    assert state["status"] == "disconnected" and state["error"] is None
    assert state["name"] is None and state["email"] is None and state["models"] == []
    assert (state["manager_model"], state["worker_model"]) == ("gpt-6-luna", "gpt-6-mini")
    assert saved(tmp_path) == {"client_id": api.client_id, "manager_model": "gpt-6-luna", "worker_model": "gpt-6-mini"}
    with pytest.raises(ChatGPTError) as caught:
        session.access_token()
    assert caught.value.code == "signed_out"
    assert dict(httpx.URL(session.start(PORT)).params)["client_id"] == api.client_id


@pytest.mark.parametrize("failure", ["refused", "offline", "no_endpoint"])
def test_a_revocation_that_cannot_be_confirmed_is_reported(tmp_path, failure):
    session, api, _ = new_session(tmp_path)
    if failure == "no_endpoint":
        del api.discovery["revocation_endpoint"]
    session.finish(api.authorize(session.start(PORT)), HOST)
    if failure == "refused":
        api.revoke_status = 503
    elif failure == "offline":
        api.offline = True
    session.sign_out()
    state = session.state()
    assert state["status"] == "disconnected" and "ChatGPT Settings" in state["error"]
    with pytest.raises(ChatGPTError):
        session.access_token()


# -- models -------------------------------------------------------------------------


def test_set_models_checks_the_catalog_and_persists(tmp_path):
    session, api, _ = signed_in(tmp_path)
    with pytest.raises(ValueError, match="gpt-6-mini"):
        session.set_models("gpt-5", "gpt-6-mini")
    with pytest.raises(ValueError):
        session.set_models("gpt-6-luna", "internal-eval")  # in the catalog, but not listed
    with pytest.raises(ValueError):
        session.set_models("", "gpt-6-mini")
    assert (session.state()["manager_model"], session.state()["worker_model"]) == ("gpt-6.1-sol", "gpt-6-luna")
    session.set_models("gpt-6-luna", "gpt-6-mini")

    later, _, _ = new_session(tmp_path, api)
    state = later.state()
    assert state["status"] == "disconnected" and state["models"] == []
    assert (state["manager_model"], state["worker_model"]) == ("gpt-6-luna", "gpt-6-mini")
    later.set_models("anything", "goes")  # no catalog loaded yet
    assert saved(tmp_path) == {"client_id": api.client_id, "manager_model": "anything", "worker_model": "goes"}


def test_the_default_models_apply_until_a_choice_is_made(tmp_path):
    session, api, _ = new_session(tmp_path)
    session.finish(api.authorize(session.start(PORT)), HOST)
    assert "manager_model" not in saved(tmp_path)
    later = ChatGPTSession(tmp_path / "settings" / "chatgpt.json", manager_model="gpt-6-mini", worker_model="gpt-6-luna")
    assert later.state()["manager_model"] == "gpt-6-mini"  # the settings' default, not one saved at registration


def test_a_default_the_plan_does_not_list_can_stay_while_the_other_role_changes(tmp_path):
    api = FakeOpenAI()
    session = ChatGPTSession(tmp_path / "settings" / "chatgpt.json", client=httpx.Client(transport=httpx.MockTransport(api)),
                             manager_model="gpt-7-preview")
    session.finish(api.authorize(session.start(PORT)), HOST)
    session.set_models("gpt-7-preview", "gpt-6-mini")
    assert (session.state()["manager_model"], session.state()["worker_model"]) == ("gpt-7-preview", "gpt-6-mini")
    with pytest.raises(ValueError):
        session.set_models("gpt-7-preview", "gpt-8")


def test_a_corrupt_settings_file_is_ignored(tmp_path):
    path = tmp_path / "settings" / "chatgpt.json"
    path.parent.mkdir()
    path.write_text("{not json")
    session, api, _ = new_session(tmp_path)
    assert dict(httpx.URL(session.start(PORT)).params)["client_id"] == REGISTRATION_ENTRY
    path.write_text(json.dumps({"client_id": REGISTRATION_ENTRY, "manager_model": 5}))
    session, api, _ = new_session(tmp_path)
    assert dict(httpx.URL(session.start(PORT)).params)["client_id"] == REGISTRATION_ENTRY
    assert session.state()["manager_model"] == "gpt-6.1-sol"
    session.finish(api.authorize(session.start(PORT)), HOST)
    assert saved(tmp_path)["client_id"] == api.client_id
