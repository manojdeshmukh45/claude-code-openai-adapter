"""Run with:  python3 -m unittest discover -s tests -v   (no dependencies, no network, no claude CLI needed)"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import adapter as A  # noqa: E402

TOOLS = [{"type": "function", "function": {"name": "get_weather", "description": "x",
                                           "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]


class JsonRecovery(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(A.extract_json('{"content":"hi"}'), {"content": "hi"})

    def test_fenced(self):
        self.assertEqual(A.extract_json('```json\n{"content":"hi"}\n```'), {"content": "hi"})

    def test_chatter_before_and_after(self):
        self.assertEqual(A.extract_json('Sure!\n{"content":"hi"}\nHope that helps'), {"content": "hi"})

    def test_missing_closers_are_repaired(self):
        raw = '{"tool_calls":[{"name":"add_intent","arguments":{"intents":[{"summary":"a","priority":8}]}}'
        j = A.extract_json(raw)
        self.assertEqual(j["tool_calls"][0]["arguments"]["intents"][0]["priority"], 8)

    def test_braces_inside_strings_do_not_confuse_repair(self):
        raw = '{"content":"a } b ] c"'
        self.assertEqual(A.extract_json(raw), {"content": "a } b ] c"})

    def test_cut_off_inside_string_is_not_guessed(self):
        self.assertIsNone(A.extract_json('{"content":"unfinished'))

    def test_garbage(self):
        self.assertIsNone(A.extract_json("no json here"))


class Conversion(unittest.TestCase):
    def test_tool_call(self):
        msg, fin = A.to_message('{"tool_calls":[{"name":"get_weather","arguments":{"city":"Paris"}}]}', True)
        self.assertEqual(fin, "tool_calls")
        self.assertIsNone(msg["content"])
        fn = msg["tool_calls"][0]["function"]
        self.assertEqual(fn["name"], "get_weather")
        self.assertEqual(json.loads(fn["arguments"]), {"city": "Paris"})

    def test_plain_answer_with_tools_present(self):
        msg, fin = A.to_message('{"content":"It is sunny"}', True)
        self.assertEqual((msg["content"], fin), ("It is sunny", "stop"))

    def test_no_tools_passes_text_through(self):
        msg, fin = A.to_message("hello", False)
        self.assertEqual((msg["content"], fin), ("hello", "stop"))


class Prompt(unittest.TestCase):
    def test_roles_and_tools(self):
        msgs = [{"role": "system", "content": "be brief"},
                {"role": "user", "content": [{"type": "text", "text": "weather?"}]},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "c1", "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "sunny"}]
        system, convo = A.build_prompt(msgs, TOOLS)
        self.assertIn("be brief", system)
        self.assertIn("get_weather", system)
        self.assertIn("USER: weather?", convo)
        self.assertIn("TOOL RESULT (id=c1)", convo)
        self.assertTrue(convo.endswith("ASSISTANT:"))


if __name__ == "__main__":
    unittest.main()
