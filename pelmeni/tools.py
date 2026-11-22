"""Tool'ы за контрактом Tool"""

from datetime import datetime
from typing import Any, Mapping, Protocol
from uuid import UUID

from .dispatcher import ErrorDetail, ToolError
from .job import Job, JobError, JobState
from .tool import ToolContext, ToolResult


# репозитории

class JobRepository(Protocol):
    def add(self, job: Job) -> None: ...
    def get(self, job_id: UUID) -> Job | None: ...
    def list_by_owner(self, owner_id: UUID) -> list[Job]: ...
    def find_conflict(
        self, start_at: datetime, end_at: datetime, *, exclude: UUID | None = None
    ) -> Job | None: ...
    def reschedule(self, job: Job, *, start_at: datetime, end_at: datetime) -> None: ...


class Room(Protocol):
    id: UUID
    capacity: int
    equipment: tuple[str, ...]
    is_available: bool


class RoomRepository(Protocol):
    def get(self, room_id: UUID) -> Room | None: ...
    def list_available(self) -> list[Room]: ...


class Notification(Protocol):
    id: UUID
    booking_id: UUID
    status: str  # PENDING / SENT / FAILED / UNKNOWN


class NotificationRepository(Protocol):
    def get(self, notification_id: UUID) -> Notification | None: ...
    def record_attempt(self, notification_id: UUID, attempt: Any) -> None: ...


class SmsNotifier(Protocol):
    def send(self, notification: Notification) -> Any: ...


# валидация параметров

def _require_uuid(request, field: str) -> UUID:
    value = request.get(field)
    if not isinstance(value, UUID):
        raise ToolError("INVALID_PARAMETERS", f"Нужен {field}.",
                        [ErrorDetail(field, "INVALID_PARAMETERS", "Нужен UUID.")])
    return value


def _require_time(request, field: str) -> datetime:
    value = request.get(field)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ToolError("INVALID_PARAMETERS", f"Нужен {field} с часовым поясом.",
                        [ErrorDetail(field, "INVALID_PARAMETERS", "Нужно время с TZ.")])
    return value


def _optional_time(request, field: str) -> datetime | None:
    if request.get(field) is None:
        return None
    return _require_time(request, field)


def _require_topic(request) -> str:
    value = request.get("topic")
    if not isinstance(value, str) or not value.strip():
        raise ToolError("INVALID_PARAMETERS", "Тема — непустая строка.",
                        [ErrorDetail("topic", "INVALID_PARAMETERS", "Тема не заполнена.")])
    return value


def _require_interval(start_at: datetime, end_at: datetime) -> None:
    if end_at <= start_at:
        raise ToolError("INVALID_INTERVAL", "Конец должен быть позже начала.",
                        [ErrorDetail("end_at", "INVALID_INTERVAL", "Неверный интервал.")])


def _require_not_past(start_at: datetime, now: datetime) -> None:
    if start_at < now:
        raise ToolError("START_IN_PAST", "Начало бронирования в прошлом.",
                        [ErrorDetail("start_at", "START_IN_PAST", "Начало в прошлом.")])


def _from_job_error(exc: JobError, default_field: str | None) -> ToolError:
    return ToolError(exc.code, str(exc),
                     [ErrorDetail(exc.field or default_field, exc.code, str(exc))])


def _require_owner_or_admin(job: Job, context: ToolContext) -> None:
    if context.actor_id != job.owner_id and not context.is_admin:
        raise ToolError("FORBIDDEN", "Нет прав на операцию с бронью.",
                        [ErrorDetail(None, "FORBIDDEN", "Нужен автор или админ.")])


def _snapshot(job: Job, room_id: UUID | None = None) -> dict:
    data = {"id": str(job.id), "state": job.state.value,
            "start_at": job.start_at.isoformat(), "end_at": job.end_at.isoformat()}
    if room_id is not None:
        data["room_id"] = str(room_id)
    return data


# Tool'ы

class CreateBookingTool:
    """room_id, start_at, end_at, topic → Booking в PENDING_CONFIRMATION."""

    def __init__(self, jobs: JobRepository, rooms: RoomRepository, clock) -> None:
        self._jobs = jobs
        self._rooms = rooms
        self._clock = clock

    def execute(self, request: Mapping[str, Any], context: ToolContext) -> ToolResult:
        room_id = _require_uuid(request, "room_id")
        start_at = _require_time(request, "start_at")
        end_at = _require_time(request, "end_at")
        topic = _require_topic(request)

        now = self._clock()
        _require_interval(start_at, end_at)
        _require_not_past(start_at, now)

        room = self._rooms.get(room_id)
        if room is None:
            raise ToolError("NOT_FOUND", "Комната не найдена.",
                            [ErrorDetail("room_id", "NOT_FOUND", "Комната не найдена.")])
        if not room.is_available:
            raise ToolError("ROOM_UNAVAILABLE", "Комната недоступна.",
                            [ErrorDetail("room_id", "ROOM_UNAVAILABLE", "Комната недоступна.")])
        conflict = self._jobs.find_conflict(start_at, end_at)
        if conflict is not None:
            raise ToolError("BOOKING_CONFLICT", "Интервал пересекается с бронью.",
                            [ErrorDetail("room_id", "BOOKING_CONFLICT", "Интервал занят.")],
                            conflicting_booking_id=conflict.id,
                            correlation_id=context.correlation_id)

        job = Job(owner_id=context.actor_id, start_at=start_at, end_at=end_at, now=now)
        self._jobs.add(job)
        return ToolResult(data=_snapshot(job, room_id))


