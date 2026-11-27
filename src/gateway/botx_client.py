"""Async client for the eXpress (BotX) CTS API.

A small self-contained library that talks to CTS directly, replicating the
behaviour of the proven standalone reference client:

* HS256 token minted per request: ``iss`` = bot id, ``aud`` = CTS host,
  ``iat``/``nbf`` = now, ``exp`` = now + 60, ``jti`` and ``version`` = 2;
* ``Content-Type``/``Accept: application/json`` headers;
* ``verify=False`` and a 30 second timeout;
* CTS base URL always keeps a trailing slash.

Every request, response and failure is logged through the ``gateway.botx_client``
logger so it shows up in the docker console.
"""
import json
import logging
import time
import uuid
from typing import Self
from urllib.parse import urlsplit

import httpx
import jwt

logger = logging.getLogger(__name__)

TOKEN_LIFETIME_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 30.0
_MAX_LOGGED_BODY = 500


class BotXClientError(Exception):
    """Base class for every error raised by BotXClient."""


class BotXConnectionError(BotXClientError):
    """CTS could not be reached (DNS, TCP or TLS problem)."""


class BotXTimeoutError(BotXClientError, TimeoutError):
    """CTS did not answer in time. Subclasses TimeoutError for retry classification."""


class BotXHTTPError(BotXClientError):
    """CTS answered with a non-2xx status code."""

    def __init__(self, method: str, url: str, status_code: int, body: str) -> None:
        self.method = method
        self.url = url
        self.status_code = status_code
        self.body = body
        super().__init__(f"BotX {method} {url} returned HTTP {status_code}: {_shorten(body)}")


class BotXResponseError(BotXClientError):
    """CTS answered with 2xx but the payload is not what the API promises."""


def _shorten(value: str) -> str:
    """Collapse a payload into one short log line."""
    flattened = " ".join(str(value).split())
    return flattened if len(flattened) <= _MAX_LOGGED_BODY else flattened[:_MAX_LOGGED_BODY] + "…"


