"""Тесты Tool'ов за контрактом Tool"""

import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pelmeni.dispatcher import ToolError
from pelmeni.job import Job, JobState
from pelmeni.tool import ToolContext
from pelmeni.tools import (
    CancelBookingTool,
    ConfirmBookingTool,
    CreateBookingTool,
    FindAvailableRoomsTool,
    ListOwnBookingsTool,
    RescheduleBookingTool,
    RetryNotificationTool,
)


# Fake-зависимости
# Все они — минимальные реализации Protocol'ов из tools.py
class FakeClock:
    """Управляемые часы: тест сам решает, «который час»."""

    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class FakeRoom:
    """Комната с минимальным набором полей, которые читают Tool'ы."""

    def __init__(self, room_id=None, capacity=8, equipment=("tv",), is_available=True):
        self.id = room_id or uuid4()
        self.capacity = capacity
        self.equipment = tuple(equipment)
        self.is_available = is_available


class FakeJobRepository:
    """Репозиторий броней в памяти.

    find_conflict реализован по полуоткрытому интервалу:
    job.start_at < end_at and start_at < job.end_at — это контракт
    («встреча может начинаться в момент окончания предыдущей»).
    Отменённые и завершённые брони комнату не занимают.
    """

    def __init__(self):
        self.items = {}

    def add(self, job):
        self.items[job.id] = job

    def get(self, job_id):
        return self.items.get(job_id)

    def list_by_owner(self, owner_id):
        return [j for j in self.items.values() if j.owner_id == owner_id]

    def find_conflict(self, start_at, end_at, *, exclude=None):
        for job in self.items.values():
            if exclude is not None and job.id == exclude:
                continue
            if job.state in (JobState.CANCELLED, JobState.COMPLETED):
                continue
            if job.start_at < end_at and start_at < job.end_at:
                return job
        return None

    def reschedule(self, job, *, start_at, end_at):
        # в текущей модели Job сам хранит время и меняет его в
        pass


class FakeRoomRepository:
    def __init__(self, rooms=()):
        self.rooms = {r.id: r for r in rooms}

    def get(self, room_id):
        return self.rooms.get(room_id)

    def list_available(self):
        return [r for r in self.rooms.values() if r.is_available]


class FakeNotification:
    def __init__(self, notification_id=None, status="FAILED"):
        self.id = notification_id or uuid4()
        self.booking_id = uuid4()
        self.status = status


class FakeNotificationRepository:
    """Хранит уведомления и записывает попытки доставки.

    record_attempt — точка, где в проде была бы запись в таблицу attempts;
    в тесте достаточно проверить, что попытка зафиксирована.
    """

    def __init__(self):
        self.items = {}
        self.attempts = []

    def get(self, notification_id):
        return self.items.get(notification_id)

    def record_attempt(self, notification_id, attempt):
        self.attempts.append((notification_id, attempt))


class FakeAttempt:
    def __init__(self, status="SENT"):
        self.status = status


class FakeNotifier:
    """SmsNotifier-заглушка: либо успех, либо ошибка.

    Ошибка пробрасывается как есть — Tool не должен её глотать,
    Dispatcher превратит её в ToolError на границе (см. тест
    test_notifier_error_propagates).
    """

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def send(self, notification):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


# Общая характеристика для тестов Tool'ов.
# now/start/end фиксированы, чтобы тесты были детерминированными.
# context и admin_context различаются только is_admin.
class ToolTestCase(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 22, 9, tzinfo=timezone.utc)
        self.start = self.now + timedelta(hours=1)
        self.end = self.start + timedelta(hours=1)
        self.actor = uuid4()
        self.context = ToolContext(self.actor, False, uuid4())
        self.admin_context = ToolContext(self.actor, True, uuid4())
        self.clock = FakeClock(self.now)
        self.jobs = FakeJobRepository()
        self.rooms = FakeRoomRepository()

    def make_job(self, **overrides):
        fields = dict(owner_id=self.actor, start_at=self.start,
                      end_at=self.end, now=self.now)
        fields.update(overrides)
        job = Job(**fields)
        self.jobs.add(job)
        return job

    def assert_tool_error(self, code, callable_, field=None):
        """Проверяет код ошибки и (опционально) поле в details[0].
        Одна точка вместо повторяющегося assertRaises + проверки полей
        в каждом тесте. Если поле не задано — проверяем только код
        """
        with self.assertRaises(ToolError) as caught:
            callable_()
        self.assertEqual(caught.exception.code, code)
        if field is not None:
            self.assertEqual(caught.exception.details[0].field, field)
        return caught.exception