class ConfirmBookingTool:
    """id → Booking в CONFIRMED."""

    def __init__(self, jobs: JobRepository, clock) -> None:
        self._jobs = jobs
        self._clock = clock

    def execute(self, request: Mapping[str, Any], context: ToolContext) -> ToolResult:
        job_id = _require_uuid(request, "id")
        job = self._jobs.get(job_id)
        if job is None:
            raise ToolError("NOT_FOUND", "Бронь не найдена.",
                            [ErrorDetail("id", "NOT_FOUND", "Бронь не найдена.")])
        try:
            job.confirm(actor_id=context.actor_id, now=self._clock())
        except JobError as exc:
            raise _from_job_error(exc, "id") from None
        return ToolResult(data=_snapshot(job))


class CancelBookingTool:
    """id → Booking в CANCELLED."""

    def __init__(self, jobs: JobRepository, clock) -> None:
        self._jobs = jobs
        self._clock = clock

    def execute(self, request: Mapping[str, Any], context: ToolContext) -> ToolResult:
        job_id = _require_uuid(request, "id")
        job = self._jobs.get(job_id)
        if job is None:
            raise ToolError("NOT_FOUND", "Бронь не найдена.",
                            [ErrorDetail("id", "NOT_FOUND", "Бронь не найдена.")])
        _require_owner_or_admin(job, context)
        try:
            # Админ проходит проверку в Tool; Job.cancel проверяет автора.
            actor = job.owner_id if context.is_admin else context.actor_id
            job.cancel(actor_id=actor, now=self._clock())
        except JobError as exc:
            raise _from_job_error(exc, "id") from None
        return ToolResult(data=_snapshot(job))


class RescheduleBookingTool:
    """id и хотя бы одно из room_id/start_at/end_at → Booking с прежним статусом."""

    def __init__(self, jobs: JobRepository, rooms: RoomRepository, clock) -> None:
        self._jobs = jobs
        self._rooms = rooms
        self._clock = clock

    def execute(self, request: Mapping[str, Any], context: ToolContext) -> ToolResult:
        job_id = _require_uuid(request, "id")
        new_room_id = request.get("room_id")
        new_start = _optional_time(request, "start_at")
        new_end = _optional_time(request, "end_at")
        if new_room_id is None and new_start is None and new_end is None:
            raise ToolError("INVALID_PARAMETERS", "Нужно хотя бы одно новое поле.",
                            [ErrorDetail(None, "INVALID_PARAMETERS",
                                         "Укажите room_id, start_at или end_at.")])
        if new_room_id is not None and not isinstance(new_room_id, UUID):
            raise ToolError("INVALID_PARAMETERS", "Нужен room_id.",
                            [ErrorDetail("room_id", "INVALID_PARAMETERS", "Нужен UUID.")])

        job = self._jobs.get(job_id)
        if job is None:
            raise ToolError("NOT_FOUND", "Бронь не найдена.",
                            [ErrorDetail("id", "NOT_FOUND", "Бронь не найдена.")])
        _require_owner_or_admin(job, context)
        if job.state not in (JobState.PENDING_CONFIRMATION, JobState.CONFIRMED):
            raise ToolError("INVALID_STATE", f"Перенос невозможен из {job.state.value}.",
                            [ErrorDetail("id", "INVALID_STATE", "Недопустимый статус.")])

        start_at = new_start if new_start is not None else job.start_at
        end_at = new_end if new_end is not None else job.end_at
        now = self._clock()
        _require_interval(start_at, end_at)
        _require_not_past(start_at, now)

        if new_room_id is not None:
            room = self._rooms.get(new_room_id)
            if room is None:
                raise ToolError("NOT_FOUND", "Комната не найдена.",
                                [ErrorDetail("room_id", "NOT_FOUND", "Комната не найдена.")])
            if not room.is_available:
                raise ToolError("ROOM_UNAVAILABLE", "Комната недоступна.",
                                [ErrorDetail("room_id", "ROOM_UNAVAILABLE", "Комната недоступна.")])
        conflict = self._jobs.find_conflict(start_at, end_at, exclude=job.id)
        if conflict is not None:
            raise ToolError("BOOKING_CONFLICT", "Интервал пересекается с бронью.",
                            [ErrorDetail("room_id", "BOOKING_CONFLICT", "Интервал занят.")],
                            conflicting_booking_id=conflict.id,
                            correlation_id=context.correlation_id)

        try:
            job.reschedule(new_start_at=start_at, new_end_at=end_at, now=now)
        except JobError as exc:
            raise _from_job_error(exc, "id") from None
        self._jobs.reschedule(job, start_at=start_at, end_at=end_at)
        return ToolResult(data=_snapshot(job, new_room_id))