class BotXClient:
    """Direct async client for a single bot's endpoints on one CTS server."""

    def __init__(
        self,
        *,
        bot_id: uuid.UUID,
        cts_url: str,
        secret_key: str,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        verify: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.bot_id = bot_id
        self.secret_key = secret_key
        self._base_url = cts_url.rstrip("/") + "/"
        self._host = urlsplit(cts_url).hostname or cts_url
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            verify=verify,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _auth_headers(self) -> dict[str, str]:
        """Mint a fresh JWT v2 auth header exactly like the reference client."""
        now = int(time.time())
        token = jwt.encode(
            {
                "iss": str(self.bot_id),
                "aud": self._host,
                "iat": now,
                "nbf": now,
                "exp": now + TOKEN_LIFETIME_SECONDS,
                "jti": uuid.uuid4().hex,
                "version": 2,
            },
            self.secret_key,
            algorithm="HS256",
        )
        return {"Authorization": f"Bearer {token}"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        not_found_ok: bool = False,
    ) -> object | None:
        url = f"{self._base_url}{path}"
        headers = self._auth_headers()
        logger.info("BotX request: %s %s", method, url)
        if json_body is not None:
            logger.debug("BotX request body: %s", _shorten(json.dumps(json_body, ensure_ascii=False)))
        started = time.monotonic()
        try:
            response = await self._client.request(
                method, path, params=params, json=json_body, headers=headers
            )
        except httpx.TimeoutException as exc:
            elapsed = time.monotonic() - started
            logger.warning("BotX %s %s timed out after %.2fs", method, url, elapsed)
            raise BotXTimeoutError(f"BotX {method} {url} timed out after {elapsed:.2f}s") from exc
        except httpx.RequestError as exc:
            logger.warning("BotX %s %s connection failed: %s", method, url, exc)
            raise BotXConnectionError(f"BotX {method} {url} failed: {exc}") from exc
        elapsed = time.monotonic() - started
        if response.status_code >= 400:
            logger.warning(
                "BotX %s %s -> HTTP %d in %.2fs: %s",
                method, url, response.status_code, elapsed, _shorten(response.text),
            )
            if response.status_code == 404 and not_found_ok:
                return None
            raise BotXHTTPError(method, url, response.status_code, response.text)
        logger.info("BotX %s %s -> HTTP %d in %.2fs", method, url, response.status_code, elapsed)
        try:
            payload = response.json()
        except ValueError as exc:
            logger.warning("BotX %s %s returned non-JSON payload", method, url)
            raise BotXResponseError(f"BotX {method} {url} returned a non-JSON payload") from exc
        logger.debug("BotX response body: %s", _shorten(json.dumps(payload, ensure_ascii=False)))
        return payload

    def _result(self, payload: object, path: str) -> object:
        if not isinstance(payload, dict) or "result" not in payload:
            raise BotXResponseError(f"BotX {path} response is missing the 'result' field")
        return payload["result"]

    @staticmethod
    def _user_record(result: object, path: str) -> dict | None:
        """Accept both a single user object and a list with one user."""
        if result is None:
            return None
        if isinstance(result, list):
            if not result:
                return None
            result = result[0]
        if not isinstance(result, dict):
            raise BotXResponseError(f"BotX {path} result must be a user object")
        return result

    async def list_chats(self) -> list[dict]:
        """GET /api/v3/botx/chats/list — list chats accessible to this bot."""
        result = self._result(await self._request("GET", "api/v3/botx/chats/list"), "chats/list")
        if not isinstance(result, list):
            raise BotXResponseError("BotX chats/list result must be a list")
        return result

    async def find_personal_chat(self, user_huid: uuid.UUID) -> uuid.UUID | None:
        path = "api/v1/botx/chats/personal"
        payload = await self._request(
            "GET", path, params={"user_huid": str(user_huid)}, not_found_ok=True,
        )
        if payload is None:
            return None
        result = self._result(payload, path)
        try:
            if isinstance(result, dict):
                return uuid.UUID(str(result.get("group_chat_id")))
        except ValueError:
            pass
        raise BotXResponseError("BotX personal chat result is missing a usable group_chat_id")

    async def find_user_by_email(self, email: str) -> dict | None:
        """GET /api/v3/botx/users/by_email — the user record, or None when CTS answers 404."""
        payload = await self._request(
            "GET", "api/v3/botx/users/by_email", params={"email": email}, not_found_ok=True
        )
        if payload is None:
            return None
        return self._user_record(self._result(payload, "users/by_email"), "users/by_email")

    async def find_user_by_ad(self, ad_login: str, ad_domain: str) -> dict | None:
        """GET /api/v3/botx/users/by_login — the user record, or None when CTS answers 404."""
        payload = await self._request(
            "GET",
            "api/v3/botx/users/by_login",
            params={"ad_login": ad_login, "ad_domain": ad_domain},
            not_found_ok=True,
        )
        if payload is None:
            return None
        return self._user_record(self._result(payload, "users/by_login"), "users/by_login")

    async def create_chat(self, name: str, chat_type: str, members: list[uuid.UUID]) -> uuid.UUID:
        """POST /api/v3/botx/chats/create — returns the id of the new chat."""
        result = self._result(
            await self._request(
                "POST",
                "api/v3/botx/chats/create",
                json_body={
                    "name": name,
                    "chat_type": chat_type,
                    "members": [str(member) for member in members],
                },
            ),
            "chats/create",
        )
        chat_id = result.get("chat_id") if isinstance(result, dict) else None
        try:
            return uuid.UUID(str(chat_id))
        except (TypeError, ValueError) as exc:
            raise BotXResponseError("BotX chats/create result is missing a usable chat_id") from exc

    async def send_notification(self, chat_id: uuid.UUID, text: str) -> dict:
        """POST /api/v4/botx/notifications/direct — returns the delivery result."""
        result = self._result(
            await self._request(
                "POST",
                "api/v4/botx/notifications/direct",
                json_body={
                    "group_chat_id": str(chat_id),
                    "notification": {"body": text},
                },
            ),
            "notifications/direct",
        )
        if not isinstance(result, dict):
            raise BotXResponseError("BotX notifications/direct result must be an object")
        return result
