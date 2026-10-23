"""Friendly diagnoses from sanitized evidence; no provider calls or key access."""

from __future__ import annotations

from .models import AttemptResult


def describe_attempt(attempt: AttemptResult) -> dict[str, str]:
    code, metrics = attempt.error_code, attempt.metrics
    detail = getattr(attempt, "diagnostics", {})
    if attempt.status == "completed":
        return {"原因": "请求完成", "建议": "质量以容器判分为准"}
    if code in {"http_401", "http_403"}:
        return {"原因": "鉴权失败" if code == "http_401" else "访问权限被拒绝",
                "建议": "检查密钥、端点及模型权限；修复后显式恢复未完成批次"}
    if code == "truncated":
        thinking_only = (
            not attempt.raw_output and metrics.reasoning_tokens is not None
            and metrics.output_tokens == metrics.reasoning_tokens
        )
        return {"原因": "输出额度被思考耗尽" if thinking_only else "达到输出或上下文上限",
                "建议": "提高最大输出 Token（如 32768），检查上下文限制；重新生成计划，留意费用"}
    if code == "timeout":
        if metrics.first_answer_seconds is not None or attempt.raw_output:
            cause = "回答生成中超时（保留部分答案）"
        elif metrics.first_content_seconds is not None or detail.get("reasoning_chunks", 0):
            cause = "思考阶段超时（尚无最终回答）"
        elif detail.get("headers_seconds") is not None:
            cause = "收到响应头后等待内容超时"
        else:
            cause = "连接或等待首内容超时"
        return {"原因": cause,
                "建议": "检查冻结超时；可试 600 秒及 32768 输出 Token，或另建较低思考强度组。重新生成计划；延长超时不保证成功"}
    advice = {
        "http_429": ("请求限流", "降低并发；为新计划设置重试间隔，检查供应商配额"),
        "configuration_error": ("请求配置无效", "检查支持参数、端点和密钥来源"),
        "incomplete_stream": ("响应流中断或缺结束标记", "检查网络与端点；不将部分输出作为成功"),
        "empty_response": ("未返回最终回答", "检查思考配置、输出预算与协议"),
        "transport_error": ("连接失败或传输中断", "检查网络与端点；未知计费请求不自动重发"),
        "response_lost": ("有派发记录但结果丢失", "计费未知，恢复不会重发此请求"),
        "cancelled_in_flight": ("在途请求被取消", "计费未知，恢复只继续未派发样本"),
    }
    cause, action = advice.get(code, (f"请求未完成：{code or attempt.status}", "展开原始结果检查错误码及冻结配置"))
    return {"原因": cause, "建议": action}