class ListOwnBookingsTool:
    """status, from, to, limit, offset (необязательные) → BookingList."""

    _STATES = {"PENDING_CONFIRMATION", "CONFIRMED", "CANCELLED", "COMPLETED"}

    def __init__(self, jobs: JobRepository) -> None:
        self._jobs = jobs

    def execute(self, request: Mapping[str, Any], context: ToolContext) -> ToolResult:
        status = request.get("status")
        if status is not None and status not in self._STATES:
            raise ToolError("INVALID_PARAMETERS", "Неверный status.",
                            [ErrorDetail("status", "INVALID_PARAMETERS", "Неверный статус.")])
        from_at = _optional_time(request, "from")
        to_at = _optional_time(request, "to")
        limit = self._page_param(request, "limit", default=50, minimum=1, maximum=200)
        offset = self._page_param(request, "offset", default=0, minimum=0)

        items = self._jobs.list_by_owner(context.actor_id)
        if status is not None:
            items = [j for j in items if j.state.value == status]
        if from_at is not None:
            items = [j for j in items if j.start_at >= from_at]
        if to_at is not None:
            items = [j for j in items if j.start_at <= to_at]
        items.sort(key=lambda j: j.start_at)
        page = items[offset: offset + limit]
        return ToolResult(data={"items": [_snapshot(j) for j in page],
                                "limit": limit, "offset": offset})

    @staticmethod
    def _page_param(request, field, *, default, minimum, maximum=None) -> int:
        value = request.get(field, default)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ToolError("INVALID_PARAMETERS", f"Неверный {field}.",
                            [ErrorDetail(field, "INVALID_PARAMETERS", "Нужно целое.")])
        if value < minimum or (maximum is not None and value > maximum):
            raise ToolError("INVALID_PARAMETERS", f"Неверный {field}.",
                            [ErrorDetail(field, "INVALID_PARAMETERS", "Вне диапазона.")])
        return value


class FindAvailableRoomsTool:
    """start_at, end_at → RoomList."""

    def __init__(self, jobs: JobRepository, rooms: RoomRepository, clock) -> None:
        self._jobs = jobs
        self._rooms = rooms
        self._clock = clock

    def execute(self, request: Mapping[str, Any], context: ToolContext) -> ToolResult:
        start_at = _require_time(request, "start_at")
        end_at = _require_time(request, "end_at")
        _require_interval(start_at, end_at)
        _require_not_past(start_at, self._clock())

        available = []
        for room in self._rooms.list_available():
            if self._jobs.find_conflict(start_at, end_at) is None:
                available.append({"id": str(room.id), "capacity": room.capacity,
                                  "equipment": list(room.equipment)})
        return ToolResult(data={"items": available})


class RetryNotificationTool:
    """id уведомления → NotificationAttempt. Только администратор."""

    def __init__(self, notifications: NotificationRepository, notifier: SmsNotifier) -> None:
        self._notifications = notifications
        self._notifier = notifier

    def execute(self, request: Mapping[str, Any], context: ToolContext) -> ToolResult:
        if not context.is_admin:
            raise ToolError("FORBIDDEN", "Повтор доставки — только администратор.",
                            [ErrorDetail(None, "FORBIDDEN", "Нужны права админа.")])
        notification_id = _require_uuid(request, "id")
        notification = self._notifications.get(notification_id)
        if notification is None:
            raise ToolError("NOT_FOUND", "Уведомление не найдено.",
                            [ErrorDetail("id", "NOT_FOUND", "Уведомление не найдено.")])
        if notification.status not in ("FAILED", "UNKNOWN"):
            raise ToolError("INVALID_STATE", "Повтор не требуется.",
                            [ErrorDetail("id", "INVALID_STATE",
                                         "Повтор только для FAILED/UNKNOWN.")])
        attempt = self._notifier.send(notification)
        self._notifications.record_attempt(notification_id, attempt)
        return ToolResult(data={"notification_id": str(notification_id),
                                "status": getattr(attempt, "status", "UNKNOWN")})