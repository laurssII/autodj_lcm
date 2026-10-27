import unittest

from autodj.agent.llm import AnthropicLLM, OpenAICompatLLM, ToolCall


class ConvertTest(unittest.TestCase):
    msgs = [
        {"role": "user", "content": "ponme algo"},
        {"role": "assistant", "content": "", "tool_calls": [ToolCall("c1", "search_library", {"query": "x"}),
                                                           ToolCall("c2", "library_overview", {})]},
        {"role": "tool", "tool_call_id": "c1", "name": "search_library", "content": "{}"},
        {"role": "tool", "tool_call_id": "c2", "name": "library_overview", "content": "{}"},
    ]

    def test_anthropic(self):
        out = AnthropicLLM.convert(self.msgs)
        self.assertEqual([m["role"] for m in out], ["user", "assistant", "user"])
        self.assertEqual([b["type"] for b in out[1]["content"]], ["tool_use", "tool_use"])
        self.assertEqual([b["tool_use_id"] for b in out[2]["content"]], ["c1", "c2"])

    def test_openai(self):
        out = OpenAICompatLLM.convert("sys", self.msgs)
        self.assertEqual([m["role"] for m in out], ["system", "user", "assistant", "tool", "tool"])
        self.assertEqual(out[2]["tool_calls"][0]["function"]["name"], "search_library")
        self.assertIsNone(out[2]["content"])


class ToolsPayloadTest(unittest.TestCase):
    def test_tools_without_arguments_are_sent_without_parameters(self):
        from autodj.agent.llm import Tool

        tools = [Tool("mix_now", "ya", {"type": "object", "properties": {}}),
                 Tool("search", "busca", {"type": "object", "properties": {"q": {"type": "string"}},
                                          "required": ["q"]})]
        out = OpenAICompatLLM.tools_payload(tools)
        self.assertNotIn("parameters", out[0]["function"])
        self.assertEqual(out[1]["function"]["parameters"]["required"], ["q"])
        self.assertEqual([t["type"] for t in out], ["function", "function"])

    def test_the_real_chat_tools_are_all_valid(self):
        from autodj.agent.agent import CHAT_TOOLS

        for t in OpenAICompatLLM.tools_payload(CHAT_TOOLS):
            self.assertTrue(t["function"]["name"])
            self.assertTrue(t["function"]["description"])


if __name__ == "__main__":
    unittest.main()
