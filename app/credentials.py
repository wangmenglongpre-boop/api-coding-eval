"""Session credentials bound to their model destination, never persisted."""

from collections.abc import Sequence

import streamlit as st

from coding_eval.credentials import credential_account, require_credential, resolve_credential
from coding_eval.models import ModelConfig


def session_key(config: ModelConfig) -> str | None:
    value = st.session_state.get("api_keys", {}).get(config.id)
    scopes = st.session_state.setdefault("_api_key_scopes", {})
    # Retain existing live sessions during this upgrade.
    if value and config.id not in scopes:
        scopes[config.id] = credential_account(config)
    return value if scopes.get(config.id) == credential_account(config) else None


def resolved_keys(models: Sequence[ModelConfig]) -> dict[str, str]:
    keys = {}
    for model in models:
        value = resolve_credential(model, session_key(model))
        require_credential(model, value)
        if value is not None:
            keys[model.id] = value
    return keys
