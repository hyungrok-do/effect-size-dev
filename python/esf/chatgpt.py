"""Sign in with ChatGPT, so the local edition's model calls run on the person's ChatGPT plan.

The browser does OpenAI's sign-in (OAuth with PKCE and an OIDC nonce) and comes back to this
server's loopback callback. Tokens live in this process's memory only and are never written to
disk; the settings file holds the host ID, registration/account binding and chosen models.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx
import jwt

from . import __version__

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
CALLBACK_PATH = "/auth/callback"
REGISTRATION_ENTRY = "dynamic_agent_client"
APP_NAME = "Effect Size Finder"
PENDING_SECONDS = 600  # how long a started sign-in waits for its callback
MAX_PENDING = 3  # sign-ins started again (another click, a closed tab) that stay valid alongside

MODELS_URL = f"{RESOURCE}/models"
# Seconds of validity left when an access token is renewed, if ChatGPT allows a refresh by then
# (earliest_refresh_at); until it does, the token is used for as long as it lasts.
REFRESH_MARGIN = 60
REFRESH_WAIT = 60  # the longest a request waits for a refresh ChatGPT doesn't allow yet, once its token has expired
# The longest a job's model call waits for it: a job would otherwise fail every record in that time.
JOB_REFRESH_WAIT = 600
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
ERROR_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,100}")
# Refresh errors after which only a new sign-in helps.
REAUTH_CODES = frozenset({
    "invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
    "refresh_token_invalidated", "refresh_token_reused",
})
API_REAUTH_CODES = REAUTH_CODES | frozenset({
    "invalid_token", "invalid_api_key", "invalid_authorization_context",
    "subscription_sharing_invalid_user", "chatpass_v2_scope_not_authorized",
})

AGAIN = "Sign in with ChatGPT again."
LATER = "Try again in a few minutes."
ENDPOINTS = ("authorization_endpoint", "token_endpoint", "jwks_uri", "revocation_endpoint")

# The page's action that calls start(new_registration=True); messages name it so the person can find it.
ANOTHER_ACCOUNT = "Use another ChatGPT account"
SIGN_IN_AGAIN = "Your ChatGPT sign-in has expired or was revoked. Sign in with ChatGPT again."
IDENTITY_UNAVAILABLE = "ChatGPT's identity check is temporarily unavailable. Try again in a few minutes."
INVALID_IDENTITY = "ChatGPT's reply did not carry a valid identity for this sign-in. Sign in with ChatGPT again."
PLAN_NOT_GRANTED = (
    "ChatGPT did not let this app use your plan. That needs a ChatGPT Plus or Pro plan. If you have one, sign in "
    f"with ChatGPT again and, on the next screen, allow {APP_NAME} to use your plan."
)
SIGN_IN_EXPIRED = "This sign-in took longer than ten minutes. Sign in with ChatGPT again."
NO_CALLBACK = (
    f"The ChatGPT tab didn't come back to {APP_NAME} within ten minutes. If ChatGPT showed an error there, "
    f'choose "{ANOTHER_ACCOUNT}"; otherwise sign in with ChatGPT again.'
)
REGISTRATION_INCOMPLETE = (
    "ChatGPT did not complete this app's registration. If you signed in with a different ChatGPT account or "
    f'workspace than before, choose "{ANOTHER_ACCOUNT}"; otherwise sign in with ChatGPT again.'
)
ACCOUNT_MISMATCH = (
    f'This app is registered to another ChatGPT account. Sign in with that account, or choose "{ANOTHER_ACCOUNT}" '
    "to register the app for this one."
)
REFRESH_NOT_READY = f"ChatGPT doesn't allow this app to renew your sign-in yet. {LATER}"
REGISTRATION_REJECTED = (
    f"ChatGPT no longer accepts this app's registration. Sign in with ChatGPT again to register {APP_NAME} anew."
)
REJECTED_REGISTRATION_CODES = frozenset({"invalid_client", "unauthorized_client"})
REVOCATION_UNCONFIRMED = (
    "You are signed out here, but ChatGPT did not confirm that this app's access was revoked. "
    f"To be sure, disconnect {APP_NAME} in ChatGPT Settings."
)
# A sign-in on a new registration revokes the grant of the tokens it replaces, issued to the earlier
# registration, which Sign out could no longer reach; these say so when ChatGPT doesn't confirm it, for
# another account's grant and for the same account's earlier registration.
EARLIER_ACCOUNT_UNCONFIRMED = (
    "ChatGPT did not confirm that the account signed in before lost this app's access, so it may still be active. "
    f"To be sure, disconnect {APP_NAME} in that account's ChatGPT Settings."
)
EARLIER_SIGN_IN_UNCONFIRMED = (
    "ChatGPT did not confirm that this app's earlier registration lost its access, so it may still be active. "
    f"To be sure, disconnect the older {APP_NAME} in ChatGPT Settings."
)


class ChatGPTError(RuntimeError):
    """A sign-in or ChatGPT request failed; str(error) is a message a person can act on."""

    def __init__(self, code: str, message: str, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status = status


# Known OAuth and API error codes; anything else gets a generic message with the HTTP status.
_KNOWN_ERRORS: dict[str, tuple[str, bool]] = {
    "access_denied": ("Sign-in was canceled. Sign in with ChatGPT again when you are ready.", False),
    "invalid_grant": (
        "ChatGPT did not accept the sign-in code; it may have expired or been used already. Sign in with ChatGPT again.",
        False,
    ),
    "invalid_client": ("ChatGPT did not accept this app's registration. Sign in with ChatGPT again.", False),
    "unauthorized_client": ("ChatGPT did not accept this app's registration. Sign in with ChatGPT again.", False),
    "invalid_scope": (
        "ChatGPT did not grant the permissions this app asks for. Using your plan in apps needs ChatGPT Plus or Pro.",
        False,
    ),
    "consent_required": ("ChatGPT needs you to sign in and approve this app again. Sign in with ChatGPT.", False),
    "login_required": ("ChatGPT needs you to sign in and approve this app again. Sign in with ChatGPT.", False),
    "interaction_required": ("ChatGPT needs you to sign in and approve this app again. Sign in with ChatGPT.", False),
    "server_error": ("ChatGPT's sign-in service had a problem. Try again in a few minutes.", True),
    "temporarily_unavailable": ("ChatGPT's sign-in service is temporarily unavailable. Try again in a few minutes.", True),
    "invalid_token": ("ChatGPT did not accept this sign-in. Sign in with ChatGPT again.", False),
    "invalid_api_key": ("ChatGPT did not accept this sign-in. Sign in with ChatGPT again.", False),
    "subscription_sharing_user_not_eligible": (
        "This ChatGPT account can't share its plan with apps; that needs ChatGPT Plus or Pro.",
        False,
    ),
    "subscription_sharing_usage_limit_exceeded": (
        "Your ChatGPT plan's usage limit for apps has been reached. Check your usage in ChatGPT Settings, "
        "or wait for the limit to reset.",
        False,
    ),
    "subscription_sharing_usage_unavailable": (f"ChatGPT could not check your plan's usage. {LATER}", True),
    "subscription_sharing_user_unavailable": (f"Your ChatGPT account is temporarily unavailable. {LATER}", True),
    "model_not_found": ("This model is not available on your ChatGPT plan. Choose another model.", False),
    **{code: (SIGN_IN_AGAIN, False) for code in REAUTH_CODES - {"invalid_grant"}},
}


def api_error(body: Any, status: int | None = None) -> ChatGPTError:
    """The error for an OAuth reply ({"error": "code"}) or an API reply ({"error": {"code": ...}}).

    The server's own text is never repeated, since it can echo what was sent."""
    detail = body.get("error") if isinstance(body, dict) else None
    raw = (detail.get("code") or detail.get("type")) if isinstance(detail, dict) else detail
    code = raw if isinstance(raw, str) and ERROR_CODE.fullmatch(raw) else "api_error"
    # Earlier replies named the plan-sharing errors with a "_v2" infix.
    known = _KNOWN_ERRORS.get(code) or _KNOWN_ERRORS.get(code.replace("subscription_sharing_v2_", "subscription_sharing_"))
    if known:
        return ChatGPTError(code, known[0], known[1], status)
    if status == 401:
        return ChatGPTError(code, f"ChatGPT did not accept this sign-in. {AGAIN}", False, status)
    if status == 403:
        message = "ChatGPT refused this request (HTTP 403). Your plan, account or region may not allow it."
        return ChatGPTError(code, message, False, status)
    if status == 429:
        return ChatGPTError(code, "ChatGPT received too many requests from this app. Wait a minute and try again.", True, status)
    if status is not None and status >= 500:
        return ChatGPTError(code, f"ChatGPT is temporarily unavailable (HTTP {status}). {LATER}", True, status)
    if status is None:
        return ChatGPTError(code, f"ChatGPT sign-in did not complete ({code}). {AGAIN}")
    named = "" if code == "api_error" else f", {code}"
    message = f"ChatGPT could not complete the request (HTTP {status}{named}). Try again; if it keeps failing, sign in with ChatGPT again."
    return ChatGPTError(code, message, False, status)


