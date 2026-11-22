"""Общий интерфейс Tool без зависимости от HTTP и БД."""

from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from uuid import UUID


@dataclass(frozen=True)
class ToolContext:
    actor_id: UUID
    is_admin: bool
    correlation_id: UUID


@dataclass(frozen=True)
class ToolResult:
    data: Any


class Tool(Protocol):
    """Параметры и бизнес-правила проверяет реализация Tool."""

    def execute(
        self, request: Mapping[str, Any], context: ToolContext
    ) -> ToolResult:
        ...
