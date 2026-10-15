"""七牛云 API 凭证校验与 endpoint 规范化。

本模块刻意 **不依赖** ``dify_plugin``，只依赖 ``requests`` 与标准库，
因此可以脱离插件运行时被单元测试直接导入。

为什么不再用具体模型做校验
--------------------------
此前的实现固定使用 ``deepseek-v3`` 调用一次 ``chat/completions`` 来验证
凭证。一旦该模型被平台下线，**所有用户在保存凭证时都会失败**，而修复
方式只能是再挑一个模型——也就是说问题会随下一次模型下线而复现。

七牛云网关的执行顺序是「**先鉴权、后路由**」：即使请求的是一个不存在的
模型，凭证无效时也会**先**返回 401 鉴权错误（实测：无效 key + 不存在的
模型 → ``401 auth_api_key_invalid``）。因此可以用一个「刻意不存在的模型名」
作为探针：

* 探针返回**显式鉴权错误** -> key 无效，**拒绝保存**（唯一会阻止保存的情况）
* 网络异常 / 超时。         -> 放行（只记警告）
* endpoint 路径可疑。       -> 放行（只记警告）
* 其它任何响应。           -> 放行

设计原则：**校验是尽力而为的，绝不因为外部因素阻止用户保存凭证**。
只要不是「网关明确告知这个 key 无效」，一律放行——断网、网关抖动、
5xx 都不应该让用户配置不了插件。

整个校验过程**不引用任何具体模型**，所以不会因为模型上下线而失效。
"""

import logging
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)

#: 默认 API 基础地址（OpenAI 兼容协议，需带版本前缀）
DEFAULT_ENDPOINT_URL = "https://openai.qiniu.com/v1"

#: 刻意使用一个绝不存在的模型名。
#: 网关先鉴权后路由，因此无效凭证会返回鉴权错误，有效凭证则返回模型不存在。
PROBE_MODEL_NAME = "__qiniu_dify_credentials_probe__"

#: 明确表示「凭证本身无效」的错误码（比较前会转小写）
AUTH_FAILURE_CODES = frozenset(
    {
        "auth_api_key_missing",
        "auth_api_key_invalid",
        "auth_api_key_disabled",
        "auth_api_key_expired",
        "invalid_api_key",
    }
)

#: 明确表示「凭证本身无效」的错误类型（比较前会转小写）
AUTH_FAILURE_TYPES = frozenset({"authentication_error"})

#: 网关自身「路由不存在」时返回的 error_msg 特征
ROUTE_NOT_FOUND_MARKERS = ("route not found",)

#: 探测请求超时（连接, 读取）
PROBE_TIMEOUT = (10, 30)

#: 用户可读的失败原因。
#: 注意：以下只有 ``MSG_MISSING_API_KEY`` / ``MSG_INVALID_API_KEY``
#: 会阻止保存凭证，其余情况一律放行（仅记日志）。
MSG_MISSING_API_KEY = (
    "API Key 不能为空，请填写七牛云 API Key / API key is required"
)
MSG_INVALID_API_KEY = (
    "API Key 无效或已失效，请检查后重试 / "
    "Invalid API key, please check your credentials"
)


class CredentialProbeError(Exception):
    """凭证探针校验失败。

    与 ``dify_plugin`` 解耦，由调用方转换成 ``CredentialsValidateFailedError``，
    以便本模块可以独立测试。
    """


def normalize_endpoint_url(raw_endpoint: str | None) -> str:
    """把用户填写的 endpoint 规范化为可直接拼接 ``chat/completions`` 的基础地址。

    兼容用户可能输入的多种形态：

    * ``openai.qiniu.com``                  -> ``https://openai.qiniu.com/v1``
    * ``https://openai.qiniu.com``          -> ``https://openai.qiniu.com/v1``
    * ``https://openai.qiniu.com/``         -> ``https://openai.qiniu.com/v1``
    * ``https://openai.qiniu.com/v1/``      -> ``https://openai.qiniu.com/v1``
    * ``https://openai.qiniu.com/v1/chat/completions``
                                            -> ``https://openai.qiniu.com/v1``
    * ``https://my-proxy.example.com/openai/v1`` -> 原样保留

    只有在路径为空或 ``/`` 时才补 ``/v1``；自定义网关的路径一律原样保留，
    避免破坏自建代理。
    """
    url = (raw_endpoint or "").strip()
    if not url:
        return DEFAULT_ENDPOINT_URL

    if "://" not in url:
        url = f"https://{url}"

    # 用户常常直接粘贴完整的接口地址，这里把已知后缀去掉
    lowered = url.lower()
    for suffix in ("/chat/completions", "/completions"):
        if lowered.endswith(suffix):
            url = url[: -len(suffix)]
            break

    url = url.rstrip("/")

    parsed = urlsplit(url)
    if parsed.path in ("", "/"):
        url = f"{url}/v1"

    return url