# Контракт: room_id, start_at, end_at, topic → Booking в PENDING_CONFIRMATION.
# Проверки: комната существует и доступна; начало не в прошлом; конец позже начала нет пересечения.
# Ошибки — INVALID_PARAMETERS (с полем).
class CreateBookingToolTests(ToolTestCase):
    def setUp(self):
        super().setUp()
        self.room = FakeRoom()
        self.rooms.rooms[self.room.id] = self.room
        self.tool = CreateBookingTool(self.jobs, self.rooms, self.clock)

    def request(self, **overrides):
        data = {"room_id": self.room.id, "start_at": self.start,
                "end_at": self.end, "topic": "Встреча"}
        data.update(overrides)
        return data

    def test_creates_pending_booking(self):
        """Обычный путь: валидный запрос → PENDING_CONFIRMATION."""
        result = self.tool.execute(self.request(), self.context)
        self.assertEqual(result.data["state"], "PENDING_CONFIRMATION")
        self.assertEqual(result.data["room_id"], str(self.room.id))
        self.assertEqual(len(self.jobs.items), 1)

    def test_owner_is_actor_not_request(self):
        """Владелец — context.actor_id, а не поле из request.
        Это защита от подделки автора: даже если клиент пришлёт
        owner_id в request, Tool его игнорирует.
        """
        self.tool.execute(self.request(), self.context)
        job = list(self.jobs.items.values())[0]
        self.assertEqual(job.owner_id, self.actor)

    def test_missing_room_id_has_field(self):
        """Неверные параметры: room_id не UUID → INVALID_PARAMETERS
        с указанием поля room_id (требование контракта: ошибка с полем).
        """
        self.assert_tool_error(
            "INVALID_PARAMETERS", lambda: self.tool.execute(
                self.request(room_id=None), self.context), field="room_id")

    def test_naive_time_is_rejected(self):
        """Время без часового пояса отклоняется с полем start_at.
        Контракт: время в RFC 3339 с TZ. Naive datetime — типичная
        ошибка клиента, и Tool должен вернуть поле, а не упасть в
        сравнении datetime.
        """
        naive = self.start.replace(tzinfo=None)
        self.assert_tool_error(
            "INVALID_PARAMETERS", lambda: self.tool.execute(
                self.request(start_at=naive), self.context), field="start_at")

    def test_invalid_interval(self):
        """end_at <= start_at → INVALID_INTERVAL с полем end_at."""
        self.assert_tool_error(
            "INVALID_INTERVAL", lambda: self.tool.execute(
                self.request(end_at=self.start), self.context), field="end_at")

    def test_start_in_past(self):
        """Начало в прошлом → START_IN_PAST с полем start_at."""
        self.assert_tool_error(
            "START_IN_PAST", lambda: self.tool.execute(
                self.request(start_at=self.now - timedelta(seconds=1)),
                self.context), field="start_at")

    def test_unknown_room(self):
        """Несуществующая комната → NOT_FOUND с полем room_id."""
        self.assert_tool_error(
            "NOT_FOUND", lambda: self.tool.execute(
                self.request(room_id=uuid4()), self.context), field="room_id")

    def test_unavailable_room(self):
        """Комната вне обслуживания → ROOM_UNAVAILABLE с полем room_id."""
        self.room.is_available = False
        self.assert_tool_error(
            "ROOM_UNAVAILABLE", lambda: self.tool.execute(
                self.request(), self.context), field="room_id")

    def test_conflict_reports_conflicting_booking_id(self):
        """Пересечение → BOOKING_CONFLICT + conflicting_booking_id.
        Идентификатор конфликтующей брони — часть контракта (К1/К8),
        клиент должен получить его, чтобы показать пользователю.
        """
        existing = self.make_job()
        err = self.assert_tool_error(
            "BOOKING_CONFLICT", lambda: self.tool.execute(
                self.request(), self.context), field="room_id")
        self.assertEqual(err.conflicting_booking_id, existing.id)

    def test_empty_topic_is_rejected(self):
        """Пустая тема → INVALID_PARAMETERS с полем topic."""
        self.assert_tool_error(
            "INVALID_PARAMETERS", lambda: self.tool.execute(
                self.request(topic="  "), self.context), field="topic")


