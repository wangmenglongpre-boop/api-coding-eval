"""Model configuration and explicitly authorized, separately recorded probes."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import streamlit as st

from coding_eval.api import APIClient, MAPPING_VERSIONS, build_request, resolve_url
from coding_eval.credentials import CredentialError, CredentialStore, credential_account
from coding_eval.models import ModelConfig, new_id
from coding_eval.storage import AppPaths, Storage
from app.credentials import resolved_keys, session_key
from coding_eval.ui_queries import current_models

PRODUCT_UI_REVISION = "product-v1"


def _latest_models(storage: Storage) -> dict[str, ModelConfig]:
    return {model.id: model for model in current_models(storage)}


def _json_dict(text: str) -> dict[str, Any]:
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("必须填写 JSON 对象")
    return value


def _save_config(storage: Storage, config: ModelConfig) -> None:
    payload = config.model_dump(mode="json")
    encoded = json.dumps(payload, ensure_ascii=False)
    secrets = list(st.session_state.get("api_keys", {}).values())
    secrets.append(CredentialStore().get(config))
    for model in _latest_models(storage).values():
        secrets.append(CredentialStore().get(model))
    if config.api_key_env:
        secrets.append(os.environ.get(config.api_key_env))
    if any(secret and secret in encoded for secret in secrets):
        raise ValueError("配置中包含当前密钥，请移除明文密钥并仅填写环境变量名称")
    # Disabled models are editable, but their request options must still validate.
    build_request(config.model_copy(update={"enabled": True}), "", "")
    version = storage.save_object("model", config.id, payload)
    latest = storage.get_object("model", config.id)
    if latest is None or ModelConfig.model_validate(latest).version_hash != version:
        raise ValueError("存储未将已有历史版本设为当前版本，请由主模块修复版本选择后重试")


def _set_session_key(config: ModelConfig, clear: bool = False) -> None:
    keys = st.session_state.setdefault("api_keys", {})
    value = st.session_state.pop("_model_key_input", "")
    if clear:
        keys.pop(config.id, None)
        st.session_state.setdefault("_api_key_scopes", {}).pop(config.id, None)
    elif value:
        keys[config.id] = value
        st.session_state.setdefault("_api_key_scopes", {})[config.id] = credential_account(config)


def _persist_key(config: ModelConfig, delete: bool = False) -> None:
    value = st.session_state.pop("_model_key_input", "") or session_key(config)
    try:
        if delete:
            CredentialStore().delete(config)
            _set_session_key(config, clear=True)
        else:
            CredentialStore().save(config, value or "")
            # Avoid a stale session override after replacing the saved credential.
            _set_session_key(config, clear=True)
    except CredentialError as exc:
        # Preserve a new input on failure so the user can retry without retyping.
        if value and not delete:
            st.session_state.setdefault("api_keys", {})[config.id] = value
            st.session_state.setdefault("_api_key_scopes", {})[config.id] = credential_account(config)
        st.session_state["_credential_notice"] = ("error", str(exc))
    else:
        st.session_state["_credential_notice"] = (
            "success", "已删除此配置的系统钥匙串密钥和会话密钥。" if delete
            else "已保存到系统钥匙串，刷新页面或重启应用后可继续使用。",
        )


async def _probe(config: ModelConfig, api_key: str | None, modes: list[str]) -> list[dict[str, Any]]:
    client = APIClient()
    results = []
    probe_id = "probe-" + new_id()
    for index, mode in enumerate(modes):
        stream = mode == "流式"
        candidate = config.model_copy(update={
            "capabilities": {**config.capabilities, "stream": stream},
        })
        result = await client.generate(
            candidate,
            "You are a connection probe. Reply only with OK.",
            "Reply with OK.",
            api_key=api_key, run_id=probe_id, sample_index=index, purpose="probe",
        )
        results.append({
            "config_hash": config.version_hash,
            "request_config_hash": candidate.version_hash,
            "mode": mode,
            "text_response": bool(result.raw_output),
            "stream_response": (
                result.metrics.stream_complete if stream and result.status == "completed" else None
            ),
            "usage_observed": bool(result.metrics.raw_usage),
            "input_usage_observed": result.metrics.input_tokens is not None,
            "output_usage_observed": result.metrics.output_tokens is not None,
            "result": result.model_dump(mode="json"),
        })
    return results


def render(storage: Storage, paths: AppPaths) -> None:
    """Render without making requests unless the probe submit button is clicked."""
    st.session_state.setdefault("api_keys", {})
    st.caption("直接调用原始 API；支持系统钥匙串、会话密钥及环境变量，密钥不写入评测数据库、日志和导出。")
    notice = st.session_state.pop("_credential_notice", None)
    if notice:
        getattr(st, notice[0])(notice[1])
    st.info(
        "首 token 指首个非空生成内容事件（包括公开返回的推理内容），"
        "同时记录首最终回答时间；不保存推理正文。非流式首 token 不可测。"
    )
    models = _latest_models(storage)
    if models:
        st.dataframe([
            {
                "ID": model.id, "显示名": model.display_name, "协议": model.protocol,
                "服务商": model.provider, "model ID": model.model_id,
                "可见版本": model.visible_version, "启用": model.enabled,
                "配置哈希": model.version_hash,
            }
            for model in models.values()
        ], use_container_width=True)
    selection = st.selectbox(
        "新建或编辑模型", ["", *models],
        format_func=lambda value: "新建模型" if not value else f"{models[value].display_name} ({value})",
        key="model_editor_selection",
    )
    current = models.get(selection)
    editor_key = current.id + "-" + current.version_hash if current else "new"
    with st.form("model_config_" + editor_key):
        model_id = st.text_input("配置 ID（保存后不可修改）", value=current.id if current else "", disabled=bool(current))
        display_name = st.text_input("显示名称", value=current.display_name if current else "")
        protocol = st.selectbox("协议", ["openai", "anthropic"], index=1 if current and current.protocol == "anthropic" else 0)
        url_mode = st.selectbox(
            "URL 模式", ["base", "full"], index=1 if current and current.url_mode == "full" else 0,
            help="base 自动补协议路径；full 原样使用完整请求 URL。",
        )
        url = st.text_input("Base URL 或完整请求 URL", value=current.url if current else "")
        exact_model = st.text_input("精确 model ID", value=current.model_id if current else "")
        provider = st.text_input("服务商", value=current.provider if current else "unknown")
        visible_version = st.text_input("可见模型版本", value=current.visible_version if current else "unknown")
        api_key_env = st.text_input(
            "API key 环境变量名称（不是密钥值）", value=current.api_key_env or "" if current else "",
            help="例如 OPENAI_API_KEY；当前会话密钥在保存配置后单独设置。",
        )
        timeout = st.number_input(
            "请求总超时（秒）", min_value=0.1,
            value=float(current.timeout_seconds) if current else 120.0,
        )
        max_tokens = st.number_input(
            "最大输出 token", min_value=1, value=current.max_output_tokens if current else 4096,
            help="OpenAI 使用下方所选字段；Anthropic 固定使用 max_tokens。",
        )
        max_tokens_field = st.selectbox(
            "OpenAI 输出限制字段", ["max_tokens", "max_completion_tokens"],
            index=1 if current and current.max_tokens_field == "max_completion_tokens" else 0,
            help="按网关或模型要求选择；不自动重试切换字段。Anthropic 忽略此选择。",
        )
        enabled = st.checkbox("启用此模型", value=current.enabled if current else True)
        stream = st.checkbox("使用流式响应", value=current.capabilities.get("stream") is not False if current else True)
        stream_usage = st.checkbox(
            "请求最终流式 usage（OpenAI stream_options）",
            value=current.capabilities.get("stream_usage") is not False if current else True,
        )
        parameters = st.text_area(
            "采样与思考参数（JSON）", value=json.dumps(current.parameters if current else {}, indent=2),
            help='例如 {"temperature": 0.2}；按服务支持填写，未知参数会原样发送并由服务明确校验。',
        )
        capabilities = st.text_area(
            "其他能力声明（JSON，值为 true / false / null）",
            value=json.dumps({
                key: value for key, value in (current.capabilities if current else {}).items()
                if key not in {"stream", "stream_usage"}
            }, indent=2),
            help="将参数名设为 false 会阻止发送；null 表示未知，不能视为已经对齐。",
        )
        st.caption("不支持用参数覆盖 model、messages、system、stream、输出限制或注入工具/历史。")
        save = st.form_submit_button("保存配置")
    if save:
        try:
            caps = _json_dict(capabilities)
            if any(value is not None and not isinstance(value, bool) for value in caps.values()):
                raise ValueError("能力声明的值必须为 true、false 或 null")
            config = ModelConfig(
                id=model_id, display_name=display_name, protocol=protocol,
                url=url, url_mode=url_mode, model_id=exact_model,
                api_key_env=api_key_env.strip() or None, provider=provider or "unknown",
                visible_version=visible_version or "unknown", enabled=enabled,
                timeout_seconds=timeout, max_output_tokens=int(max_tokens),
                max_tokens_field=max_tokens_field,
                parameters=_json_dict(parameters),
                capabilities={**caps, "stream": stream, "stream_usage": stream_usage},
                usage_mapping_version=MAPPING_VERSIONS[protocol],
            )
            _save_config(storage, config)
        except (ValueError, TypeError):
            # Pydantic/JSON errors can echo a pasted key, so do not render them.
            st.error("配置保存失败：请检查必填项、URL、JSON、核心参数冲突及密钥字段；不支持回选已有历史版本。")
        else:
            st.success("配置已保存。请在上方选择该模型以设置会话密钥或运行探针。")
            st.rerun()

    if not current:
        return
    st.subheader("当前配置")
    st.code(resolve_url(current), language=None)
    st.caption(f"配置哈希：{current.version_hash}；usage 映射：{MAPPING_VERSIONS[current.protocol]}")
    if st.checkbox("查看当前配置 JSON", key=f"config-json-{current.id}"):
        st.json(current.model_dump(mode="json"))
    if current.enabled and st.button("禁用此模型", key="disable_" + current.id):
        try:
            _save_config(storage, current.model_copy(update={"enabled": False}))
        except ValueError:
            st.error("禁用失败，存储未切换到该配置版本。")
        else:
            st.rerun()

    st.subheader("API 密钥")
    st.caption("优先使用会话密钥，其次系统钥匙串，最后环境变量。保存到系统钥匙串后，刷新或重启无需重新输入。")
    try:
        stored_key = CredentialStore().get(current)
    except CredentialError as exc:
        stored_key = None
        st.error(str(exc))
    if session_key(current):
        st.success("当前会话已设置密钥（不显示明文）。")
    if stored_key:
        st.success("系统钥匙串已保存密钥（不显示明文），新会话可自动使用。")
    elif current.api_key_env:
        st.caption("环境变量状态：" + ("已设置" if os.environ.get(current.api_key_env) else "未设置"))
    st.caption("系统保存仅支持本机 macOS 钥匙串。只设置会话密钥仍会在会话结束后丢失；更换服务地址后需要重新保存。")
    with st.form("model_credentials_" + current.id):
        st.text_input("当前会话 API key", type="password", key="_model_key_input")
        st.form_submit_button("保存密钥到系统钥匙串", type="primary", on_click=_persist_key, args=(current,))
        st.form_submit_button("设置会话密钥", on_click=_set_session_key, args=(current,))
        st.form_submit_button("清除会话密钥", on_click=_set_session_key, args=(current, True))
        st.form_submit_button("删除系统钥匙串密钥", on_click=_persist_key, args=(current, True))

    st.subheader("连接探针")
    st.warning("探针可能产生真实费用，调用与 token 用量单列，不进入评测评分。首版不推算货币价格。")
    with st.form("model_probe_" + current.id):
        modes = st.multiselect("测试响应模式", ["非流式", "流式"], default=["非流式", "流式"])
        st.caption(
            f"每种模式一次请求，无自动重试；每次输出上限 {current.max_output_tokens} token。"
            "流式 usage 是否请求沿用上方配置；失败不会自动降级或声称支持。"
        )
        confirmed = st.checkbox("我确认允许所选探针请求及可能产生的费用")
        probe = st.form_submit_button("运行连接探针", disabled=not current.enabled)
    if probe:
        if not confirmed or not modes:
            st.error("请选择至少一种模式并确认调用授权。")
        else:
            try:
                key = resolved_keys([current]).get(current.id)
                with st.spinner("正在运行已授权探针…"):
                    results = asyncio.run(_probe(current, key, modes))
                    for result in results:
                        storage.save_object("probe", result["result"]["id"], result)
            except CredentialError as exc:
                st.error(str(exc))
            else:
                st.success("探针已完成，结果独立记录如下。缺失 usage 表示未提供，不是零。")

    if not st.checkbox("查看历史连接探针", key=f"probe-history-{current.id}"):
        return
    history = [
        row for row in storage.list_objects("probe")
        if row["payload"].get("result", {}).get("model_id") == current.id
    ]
    for row in reversed(history[-20:]):
        payload = row["payload"]
        result = payload["result"]
        with st.expander(f'{row["created_at"]} · {payload["mode"]} · {result["status"]}'):
            if payload["config_hash"] != current.version_hash:
                st.caption("这是历史配置的探针，不能证明当前配置支持同样能力。")
            st.json(payload)
            if result["status"] != "completed":
                st.warning("连接未通过；请按错误码调整配置。未确认的能力仍为未知。")
            elif not payload["usage_observed"]:
                st.warning("文本响应成功，但服务未提供 usage；不能估算计费 token。")
