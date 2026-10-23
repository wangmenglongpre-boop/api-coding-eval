"""Framework tests must never access the developer's real OS credentials."""

import pytest

from coding_eval import credentials

_native_secure_backend = credentials._secure_backend


@pytest.fixture
def native_secure_backend():
    return _native_secure_backend


@pytest.fixture(autouse=True)
def isolate_os_credentials(monkeypatch):
    def unavailable():
        raise credentials.CredentialError("测试环境禁用真实系统钥匙串。")

    monkeypatch.setattr(credentials, "_secure_backend", unavailable)