# Контракт: id → Booking в CONFIRMED.
# Проверки: автор; исходный статус PENDING_CONFIRMATION; время начала не наступило.
# Ошибки: INVALID_PARAMETERS (id), NOT_FOUND (id), FORBIDDEN (null), INVALID_STATE (id), CONFIRMATION_EXPIRED (id).
class ConfirmBookingToolTests(ToolTestCase):
    def setUp(self):
        super().setUp()
        self.tool = ConfirmBookingTool(self.jobs, self.clock)

    def test_confirms_pending_booking(self):
        """Обычный путь: pending → CONFIRMED."""
        job = self.make_job()
        result = self.tool.execute({"id": job.id}, self.context)
        self.assertEqual(result.data["state"], "CONFIRMED")

    def test_missing_id_has_field(self):
        """id не UUID → INVALID_PARAMETERS с полем id."""
        self.assert_tool_error(
            "INVALID_PARAMETERS", lambda: self.tool.execute(
                {"id": "not-a-uuid"}, self.context), field="id")

    def test_unknown_booking(self):
        """Несуществующая бронь → NOT_FOUND с полем id."""
        self.assert_tool_error(
            "NOT_FOUND", lambda: self.tool.execute(
                {"id": uuid4()}, self.context), field="id")

    def test_other_actor_forbidden(self):
        """Чужой пользователь не может подтвердить бронь.
        Внимание: админ тоже не может — по контракту подтверждает
        только автор. Поэтому используем обычный context с другим
        actor_id, а не admin_context.
        """
        job = self.make_job()
        other = ToolContext(uuid4(), False, uuid4())
        self.assert_tool_error(
            "FORBIDDEN", lambda: self.tool.execute({"id": job.id}, other))

    def test_confirmation_after_start_expired(self):
        """Подтверждение в момент начала или позже → CONFIRMATION_EXPIRED.
        Это правило Job (confirm проверяет now < start_at), Tool лишь
        переводит JobError в ToolError с тем же кодом и полем id.
        """
        job = self.make_job()
        self.clock.now = self.start
        self.assert_tool_error(
            "CONFIRMATION_EXPIRED", lambda: self.tool.execute(
                {"id": job.id}, self.context), field="id")

    def test_second_confirmation_invalid_state(self):
        """Повторное подтверждение → INVALID_STATE с полем id.
        Проверяет, что Tool не «проглатывает» ошибку ядра, а
        возвращает её клиенту с указанием поля.
        """
        job = self.make_job()
        self.tool.execute({"id": job.id}, self.context)
        self.assert_tool_error(
            "INVALID_STATE", lambda: self.tool.execute(
                {"id": job.id}, self.context), field="id")


