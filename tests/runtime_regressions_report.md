# API/Runner 修复验证报告

签名：API/Runner 实现 agent

签署时间：2026-10-23T01:36:14Z

结论：本次所有权范围内的离线回归通过；不代表真实容器、供应商或完整正式成绩验收通过。工作区无 Git 仓库，以下 SHA256 标识验证快照，不是数字证书签名。

## 范围

- 修改 `coding_eval/api.py`、`coding_eval/runner.py`、`coding_eval/models.py`、`app/run_page.py`、`app/model_page.py`。
- 更新 `tests/test_api.py`、`tests/test_runner.py`，新增 `tests/test_runtime_regressions.py`。
- 未修改 spec、verification.md、test_acceptance_tmp.py、agent.py、judge 或 report 实现；其他 agent 的并行改动参与全量验证，但不计为本 agent 修改。

## 缺陷分析与修复

- B02：新增 `APICancelledError(asyncio.CancelledError)`，公开 `result: AttemptResult`。结果保持 uncertain，保存脱敏 partial response、已知 usage、首内容/首回答/总耗时、request hash 和 protocol mapping，stream_complete=false。响应流关闭后传播取消。runner 使用 `getattr(exc, "result", None)` 并检查身份，兼容无结果的普通取消；恢复不重发 uncertain。已确认另一 agent 的控制调用接入同一协议。
- B03：非诊断批次每次正式启动或恢复，在任何 request intent/API 调用前校验全部任务的契约和 ready/image ID，并调用全部 `DockerSandbox.prepare()`；任一失败或 ID 不一致即拒绝派发。诊断模式保留显式授权及费用提示。
- B04：正式 create_plan 只接受已验证的 `sha256:<64 位小写十六进制>` runtime.image；不在冻结阶段偷偷替换 tag。重验证允许验证器将未冻结 tag 绑定为 ID，不允许替换既有 ID 或修改其他任务内容。预览显示冻结的 runtime_image。实际 validate_task 绑定和 Docker ID 检查由其他 agent 实现。
- 输出限制：ModelConfig 新增 `max_tokens_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"`；OpenAI 使用选定字段，Anthropic 固定 max_tokens。页面可选择、编辑、保存；不自动换字段重试。

## 生成的用例

- API：双协议取消前无事件/已有事件、分片凭据回显脱敏、流关闭、已知/未知 token、精确计时、hash 和取消异常协议；两个输出限制字段及表单持久化。
- Runtime：四种历史运行状态的不可用/ID 错配预检，所有任务预检先于请求，第二任务失败时零 intent，运行契约错误、legacy tag 拒绝和显式诊断分支，冻结 ID/内容不可被重验证器替换。
- 集成：本地停止与调用方取消均保留 partial evidence；恢复不重发；真实 PlanningAgent + MockTransport 控制取消使用相同 result 协议。
- 新增 runtime 文件含 22 个参数展开后的用例。所有 ready 数据均为合成夹具，不是题目通过证据。

## 测试证据

环境：现有 `.venv/bin/python`，不安装项目依赖。所有命令使用 `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:tests CODING_EVAL_DOCKER_TESTS=0`。

```sh
.venv/bin/python -B -m pytest -q -p no:cacheprovider \
  tests/test_api.py tests/test_runner.py tests/test_runtime_regressions.py \
  --tb=short --show-capture=no
```

最终：`157 passed in 8.24s`，exit 0。

```sh
.venv/bin/python -B -m pytest -q -p no:cacheprovider tests \
  --tb=short --show-capture=no
```

全量：`519 passed, 80 skipped in 13.84s`，exit 0。80 项为显式 Docker 门禁，不能计作通过。

生成、验证、修复过程：

1. API 修复前：98 passed / 9 failed，失败对应缺少取消 result 和缺少可选输出限制字段。修复后：107 passed。
2. Runtime 修复前：3 passed / 15 failed，复现 B02/B03/B04。修复后联同原 runner：46 passed。
3. 增强第二任务失败、冻结内容不可改换、控制取消及表单断言后，定向集 157 passed，全量集 519 passed / 80 skipped。

原验收文件未修改，执行以下子集：

```sh
.venv/bin/python -B -m pytest -q -p no:cacheprovider \
  .trae/specs/build-api-coding-eval-app/test_acceptance_tmp.py \
  -k 'api_cancellation or formal_preflight or runtime_image' \
  --tb=short --show-capture=no
```

结果：1 passed / 2 failed / 10 deselected，exit 1。旧 B02 夹具的 runtime.image 为可变 tag，正式预检先拒绝，SSE 等待超时；旧 B04 要求 `@sha256:` 并传入 tag，而本次授权契约要求先验证为 `sha256:` ID。不能声称原验收全部通过。新 runtime 回归覆盖等价的实际取消/预检/冻结路径。

## 未验证与工具限制

- 未启动 Docker、未绕过 sandbox、未在宿主执行题目/参考解/生成代码，无真实 key、无付费 API 请求。
- MockTransport、模拟 sandbox 和 AppTest 不证明真实容器内核隔离、镜像执行、供应商兼容或浏览器视觉表现。
- 保持 models.py 为单字段最小改动；未实现旧数据库中缺少新字段的冻结 ModelConfig 快照迁移，不将历史数据库升级列为已验证能力。
- Step1 工具准备执行后，utree 下载失败；Step7 的 `utree flush` 已尝试，exit 127（工具不存在）。测试统计来自实际 pytest，不来自 utree 报告。
- Step6 无用户指定覆盖率门槛及 CI 配置，按 skill 的 skip 模式处理，未声称覆盖率达标。

## SHA256

```text
66bf90a8c1c1b1a2891a3d2763e7adc414ade65a9d1cea1d9325ae4863e9e760  coding_eval/api.py
07ac37aa4ce0cdcc39fccaf0a0a419ad5819c231c8f507467f16bccc58eaf5e7  coding_eval/runner.py
50c5c222c95ace6a803b26824482036341ea7f31dfa66e9a0ee1a7076ea8602b  coding_eval/models.py
74a109965cde87bd55e02ba90dced7071768188e9de42ffdb92d07da84b82324  app/run_page.py
04d954207c13e176afd4dc8ee68c6fa8b6301ee52b7e92b299c42b16ff4fdfa8  app/model_page.py
f0e246826e9490a1448e03f6be6582aa73b625e623d4ee62d1ef31dd9b7c490a  tests/test_api.py
684c2ddbd84dc5e3f608ef1c2d3039eb51d478a83c40178f2bdc874d858f0778  tests/test_runner.py
8bae16b7387a712fe7dd6d9a3de998b041d69b90a956c7cd22f5f1c202eff5fe  tests/test_runtime_regressions.py
```

验证期间读取的协作依赖：

```text
e29295cb96612fd601f16ceb8b4dc83658f429ced93f7f880d1fa602fa7520c3  coding_eval/agent.py
489e43dad419f00f8507f1905b0e73de8c2e3d737b74ee11151f48c3c8deb3bf  coding_eval/judges.py
6c31f38b7c3da4f2040b13798a69f80ab067c66fee6cce0154c9ebd06cf819c9  coding_eval/container_check.py
```
