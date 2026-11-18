"""Explicit, code-defined bot templates used by the queued message worker.

Templates are registered by trusted application code at import time. Admin API
requests can select an id, but cannot import or instantiate arbitrary classes.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable, TypeVar

from .integrations import HandlerRequest, HandlerResult


@dataclass(frozen=True)
class CommandInfo:
    name: str
    description: str


@dataclass(frozen=True)
class _Command:
    info: CommandInfo
    method_name: str


def command(name: str, description: str = "") -> Callable:
    """Mark an async BaseBot method as an explicit BotX message command."""
    normalized = name.strip().lower()
    if not re.fullmatch(r"/[a-z0-9][a-z0-9_-]*", normalized):
        raise ValueError("Bot template command must be a slash name such as '/ping'")

    def decorate(method: Callable) -> Callable:
        setattr(method, "__gateway_command__", CommandInfo(normalized, description.strip()))
        return method

    return decorate


class BaseBot:
    """Base class for one bot behavior template.

    Subclasses may override inherited command methods or register another
    method for an inherited command name. Unmatched input goes to on_message.
    """

    template_id = ""

    @classmethod
    def _commands(cls) -> dict[str, _Command]:
        resolved: dict[str, _Command] = {}
        for parent in reversed(cls.__mro__):
            for method_name, method in parent.__dict__.items():
                info = getattr(method, "__gateway_command__", None)
                if not isinstance(info, CommandInfo):
                    continue
                # A decorated override of a method can rename its command.
                for inherited_name, inherited in tuple(resolved.items()):
                    if inherited.method_name == method_name and inherited_name != info.name:
                        del resolved[inherited_name]
                resolved[info.name] = _Command(info=info, method_name=method_name)
        return resolved

    @classmethod
    def command_catalog(cls) -> list[CommandInfo]:
        return [command.info for command in sorted(cls._commands().values(), key=lambda item: item.info.name)]

    @classmethod
    def has_command(cls, name: str | None) -> bool:
        return bool(name and name.strip().lower() in cls._commands())

    async def on_message(self, request: HandlerRequest) -> HandlerResult | str | None:
        """Handle ordinary text or commands not declared by this template."""
        return HandlerResult(text=request.text)

    async def dispatch(self, request: HandlerRequest) -> HandlerResult:
        command_name = request.command
        if not command_name and request.text.startswith("/"):
            command_name = request.text.split(maxsplit=1)[0]
        command_name = command_name.lower() if command_name else None
        definition = self._commands().get(command_name) if command_name else None
        if definition is None:
            result = await self.on_message(request)
        else:
            result = await getattr(self, definition.method_name)(request)
        if isinstance(result, HandlerResult):
            return result
        if isinstance(result, str):
            return HandlerResult(text=result)
        if result is None:
            return HandlerResult()
        raise TypeError(
            f"{type(self).__name__} handlers must return HandlerResult, str, or None"
        )


TemplateT = TypeVar("TemplateT", bound=type[BaseBot])


@dataclass(frozen=True)
class TemplateRegistration:
    id: str
    name: str
    description: str
    bot_class: type[BaseBot]


TEMPLATE_REGISTRY: dict[str, TemplateRegistration] = {}


def register_template(
    template_id: str,
    *,
    name: str,
    description: str = "",
) -> Callable[[TemplateT], TemplateT]:
    """Register one trusted BaseBot class under an admin-selectable id."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", template_id):
        raise ValueError("Template id must be a lowercase slug")
    if not name.strip():
        raise ValueError("Template name is required")

    def decorate(bot_class: TemplateT) -> TemplateT:
        if not issubclass(bot_class, BaseBot):
            raise TypeError("Registered templates must subclass BaseBot")
        existing = TEMPLATE_REGISTRY.get(template_id)
        if existing and existing.bot_class is not bot_class:
            raise ValueError(f"Template id '{template_id}' is already registered")
        bot_class.template_id = template_id
        TEMPLATE_REGISTRY[template_id] = TemplateRegistration(
            id=template_id,
            name=name.strip(),
            description=description.strip(),
            bot_class=bot_class,
        )
        return bot_class

    return decorate


def get_template(template_id: str) -> TemplateRegistration | None:
    return TEMPLATE_REGISTRY.get(template_id)


def template_catalog() -> list[dict]:
    return [
        {
            "id": registration.id,
            "name": registration.name,
            "description": registration.description,
            "commands": [
                {"name": item.name, "description": item.description}
                for item in registration.bot_class.command_catalog()
            ],
        }
        for registration in sorted(TEMPLATE_REGISTRY.values(), key=lambda item: item.id)
    ]


@register_template(
    "echo",
    name="Echo Bot",
    description="Повторяет текст и предоставляет команды справки и проверки связи.",
)
class EchoBot(BaseBot):
    @command("/help", "Показать команды этого шаблона")
    async def help(self, request: HandlerRequest) -> HandlerResult:
        lines = [f"{item.name} — {item.description}" for item in self.command_catalog()]
        return HandlerResult(text="Доступные команды:\n" + "\n".join(lines))

    @command("/ping", "Проверить доступность бота")
    async def ping(self, request: HandlerRequest) -> HandlerResult:
        return HandlerResult(text="pong")