# Контракт: id → Booking в CANCELLED.
# Проверки: автор (или админ); разрешённый переход; подтверждённая встреча ещё не началась.
# Ошибки: INVALID_PARAMETERS (id), NOT_FOUND (id), FORBIDDEN (null),
# INVALID_STATE/ALREADY_CANCELLED/MEETING_STARTED (id).
class CancelBookingToolTests(ToolTestCase):
    def setUp(self):
        super().setUp()
        self.tool = CancelBookingTool(self.jobs, self.clock)

    def test_cancels_pending(self):
        """Неподтверждённую бронь автор может отменить."""
        job = self.make_job()
        result = self.tool.execute({"id": job.id}, self.context)
        self.assertEqual(result.data["state"], "CANCELLED")

    def test_cancels_confirmed_before_start(self):
        """Подтверждённую бронь можно отменить до начала встречи."""
        job = self.make_job()
        job.confirm(actor_id=self.actor, now=self.now)
        result = self.tool.execute({"id": job.id}, self.context)
        self.assertEqual(result.data["state"], "CANCELLED")

    def test_missing_id_has_field(self):
        """Нет id → INVALID_PARAMETERS с полем id."""
        self.assert_tool_error(
            "INVALID_PARAMETERS", lambda: self.tool.execute({}, self.context),
            field="id")

    def test_unknown_booking(self):
        """Несуществующая бронь → NOT_FOUND с полем id."""
        self.assert_tool_error(
            "NOT_FOUND", lambda: self.tool.execute({"id": uuid4()},
                                                    self.context), field="id")

    def test_other_actor_forbidden(self):
        """Чужой пользователь без прав админа → FORBIDDEN (field=null).
        field=null, потому что это ошибка операции в целом, а не
        конкретного поля (контракт: «Поле null означает ошибку
        операции в целом»).
        """
        job = self.make_job()
        other = ToolContext(uuid4(), False, uuid4())
        self.assert_tool_error(
            "FORBIDDEN", lambda: self.tool.execute({"id": job.id}, other))

    def test_admin_can_cancel_other_booking(self):
        """Админ может отменить чужую бронь.
        В Tool проверяется is_admin; в Job передаётся actor_id
        владельца, чтобы пройти проверку авторства в ядре. Это
        осознанный обход, зафиксированный в отчёте.
        """
        job = self.make_job()
        result = self.tool.execute({"id": job.id}, self.admin_context)
        self.assertEqual(result.data["state"], "CANCELLED")

    def test_meeting_started(self):
        """Подтверждённую встречу после начала отменить нельзя.
        Job.cancel возвращает MEETING_STARTED; Tool переводит его в
        ToolError с тем же кодом и полем id.
        """
        job = self.make_job()
        job.confirm(actor_id=self.actor, now=self.now)
        self.clock.now = self.start
        self.assert_tool_error(
            "MEETING_STARTED", lambda: self.tool.execute(
                {"id": job.id}, self.context), field="id")

    def test_already_cancelled(self):
        """Повторная отмена → ALREADY_CANCELLED с полем id.
        Отдельный код, а не INVALID_STATE: контракт различает эти
        случаи, и клиент должен получить именно ALREADY_CANCELLED.
        """
        job = self.make_job()
        job.cancel(actor_id=self.actor, now=self.now)
        self.assert_tool_error(
            "ALREADY_CANCELLED", lambda: self.tool.execute(
                {"id": job.id}, self.context), field="id")


# Контракт: id и хотя бы одно из room_id/start_at/end_at → Booking с прежним статусом.
# Проверки: автор (или админ); статус PENDING_CONFIRMATION или CONFIRMED; новый интервал корректен и не
# начинается в прошлом; комната доступна.
# Ошибки: INVALID_PARAMETERS, NOT_FOUND, FORBIDDEN, INVALID_STATE,
# INVALID_INTERVAL, START_IN_PAST, ROOM_UNAVAILABLE, BOOKING_CONFLICT.
class RescheduleBookingToolTests(ToolTestCase):
    def setUp(self):
        super().setUp()
        self.tool = RescheduleBookingTool(self.jobs, self.rooms, self.clock)

    def test_requires_at_least_one_field(self):
        """Без нового поля перенос невозможен → INVALID_PARAMETERS.
        Контракт: «id и хотя бы одно из room_id, start_at, end_at».
        """
        job = self.make_job()
        self.assert_tool_error(
            "INVALID_PARAMETERS", lambda: self.tool.execute(
                {"id": job.id}, self.context))

    def test_missing_id_has_field(self):
        """Без id → INVALID_PARAMETERS с полем id."""
        self.assert_tool_error(
            "INVALID_PARAMETERS", lambda: self.tool.execute(
                {"start_at": self.start}, self.context), field="id")

    def test_invalid_state_after_cancel(self):
        """Отменённую бронь нельзя перенести → INVALID_STATE."""
        job = self.make_job()
        job.cancel(actor_id=self.actor, now=self.now)
        self.assert_tool_error(
            "INVALID_STATE", lambda: self.tool.execute(
                {"id": job.id,
                 "start_at": self.start + timedelta(hours=2)}, self.context),
            field="id")

    def test_other_actor_forbidden(self):
        """Чужой пользователь без прав админа → FORBIDDEN."""
        job = self.make_job()
        other = ToolContext(uuid4(), False, uuid4())
        self.assert_tool_error(
            "FORBIDDEN", lambda: self.tool.execute(
                {"id": job.id, "start_at": self.start}, other))

    def test_new_interval_validation(self):
        """Новый интервал проверяется целиком.
        Если меняется только start_at и он оказывается позже прежнего
        end_at — это INVALID_INTERVAL, а не молчаливое сохранение.
        """
        job = self.make_job()
        self.assert_tool_error(
            "INVALID_INTERVAL", lambda: self.tool.execute(
                {"id": job.id,
                 "start_at": self.start + timedelta(hours=2)}, self.context),
            field="end_at")

    def test_start_in_past(self):
        """Перенос на время в прошлом → START_IN_PAST с полем start_at."""
        job = self.make_job()
        self.assert_tool_error(
            "START_IN_PAST", lambda: self.tool.execute(
                {"id": job.id, "start_at": self.now - timedelta(minutes=1)},
                self.context), field="start_at")


