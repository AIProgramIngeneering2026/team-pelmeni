import unittest
from uuid import uuid4

from pelmeni.dispatcher import Dispatcher, DispatchResult, ToolError
from pelmeni.registry import Registry
from pelmeni.tool import ToolContext, ToolResult


class FakeUoW:
    def __init__(self):
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.rolled_back = True

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class FakeTool:
    def __init__(self, result=None, error=None, crash=None):
        self.result = result
        self.error = error
        self.crash = crash
        self.calls = 0

    def execute(self, request, context):
        self.calls += 1
        if self.crash is not None:
            raise self.crash
        if self.error is not None:
            raise self.error
        return self.result


class RecordingScheduler:
    def __init__(self):
        self.ticks = 0

    def tick(self):
        self.ticks += 1


class RecordingIdempotency:
    """Минимальный in-memory store для тестов."""

    def __init__(self):
        self.records = {}

    class Record:
        def __init__(self, fingerprint):
            self.state = "NEW"
            self.fingerprint = fingerprint
            self.result_data = None

    def begin(self, *, scope, key, request_fingerprint):
        rec = self.records.get((scope, key))
        if rec is None:
            rec = self.Record(request_fingerprint)
            self.records[(scope, key)] = rec
            return rec
        if rec.fingerprint != request_fingerprint:
            rec.state = "REUSED"
        elif rec.state == "NEW":
            rec.state = "IN_PROGRESS"
        return rec

    def complete(self, record, result_data):
        record.state = "DONE"
        record.result_data = result_data

    def fail(self, record):
        record.state = "FAILED"


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.registry = Registry()
        self.uow = FakeUoW()
        self.scheduler = RecordingScheduler()
        self.idem = RecordingIdempotency()
        self.dispatcher = Dispatcher(
            registry=self.registry,
            uow_factory=lambda: self.uow,
            idempotency=self.idem,
            scheduler=self.scheduler,
        )
        self.context = ToolContext(uuid4(), False, uuid4())

    def test_selects_tool_by_name_and_returns_data(self):
        tool = FakeTool(result=ToolResult(data={"id": "1"}))
        self.registry.register("create_booking", tool)
        out = self.dispatcher.dispatch("create_booking", {"topic": "x"}, self.context)
        self.assertEqual(out.data, {"id": "1"})
        self.assertTrue(self.uow.committed)
        self.assertEqual(tool.calls, 1)

    def test_unknown_operation_becomes_tool_error(self):
        with self.assertRaises(ToolError) as caught:
            self.dispatcher.dispatch("missing", {}, self.context)
        err = caught.exception
        self.assertEqual(err.code, "UNKNOWN_OPERATION")
        self.assertEqual(err.details[0].field, "operation")
        self.assertEqual(err.correlation_id, self.context.correlation_id)

    def test_domain_error_rolls_back_and_keeps_correlation_id(self):
        domain = ToolError("BOOKING_CONFLICT", "пересечение",
                           conflicting_booking_id=uuid4())
        self.registry.register("create_booking", FakeTool(error=domain))
        with self.assertRaises(ToolError) as caught:
            self.dispatcher.dispatch("create_booking", {}, self.context)
        self.assertTrue(self.uow.rolled_back)
        self.assertFalse(self.uow.committed)
        self.assertEqual(caught.exception.correlation_id, self.context.correlation_id)

    def test_tool_crash_becomes_internal_error(self):
        self.registry.register("create_booking", FakeTool(crash=RuntimeError("boom")))
        with self.assertRaises(ToolError) as caught:
            self.dispatcher.dispatch("create_booking", {}, self.context)
        self.assertEqual(caught.exception.code, "INTERNAL_ERROR")
        self.assertTrue(self.uow.rolled_back)

    def test_scheduler_ticks_before_operation(self):
        self.registry.register("cancel_booking", FakeTool(result=ToolResult({})))
        self.dispatcher.dispatch("cancel_booking", {}, self.context)
        self.assertEqual(self.scheduler.ticks, 1)

    def test_idempotent_replay_returns_saved_result_without_second_call(self):
        tool = FakeTool(result=ToolResult(data={"id": "1"}))
        self.registry.register("create_booking", tool)
        first = self.dispatcher.dispatch(
            "create_booking", {"topic": "x"}, self.context, idempotency_key="k")
        second = self.dispatcher.dispatch(
            "create_booking", {"topic": "x"}, self.context, idempotency_key="k")
        self.assertEqual(first.data, second.data)
        self.assertTrue(second.replayed)
        self.assertEqual(tool.calls, 1)

    def test_idempotency_key_reused_with_different_params(self):
        self.registry.register("create_booking", FakeTool(result=ToolResult({})))
        self.dispatcher.dispatch(
            "create_booking", {"topic": "x"}, self.context, idempotency_key="k")
        with self.assertRaises(ToolError) as caught:
            self.dispatcher.dispatch(
                "create_booking", {"topic": "y"}, self.context, idempotency_key="k")
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_KEY_REUSED")

    def test_request_in_progress_while_first_running(self):
        class SlowTool:
            def __init__(self, dispatcher, context):
                self.dispatcher = dispatcher
                self.context = context
                self.inner = None

            def execute(self, request, ctx):
                # имитируем параллельный повтор с тем же ключом
                if self.inner is None:
                    self.inner = True
                    with self.assertRaises(ToolError) as caught:
                        self.dispatcher.dispatch(
                            "create_booking", request, ctx,
                            idempotency_key="k")
                    assert caught.exception.code == "REQUEST_IN_PROGRESS"
                return ToolResult(data={"ok": True})

        self.registry.register("create_booking", SlowTool(self.dispatcher, self.context))
        self.dispatcher.dispatch(
            "create_booking", {"topic": "x"}, self.context, idempotency_key="k")

    def test_new_tool_added_without_touching_dispatcher(self):
        # Dispatcher не меняется — только регистрация.
        self.registry.register("retry_notification",
                               FakeTool(result=ToolResult(data={"sent": True})))
        out = self.dispatcher.dispatch("retry_notification", {}, self.context)
        self.assertEqual(out.data, {"sent": True})


if __name__ == "__main__":
    unittest.main()