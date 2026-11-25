from dataclasses import dataclass, field
from typing import Literal, Protocol
import httpx


ExternalAuthType = Literal["none", "bearer", "api_key", "token", "basic"]


@dataclass(frozen=True)
class ExternalSystem:
    id: int
    name: str
    url: str
    auth_type: ExternalAuthType
    _token: str | None = field(repr=False)

    async def request(
        self,
        method: str,
        *,
        params: httpx.QueryParams | dict | None = None,
        json: object = None,
    ) -> httpx.Response:
        """Send a request to the configured HTTPS endpoint."""
        headers = {}
        if self.auth_type != "none":
            if self._token is None:
                raise RuntimeError("External system token is missing")
            if self.auth_type == "api_key":
                headers["X-API-Key"] = self._token
            else:
                headers["Authorization"] = f"{self.auth_type.title()} {self._token}"
        url = httpx.URL(self.url)
        if params is not None:
            url = url.copy_merge_params(params)
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.request(method, url, headers=headers, json=json)
            return response.raise_for_status()


@dataclass
class HandlerRequest:
    bot_id: str
    chat_id: str
    chat_type: str
    user_id: str
    message_id: str
    text: str
    command: str | None = None
    metadata: dict = field(default_factory=dict)
    context: list[dict] = field(default_factory=list)
    external_system: ExternalSystem | None = field(default=None, repr=False)


@dataclass
class HandlerResult:
    text: str | None = None
    status: str = "completed"
    error: str | None = None


class MessageHandler(Protocol):
    async def handle(self, request: HandlerRequest) -> HandlerResult: ...


class EchoHandler:
    async def handle(self, request: HandlerRequest) -> HandlerResult:
        return HandlerResult(text=request.text)


class HttpHandler:
    def __init__(self, url: str, token: str = "", timeout: float = 20):
        self.url, self.token, self.timeout = url, token, timeout

    async def handle(self, request: HandlerRequest) -> HandlerResult:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.url, json={
                "bot_id": request.bot_id, "chat_id": request.chat_id, "chat_type": request.chat_type,
                "user_id": request.user_id, "message_id": request.message_id, "text": request.text,
                "command": request.command, "metadata": request.metadata, "context": request.context,
            }, headers=headers)
            response.raise_for_status()
            data = response.json()
        return HandlerResult(text=data.get("text"), status=data.get("status", "completed"), error=data.get("error"))
