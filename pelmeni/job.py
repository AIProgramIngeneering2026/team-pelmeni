"""Job — бронь по диаграмме из docs/architecture.md, без HTTP и БД."""

from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4


class JobState(str, Enum):
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"


class JobError(ValueError):
    """Явная доменная ошибка; транспортный слой сам выбирает HTTP-ответ."""

    def __init__(self, code: str, message: str, field: str | None = None):
        super().__init__(message)
        self.code = code
        self.field = field


def _check_time(value: datetime, field: str) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise JobError("INVALID_PARAMETERS", "Нужно время с часовым поясом.", field)


class Job:
    """Изменяет статус только через доменные команды.

    now передаёт доверенный вызывающий код, а не пользовательский запрос.
    Проверка доступности комнаты и пересечений — ответственность будущих Tool.
    """

    __slots__ = ("__id", "__owner_id", "__start_at", "__end_at", "__state")

    def __init__(
        self, *, owner_id: UUID, start_at: datetime, end_at: datetime,
        now: datetime, job_id: UUID | None = None,
    ):
        for field, value in (("start_at", start_at), ("end_at", end_at), ("now", now)):
            _check_time(value, field)
        if not isinstance(owner_id, UUID):
            raise JobError("INVALID_PARAMETERS", "Нужен UUID автора.", "owner_id")
        if job_id is not None and not isinstance(job_id, UUID):
            raise JobError("INVALID_PARAMETERS", "Нужен UUID брони.", "job_id")
        if end_at <= start_at:
            raise JobError("INVALID_INTERVAL", "Конец должен быть позже начала.", "end_at")
        if start_at < now:
            raise JobError("START_IN_PAST", "Начало бронирования в прошлом.", "start_at")
        self.__id = job_id if job_id is not None else uuid4()
        self.__owner_id = owner_id
        self.__start_at = start_at
        self.__end_at = end_at
        self.__state = JobState.PENDING_CONFIRMATION

    @property
    def id(self) -> UUID:
        return self.__id

    @property
    def owner_id(self) -> UUID:
        return self.__owner_id

    @property
    def start_at(self) -> datetime:
        return self.__start_at

    @property
    def end_at(self) -> datetime:
        return self.__end_at

    @property
    def state(self) -> JobState:
        return self.__state

    def _require_state(self, *allowed: JobState) -> None:
        if self.__state not in allowed:
            raise JobError("INVALID_STATE", f"Операция невозможна из {self.__state.value}.")

    def _require_owner(self, actor_id: UUID) -> None:
        if actor_id != self.__owner_id:
            raise JobError("FORBIDDEN", "Изменять бронь может только её автор.")

    def confirm(self, *, actor_id: UUID, now: datetime) -> None:
        self._require_owner(actor_id)
        self._require_state(JobState.PENDING_CONFIRMATION)
        _check_time(now, "now")
        if now >= self.__start_at:
            raise JobError("CONFIRMATION_EXPIRED", "Время подтверждения истекло.")
        self.__state = JobState.CONFIRMED

    def cancel(self, *, actor_id: UUID, now: datetime) -> None:
        self._require_owner(actor_id)
        if self.__state == JobState.CANCELLED:
            raise JobError("ALREADY_CANCELLED", "Бронь уже отменена.")
        self._require_state(JobState.PENDING_CONFIRMATION, JobState.CONFIRMED)
        _check_time(now, "now")
        if self.__state == JobState.CONFIRMED and now >= self.__start_at:
            raise JobError("MEETING_STARTED", "Встреча уже началась, отмена невозможна.")
        self.__state = JobState.CANCELLED

    def expire(self, *, now: datetime) -> None:
        """Команда Scheduler: отменить неподтверждённую бронь при начале."""
        self._require_state(JobState.PENDING_CONFIRMATION)
        _check_time(now, "now")
        if now < self.__start_at:
            raise JobError("INVALID_STATE", "Время начала ещё не наступило.")
        self.__state = JobState.CANCELLED

    def complete(self, *, now: datetime) -> None:
        """Команда Scheduler: завершить подтверждённую бронь по окончании."""
        self._require_state(JobState.CONFIRMED)
        _check_time(now, "now")
        if now < self.__end_at:
            raise JobError("INVALID_STATE", "Встреча ещё не закончилась.")
        self.__state = JobState.COMPLETED