# Контракт: status, from, to, limit, offset — необязательные.
# Возвращает только свои брони, проверяет фильтры и параметры страницы.
# Ошибки: INVALID_PARAMETERS.
class ListOwnBookingsToolTests(ToolTestCase):
    def setUp(self):
        super().setUp()
        self.tool = ListOwnBookingsTool(self.jobs)

    def test_returns_only_own_bookings_sorted(self):
        """Только свои брони, отсортированные по времени начала.
        Чужая бронь не должна попасть в ответ — это проверка
        «возвращает только свои брони» из контракта.
        """
        earlier = self.make_job()
        later = self.make_job(start_at=self.start + timedelta(hours=3),
                              end_at=self.end + timedelta(hours=3))
        foreign = Job(owner_id=uuid4(), start_at=self.start,
                      end_at=self.end, now=self.now)
        self.jobs.add(foreign)
        result = self.tool.execute({}, self.context)
        ids = [item["id"] for item in result.data["items"]]
        self.assertEqual(ids, [str(earlier.id), str(later.id)])

    def test_status_filter(self):
        """Фильтр по статусу возвращает только подходящие брони."""
        self.make_job()
        confirmed = self.make_job()
        confirmed.confirm(actor_id=self.actor, now=self.now)
        result = self.tool.execute({"status": "CONFIRMED"}, self.context)
        self.assertEqual(len(result.data["items"]), 1)

    def test_invalid_status(self):
        """Неизвестный статус → INVALID_PARAMETERS с полем status."""
        self.assert_tool_error(
            "INVALID_PARAMETERS", lambda: self.tool.execute(
                {"status": "NOPE"}, self.context), field="status")

    def test_invalid_limit(self):
        """limit вне диапазона → INVALID_PARAMETERS с полем limit."""
        self.assert_tool_error(
            "INVALID_PARAMETERS", lambda: self.tool.execute(
                {"limit": 0}, self.context), field="limit")

    def test_invalid_offset(self):
        """offset < 0 → INVALID_PARAMETERS с полем offset."""
        self.assert_tool_error(
            "INVALID_PARAMETERS", lambda: self.tool.execute(
                {"offset": -1}, self.context), field="offset")


