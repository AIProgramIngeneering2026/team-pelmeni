import unittest
from uuid import uuid4

from pelmeni.registry import Registry, RegistryError
from pelmeni.tool import ToolContext, ToolResult


class RecordingTool:
    def __init__(self):
        self.calls = []

    def execute(self, request, context):
        self.calls.append((request, context))
        return ToolResult(data={"ok": True})


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = Registry()
        self.tool = RecordingTool()

    def test_returns_exact_registered_instance_without_executing_it(self):
        self.registry.register("create_booking", self.tool)
        self.assertIs(self.registry.get("create_booking"), self.tool)
        self.assertEqual(self.tool.calls, [])

    def test_selects_two_tools_independently(self):
        another = RecordingTool()
        self.registry.register("create_booking", self.tool)
        self.registry.register("cancel_booking", another)
        self.assertIs(self.registry.get("create_booking"), self.tool)
        self.assertIs(self.registry.get("cancel_booking"), another)

    def test_unknown_operation_has_code_and_field(self):
        with self.assertRaises(RegistryError) as caught:
            self.registry.get("missing")
        error = caught.exception
        self.assertEqual(error.code, "UNKNOWN_OPERATION")
        self.assertEqual(error.field, "operation")
        self.assertEqual(error.details[0]["field"], "operation")
        self.assertTrue(error.message)

    def test_duplicate_registration_preserves_original(self):
        self.registry.register("create_booking", self.tool)
        with self.assertRaises(RegistryError) as caught:
            self.registry.register("create_booking", RecordingTool())
        self.assertEqual(caught.exception.code, "DUPLICATE_OPERATION")
        self.assertIs(self.registry.get("create_booking"), self.tool)

    def test_invalid_names_are_rejected_by_both_methods(self):
        for name in (None, 123, [], "", " ", " create", "create "):
            for method in (self.registry.get,
                           lambda value: self.registry.register(value, self.tool)):
                with self.subTest(name=name, method=method):
                    with self.assertRaises(RegistryError) as caught:
                        method(name)
                    self.assertEqual(caught.exception.code, "INVALID_PARAMETERS")
                    self.assertEqual(caught.exception.field, "operation")

    def test_invalid_tool_does_not_reserve_name(self):
        for invalid in (None, object(), RecordingTool):
            with self.subTest(tool=invalid):
                with self.assertRaises(RegistryError) as caught:
                    self.registry.register("create_booking", invalid)
                self.assertEqual(caught.exception.field, "tool")
        self.registry.register("create_booking", self.tool)
        self.assertIs(self.registry.get("create_booking"), self.tool)

    def test_names_are_case_sensitive(self):
        self.registry.register("create_booking", self.tool)
        with self.assertRaises(RegistryError) as caught:
            self.registry.get("CREATE_BOOKING")
        self.assertEqual(caught.exception.code, "UNKNOWN_OPERATION")

    def test_registries_do_not_share_entries(self):
        self.registry.register("create_booking", self.tool)
        with self.assertRaises(RegistryError):
            Registry().get("create_booking")

    def test_caller_can_execute_returned_tool_using_contract(self):
        self.registry.register("create_booking", self.tool)
        request = {"topic": "Встреча"}
        context = ToolContext(uuid4(), False, uuid4())
        result = self.registry.get("create_booking").execute(request, context)
        self.assertEqual(result.data, {"ok": True})
        self.assertEqual(self.tool.calls, [(request, context)])


if __name__ == "__main__":
    unittest.main()
