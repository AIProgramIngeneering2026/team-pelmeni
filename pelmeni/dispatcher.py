"""Dispatcher: выбор Tool из Registry, транзакция, idempotency, ошибки.

Не импортирует конкретные Tool и не знает имён операций.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol
from uuid import UUID

from .registry import Registry, RegistryError
from .tool import Tool, ToolContext, ToolResult


# ---------- Ошибки ----------

@dataclass(frozen=True)
class ErrorDetail:
    field: str | None
    code: str
    message: str


@dataclass
class ToolError(Exception):
    """Единый формат ошибки операции (см. таблицу ошибок контракта)."""

    code: str
    message: str
    details: list[ErrorDetail] = field(default_factory=list)
    conflicting_booking_id: UUID | None = None
    correlation_id: UUID | None = None  # заполняет Dispatcher


# ---------- Зависимости (Protocol) ----------

class UnitOfWork(Protocol):
    def __enter__(self) -> "UnitOfWork": ...
    def __exit__(self, exc_type, exc, tb) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


class IdempotencyRecord(Protocol):
    @property
    def state(self) -> str: ...  # "NEW" | "IN_PROGRESS" | "DONE" | "REUSED"
    @property
    def result_data(self) -> Any: ...


class IdempotencyStore(Protocol):
    def begin(
        self, *, scope: str, key: str, request_fingerprint: str
    ) -> IdempotencyRecord: ...
    def complete(self, record: IdempotencyRecord, result_data: Any) -> None: ...
    def fail(self, record: IdempotencyRecord) -> None: ...


class Scheduler(Protocol):
    def tick(self) -> None: ...


# ---------- Результат для входного слоя ----------

@dataclass(frozen=True)
class DispatchResult:
    data: Any
    replayed: bool = False  # True, если вернули сохранённый результат idempotency


# ---------- Dispatcher ----------

class Dispatcher:
    """Выбирает Tool по имени операции и управляет транзакцией.

    Конкретные Tool и их имена Dispatcher не знает: получает Registry
    и зависимости через конструктор.
    """

    def __init__(
        self,
        registry: Registry,
        uow_factory,
        idempotency: IdempotencyStore | None = None,
        scheduler: Scheduler | None = None,
    ) -> None:
        self._registry = registry
        self._uow_factory = uow_factory
        self._idempotency = idempotency
        self._scheduler = scheduler

    # -- публичный вход --

    def dispatch(
        self,
        operation: str,
        request: Mapping[str, Any],
        context: ToolContext,
        *,
        idempotency_key: str | None = None,
    ) -> DispatchResult:
        # 1. Автопереходы Scheduler до пользовательской операции.
        if self._scheduler is not None:
            self._scheduler.tick()

        # 2. Выбор Tool. UNKNOWN_OPERATION / INVALID_PARAMETERS из Registry
        #    конвертируются в ToolError и уходят вызывающему коду.
        try:
            tool = self._registry.get(operation)
        except RegistryError as exc:
            raise self._to_tool_error(exc, context) from None

        # 3. Idempotency (только если ключ передан).
        record = None
        if idempotency_key is not None and self._idempotency is not None:
            record = self._idempotency.begin(
                scope=f"{context.actor_id}:{operation}",
                key=idempotency_key,
                request_fingerprint=self._fingerprint(request),
            )
            if record.state == "DONE":
                return DispatchResult(data=record.result_data, replayed=True)
            if record.state == "IN_PROGRESS":
                raise ToolError(
                    code="REQUEST_IN_PROGRESS",
                    message="Операция с этим Idempotency-Key уже выполняется.",
                    details=[ErrorDetail("header.Idempotency-Key",
                                         "REQUEST_IN_PROGRESS",
                                         "Повторите позже.")],
                    correlation_id=context.correlation_id,
                )
            if record.state == "REUSED":
                raise ToolError(
                    code="IDEMPOTENCY_KEY_REUSED",
                    message="Idempotency-Key уже использован с другими параметрами.",
                    details=[ErrorDetail("header.Idempotency-Key",
                                         "IDEMPOTENCY_KEY_REUSED",
                                         "Используйте новый ключ.")],
                    correlation_id=context.correlation_id,
                )

        # 4. Транзакция: Tool + commit атомарно с idempotency.
        try:
            with self._uow_factory() as uow:
                result: ToolResult = tool.execute(request, context)
                if record is not None:
                    self._idempotency.complete(record, result.data)
                uow.commit()
        except ToolError as exc:
            # Доменная ошибка: изменение не сохраняется.
            if record is not None:
                self._idempotency.fail(record)
            exc.correlation_id = context.correlation_id
            raise
        except RegistryError as exc:  # на случай, если Tool выбросит
            if record is not None:
                self._idempotency.fail(record)
            raise self._to_tool_error(exc, context) from None
        except Exception as exc:  # падение Tool
            if record is not None:
                self._idempotency.fail(record)
            raise ToolError(
                code="INTERNAL_ERROR",
                message="Внутренняя ошибка при выполнении операции.",
                details=[ErrorDetail(None, "INTERNAL_ERROR", str(exc))],
                correlation_id=context.correlation_id,
            ) from exc

        return DispatchResult(data=result.data)

    # -- вспомогательное --

    @staticmethod
    def _fingerprint(request: Mapping[str, Any]) -> str:
        return json.dumps(request, sort_keys=True, default=str, ensure_ascii=False)

    @staticmethod
    def _to_tool_error(exc: RegistryError, context: ToolContext) -> ToolError:
        return ToolError(
            code=exc.code,
            message=exc.message,
            details=[ErrorDetail(d["field"], d["code"], d["message"])
                     for d in exc.details],
            correlation_id=context.correlation_id,
        )