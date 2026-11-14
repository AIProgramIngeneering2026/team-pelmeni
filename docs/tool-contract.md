# Контракт Tool

Схемы HTTP-запросов и ответов определены в [OpenAPI](api/openapi.yaml). Состояния брони взяты из диаграммы команды. Здесь Job означает бронь, доступную через /bookings. Внутренние задачи доставки уведомлений имеют отдельный жизненный цикл.

## Интерфейс

Tool выполняет одну операцию. Dispatcher выбирает исполнителя и управляет транзакцией. Registry хранит состояния броней и проверяет разрешённые переходы согласно архитектуре команды. Storage скрывает работу с БД. SmsNotifier скрывает API SMS-провайдера.

~~~text
execute(request, context) -> ToolResult
context:
  actor_id: UUID
  is_admin: boolean
  correlation_id: UUID
ToolResult:
  data: результат операции по схеме OpenAPI
ToolError:
  code: строка
  message: строка
  details: список {field: строка или null, code: строка, message: строка}
  conflicting_booking_id: UUID, только при конфликте броней
~~~

Контекст передаёт входной слой после аутентификации. Tool работает с доменными объектами и интерфейсами зависимостей. Формат HTTP скрыт входным слоем.

## Кто проверяет параметры

HTTP-слой проверяет JSON, обязательные поля и типы. Tool гарантирует проверку своих входных параметров и бизнес-правил при любом вызове. Владелец, состояние брони, время, статус комнаты и пересечения проверяются перед изменением. Dispatcher возвращает UNKNOWN_OPERATION при неизвестной операции. Неверные параметры дают INVALID_PARAMETERS.

При ошибке изменение брони не сохраняется. Создание брони и связанных заданий атомарно. Ошибки приводятся к единому формату на границе HTTP.

## Исполнители

| Tool | Получает | Возвращает | Проверки |
|---|---|---|---|
| CreateBookingTool | room_id, start_at, end_at, topic | Booking в PENDING_CONFIRMATION | Комната существует и доступна; начало не в прошлом; конец позже начала; нет пересечения |
| ConfirmBookingTool | id | Booking в CONFIRMED | Автор брони; исходный статус PENDING_CONFIRMATION; время начала не наступило |
| CancelBookingTool | id | Booking в CANCELLED | Автор; разрешённый переход; подтверждённая встреча ещё не началась |
| RescheduleBookingTool | id и хотя бы одно из room_id, start_at, end_at | Booking с обновлёнными данными и прежним статусом | Автор; статус PENDING_CONFIRMATION или CONFIRMED; новый интервал корректен и не начинается в прошлом; комната доступна |
| ListOwnBookingsTool | status, from, to, limit, offset — необязательные | BookingList | Возвращает только свои брони, проверяет фильтры и параметры страницы |
| FindAvailableRoomsTool | start_at, end_at | RoomList | Корректный интервал; только доступные комнаты без пересечений |
| RetryNotificationTool | id уведомления | NotificationAttempt | Права администратора; уведомление существует и требует повторной доставки |

### CreateBookingTool

Владелец берётся из context.actor_id. Время передаётся в RFC 3339 с часовым поясом. Интервалы полуоткрытые — встреча может начинаться в момент окончания предыдущей.

Комнату занимают брони PENDING_CONFIRMATION и CONFIRMED. При двух одновременных запросах на пересекающиеся интервалы создаётся ровно одна бронь. Второй запрос получает BOOKING_CONFLICT с идентификатором конфликтующей брони.

Dispatcher обрабатывает Idempotency-Key для защиты от повторного создания. Область ключа — пользователь и операция. Повтор с теми же параметрами возвращает первоначальный результат без второго вызова Tool. Другие параметры с тем же ключом вызывают IDEMPOTENCY_KEY_REUSED. Пока первая операция выполняется, повтор получает REQUEST_IN_PROGRESS. Успешный ключ сохраняется атомарно с бронью. Срок хранения ключа контрактом не ограничивается.

### CancelBookingTool

Автор может отменить PENDING_CONFIRMATION до подтверждения; при начале такая бронь автоматически отменяется. Отмена CONFIRMED разрешена только до начала встречи.

Из CANCELLED возвращается ALREADY_CANCELLED, из COMPLETED — INVALID_STATE. Для начавшейся подтверждённой встречи возвращается MEETING_STARTED. Успешная операция сохраняет бронь со статусом CANCELLED и создаёт уведомление об отмене.

### RescheduleBookingTool

Перед проверкой новые поля объединяются с прежними. Из проверки пересечений исключается сама переносимая бронь. При ошибке исходные данные сохраняются. При успехе обновляются данные брони и расписание связанных заданий, статус сохраняется.

### ConfirmBookingTool и Scheduler

Подтверждение после начала запрещено и возвращает CONFIRMATION_EXPIRED. Переходы из CANCELLED и COMPLETED запрещены. Повторное подтверждение CONFIRMED возвращает INVALID_STATE.

Scheduler автоматически отменяет неподтверждённые брони при начале и завершает подтверждённые по окончании. Проверка времени при пользовательской операции не зависит от задержки Scheduler.

### RetryNotificationTool

Администратор вручную повторяет доставку неуспешного уведомления. Получатель и сообщение берутся из сохранённой записи. SmsNotifier отправляет HTTP-запрос с тайм-аутом. Автоматические повторы не предусмотрены исходными материалами. Ошибка SMS не меняет состояние брони.

## Ошибки

| Код | HTTP | Поле |
|---|---|---|
| INVALID_PARAMETERS, VALIDATION_ERROR | 422 | Конкретное поле запроса |
| UNKNOWN_OPERATION | 422 | operation |
| INVALID_INTERVAL | 422 | body.end_at или query.end_at |
| START_IN_PAST | 422 | body.start_at или query.start_at |
| BOOKING_CONFLICT | 409 | body.room_id; отдельно conflicting_booking_id |
| ROOM_UNAVAILABLE | 409 | body.room_id |
| INVALID_STATE, ALREADY_CANCELLED, MEETING_STARTED, CONFIRMATION_EXPIRED | 409 | path.id |
| IDEMPOTENCY_KEY_REUSED, REQUEST_IN_PROGRESS | 409 | header.Idempotency-Key |
| NOT_FOUND | 404 | path.id или body.room_id |
| UNAUTHENTICATED | 401 | null |
| FORBIDDEN | 403 | null |
| NOTIFICATION_PROVIDER_ERROR | 502 | null |
| NOTIFICATION_TIMEOUT | 504 | null |
| SERVICE_UNAVAILABLE | 503 | null |
| INTERNAL_ERROR | 500 | null |

Неверный JSON возвращает 400 BAD_REQUEST. Поле null означает ошибку операции в целом. HTTP-ответ оборачивает ToolError в объект error и добавляет correlation_id.

~~~json
{
  "error": {
    "code": "START_IN_PAST",
    "message": "Начало бронирования в прошлом.",
    "details": [
      {"field": "body.start_at", "code": "START_IN_PAST", "message": "Укажите время не раньше текущего."}
    ],
    "correlation_id": "6bc9695f-37cc-4a32-9918-6288ef0a446f"
  }
}
~~~
