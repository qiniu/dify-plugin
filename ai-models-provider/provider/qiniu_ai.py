import logging

from dify_plugin import ModelProvider
from dify_plugin.errors.model import CredentialsValidateFailedError

from qiniu_credentials import (
    DEFAULT_ENDPOINT_URL,
    CredentialProbeError,
    validate_api_credentials,
)

logger = logging.getLogger(__name__)


class QiniuProvider(ModelProvider):
    """
    七牛云 AI 模型提供商实现类

    提供七牛云 AI 模型的接入能力，支持多种大语言模型
    """

    def validate_provider_credentials(self, credentials: dict) -> None:
        """
        验证供应商认证信息
        如果验证失败，会抛出异常

        校验过程**不依赖任何具体模型**：七牛云网关先鉴权、后路由，
        因此用一个刻意不存在的模型名做探针即可判断 key 是否有效。
        这样可以避免因某个模型（例如曾经的 deepseek-v3）下架而导致
        所有用户无法保存凭证。

        Args:
            credentials: 供应商认证信息，格式由 `provider_credential_schema` 定义

        Raises:
            CredentialsValidateFailedError: 认证验证失败
        """
        try:
            endpoint_url = validate_api_credentials(
                api_key=credentials.get("api_key"),
                endpoint_url=credentials.get("endpoint_url") or DEFAULT_ENDPOINT_URL,
            )
        except CredentialProbeError as ex:
            raise CredentialsValidateFailedError(str(ex)) from ex
        except Exception as ex:  # noqa: BLE001 - 兜底并记录，避免泄漏原始异常
            logger.exception("qiniu credentials validate failed")
            raise CredentialsValidateFailedError(str(ex)) from ex

        # 回写规范化后的地址，保证后续调用使用一致的基础地址
        credentials["endpoint_url"] = endpoint_url
