import unittest

from dgutbot.agent.agent_protocol import AgentError, ToolDefinition, ToolRegistry, validate_schema


class AgentProtocolTests(unittest.TestCase):
    def test_registry_validates_input_and_orders_capabilities(self):
        registry = ToolRegistry()
        schema = {
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "integer", "minimum": 1}},
            "additionalProperties": False,
        }
        registry.register(ToolDefinition("z.tool", "z", schema, {"type": "object"}, True, True, "none", lambda p: p))
        registry.register(ToolDefinition("a.tool", "a", schema, {"type": "object"}, True, True, "none", lambda p: p))
        self.assertEqual([item["name"] for item in registry.capabilities()], ["a.tool", "z.tool"])
        with self.assertRaises(AgentError) as raised:
            registry.call("a.tool", {"value": 0})
        self.assertEqual(raised.exception.code, "TOOL_INPUT_INVALID")

    def test_schema_subset_checks_arrays_and_unknown_properties(self):
        schema = {
            "type": "object",
            "properties": {"items": {"type": "array", "uniqueItems": True, "items": {"type": "string"}}},
            "additionalProperties": False,
        }
        self.assertEqual(validate_schema({"items": ["a"]}, schema), [])
        self.assertTrue(validate_schema({"items": ["a", "a"], "extra": 1}, schema))


if __name__ == "__main__":
    unittest.main()
