"""``ai-models-provider`` 凭证校验的回归测试。

重点守护两个历史事故：

1. 插件曾经硬编码用 ``deepseek-v3`` 调用一次真实模型来验证凭证，该模型
   下架后**所有用户都无法保存凭证**。现在校验完全与具体模型无关。
2. 凭证校验**不得因为外部因素阻止用户保存**。只有「网关明确告知 key 无效」
   才是拒绝理由；断网、超时、路径可疑、5xx 一律放行。

运行方式（无需安装额外依赖）::

    python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

import requests

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "ai-models-provider"
PROVIDER_DIR = PLUGIN_DIR / "provider"
sys.path.insert(0, str(PLUGIN_DIR))

from qiniu_credentials import (  # noqa: E402
    DEFAULT_ENDPOINT_URL,
    MSG_INVALID_API_KEY,
    MSG_MISSING_API_KEY,
    PROBE_MODEL_NAME,
    CredentialProbeError,
    normalize_endpoint_url,
    validate_api_credentials,
)


class FakeResponse:
    """最小化的 requests.Response 替身。"""

    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (str(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """记录请求并返回预设响应的假会话。"""

    def __init__(self, response=None, exception=None):
        self.response = response
        self.exception = exception
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        if self.exception is not None:
            raise self.exception
        return self.response

    def close(self):
        pass


def auth_error_response(code="auth_api_key_invalid"):
    return FakeResponse(
        401,
        {
            "error": {
                "message": "Invalid API key",
                "type": "authentication_error",
                "code": code,
            }
        },
    )


class NormalizeEndpointUrlTests(unittest.TestCase):
    def test_empty_falls_back_to_default(self):
        self.assertEqual(normalize_endpoint_url(None), DEFAULT_ENDPOINT_URL)
        self.assertEqual(normalize_endpoint_url("   "), DEFAULT_ENDPOINT_URL)

    def test_bare_host_gets_scheme_and_version(self):
        self.assertEqual(normalize_endpoint_url("openai.qiniu.com"), DEFAULT_ENDPOINT_URL)

    def test_trailing_slash_removed(self):
        self.assertEqual(
            normalize_endpoint_url("https://openai.qiniu.com/"), DEFAULT_ENDPOINT_URL
        )
        self.assertEqual(
            normalize_endpoint_url("https://openai.qiniu.com/v1/"),
            DEFAULT_ENDPOINT_URL,
        )

    def test_full_completions_url_is_trimmed(self):
        self.assertEqual(
            normalize_endpoint_url("https://openai.qiniu.com/v1/chat/completions"),
            DEFAULT_ENDPOINT_URL,
        )

    def test_custom_path_preserved(self):
        # 自建代理路径不应被改写
        self.assertEqual(
            normalize_endpoint_url("https://proxy.example.com/openai/v1"),
            "https://proxy.example.com/openai/v1",
        )


class ValidateCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.key = "sk-test-key"

    def test_missing_api_key_is_rejected_without_any_request(self):
        session = FakeSession()
        with self.assertRaises(CredentialProbeError) as ctx:
            validate_api_credentials("", DEFAULT_ENDPOINT_URL, session=session)
        self.assertEqual(str(ctx.exception), MSG_MISSING_API_KEY)
        self.assertEqual(session.calls, [], "空 key 不应发起网络请求")

    def test_invalid_api_key_is_rejected(self):
        session = FakeSession(response=auth_error_response())
        with self.assertRaises(CredentialProbeError) as ctx:
            validate_api_credentials(self.key, DEFAULT_ENDPOINT_URL, session=session)
        self.assertEqual(str(ctx.exception), MSG_INVALID_API_KEY)

    def test_invalid_key_is_rejected_even_when_model_is_unknown(self):
        """核心保证：key 无效时必须报错，即便探针模型本身不存在。"""
        session = FakeSession(response=auth_error_response("auth_api_key_missing"))
        with self.assertRaises(CredentialProbeError) as ctx:
            validate_api_credentials(self.key, DEFAULT_ENDPOINT_URL, session=session)
        self.assertEqual(str(ctx.exception), MSG_INVALID_API_KEY)

    def test_model_not_found_means_key_is_valid(self):
        """有效 key + 不存在的模型 -> 凭证通过（这正是探针的原理）。"""
        response = FakeResponse(
            404,
            {"error": {"message": "Model not found", "code": "model_not_found"}},
        )
        session = FakeSession(response=response)
        endpoint = validate_api_credentials(
            self.key, DEFAULT_ENDPOINT_URL, session=session
        )
        self.assertEqual(endpoint, DEFAULT_ENDPOINT_URL)

    def test_successful_probe_passes(self):
        session = FakeSession(response=FakeResponse(200, {"object": "chat.completion"}))
        self.assertEqual(
            validate_api_credentials(self.key, DEFAULT_ENDPOINT_URL, session=session),
            DEFAULT_ENDPOINT_URL,
        )

    def test_non_auth_403_is_not_treated_as_invalid_key(self):
        """余额不足等 403 不应被误判成 key 无效。"""
        response = FakeResponse(
            403,
            {"error": {"message": "Insufficient balance", "code": "insufficient_quota"}},
        )
        session = FakeSession(response=response)
        validate_api_credentials(self.key, DEFAULT_ENDPOINT_URL, session=session)

    def test_route_not_found_still_accepts_credentials(self):
        """路由 404 属于地址可疑，按「绝不阻断」策略放行（仅记警告）。"""
        response = FakeResponse(404, {"error_msg": "404 Route Not Found"})
        session = FakeSession(response=response)
        bad_endpoint = "https://openai.qiniu.com/bogus"
        with self.assertLogs("qiniu_credentials", level="WARNING") as logs:
            result = validate_api_credentials(self.key, bad_endpoint, session=session)
        self.assertEqual(result, bad_endpoint)
        self.assertTrue(any("route not found" in m for m in logs.output))

    def test_html_response_still_accepts_credentials(self):
        """endpoint 指向普通网站时也放行，仅记警告；不能挡住用户保存。"""
        response = FakeResponse(200, payload=None, text="<!DOCTYPE html><html>...")
        session = FakeSession(response=response)
        with self.assertLogs("qiniu_credentials", level="WARNING") as logs:
            result = validate_api_credentials(
                self.key, "https://example.com", session=session
            )
        # 空路径会被补成 /v1，这是 endpoint 规范化的既有行为
        self.assertEqual(result, "https://example.com/v1")
        self.assertTrue(any("did not return an API response" in m for m in logs.output))

    def test_network_error_still_accepts_credentials(self):
        """断网 / 超时属于外部因素，绝不能因此让用户存不了凭证。"""
        reason = "dns failure"
        endpoint = "https://unreachable.invalid/v1"
        session = FakeSession(exception=requests.ConnectionError(reason))
        with self.assertLogs("qiniu_credentials", level="WARNING") as logs:
            result = validate_api_credentials(self.key, endpoint, session=session)
        self.assertEqual(result, endpoint)
        self.assertTrue(any("could not reach" in m for m in logs.output))

    def test_timeout_still_accepts_credentials(self):
        session = FakeSession(exception=requests.Timeout("read timed out"))
        result = validate_api_credentials(
            self.key, DEFAULT_ENDPOINT_URL, session=session
        )
        self.assertEqual(result, DEFAULT_ENDPOINT_URL)

    def test_server_error_still_accepts_credentials(self):
        session = FakeSession(
            response=FakeResponse(503, {"error": {"message": "gateway down"}})
        )
        self.assertEqual(
            validate_api_credentials(self.key, DEFAULT_ENDPOINT_URL, session=session),
            DEFAULT_ENDPOINT_URL,
        )

    def test_probe_uses_unknown_model_and_never_a_real_one(self):
        session = FakeSession(response=FakeResponse(200, {"object": "chat.completion"}))
        validate_api_credentials(self.key, DEFAULT_ENDPOINT_URL, session=session)
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(call["json"]["model"], PROBE_MODEL_NAME)
        self.assertTrue(call["url"].endswith("/chat/completions"))
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {self.key}")

    def test_probe_model_name_is_not_a_known_model(self):
        self.assertTrue(PROBE_MODEL_NAME.startswith("__"))
        for token in ("deepseek", "gpt", "claude", "qwen", "glm", "kimi"):
            self.assertNotIn(token, PROBE_MODEL_NAME.lower())


class NeverBlocksSavingTests(unittest.TestCase):
    """核心承诺：除「key 无效」外，任何情况都不能阻止用户保存凭证。"""

    def setUp(self):
        self.key = "sk-test-key"

    def test_only_explicit_auth_failure_blocks_saving(self):
        scenarios = [
            ("model not found", FakeResponse(404, {"error": {"message": "no model"}})),
            ("forbidden quota", FakeResponse(403, {"error": {"code": "insufficient_quota"}})),
            ("rate limited", FakeResponse(429, {"error": {"message": "slow down"}})),
            ("bad request", FakeResponse(400, {"error": {"message": "bad input"}})),
            ("server error", FakeResponse(500, {"error": {"message": "boom"}})),
            ("bad gateway", FakeResponse(502, {"error": {"message": "boom"}})),
            ("route not found", FakeResponse(404, {"error_msg": "404 Route Not Found"})),
            ("html page", FakeResponse(200, payload=None, text="<html></html>")),
            ("unknown json", FakeResponse(200, {"weird": "payload"})),
        ]
        for label, response in scenarios:
            with self.subTest(scenario=label):
                session = FakeSession(response=response)
                result = validate_api_credentials(
                    self.key, DEFAULT_ENDPOINT_URL, session=session
                )
                self.assertEqual(result, DEFAULT_ENDPOINT_URL)

    def test_network_failures_never_block_saving(self):
        errors = [
            requests.ConnectionError("dns"),
            requests.Timeout("timeout"),
            requests.exceptions.SSLError("tls"),
            requests.RequestException("other"),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                session = FakeSession(exception=error)
                result = validate_api_credentials(
                    self.key, DEFAULT_ENDPOINT_URL, session=session
                )
                self.assertEqual(result, DEFAULT_ENDPOINT_URL)

    def test_only_two_error_messages_can_be_raised(self):
        """可被抛出的错误消息应当只有「key 为空」与「key 无效」两种。"""
        raised = set()
        probes = [
            ("", DEFAULT_ENDPOINT_URL, FakeSession()),
            (self.key, DEFAULT_ENDPOINT_URL, FakeSession(response=auth_error_response())),
        ]
        for api_key, endpoint, session in probes:
            with self.assertRaises(CredentialProbeError) as ctx:
                validate_api_credentials(api_key, endpoint, session=session)
            raised.add(str(ctx.exception))
        self.assertEqual(raised, {MSG_MISSING_API_KEY, MSG_INVALID_API_KEY})


class NoHardcodedModelRegressionTests(unittest.TestCase):
    """守护历史事故：provider 不应再硬编码任何真实模型名。"""

    #: 已被平台下架、绝不能再出现在校验逻辑里的模型
    RETIRED_MODELS = ("deepseek-v3",)

    def _source(self, path):
        return path.read_text(encoding="utf-8")

    def test_provider_does_not_hardcode_retired_models(self):
        source = self._source(PROVIDER_DIR / "qiniu_ai.py")
        for model in self.RETIRED_MODELS:
            self.assertNotIn(
                f'"{model}"',
                source,
                f"provider 不应再硬编码模型 {model!r}，否则该模型下架后会再次失败",
            )

    def test_credentials_module_does_not_hardcode_retired_models(self):
        source = self._source(PLUGIN_DIR / "qiniu_credentials.py")
        for model in self.RETIRED_MODELS:
            self.assertNotIn(f'"{model}"', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
