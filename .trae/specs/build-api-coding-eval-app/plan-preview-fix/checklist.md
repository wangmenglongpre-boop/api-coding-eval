# 验收

- [x] 同维度 ready 任务修改批次场景权重不调用容器验证、不改原任务 hash。
- [x] 预算不足、场景不匹配在容器执行前报错。
- [x] 真实维度变化仍验证参考/负解，不接受伪造证据或替换镜像。
- [x] 具体题目/判分原因/失败检查/日志路径可见；配置错误不回显敏感 input。
- [x] 正常页面预览成功，勾选控制授权不额外调用 API。
- [x] 新模块可在长驻服务加载，无模型请求、无冻结历史变更。

最终 573 passed/85 skipped，日志 `.tools/preview-full-final.log`。AppTest 迁移验证 worker_registry 资源身份保持；真实页面 DOM 新提示出现且无 ImportError/Traceback。未重新执行已 ready 题目/付费 API。
