"""分析使用的 OpenAI 兼容请求与工具循环测试。"""

import unittest
from unittest.mock import patch

import ai_client
import settings


class FakeResponse:
    def __init__(self, data):
        self.data = data
        self.status_code = 200
        self.text = ""
        self.response = None

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


class JournalAITests(unittest.TestCase):
    def setUp(self):
        self.original_third_search = settings.CONFIG.get("third_search")
        settings.CONFIG["third_search"] = {"enabled": False}
        self.model = {
            "name": "test-model",
            "model_id": "test-model-id",
            "api_url": "https://example.test/chat/completions",
            "api_key": "secret",
            "search": False,
        }

    def tearDown(self):
        if self.original_third_search is None:
            settings.CONFIG.pop("third_search", None)
        else:
            settings.CONFIG["third_search"] = self.original_third_search

    @patch("ai_client.requests.post")
    def test_returns_complete_openai_compatible_response(self, post):
        post.return_value = FakeResponse(
            {"choices": [{"message": {"role": "assistant", "content": "最终回答"}}]}
        )

        answer, success, web_count, tool_counts, result_count = ai_client.call_ai(
            "问题", self.model
        )

        self.assertTrue(success)
        self.assertEqual("最终回答", answer)
        self.assertEqual((0, {}, 0), (web_count, tool_counts, result_count))
        payload = post.call_args.kwargs["json"]
        self.assertEqual("test-model-id", payload["model"])
        self.assertNotIn("web_search", payload)

    @patch("ai_client.execute_tool")
    @patch("ai_client.requests.post")
    def test_executes_tool_call_then_requests_final_answer(self, post, execute_tool):
        post.side_effect = [
            FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "read_daily_log",
                                            "arguments": '{"date":"2026-07-14"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            ),
            FakeResponse(
                {"choices": [{"message": {"role": "assistant", "content": "工具后回答"}}]}
            ),
        ]
        execute_tool.return_value = ("日记内容", 0)

        answer, success, _, tool_counts, _ = ai_client.call_ai("分析任务", self.model)

        self.assertTrue(success)
        self.assertEqual("工具后回答", answer)
        self.assertEqual({"read_daily_log": 1}, tool_counts)
        self.assertEqual(2, post.call_count)
        second_payload = post.call_args_list[1].kwargs["json"]
        self.assertEqual("tool", second_payload["messages"][-1]["role"])
        self.assertEqual("日记内容", second_payload["messages"][-1]["content"])

    def test_system_prompt_is_for_analysis_not_realtime_chat(self):
        prompt = ai_client._build_system_prompt()

        self.assertIn("分析引擎", prompt)
        self.assertIn("不承担日常聊天", prompt)
        self.assertNotIn("@AI 提问", prompt)


if __name__ == "__main__":
    unittest.main()