def _join_url(base_url: str, path: str) -> str:
    """拼接 base URL 与子路径。"""
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _parse_json_object(response: requests.Response) -> dict:
    """尽力把响应体解析成 JSON 对象，失败时返回空字典。"""
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _is_auth_failure(response: requests.Response) -> bool:
    """判断响应是否明确表示「凭证无效」。

    只有拿到确凿的鉴权信号才返回 True，避免把「余额不足 / 无权限」等
    其它 403 误判成 key 无效。
    """
    if response.status_code == requests.codes.unauthorized:
        return True

    error = _parse_json_object(response).get("error")
    if not isinstance(error, dict):
        return False

    code = str(error.get("code") or "").strip().lower()
    if code in AUTH_FAILURE_CODES:
        return True

    error_type = str(error.get("type") or "").strip().lower()
    return error_type in AUTH_FAILURE_TYPES


def _is_route_not_found(response: requests.Response) -> bool:
    """判断响应是否表示「网关没有这个路由」，即 endpoint 路径配错了。

    网关路由 404 返回 ``{"error_msg": "404 Route Not Found"}``，**没有**
    OpenAI 风格的 ``error`` 对象。这里要求两者同时成立，避免把
    「模型不存在」一类的业务 404 误判成地址错误。
    """
    if response.status_code != requests.codes.not_found:
        return False

    payload = _parse_json_object(response)
    if "error" in payload:
        return False

    error_msg = str(payload.get("error_msg") or "").strip().lower()
    return any(marker in error_msg for marker in ROUTE_NOT_FOUND_MARKERS)


def _looks_like_api_response(response: requests.Response) -> bool:
    """判断响应是否来自一个真正的 OpenAI 兼容 API。

    有效凭证的探针请求会得到「模型不存在」一类的业务错误，这些响应一定是
    JSON 对象。如果拿到的是 HTML（例如把 endpoint 填成了某个网站首页），
    即便状态码是 2xx/4xx 也说明地址配错了。5xx 视为服务端异常但地址正确。
    """
    if response.status_code >= requests.codes.internal_server_error:
        return True

    try:
        payload = response.json()
    except ValueError:
        return False

    return isinstance(payload, (dict, list))


def validate_api_credentials(
    api_key: str | None,
    endpoint_url: str | None = None,
    session: requests.Session | None = None,
) -> str:
    """**尽力而为**地校验七牛云凭证，返回规范化后的 endpoint。

    只发起一次探测请求：``POST {base}/chat/completions``，模型名使用
    ``PROBE_MODEL_NAME``。

    与常见实现不同，这里**不会因为外部因素阻止保存凭证**：

    * 网关明确返回鉴权错误 -> 抛 ``CredentialProbeError``（key 真的无效）
    * 网络异常 / 超时。      -> 放行，仅记警告
    * endpoint 路径可疑。    -> 放行，仅记警告
    * 其它任何响应。        -> 放行

    Args:
        api_key: 用户填写的 API Key。
        endpoint_url: 用户填写的 API 基础地址，可为空。
        session: 可注入的 requests 会话，便于测试。

    Returns:
        规范化后的 endpoint 地址。

    Raises:
        CredentialProbeError: 仅当 API Key 为空，或网关明确拒绝该 key 时。
    """
    key = (api_key or "").strip()
    if not key:
        raise CredentialProbeError(MSG_MISSING_API_KEY)

    base_url = normalize_endpoint_url(endpoint_url)
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    owns_session = session is None
    session = session or requests.Session()
    try:
        try:
            response = session.post(
                _join_url(base_url, "chat/completions"),
                headers=headers,
                json={
                    "model": PROBE_MODEL_NAME,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                },
                timeout=PROBE_TIMEOUT,
            )
        except requests.RequestException as ex:
            # 网络层失败属于外部因素，按「绝不阻断」策略放行
            logger.warning(
                "Qiniu credentials probe could not reach %s (%s); "
                "accepting credentials anyway (best-effort validation)",
                base_url,
                ex,
            )
            return base_url

        if _is_auth_failure(response):
            # 唯一会阻止保存的情况：网关明确告知这个 key 无效
            raise CredentialProbeError(MSG_INVALID_API_KEY)

        if _is_route_not_found(response):
            logger.warning(
                "Qiniu endpoint %s looks like a wrong path (route not found); "
                "accepting credentials anyway. Expected base URL such as %s",
                base_url,
                DEFAULT_ENDPOINT_URL,
            )
            return base_url

        if not _looks_like_api_response(response):
            logger.warning(
                "Qiniu endpoint %s did not return an API response "
                "(status=%s); accepting credentials anyway. "
                "Expected base URL such as %s",
                base_url,
                response.status_code,
                DEFAULT_ENDPOINT_URL,
            )
            return base_url

        # 其余响应（模型不存在的 400·404 / 无权限的 403 / 5xx）
        # 都说明 key 已通过网关鉴权，视为凭证有效。
        logger.info(
            "Qiniu credentials validated via model-agnostic probe "
            "(endpoint=%s, status=%s)",
            base_url,
            response.status_code,
        )
        return base_url
    finally:
        if owns_session:
            session.close()
