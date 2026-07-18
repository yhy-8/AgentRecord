"""分析使用的 OpenAI 兼容请求与工具循环测试。"""

import unittest
from unittest.mock import patch

from AgentRecord import ai_client, settings


class FakeResponse:
    def __init__(self, data):
        self.data = data
        self.status_code = 200
        self.text = ""
        self.response = None

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ai_client.requests.HTTPError(
                str(self.status_code), response=self
            )

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

    @patch("AgentRecord.ai_client.requests.post")
    def test_returns_complete_openai_compatible_response(self, post):
        post.return_value = FakeResponse(
            {
                "choices": [{"message": {"role": "assistant", "content": "最终回答"}}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "prompt_tokens_details": {"cached_tokens": 80},
                },
            }
        )

        response = ai_client.call_ai(
            "问题", self.model
        )
        answer, success, web_count, tool_counts, result_count = response

        self.assertTrue(success)
        self.assertEqual("最终回答", answer)
        self.assertEqual((0, {}, 0), (web_count, tool_counts, result_count))
        payload = post.call_args.kwargs["json"]
        self.assertEqual("test-model-id", payload["model"])
        self.assertNotIn("web_search", payload)
        self.assertEqual(120, response.telemetry["usage"]["total_tokens"])
        self.assertEqual(80, response.telemetry["usage"]["cached_tokens"])
        self.assertEqual(1, response.telemetry["http_attempts"])

    @patch("AgentRecord.ai_client.execute_tool")
    @patch("AgentRecord.ai_client.requests.post")
    def test_search_query_must_match_central_allowlist(self, post, execute_tool):
        settings.CONFIG["third_search"] = {
            "enabled": True,
            "api_key": "search-key",
            "max_rounds": 3,
        }
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
                                        "id": "search-1",
                                        "type": "function",
                                        "function": {
                                            "name": "web_search",
                                            "arguments": '{"query":"私自改写的查询"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            ),
            FakeResponse(
                {"choices": [{"message": {"role": "assistant", "content": "{}"}}]}
            ),
        ]

        response = ai_client.call_ai(
            "研究",
            self.model,
            allowed_tools={"web_search"},
            allowed_search_queries=["中控给定的查询"],
        )

        self.assertTrue(response.success)
        execute_tool.assert_not_called()
        self.assertEqual(["私自改写的查询"], response.telemetry["rejected_search_queries"])
        self.assertEqual(0, response.search_results)

    @patch("AgentRecord.ai_client.requests.post")
    def test_central_permission_can_remove_all_tools(self, post):
        post.return_value = FakeResponse(
            {"choices": [{"message": {"role": "assistant", "content": "JSON"}}]}
        )

        answer, success, _, _, _ = ai_client.call_ai(
            "结构化任务", self.model, allowed_tools=frozenset()
        )

        self.assertTrue(success)
        self.assertEqual("JSON", answer)
        payload = post.call_args.kwargs["json"]
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)

    @patch("AgentRecord.ai_client.execute_tool")
    @patch("AgentRecord.ai_client.requests.post")
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

    @patch("AgentRecord.ai_client.time.sleep")
    @patch("AgentRecord.ai_client.requests.post")
    def test_transient_connection_errors_use_bounded_retry(self, post, sleep):
        expected = FakeResponse({"ok": True})
        post.side_effect = [
            ai_client.requests.ConnectionError("dns"),
            ai_client.requests.Timeout("timeout"),
            expected,
        ]

        response = ai_client._post_with_transient_retry("https://example.test")

        self.assertIs(expected, response)
        self.assertEqual(3, post.call_count)
        self.assertEqual([1, 2], [call.args[0] for call in sleep.call_args_list])

    @patch("AgentRecord.ai_client.time.sleep")
    @patch("AgentRecord.ai_client.requests.post")
    def test_transient_server_errors_use_bounded_retry(self, post, sleep):
        unavailable = FakeResponse({})
        unavailable.status_code = 503
        expected = FakeResponse({"ok": True})
        post.side_effect = [unavailable, unavailable, expected]

        response = ai_client._post_with_transient_retry("https://example.test")

        self.assertIs(expected, response)
        self.assertEqual(3, post.call_count)
        self.assertEqual([1, 2], [call.args[0] for call in sleep.call_args_list])

    @patch("AgentRecord.ai_client.time.sleep")
    @patch("AgentRecord.ai_client.requests.post")
    def test_exhausted_connection_errors_are_marked_as_network_failure(
        self, post, sleep
    ):
        post.side_effect = ai_client.requests.ConnectionError("dns")

        message, success, _, _, _ = ai_client.call_ai("自动任务", self.model)

        self.assertFalse(success)
        self.assertTrue(ai_client.is_network_failure(message))
        self.assertEqual(3, post.call_count)
        self.assertEqual([1, 2], [call.args[0] for call in sleep.call_args_list])

    @patch("AgentRecord.ai_client.time.sleep")
    @patch("AgentRecord.ai_client.requests.post")
    def test_rate_limit_and_auth_errors_are_classified_separately(self, post, sleep):
        rate_limited = FakeResponse({})
        rate_limited.status_code = 429
        post.return_value = rate_limited

        message, success, _, _, _ = ai_client.call_ai("自动任务", self.model)
        self.assertFalse(success)
        self.assertTrue(ai_client.is_rate_limit_failure(message))
        self.assertEqual(3, post.call_count)

        post.reset_mock()
        unauthorized = FakeResponse({})
        unauthorized.status_code = 401
        post.return_value = unauthorized
        message, success, _, _, _ = ai_client.call_ai("自动任务", self.model)
        self.assertFalse(success)
        self.assertTrue(ai_client.is_config_failure(message))
        self.assertEqual(1, post.call_count)


if __name__ == "__main__":
    unittest.main()