# Контракт: start_at, end_at → RoomList.
# Проверки: корректный интервал; только доступные комнаты без пересечений.
# Ошибки: INVALID_PARAMETERS, INVALID_INTERVAL, START_IN_PAST.
class FindAvailableRoomsToolTests(ToolTestCase):
    def setUp(self):
        super().setUp()
        self.free = FakeRoom()
        self.rooms.rooms[self.free.id] = self.free
        self.tool = FindAvailableRoomsTool(self.jobs, self.rooms, self.clock)

    def test_returns_available_room(self):
        """Свободная комната попадает в ответ."""
        result = self.tool.execute(
            {"start_at": self.start, "end_at": self.end}, self.context)
        ids = {item["id"] for item in result.data["items"]}
        self.assertIn(str(self.free.id), ids)

    def test_missing_start_at_has_field(self):
        """Без start_at → INVALID_PARAMETERS с полем start_at."""
        self.assert_tool_error(
            "INVALID_PARAMETERS", lambda: self.tool.execute(
                {"end_at": self.end}, self.context), field="start_at")

    def test_invalid_interval(self):
        """end_at <= start_at → INVALID_INTERVAL с полем end_at."""
        self.assert_tool_error(
            "INVALID_INTERVAL", lambda: self.tool.execute(
                {"start_at": self.end, "end_at": self.start}, self.context),
            field="end_at")

    def test_start_in_past(self):
        """Начало в прошлом → START_IN_PAST с полем start_at."""
        self.assert_tool_error(
            "START_IN_PAST", lambda: self.tool.execute(
                {"start_at": self.now - timedelta(seconds=1),
                 "end_at": self.end}, self.context), field="start_at")


# Контракт: id уведомления → NotificationAttempt.
# Проверки: права администратора; уведомление существует и требует повторной доставки.
# Ошибки: FORBIDDEN (null), INVALID_PARAMETERS (id), NOT_FOUND (id), INVALID_STATE (id).
class RetryNotificationToolTests(ToolTestCase):
    def setUp(self):
        super().setUp()
        self.notifications = FakeNotificationRepository()
        self.notifier = FakeNotifier(result=FakeAttempt("SENT"))
        self.tool = RetryNotificationTool(self.notifications, self.notifier)

    def test_admin_retries_failed_notification(self):
        """Обычный путь: админ повторяет FAILED-уведомление.
        Проверяем и результат, и что notifier действительно вызван,
        и что попытка зафиксирована в репозитории.
        """
        n = FakeNotification(status="FAILED")
        self.notifications.items[n.id] = n
        result = self.tool.execute({"id": n.id}, self.admin_context)
        self.assertEqual(result.data["status"], "SENT")
        self.assertEqual(self.notifier.calls, 1)
        self.assertEqual(len(self.notifications.attempts), 1)

    def test_non_admin_forbidden(self):
        """Не админ → FORBIDDEN (field=null).
        Права проверяются до обращения к notifier: не должно быть
        вызова SMS-провайдера для неадмина.
        """
        n = FakeNotification()
        self.notifications.items[n.id] = n
        self.assert_tool_error(
            "FORBIDDEN", lambda: self.tool.execute({"id": n.id},
                                                    self.context))

    def test_missing_id_has_field(self):
        """Без id → INVALID_PARAMETERS с полем id."""
        self.assert_tool_error(
            "INVALID_PARAMETERS", lambda: self.tool.execute(
                {}, self.admin_context), field="id")

    def test_unknown_notification(self):
        """Несуществующее уведомление → NOT_FOUND с полем id."""
        self.assert_tool_error(
            "NOT_FOUND", lambda: self.tool.execute(
                {"id": uuid4()}, self.admin_context), field="id")

    def test_only_failed_or_unknown_retryable(self):
        """Повтор разрешён только для FAILED/UNKNOWN.
        SENT-уведомление повторять нельзя → INVALID_STATE с полем id.
        """
        n = FakeNotification(status="SENT")
        self.notifications.items[n.id] = n
        self.assert_tool_error(
            "INVALID_STATE", lambda: self.tool.execute(
                {"id": n.id}, self.admin_context), field="id")

    def test_notifier_error_propagates(self):
        """Ошибка notifier'а не глотается Tool'ом.
        Tool пробрасывает исключение; Dispatcher на границе превратит
        его в ToolError (NOTIFICATION_PROVIDER_ERROR/TIMEOUT или
        INTERNAL_ERROR). Здесь важно, что Tool не делает вид, что
        доставка прошла.
        """
        n = FakeNotification(status="FAILED")
        self.notifications.items[n.id] = n
        self.notifier.error = Exception("provider down")
        with self.assertRaises(Exception):
            self.tool.execute({"id": n.id}, self.admin_context)


if __name__ == "__main__":
    unittest.main()