def redirect_uri(port: int) -> str:
    return f"http://127.0.0.1:{port}{CALLBACK_PATH}"


# repr=False throughout: these hold secrets, and object's repr shows none of them.
@dataclass(repr=False)
class _Tokens:
    access_token: str
    refresh_token: str
    expires_at: float  # on the session's clock
    scopes: list[str]
    client_id: str  # the registration they were issued to, which renews and revokes them
    refresh_after: float = 0.0  # earliest_refresh_at in epoch seconds: ChatGPT refuses a refresh before it


@dataclass(repr=False)
class _Attempt:
    state: str
    nonce: str
    verifier: str
    port: int
    started_at: float


@dataclass(repr=False)
class _Rotation:
    """Tokens from a refresh whose new id_token is not verified yet. The refresh token they replace has
    been used up, so they are kept for the retry, but not used until the identity is verified."""

    tokens: _Tokens
    id_token: str
    received_at: float  # real time: the id_token is checked as of when it arrived


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def _origin(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if not parts.scheme or not parts.hostname:
        return None
    default = {"https": 443, "http": 80}.get(parts.scheme)
    return f"{parts.scheme}://{parts.hostname}" + (f":{port}" if port is not None and port != default else "")


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _positive(value: Any) -> bool:
    return _number(value) and value > 0


def _epoch(value: Any) -> float:
    """earliest_refresh_at as epoch seconds. It may come as seconds or as an ISO 8601 time; anything
    else allows a refresh at once, as in the DevKit."""
    if isinstance(value, str):
        text = value.strip()
        try:
            # Python before 3.11 doesn't read a "Z"; a time without a zone is taken as UTC.
            when = datetime.fromisoformat(text[:-1] + "+00:00" if text[-1:] in ("Z", "z") else text)
            return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()
        except (ValueError, OverflowError, OSError):
            return 0.0
    return float(value) if _number(value) else 0.0


def _token_fields(body: dict, sent_at: float, client_id: str, previous_scopes: list[str] | None = None) -> _Tokens:
    scope = body.get("scope")
    if scope is None and previous_scopes is not None:
        scopes = list(previous_scopes)  # a refresh may leave out scope when it is unchanged
    elif isinstance(scope, str):
        scopes = scope.split()
    else:
        raise ChatGPTError("invalid_token_response", f"ChatGPT did not confirm which permissions it granted. {AGAIN}")
    if PLAN_SCOPE not in scopes:
        raise ChatGPTError("plan_not_granted", PLAN_NOT_GRANTED)
    access, token_type, refresh = body.get("access_token"), body.get("token_type"), body.get("refresh_token")
    if (
        not (isinstance(access, str) and access)
        or not (isinstance(token_type, str) and token_type.lower() == "bearer")
        or not _positive(body.get("expires_in"))
        or not (isinstance(refresh, str) and refresh)
    ):
        raise ChatGPTError("invalid_token_response", f"ChatGPT returned incomplete sign-in credentials. {AGAIN}")
    expires_at = sent_at + float(body["expires_in"])
    return _Tokens(access, refresh, expires_at, scopes, client_id, _epoch(body.get("earliest_refresh_at")))


def _reasoning_levels(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    levels = []
    for item in value:
        effort = item.get("effort") if isinstance(item, dict) else item
        if isinstance(effort, str) and effort:
            levels.append(effort)
    return levels


def _parse_models(body: Any) -> list[dict]:
    """The models the plan lists. Anything unexpected is skipped rather than failing the sign-in."""
    if not isinstance(body, dict) or not isinstance(body.get("models"), list):
        return []
    models: list[dict] = []
    for entry in body["models"]:
        if not isinstance(entry, dict) or entry.get("visibility") != "list":
            continue
        slug, name = entry.get("slug"), entry.get("display_name")
        if not (isinstance(slug, str) and slug.strip() and isinstance(name, str) and name.strip()):
            continue
        if any(m["slug"] == slug for m in models):
            continue
        levels = _reasoning_levels(entry.get("supported_reasoning_levels"))
        models.append({"slug": slug, "display_name": name, "reasoning_levels": levels})
    return models


def _copy_model(model: dict) -> dict:
    levels = model["reasoning_levels"]
    return {**model, "reasoning_levels": list(levels) if levels is not None else None}


class ChatGPTSession:
    """One person's ChatGPT sign-in for this server process."""

    def __init__(
        self,
        path: Path,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
        manager_model: str = "gpt-6.1-sol",
        worker_model: str = "gpt-6-luna",
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.path = Path(path)
        self._client = client or httpx.Client(
            timeout=TIMEOUT, follow_redirects=False, headers={"User-Agent": f"effect-size-finder/{__version__}"}
        )
        # Wall-clock time (earliest_refresh_at is compared with it); tests replace it and sleep together.
        self._clock = clock
        self._sleep = sleep
        # _lock guards the fields below and is never held across a network call. _refresh_lock is taken
        # (before _lock) by everything that replaces the tokens: refresh tokens rotate on every refresh,
        # so two refreshes must never run at once, and a sign-in or sign-out must not interleave with one.
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._discovered: dict[str, str] | None = None
        self._client_id: str | None = None
        self._host_id: str | None = None
        self._registered_subject: str | None = None
        self._manager_model = manager_model
        self._worker_model = worker_model
        self._models_chosen = False  # saved only once chosen, so until then the settings' defaults apply
        self._status = "disconnected"
        self._name: str | None = None
        self._email: str | None = None
        self._subject: str | None = None
        self._error: str | None = None
        self._models: list[dict] = []
        self._tokens: _Tokens | None = None
        self._rotation: _Rotation | None = None
        self._attempts: list[_Attempt] = []
        self._reconsent = False  # ChatGPT did not grant use of the plan: the next sign-in asks again
        self._account_changed = False  # the latest sign-in was for another account than the one before it
        # Bumped by sign-out and cancel, so a code exchange already under way can't sign the person back in.
        self._generation = 0
        self._load()

    # -- settings file ------------------------------------------------------------

    def _load(self) -> None:
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return  # missing or corrupt: the next save replaces it
        if not isinstance(saved, dict):
            return
        client_id = saved.get("client_id")
        if isinstance(client_id, str) and CLIENT_ID.fullmatch(client_id) and client_id != REGISTRATION_ENTRY:
            self._client_id = client_id
            subject = saved.get("subject")
            if isinstance(subject, str) and subject:
                self._registered_subject = subject
        host_id = saved.get("host_id")
        if isinstance(host_id, str) and host_id.startswith("urn:uuid:"):
            try:
                self._host_id = uuid.UUID(host_id).urn
            except ValueError:
                pass
        manager, worker = saved.get("manager_model"), saved.get("worker_model")
        if isinstance(manager, str) and manager.strip() and isinstance(worker, str) and worker.strip():
            self._manager_model, self._worker_model, self._models_chosen = manager, worker, True

    def _save(self) -> None:
        """Write the settings file atomically. Called with _lock held."""
        data: dict[str, Any] = {"client_id": self._client_id}
        if self._host_id is not None:
            data["host_id"] = self._host_id
        if self._client_id is not None and self._registered_subject is not None:
            data["subject"] = self._registered_subject
        if self._models_chosen:
            data.update(manager_model=self._manager_model, worker_model=self._worker_model)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # mkstemp creates the file readable and writable by its owner only (0o600).
            fd, temp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                    f.write("\n")
                os.replace(temp, self.path)
            except BaseException:
                try:
                    os.unlink(temp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            message = f"The ChatGPT settings could not be saved to {self.path} ({exc.strerror or exc}). Check that the folder is writable."
            raise ChatGPTError("storage_failed", message) from exc

    # -- state --------------------------------------------------------------------

    def _expired(self, attempt: _Attempt) -> bool:
        return self._clock() - attempt.started_at > PENDING_SECONDS

    def state(self) -> dict:
        """The sign-in ("status": disconnected, connected or reauth_required, from whether tokens are held)
        and whether a sign-in waits for its callback ("pending"); never a token. "account_changed" says
        the latest sign-in was for another ChatGPT account than the one signed in before it,
        "reconsent" that the next sign-in asks ChatGPT to show its permission screen again, and
        "registered" that the app's registration with ChatGPT is saved (it is bound to one account, so
        another account needs a new one), after a sign-out or a restart too."""
        with self._lock:
            pending = any(not self._expired(a) for a in self._attempts)
            error = self._error
            if error is None and self._attempts and not pending and self._status != "connected":
                # Sign-ins that ran out of time with no callback at all: ChatGPT shows its own error page,
                # and sends nobody back, for a client ID it won't take (another account's, for one). Not
                # while signed in: that sign-in still serves, and the message would stay until the next one.
                error = NO_CALLBACK
            return {
                "status": self._status,
                "pending": pending,
                "name": self._name,
                "email": self._email,
                "error": error,
                "account_changed": self._account_changed,
                "reconsent": self._reconsent,
                "registered": self._client_id is not None,
                "models": [_copy_model(m) for m in self._models],
                "manager_model": self._manager_model,
                "worker_model": self._worker_model,
            }

    # -- HTTP ---------------------------------------------------------------------

    def _request(self, method: str, url: str, headers: dict[str, str] | None = None, **kwargs: Any) -> httpx.Response:
        try:
            # Per request, so a shared client that follows redirects still doesn't here.
            return self._client.request(
                method, url, headers={"Accept": "application/json", **(headers or {})},
                follow_redirects=False, timeout=TIMEOUT, **kwargs,
            )
        except httpx.HTTPError as exc:
            message = "Could not reach ChatGPT. Check your internet connection and try again."
            raise ChatGPTError("network_error", message, True) from exc

    def _discovery(self) -> dict[str, str]:
        if self._discovered is not None:
            return self._discovered
        failed = ChatGPTError(
            "discovery_failed",
            f"ChatGPT's sign-in service could not be reached or verified. Check your internet connection. {LATER}",
            True,
        )
        try:
            response = self._request("GET", f"{ISSUER}/.well-known/openid-configuration")
        except ChatGPTError as exc:
            raise failed from exc
        body = _json(response)
        if response.status_code != 200 or not isinstance(body, dict) or body.get("issuer") != ISSUER:
            raise failed
        for key in ENDPOINTS:
            if key == "revocation_endpoint" and key not in body:
                continue
            if not isinstance(body.get(key), str) or _origin(body[key]) != ISSUER:
                raise failed
        # Cached only once it checks out, so a failed fetch is tried again next time.
        self._discovered = {key: body[key] for key in ENDPOINTS if key in body}
        return self._discovered

    def _token_request(self, form: dict[str, str]) -> dict:
        response = self._request("POST", self._discovery()["token_endpoint"], data=form)
        body = _json(response)
        if not response.is_success:
            raise api_error(body, response.status_code)
        if not isinstance(body, dict):
            raise ChatGPTError("invalid_token_response", f"ChatGPT returned an unexpected sign-in reply. {AGAIN}")
        return body

    def _signing_key(self, jwks_uri: str, kid: str) -> jwt.PyJWK:
        unavailable = ChatGPTError("identity_check_unavailable", IDENTITY_UNAVAILABLE, True)
        try:
            response = self._request("GET", jwks_uri)
        except ChatGPTError as exc:
            raise unavailable from exc
        body = _json(response)
        if response.status_code != 200 or not isinstance(body, dict) or not isinstance(body.get("keys"), list):
            raise unavailable
        try:
            keys = jwt.PyJWKSet.from_dict({"keys": [k for k in body["keys"] if isinstance(k, dict)]})
        except Exception as exc:  # whatever the parser makes of malformed keys, the key service is at fault
            raise unavailable from exc
        for key in keys.keys:
            if key.key_id == kid:
                return key
        # A key the set doesn't list yet is the key service lagging a rotation, not a forged token: a
        # token signed with a listed key but not by it fails the signature check instead.
        raise unavailable

    def _verify(self, id_token: str, client_id: str, nonce: str | None, leeway: float = 5) -> dict:
        """The id_token's claims once its signature, issuer, audience, times and nonce check out."""
        try:
            jwks_uri = self._discovery()["jwks_uri"]
        except ChatGPTError as exc:
            raise ChatGPTError("identity_check_unavailable", IDENTITY_UNAVAILABLE, True) from exc
        invalid = ChatGPTError("invalid_id_token", INVALID_IDENTITY)
        try:
            kid = jwt.get_unverified_header(id_token).get("kid")
        except jwt.PyJWTError as exc:
            raise invalid from exc
        if not isinstance(kid, str) or not kid:
            raise invalid
        key = self._signing_key(jwks_uri, kid)
        try:
            # iat must be there, but isn't held against this computer's clock, which may run behind
            # OpenAI's: jose, which the DevKit uses, checks it only for a maximum token age.
            claims = jwt.decode(
                id_token, key.key, algorithms=["RS256"], audience=client_id, issuer=ISSUER, leeway=leeway,
                options={"require": ["exp", "iat", "sub", "aud", "iss"], "verify_iat": False},
            )
        except jwt.PyJWTError as exc:
            raise invalid from exc
        audience = claims.get("aud")
        if (
            not _number(claims.get("iat"))
            or not isinstance(claims.get("sub"), str)
            or not claims["sub"]
            or (nonce is not None and claims.get("nonce") != nonce)
            or ("azp" in claims and claims["azp"] != client_id)
            or (isinstance(audience, list) and len(audience) > 1 and claims.get("azp") != client_id)
        ):
            raise invalid
        return claims

    def _fetch_models(self, access_token: str) -> list[dict]:
        response = self._request("GET", MODELS_URL, headers={"Authorization": f"Bearer {access_token}"})
        body = _json(response)
        if not response.is_success:
            error = api_error(body, response.status_code)
            self.handle_api_error(access_token, error.code, error.status)
            raise error
        return _parse_models(body)

    def _revoke(self, refresh_token: str, client_id: str | None) -> bool:
        try:
            endpoint = self._discovery().get("revocation_endpoint")
            if not endpoint or client_id is None:
                return False
            response = self._request(
                "POST", endpoint, data={"token": refresh_token, "token_type_hint": "refresh_token", "client_id": client_id}
            )
        except ChatGPTError:
            return False
        return response.status_code == 200

    # -- sign-in ------------------------------------------------------------------

    def start(self, port: int, reconsent: bool = False, new_registration: bool = False) -> str:
        """The address of ChatGPT's sign-in page, for a sign-in that comes back to this port.

        reconsent asks ChatGPT to show its permission screen again, as the next sign-in does by itself
        after ChatGPT did not grant use of the plan. new_registration (the page's "Use another ChatGPT
        account") forgets the saved registration and registers the app anew, since OpenAI binds each
        issued client ID to one ChatGPT account and workspace."""
        if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
            raise ValueError("The sign-in callback port must be a number from 1 to 65535.")
        endpoint = self._discovery()["authorization_endpoint"]
        attempt = _Attempt(
            state=secrets.token_urlsafe(32), nonce=secrets.token_urlsafe(32), verifier=secrets.token_urlsafe(32),
            port=port, started_at=self._clock(),
        )
        with self._lock:
            before = self._host_id, self._client_id, self._registered_subject
            if self._host_id is None:
                self._host_id = uuid.uuid4().urn
            if new_registration:
                # Tokens held stay usable until the new sign-in replaces them: they carry their own client ID.
                self._client_id = self._registered_subject = None
            if (self._host_id, self._client_id, self._registered_subject) != before:
                try:
                    self._save()
                except ChatGPTError:
                    self._host_id, self._client_id, self._registered_subject = before
                    raise
            if new_registration:
                # Sign-ins already started, and a code exchange under way, are for the forgotten registration.
                self._attempts = []
                self._generation += 1
            client_id = self._client_id
            host_id = self._host_id
            # After a sign-in expires, ChatGPT is told which account to expect; a new registration may be for any.
            login_hint = self._email if self._status == "reauth_required" and not new_registration else None
            consent = reconsent or self._reconsent
            # Earlier attempts stay valid (a second click, a closed tab): whichever finishes first wins.
            self._attempts = [a for a in self._attempts if not self._expired(a)][-(MAX_PENDING - 1):] + [attempt]
            self._error = None
        params = {
            "client_id": client_id or REGISTRATION_ENTRY,
            "ext_agent_host_id": host_id,
            "response_type": "code",
            "redirect_uri": redirect_uri(port),
            "scope": SCOPES,
            "resource": RESOURCE,
            "state": attempt.state,
            "nonce": attempt.nonce,
            "code_challenge": _challenge(attempt.verifier),
            "code_challenge_method": "S256",
        }
        if client_id is None:
            # The first sign-in registers the app under this name; later ones reuse the issued client ID.
            params["agent_name_hint"] = APP_NAME
        if login_hint:
            params["login_hint"] = login_hint
        if consent:
            params["prompt"] = "consent"
        return endpoint + ("&" if urlsplit(endpoint).query else "?") + urlencode(params, quote_via=quote)

    def finish(self, query: str, host: str) -> None:
        try:
            params = parse_qs(query, keep_blank_values=True, max_num_fields=32)
        except ValueError:
            params = {}
        states = params.get("state", [])
        with self._lock:
            # A request that doesn't belong to a sign-in in progress must not cancel it, so these leave it pending.
            given = states[0].encode() if len(states) == 1 else b""
            attempt = next((a for a in self._attempts if hmac.compare_digest(given, a.state.encode())), None)
            if attempt is None:
                if not any(not self._expired(a) for a in self._attempts):
                    raise ChatGPTError(
                        "no_sign_in",
                        f"No ChatGPT sign-in is waiting for this callback. If you already signed in, return to {APP_NAME}; "
                        "otherwise start sign-in again.",
                    )
                raise ChatGPTError(
                    "wrong_state",
                    "This callback does not belong to the sign-in in progress. Return to the browser tab that started "
                    "sign-in, or start sign-in again.",
                )
            if self._expired(attempt):
                self._attempts.remove(attempt)
                self._error = SIGN_IN_EXPIRED
                raise ChatGPTError("sign_in_expired", SIGN_IN_EXPIRED)
            # A browser leaves the default port out of Host.
            if host not in (f"127.0.0.1:{attempt.port}", *(("127.0.0.1",) if attempt.port == 80 else ())):
                raise ChatGPTError(
                    "wrong_host",
                    f"This callback did not come to {redirect_uri(attempt.port)}. Return to the browser tab that started sign-in.",
                )
            self._attempts = []  # one use, and the other tabs' sign-ins are no longer needed
            saved, generation = self._client_id, self._generation
        try:
            self._complete(attempt, params, saved, generation)
        except ChatGPTError as exc:
            with self._lock:
                self._error = str(exc)
            raise

    def cancel(self) -> None:
        """Drop the sign-ins waiting for their callback; a code exchange already under way is discarded."""
        with self._lock:
            self._attempts = []
            self._generation += 1

    def _complete(self, attempt: _Attempt, params: dict[str, list[str]], saved: str | None, generation: int) -> None:
        if "error" in params:
            error = api_error({"error": params["error"][0]})
            raise self._registration_rejected(saved) if error.code in REJECTED_REGISTRATION_CODES else error
        codes, returned = params.get("code", []), params.get("client_id", [])
        if len(codes) != 1 or not codes[0]:
            raise ChatGPTError("invalid_callback", f"ChatGPT's sign-in callback had no authorization code. {AGAIN}")
        client_id = returned[0] if len(returned) == 1 else saved if not returned else None
        if (
            client_id is None
            or not CLIENT_ID.fullmatch(client_id)
            or client_id == REGISTRATION_ENTRY
            or (saved is not None and client_id != saved)
        ):
            raise ChatGPTError("registration_incomplete", REGISTRATION_INCOMPLETE)
        if client_id != saved:
            # Saved before the code exchange, so a failed exchange doesn't register the app a second time.
            with self._lock:
                self._client_id = client_id
                self._save()
        sent = self._clock()
        try:
            body = self._token_request({
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": codes[0],
                "code_verifier": attempt.verifier,
                "redirect_uri": redirect_uri(attempt.port),
                "resource": RESOURCE,
            })
        except ChatGPTError as exc:
            if exc.code in REJECTED_REGISTRATION_CODES:
                raise self._registration_rejected(client_id) from None
            raise
        try:
            tokens = _token_fields(body, sent, client_id)
        except ChatGPTError as exc:
            if exc.code == "plan_not_granted":
                self._plan_refused(body, client_id)
            raise
        id_token = body.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise ChatGPTError("invalid_id_token", INVALID_IDENTITY)
        claims = self._verify(id_token, client_id, attempt.nonce)
        with self._refresh_lock:
            try:
                with self._lock:
                    if generation != self._generation:
                        raise ChatGPTError("canceled", f"Sign-in was canceled in {APP_NAME} before it finished.")
                    if self._registered_subject is not None and claims["sub"] != self._registered_subject:
                        raise ChatGPTError("account_mismatch", ACCOUNT_MISMATCH)
                    if self._registered_subject is None:
                        # Also migrates registrations saved before account binding was recorded.
                        self._registered_subject = claims["sub"]
                        try:
                            self._save()
                        except ChatGPTError:
                            self._registered_subject = None
                            raise
                    # The rotation's tokens are the live ones while one is held: its parent is used up.
                    replaced = self._rotation.tokens if self._rotation is not None else self._tokens
                    self._tokens, self._rotation = tokens, None
                    self._status, self._error = "connected", None
                    # Against the account signed in before (kept through an expired sign-in), so the page
                    # can say the plan in use is another account's now.
                    self._account_changed = self._subject is not None and claims["sub"] != self._subject
                    other_account = self._account_changed
                    self._subject = claims["sub"]
                    self._reconsent = False
                    self._name = claims["name"] if isinstance(claims.get("name"), str) else None
                    self._email = claims["email"] if isinstance(claims.get("email"), str) else None
                    self._models = []
            except ChatGPTError:
                self._revoke(tokens.refresh_token, client_id)
                raise
        # Only a grant of another registration: revoking one issued to this registration could, on a server
        # that revokes per account and client, take the new sign-in with it. Sign out still reaches that.
        if replaced is not None and replaced.client_id != tokens.client_id:
            self._revoke_replaced(replaced, tokens, other_account)
        try:
            models = self._fetch_models(tokens.access_token)
        except ChatGPTError as exc:
            with self._lock:
                if self._tokens is tokens:
                    # After the note about the replaced grant, if there is one: both still hold.
                    failed = f"You are signed in, but the list of ChatGPT models could not be loaded. {exc}"
                    self._error = f"{self._error} {failed}" if self._error else failed
        else:
            with self._lock:
                if self._tokens is tokens:
                    self._models = models

    def _revoke_replaced(self, replaced: _Tokens, tokens: _Tokens, other_account: bool) -> None:
        """Revoke the grant of the tokens a sign-in replaced, with the client ID they were issued to, best
        effort: no one holds them any more, so Sign out could not revoke it later."""
        if self._revoke(replaced.refresh_token, replaced.client_id):
            return
        with self._lock:
            if self._tokens is tokens:  # not signed out, or in again, meanwhile
                self._error = EARLIER_ACCOUNT_UNCONFIRMED if other_account else EARLIER_SIGN_IN_UNCONFIRMED

    # -- tokens -------------------------------------------------------------------

    def access_token(self, max_wait: float = REFRESH_WAIT) -> str:
        """A valid access token, renewed when it needs to be. A renewal ChatGPT doesn't allow yet is
        waited for, for up to max_wait seconds: REFRESH_WAIT suits a page's request, and a job's model
        calls wait longer (JOB_REFRESH_WAIT)."""
        token = self._current_token()
        if token is not None:
            return token
        self._wait_for_refresh(max_wait)  # outside the locks, so a sign-out doesn't wait for it
        with self._refresh_lock:
            # Another thread may have refreshed while this one waited for the lock.
            token = self._current_token()
            if token is None:
                token = self._refresh()
        # None: a kept rotation was installed, and the refresh it needs is not allowed yet.
        return token if token is not None else self.access_token(max_wait)

    def _current_token(self) -> str | None:
        """The access token while it has more than REFRESH_MARGIN seconds left, or while it is still valid
        and ChatGPT doesn't allow a refresh yet; None when it needs renewing."""
        with self._lock:
            tokens = self._tokens
            if tokens is None:
                if self._status == "reauth_required":
                    raise ChatGPTError("reauth_required", SIGN_IN_AGAIN)
                raise ChatGPTError("signed_out", "Sign in with ChatGPT first.")
            if self._rotation is not None:
                return None
            now = self._clock()
            left = tokens.expires_at - now
            if left > REFRESH_MARGIN or (left > 0 and now < tokens.refresh_after):
                return tokens.access_token
            return None

    def _wait_for_refresh(self, max_wait: float) -> None:
        """Wait until ChatGPT allows a refresh, when the access token expired before it does, for up to
        max_wait seconds; longer than that is a retryable error, on which a job's run stops."""
        with self._lock:
            if self._tokens is None or self._rotation is not None:
                return
            wait = self._tokens.refresh_after - self._clock()
        if wait <= 0:
            return
        if wait > max_wait:
            raise self._note(ChatGPTError("refresh_not_ready", REFRESH_NOT_READY, True))
        self._sleep(wait)

    def _refresh(self) -> str | None:
        """Renew the tokens; None when a kept rotation, installed now, needs a refresh ChatGPT doesn't
        allow yet, which the caller waits for outside the locks. Called with _refresh_lock held."""
        with self._lock:
            tokens, rotation, subject = self._tokens, self._rotation, self._subject
        if tokens is None:
            raise ChatGPTError("signed_out", "Sign in with ChatGPT first.")
        kept = rotation is not None  # received by an earlier call, which couldn't verify it then
        if rotation is None:
            sent = self._clock()
            try:
                body = self._token_request({
                    "grant_type": "refresh_token",
                    "client_id": tokens.client_id,
                    "refresh_token": tokens.refresh_token,
                    "resource": RESOURCE,
                })
            except ChatGPTError as exc:
                if exc.code in REAUTH_CODES:
                    raise self._sign_in_again(ChatGPTError(exc.code, SIGN_IN_AGAIN, False, exc.status)) from None
                if exc.code in REJECTED_REGISTRATION_CODES:
                    raise self._sign_in_again(self._registration_rejected(tokens.client_id)) from None
                raise self._note(exc)
            try:
                fresh = _token_fields(body, sent, tokens.client_id, previous_scopes=tokens.scopes)
                id_token = body.get("id_token")
                if id_token is not None and not (isinstance(id_token, str) and id_token):
                    raise ChatGPTError("invalid_id_token", INVALID_IDENTITY)
            except ChatGPTError as exc:
                if exc.code == "plan_not_granted":
                    self._plan_refused(body, tokens.client_id)
                # The old refresh token is used up, so there is nothing left to retry with.
                raise self._sign_in_again(exc) from None
            if id_token is None:
                return self._install(fresh, None)
            rotation = _Rotation(fresh, id_token, time.time())
            with self._lock:
                self._rotation = rotation
        try:
            # As of when it arrived (as the DevKit does): a key-service outage may outlast the id_token.
            claims = self._verify(
                rotation.id_token, rotation.tokens.client_id, None, leeway=5 + max(0.0, time.time() - rotation.received_at)
            )
            if claims["sub"] != subject:
                raise ChatGPTError(
                    "account_mismatch", "ChatGPT renewed the sign-in for a different account. Sign in with ChatGPT again."
                )
        except ChatGPTError as exc:
            if exc.retryable:
                raise self._note(exc)  # the rotation is kept, and verified on the next call
            raise self._sign_in_again(exc) from None
        token = self._install(rotation.tokens, claims)
        if kept and self._current_token() is None:
            # The outage outlasted the rotated access token too. It is renewed with the rotation's refresh
            # token, unless ChatGPT doesn't allow that yet: then the caller waits for it.
            return None if rotation.tokens.refresh_after > self._clock() else self._refresh()
        return token

    def _install(self, tokens: _Tokens, claims: dict | None) -> str:
        with self._lock:
            self._tokens, self._rotation, self._error = tokens, None, None
            if claims is not None:
                if isinstance(claims.get("name"), str):
                    self._name = claims["name"]
                if isinstance(claims.get("email"), str):
                    self._email = claims["email"]
            return tokens.access_token

    def _registration_rejected(self, client_id: str | None) -> ChatGPTError:
        """Forget a registration ChatGPT rejects for good, so the next sign-in registers the app again."""
        with self._lock:
            if client_id is not None and self._client_id == client_id:
                self._client_id = None
                self._registered_subject = None
                try:
                    self._save()
                except ChatGPTError:
                    pass  # forgotten in memory; the file is rewritten at the next registration
            # Tokens held that were issued to the rejected registration go too.
            held = self._rotation.tokens if self._rotation is not None else self._tokens
            if held is not None and held.client_id == client_id:
                self._tokens = self._rotation = None
                self._status = "reauth_required"
        return ChatGPTError("registration_rejected", REGISTRATION_REJECTED)

    def _plan_refused(self, body: dict, client_id: str) -> None:
        """ChatGPT granted a sign-in without use of the plan: give back the grant that came with it, and
        have the next sign-in show ChatGPT's permission screen again, where the person can allow it."""
        with self._lock:
            self._reconsent = True
        refresh = body.get("refresh_token")
        if isinstance(refresh, str) and refresh:
            self._revoke(refresh, client_id)  # best effort: the sign-in fails either way

    def _note(self, error: ChatGPTError) -> ChatGPTError:
        with self._lock:
            self._error = str(error)
        return error

    def _sign_in_again(self, error: ChatGPTError) -> ChatGPTError:
        # The name and email stay, so the page can say which account to sign in with.
        with self._lock:
            self._tokens = self._rotation = None
            self._status, self._error = "reauth_required", str(error)
        return error

    # -- models -------------------------------------------------------------------

    def handle_api_error(self, access_token: str, code: str, status: int | None) -> None:
        """Invalidate only the credential rejected by the API, never a newer sign-in or refresh."""
        code = code.replace("subscription_sharing_v2_", "subscription_sharing_")
        if status != 401 and code not in API_REAUTH_CODES:
            return
        with self._refresh_lock:
            with self._lock:
                if self._tokens is None or self._tokens.access_token != access_token or self._rotation is not None:
                    return
                self._tokens = None
                self._models = []
                self._status, self._error = "reauth_required", SIGN_IN_AGAIN

    def refresh_models(self) -> list[dict]:
        token = self.access_token()
        try:
            models = self._fetch_models(token)
        except ChatGPTError as exc:
            with self._lock:
                if self._tokens is not None and self._tokens.access_token == token:
                    self._error = str(exc)
            raise
        with self._lock:
            if self._tokens is not None and self._tokens.access_token == token:
                self._models, self._error = models, None
        return self.state()["models"]

    def reasoning_levels(self, model: str) -> list[str] | None:
        with self._lock:
            for entry in self._models:
                if entry["slug"] == model:
                    return _copy_model(entry)["reasoning_levels"]
        return None

    def set_models(self, manager: str, worker: str) -> None:
        with self._lock:
            slugs = [m["slug"] for m in self._models]
            for role, slug, current in (("manager", manager, self._manager_model), ("worker", worker, self._worker_model)):
                if not isinstance(slug, str) or not slug.strip():
                    raise ValueError(f"Choose a {role} model.")
                # Only a change is checked: a default the plan doesn't list may stay while the other role changes.
                if slugs and slug != current and slug not in slugs:
                    offered = ", ".join(slugs)
                    raise ValueError(f"{slug!r} is not one of the models your ChatGPT plan offers. Choose the {role} model from: {offered}.")
            previous = self._manager_model, self._worker_model, self._models_chosen
            self._manager_model, self._worker_model, self._models_chosen = manager, worker, True
            try:
                self._save()
            except ChatGPTError:
                self._manager_model, self._worker_model, self._models_chosen = previous
                raise

    # -- sign-out -----------------------------------------------------------------

    def sign_out(self) -> None:
        with self._refresh_lock:
            with self._lock:
                held = self._rotation.tokens if self._rotation is not None else self._tokens
                self._tokens = self._rotation = None
                self._attempts = []
                self._generation += 1
                self._status = "disconnected"
                self._name = self._email = self._subject = None
                self._models, self._error = [], None
                self._account_changed = False
            # Local tokens are gone whatever happens next; revoking the grant at OpenAI is best effort.
            if held is not None and not self._revoke(held.refresh_token, held.client_id):
                with self._lock:
                    self._error = REVOCATION_UNCONFIRMED
