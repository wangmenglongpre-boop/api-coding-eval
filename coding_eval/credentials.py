"""OS-managed credentials, kept outside evaluation storage and exports."""

from __future__ import annotations

import ipaddress
import os
import sys
from urllib.parse import urlsplit

from .models import ModelConfig, content_hash

SERVICE = "coding-eval.model-api"


class CredentialError(ValueError):
    """Messages are fixed: backend exceptions can contain sensitive values."""


def credential_account(config: ModelConfig) -> str:
    # Bind to the entire declared endpoint, protocol and configuration ID.
    # Parameter changes retain credentials; changing destinations requires a save.
    return content_hash([config.id, config.protocol, config.url_mode, config.url.rstrip("/")])


def _secure_backend():
    # Select the OS backend directly, never keyring's configurable fallback chain.
    if sys.platform != "darwin":
        raise CredentialError("当前环境未支持系统钥匙串，请使用会话密钥或环境变量。")
    try:
        from keyring.backends.macOS import Keyring

        return Keyring()
    except Exception:
        raise CredentialError("系统钥匙串不可用，请检查 keyring 安装及系统权限。") from None


class CredentialStore:
    def get(self, config: ModelConfig) -> str | None:
        try:
            backend = _secure_backend()
        except CredentialError:
            return None
        try:
            return backend.get_password(SERVICE, credential_account(config))
        except Exception:
            raise CredentialError("无法读取系统钥匙串，请解锁钥匙串或重新设置密钥。") from None

    def save(self, config: ModelConfig, secret: str) -> None:
        if not secret or not secret.strip():
            raise CredentialError("请先输入密钥，或设置当前会话密钥。")
        backend = _secure_backend()
        try:
            backend.set_password(SERVICE, credential_account(config), secret)
        except Exception:
            raise CredentialError("密钥未保存：请检查系统钥匙串权限后重试。") from None

    def delete(self, config: ModelConfig) -> None:
        backend = _secure_backend()
        try:
            if backend.get_password(SERVICE, credential_account(config)) is not None:
                backend.delete_password(SERVICE, credential_account(config))
        except Exception:
            raise CredentialError("密钥未删除：请检查系统钥匙串权限后重试。") from None


def credential_required(config: ModelConfig) -> bool:
    if config.api_key_env or config.capabilities.get("api_key_required") is True:
        return True
    if config.capabilities.get("api_key_required") is False:
        return False
    host = (urlsplit(config.url).hostname or "").lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".invalid", ".test")):
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


def resolve_credential(config: ModelConfig, explicit: str | None = None) -> str | None:
    """Explicit session input > OS keychain > named environment variable."""
    if explicit is not None:
        return explicit
    return CredentialStore().get(config) or (
        os.environ.get(config.api_key_env) if config.api_key_env else None
    )


def require_credential(config: ModelConfig, secret: str | None) -> None:
    if credential_required(config) and not secret:
        raise CredentialError(
            f"模型 {config.id} 缺少 API 密钥；请在模型配置页保存到系统钥匙串或设置会话密钥。"
        )
