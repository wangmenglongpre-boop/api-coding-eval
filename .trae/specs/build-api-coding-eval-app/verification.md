# 独立实施验收

## 产品体验多轮优化（2026-10-23）

精简/批量UI查询、原文/响应/编辑按需显示、点击生成导出、fragment运行监控、
顶部/侧栏/URL同步及窄屏样式完成；保留后台worker资源、原始评分及费用授权。
完整合并 **587 passed、85 skipped（17.37s）**；新增8项查询单测和6项交互回归，
新增查询模块36条语句全部覆盖。真实Chrome桌面/390px、答案/原始响应/运行详情/编辑入口、
完整HTML下载17.9MB通过。真实数据报告暖重跑1.19→0.18秒，元素2.6MB→66KB；
运行页0.15→0.02秒，895KB→18KB。服务端脚本与浏览器重绘指标不混用。
未新增供应商请求、未访问真实密钥、未重启主服务、未重跑85项无关Docker测试。
四件套、逐轮问题和精确验证边界见 [product-experience/verification.md](product-experience/verification.md)。

## 计划预览重复验证与错误提示（2026-10-23）

用户截图停留在“验证并冻结任务与评分配置”，随后出现笼统“计划无效”。代码将 task.scoring 与 batch.scoring 整体比较，场景权重差异导致 ready 题目重复执行参考/负解；预算、选择等错误直到末尾构造 RunPlan 才检查。近期验证结果另有 blocked / Docker daemon unavailable，原 UI 丢失了具体原因。

修复在独立 `coding_eval.plan_preview`：仅内部 draft 副本先验证预算、选择、场景覆盖，副本不持久化；同维度/模板 ready 任务保留原始快照和证书，场景权重独立冻结在计划中；真正维度/模板变化仍走旧严格验证和镜像/内容核对。runner 保留兼容委托，UI 直接导入新入口，增加进度、具体任务及日志路径。Pydantic 错误只展示 loc/msg，不展示 input；控制授权勾选不触发手动预览 API。

长驻服务第一次刷新仍显示旧说明，经 DOM 确认 run_page 模块缓存。主入口新增页面修订标记检查，只 reload UI 模块一次；不 reload 后台模块/worker，缓存资源身份有 AppTest 验证。再次刷新真实 DOM 新说明已出现，无 ImportError/Traceback，主服务未重启。

```bash
.venv/bin/python -m pytest tests .trae/specs/build-api-coding-eval-app/test_acceptance_tmp.py -q
```

最终 **573 passed、85 skipped（11.62s）**，日志 `.tools/preview-full-final.log`；新增 8 项覆盖复用快照、预算/场景提前失败、维度变更证书/镜像、题目错误证据、敏感 input 不回显、页面手动预览无控制调用及旧页面缓存升级保留 worker。初次新测试断言误把 st.write(list) 当 Markdown，修正为 JSON；缓存模拟恢复导出函数身份以隔离测试，首次 AppTest 3 秒冷启动超时改为 10 秒，未修改产品时限。

用实际数据库三个启用模型、十五道 ready 题，以默认三次重复和足额预算生成预览：**0.017 秒**，所有 task Snapshot 与原始记录逐一相等；禁止验证器的 guard 未触发，无 Docker/模型调用、无计划持久化或历史修改。真正执行批次时仍需要 Docker 与凭据预检。规格四件套在 `plan-preview-fix/`。

wheel/sdist 已重建（`.tools/preview-package.log`），安装至 `.tools/preview-wheel-install` 后在 `/tmp` 导入新 plan_preview 模块及页面修订标记正常；本轮未请求模型或重验 ready 题目。

## 运行失败诊断与报告概览（2026-10-23）

用户要求定位频繁 failed 并优化，突出质量总分、雷达及平均输入/缓存命中/输出 Token。规格四件套在 `run-report-improvements/`。

生产历史只读证据：9 次旧 HTTP 401、Seed 7 次超时（已返回思考但无最终答案）、DeepSeek 3 次 8192 截断且全部为思考；当前 DeepSeek 32768 已有成功，不能归为当前仍用 8192。最新批次另有 1 条 API completed 但 judge blocked（Docker daemon unavailable），与模型失败分开。

实现：

- AttemptResult 默认空 diagnostics 兼容旧记录，新增响应头/最近内容时点、片段数、冻结限额与 finish_reason；不持久化思考正文、响应错误正文或密钥。旧记录按已知指标判断阶段。
- 401/403 同执行轮次按模型暂停后续派发与重试，其他模型继续；不伪造 intent/attempt，显式恢复重置暂停，仍按冻结预算与重试额度执行。
- 总分卡片/柱图、五维雷达和 Token 并列柱图优先；可切换全部/完成/未完成实际请求。每项均值仅基于已上报值，显示 n/total 与缺失率，真实零与未上报分开，缓存不重复计输入。
- HTML 内嵌 SVG/CSS，全部动态文本转义，无脚本或外部资源，原始证据折叠保留。算法 t6-v2 及正式排名完整性规则不变。
- 用户授权优化下追加 Seed 新配置 600 秒/32768，保持思考/采样参数；核对所有冻结计划 hash 未变。新限额须重新生成计划，未实测供应商成功率。

验证命令：

```bash
.venv/bin/python -m pytest tests .trae/specs/build-api-coding-eval-app/test_acceptance_tmp.py -q
.venv/bin/python -m build
.venv/bin/python -m pip install --no-deps --target .tools/report-wheel-install dist/coding_eval-0.1.0-py3-none-any.whl
```

最终 **565 passed、85 skipped（18.70s）**，日志 `.tools/run-report-final.log`。新增 9 项覆盖思考/回答超时、脱敏、截断、401/403 恢复/预算/无虚假派发、缺失与零、分母口径、旧报告雷达、HTML 转义及筛选。wheel/sdist 成功，异目录 `/tmp` 从安装目标导入新模块/页面/diagnostics，15 题资源正常。

真实服务 PID51232 未重启，health=ok。通过 `?page=结果对比` 打开新页面，DOM 确认总分、雷达、Token 图表已加载；运行页失败诊断与容器故障提示正常，无 ImportError/Traceback。独立 HTML 浏览器已观察雷达与总分正确布局。小视口侧栏遮挡筛选点击，筛选由 AppTest 验证，未宣称浏览器点击筛选成功。

批次 d97a52d816e747b6a5ea7ec68de2fd5e 已结束后，使用 `.tools/rejudge_existing_batch.py` 补判单条 Docker 临时故障。执行前断言全部样本已有请求、max_retries=0；NoRequests 客户端禁止请求，使用占位值避免 OS 凭据读取。真实容器补判成功，批次恢复 completed；逐条 attempt JSON、全部 intent、冻结计划 hash 核对未变，日志 `.tools/run-report-rejudge.log`。本次没有供应商 API 调用。旧报告版本不覆盖，现有证据的新报告/HTML 在 `data/report_overviews/`。

补判暴露旧服务判分版本 v2、当前磁盘版本 v3 的混用，报告按设计 invalid，未绕过版本门槛。对该批次其余 8 条旧版本记录统一用当前 v3 重判，原始请求/旧判分版本保留、无 API 或凭据读取，9 条最终判分均 completed/v3；逐条 attempt 与 intent 再次核对未变。日志 `.tools/run-report-unified-judges.log`。统一后的新报告与旧 provisional/invalid 版本分开保存。

最终报告 `c92fa168aca942b3bca3c3c2feb82dd6` 为 complete，真实页面 DOM 确认选中该版本、批次 completed、三个 Plotly 图表且无异常。离线文件 `data/report_overviews/report-c92fa168aca942b3bca3c3c2feb82dd6.html`，原始 9 请求完整保留。临时 HTML 预览服务监听 127.0.0.1:8126；关闭其进程的动作被自动审批以“未确认归属”为由拒绝，未绕过，该临时服务保留，主评测服务无变更。

## 长驻服务新增目录导入修复（2026-10-23）

用户截图：`ImportError: cannot import name 'load_reference_tasks' from 'coding_eval.datasets'`。
磁盘函数存在且新Python进程正常导入；原Streamlit进程持续运行，页面更新时可能仍持有
更新前的datasets模块。发现当前running批次持有OS排他锁，因此没有重启服务。

参考加载实现移至独立 `coding_eval.reference_datasets`，页面从新模块导入，
datasets原入口保留委托兼容。新增回归删除缓存datasets中的新函数和路径符号，
重新导入页面/新模块，验证页面正常加载五题，无模型请求。

完整离线 **556 passed、85 skipped（10.36s）**，
日志 `.tools/reference-import-cache-regression.log`。
实际浏览器刷新后模型配置页面无ImportError；自动化侧栏点击未改变选项，
不声称数据集页面浏览器导航已验证，数据集加载由AppTest覆盖。
原进程PID51232保持，running批次仍有新的judge记录写入，没有修改计划、任务或证据。
未重新执行Docker题目验证：本次只调整加载入口，题目内容与先前证据不变。

## 五道参考改编题增补（2026-10-23）

依据用户飞书《基础模型评测》五题描述、前三题评测附件/Faker题目附件及公开PR，
增补 C11–C15 独立Python核心逻辑任务。ShardingSphere/aiohttp两个评测附件403，
未声称读取原隐藏测试；所有任务注明原语言、PR与改编限制。
设计与四件套见 `reference-datasets/`。

首轮 C11–C14 已通过，C15位置检查读取运行时调用栈被沙箱拒绝，状态judge_error，
没有计为模型失败或发布ready。改为生成器解析测试AST计算调用行号，保留warning
filename/lineno、stacklevel=2断言与沙箱防栈窥探。修正后五题全部重新真实验证通过。

```bash
PYTHONPATH=. .venv/bin/python scripts/validate_reference_catalog.py
CODING_EVAL_DOCKER_TESTS=1 PYTHONPATH=.:tests \
  .venv/bin/python -m pytest -q tests/test_reference_datasets.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:tests CODING_EVAL_DOCKER_TESTS=0 \
  .venv/bin/python -B -m pytest -q -p no:cacheprovider tests \
  .trae/specs/build-api-coding-eval-app/test_acceptance_tmp.py
```

结果：

- 发布验证：五参考解共25项全通过；10份负解均由预定检查检出且非语法失败。
  五题全通过后新增ready头，原头hash保持，本地C01–C15十五题全部ready。
- 新增测试：**12 passed（45.60s）**，包含7项宿主静态/ZIP/页面验证和5项真实Docker逐题验证；
  与下述完整离线测试有包含关系，不累加为独立测试总数。
- 完整离线：**555 passed、85 skipped（13.65s）**。85项是原80项加新增5项Docker验证；
  跳过只表示该次离线命令禁用Docker，各动态集合已有另次真实执行证据。
- wheel构建、隔离目录安装后从 `/tmp` 加载10+5任务资源通过；
  sdist含两个JSON目录资源、两个重建/验证脚本与参考规格。
  没有重跑原80项Docker集合；该集合的前次精确记录见下一节。

永久本地证据：`data/reference_tasks/validation-evidence.json` 含15份完整结果；
`data/reference_tasks/C11.json` 至 `C15.json` 及同名5份ZIP。
每份ZIP实际重新导入均为draft且无验证证据。
不可变image ID为 `sha256:b7a9e5f6a93d6d4a31f129bbd4bdee69efccb6ded0fb230d78aafaf37af98377`。
运行日志：`.tools/reference-validation-final.log`、`.tools/reference-docker-regression.log`、
`.tools/reference-offline-final.log`；首轮错误记录保留 `.tools/reference-validation.log`。

题目源码在宿主仅作AST/字符串数据处理，真实行为仅在禁网非root只读受限容器执行。
没有真实模型API调用或本次模型成绩。五题不等于原Vue/Go/Java仓库修复成功，
原仓库执行、真实供应商与浏览器完整验收仍属于未来扩展/验收。

## 后续容器验证修复（2026-10-23）

真实 Docker 29.3.1 已可用。C03/C10 的 tempfile.tempdir 惰性初始化被依赖身份保护误判；C10 子进程的 DEVNULL 被文件访问限制阻断。修复为保护前初始化 tempfile 缓存、精确允许 /dev/null，判分版本 docker-python-v3；没有更改任务、参考解、负解、评分门槛或隐藏测试。

C03/C05/C10 已分别执行参考解和全部负解，追加 ready 到应用数据库，保存 4/2/5 份真实 result.json；全部十题当前均 ready。证据清单见 `.tools/container-validation-evidence.json`。C05 原失败为 daemon unavailable，本次复跑通过。

完整离线回归：548 passed、80 skipped（25.81s）。真实 Docker 回归命令：

```bash
CODING_EVAL_DOCKER_TESTS=1 PYTHONPATH=.:tests .venv/bin/python -m pytest -q \
  tests/test_judges.py tests/test_dataset_integrity.py tests/test_judge_trust.py
```

结果：182 passed、1 failed（251.79s）。失败的 C03 duplicate_credit 原始结果为 blocked / Docker daemon unavailable；参考解在同用例内通过。单独复跑该完整用例结果 1 passed（4.05s）。没有修改、跳过或削弱该测试；不把组合结果称为一次全绿。80 项原动态门禁均已实际执行并取得通过证据，其中十题参考/负解、C10 缺陷变体、防篡改、隔离和异常归因均已覆盖。原有“80 skipped/Docker blocked”结论为历史快照，真实供应商和浏览器完整验收仍保留。

## 后续密钥持久化增补复验（2026-10-23）

用户要求刷新/重启不丢会话密钥，本轮新增原生 macOS 钥匙串与统一凭据解析，更新模型页、启动/恢复、控制计划和报告解释。新增 `tests/test_credentials.py` 共 10 项，使用模拟 backend；`tests/conftest.py` 隔离全部测试的真实 OS 凭据。既有业务测试未删改。

最终命令：

```bash
CODING_EVAL_DOCKER_TESTS=0 PYTHONPATH=.:tests .venv/bin/python -m pytest -q \
  tests .trae/specs/build-api-coding-eval-app/test_acceptance_tmp.py
```

结果：**548 passed、80 skipped、0 failed，10.21s**。验证新会话恢复、优先级、地址绑定、删除、失败重试、无明文 fallback、缺失零请求及 API/控制/报告脱敏。初次新增测试夹具缺失 max_requests 和场景权重，已补齐必填契约并完整复验；没有降低核心断言。

已安装 keyring 25.7.0，`keyring.backends.macOS.Keyring` 可导入，现有 `http://127.0.0.1:8501/_stcore/health` 返回 ok。没有重启正在运行的应用、读取/写入真实 API 密钥、调用供应商或执行 Docker；真实 OS 权限弹窗与用户首次保存后的重启读取尚待用户操作。下方为此前独立验收快照，538 的计数不包含本轮新增 10 项。

## 最终复验结论（2026-10-23）

**离线复验通过，完整交付仍未通过。B01-B07 均为 closed offline，真实动态环境未验。** 当前权威状态以本节至“历史快照”之前为准；下方折叠保留的 2026-10-21 原始失败记录只作修复前证据，不再表示当前缺陷。

- 已读取批准的 spec/tasks/checklist 及原 verification，独立复跑完整 `tests/` 和 7B，不直接采用实现 agents 自报结果。
- 本轮只修改 `test_acceptance_tmp.py` 的 runtime 夹具、sandbox mock 和对应 B04 固定镜像契约，以及本文件、tasks/checklist/spec/README。未修改业务模块或 `tests/`，不新增重复的 `tests/test_final_acceptance.py`。
- 原独立验收 13 项均保留。B01/B02/B03/B05/B06/B07 的核心断言未删改；B04 先断言可变 tag 的 ready 夹具被 `create_plan` 拒绝，再验证已 pin 的 ready 快照可接受且冻结内容完全一致。不可变本地 ID 为 `sha256:<64 位小写十六进制>`，不再错误限定为 `@sha256:` 引用格式。
- 正式 preflight 仍执行真实 runner/任务契约检查，只 mock Docker 边界；独立用例额外禁止 `container_check.run_command` 和 sandbox.execute，避免触发真实 Docker 或题目执行。
- 所有 API 使用 MockTransport/固定控制模型替身，无真实凭据或请求；合成 ready/check/result 不能充当任何题目发布或模型能力证据。pytest 数据、锁和产物在临时目录。
- 未启动或探测 Docker，不在宿主执行题目、答案、参考解、隐藏测试或负解。全量测试中的固定框架子进程仅用于崩溃恢复、日志限长及超时；AST/文本检查不执行题目。
- 未发现新的可复现产品 bug；本结论不覆盖未执行的 Docker/真实客户端路径，也不表示已证明任意不可信 Python 均不可逃逸。

## 最终测试结果

环境：macOS，现有 `.venv/bin/python`，Python 3.11.17、pytest 8.4.2、Streamlit 1.66.0、httpx 0.28.1、Pydantic 2.14.1；未安装依赖。

| 本轮实际执行集合 | passed | failed | skipped | deselected | 耗时 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 完整 `tests/` | 525 | 0 | 80 | 0 | 14.64s |
| 适配后的独立验收文件 | 13 | 0 | 0 | 0 | 6.15s |
| 原 B01-B07 单独筛选 | 7 | 0 | 0 | 6 | 15.19s |
| 最终合并 `tests/` + 独立验收 | **538** | **0** | **80** | **0** | **26.08s** |

7B 是独立 13 项的子集；合并 538 包含完整 525 和独立 13，不将重复执行、deselected 或 skipped 累加为通过数。完整 `tests/` 本身已有一个调用原 B01 验收体的回归，因此这些是 pytest 用例数，不是互不重复的业务场景数。

复验命令（均在项目根目录执行）：

```sh
env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:tests CODING_EVAL_DOCKER_TESTS=0 \
  .venv/bin/python -B -m pytest -q -p no:cacheprovider --tb=short --show-capture=no tests

env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:tests CODING_EVAL_DOCKER_TESTS=0 \
  .venv/bin/python -B -m pytest -q -p no:cacheprovider --tb=short --show-capture=no \
  .trae/specs/build-api-coding-eval-app/test_acceptance_tmp.py

env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:tests CODING_EVAL_DOCKER_TESTS=0 \
  .venv/bin/python -B -m pytest -q -p no:cacheprovider --tb=short --show-capture=no \
  .trae/specs/build-api-coding-eval-app/test_acceptance_tmp.py \
  -k 'cancelled_retry or api_cancellation or formal_preflight or runtime_image or report_recomputes or report_ui_explanation or explanation_receives'

env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:tests CODING_EVAL_DOCKER_TESTS=0 \
  .venv/bin/python -B -m pytest -q -p no:cacheprovider --tb=short --show-capture=no \
  tests .trae/specs/build-api-coding-eval-app/test_acceptance_tmp.py
```

测试环境适配过程：先原样运行旧独立文件，得到 `5 failed, 8 passed in 8.78s`。B01/B04、导出脱敏及预算用例被旧 mutable tag 拒绝；B02 在预检拒绝后等待 SSE 超时。这些失败没有到达相应核心断言。只在独立文件包装共享的 `test_reports.make_plan`，为合成 ready 绑定固定 ID 和对应 validation hash，并模拟正式 preflight 的 Docker prepare 后，13 项全通过。没有改成 diagnostic 绕过正式预检，也没有将产品异常吞掉以制造通过。

## 7B 修复证据

下列独立用例均位于本目录 `test_acceptance_tmp.py`。关闭范围限于离线；真实 Docker 镜像切换/执行、远端取消计费、真实 LLM 解释仍未验。

| 项目 | 当前修复与独立证据 | 补充回归证据（本轮全量已跑） | 状态 |
| --- | --- | --- | --- |
| B01 | `test_cancelled_retry_is_not_formally_ranked`：真实 runner/SQLite/AppTest，在失败后取消且尚有重试时报告 provisional | `test_report_regressions.py` 断言 pending_retry/空排名；`test_reports.py` 覆盖全部运行状态、导出、终结样本与重评分的冻结 run_status | closed offline |
| B02 | `test_api_cancellation_retains_observed_text_usage_and_timing`：MockTransport SSE 已接收的 partial-answer、input_tokens=12、首内容和总耗时随 uncertain 保存 | `test_runtime_regressions.py` 本地/外部取消、流关闭、脱敏、request hash、恢复不重发及真实 PlanningAgent 控制取消；`test_api.py` 双协议取消 | closed offline |
| B03 | `test_formal_preflight_blocks_before_requests_if_docker_unavailable`：固定 ready/image ID 后，模拟 prepare unavailable，真实 runner 零 API 请求 | `test_runtime_regressions.py` queued/failed/cancelled/running 启动恢复、错配 ID、第二任务失败、全部任务先预检、零 intent | closed offline |
| B04 | `test_runtime_image_is_frozen_in_plan`：有效 ready 但 mutable tag 被拒绝；已 pin ready 可冻结且 task_snapshots 不变 | `test_judges.py` validate_task 绑定 ID/证据、已 pin ID 不重新解析 tag 或拉取；`test_runtime_regressions.py` 拒绝验证器替换冻结 ID/内容；`test_reports.py` 测试任务版本分组，代码核对确认镜像参与任务哈希（非实际跨镜像运行） | closed offline |
| B05 | `test_report_recomputes_dimension_scores_from_frozen_checks`：全 false checks/全 100 缓存分不再产生 100 分 | `test_reports.py::test_scores_use_frozen_weighted_checks_and_not_cache_including_rescore` 按冻结 rubric.points 复算并保持父报告不变 | closed offline |
| B06 | `test_report_ui_explanation_has_an_authorized_action`：报告页解释动作已存在 | `test_reports.py::test_report_explanation_requires_selection_and_single_use_authorization` 覆盖未授权零调用、单次授权、成功/失败、用量单列和不改报告 | closed offline |
| B07 | `test_explanation_receives_actual_report_metric_structure`：真实 build_report → 已保存报告 → PlanningAgent 输入保留 completed_attempts 中位数 2 秒及 quality_interval | `test_agent.py` 和 `test_reports.py` 检查明确 schema 白名单、分母/缺失率/区间/ID，排除答案、隐藏测试和凭据 | closed offline |

代码路径核对：`reports._sample` 对未耗尽重试使用 pending_retry，并从冻结 checks/points 复算维度；`APIClient` 取消异常携带部分 AttemptResult，runner/agent 保留证据；`BatchRunner._preflight` 在 intent 前检查所有正式任务；`validate_task` 先 pin ID 再产生验证 hash；报告页显式授权调用 `PlanningAgent.explain`，后者仅解释与冻结计划一致的已保存报告及 schema 化摘要。

原记录额外提示的 `_case` 异常归因风险已有框架修复/回归：`test_judge_trust.py` 验证异常链来源、候选回调、框架帧和依赖防篡改；但真实容器中的 23 个相关用例仍全部 skipped，不能标为动态修复验收通过。

## 任务和清单同步

- **T1/T2/T4/T6 已勾选（离线）**：公共契约/存储、协议及指标、编排/预算/恢复、报告/解释路径均有全量与跨模块测试证据。AppTest 是控件及服务端逻辑测试，不是浏览器视觉验收。
- **T3/T5/T7 不勾选**：逐项区分已实现/离线已验证与 blocked；十题实际参考/负解、C10 变体、容器隔离/归因、完整动态成绩和真实客户端/UI 验收仍缺。
- checklist 只勾离线已证实能力；真实 Docker、浏览器上传/下载、供应商及控制模型保留未勾。合并“实现已完成”和“动态待验”的条目未伪装为全部通过。
- spec 仅更新实施状态与边界，批准的 R1-R8/交付范围不降级。README 已同步 `525/80` 和 `538/80`、解释接线、Docker 禁止启动与镜像 pin 流程；OpenAI 新增 `max_tokens_field` 默认 `max_tokens`，可显式改为 `max_completion_tokens`，Anthropic 固定 `max_tokens`，不自动切换字段重试。

## 最终阻碍与未验证项

| Docker 门禁位置 | skipped | 动态要求 |
| --- | ---: | --- |
| `tests/test_judges.py:437` | 10 | C01-C10 参考/全部声明负解及 ready 证据 |
| `tests/test_judges.py:448` | 12 | 实际候选/测试错误归因、协议/退出/stdout/HMAC 反例 |
| `tests/test_judges.py:469` | 1 | check 间独立 tmpfs/进程 |
| `tests/test_dataset_integrity.py:260,277,305` | 34（17+14+3） | 针对性负例、C10 嵌套逃逸与支持的测试风格 |
| `tests/test_judge_trust.py:181,203,229` | 23（9+8+6） | 种子依赖篡改、运行期依赖修改、legacy 异常链归因 |
| **总计** | **80** | **全部是 skip，不是 pass** |

1. **Docker / T3、T5、T7**：本轮环境禁止启动 Docker。没有十题真实通过日志、C02/C03 可控竞争/死锁、C04 实际补丁回归、C10 变体检出及重复性证据；非 root/禁网/无凭据/无宿主 socket、seccomp/audit、timeout/OOM/PID、清理与依赖防篡改的真实效果未验。禁止用宿主执行代替。
2. **真实 API 客户端与控制模型**：未请求 OpenAI、Anthropic 或企业网关。鉴权、model ID、两个输出限制字段、代理缓冲/SSE、usage/缓存/推理计费、限流、远端取消计费均待真实凭据和额度授权。固定替身不能证明真实自然语言理解或解释引用的事实质量。
3. **真实浏览器**：未验证文件选择器上传、下载落盘、响应式/视觉、独立 HTML 打开、跨会话密钥生命周期。AppTest/函数层通过不能替代。
4. **安装/升级兼容**：README 保留并明确标注历史构建/wheel 安装/健康检查记录，本轮未重跑；未验证所有声明依赖版本组合。新增 `ModelConfig.max_tokens_field` 缺省配置的序列化可用，不等于旧冻结 ModelConfig 快照/数据库升级兼容已验证。
5. **正式模型成绩**：没有“十题真实 ready → 真实供应商 → 容器判分 → 正式排行”证据。合成闭环只验证框架，不能给出真实模型分数或行业排名。

后续需在明确允许的 Docker 环境实际执行全部 80 项动态用例及真实浏览器验收；有独立 API 授权后再做小规模双协议/控制模型冒烟。未完成前保持 T3/T5/T7 与相关 checklist 空框。

## 本轮文件指纹

工作区仍不是 Git 仓库。以下 SHA256 在最终合并测试后读取，标识本次复验业务代码及适配后的独立验收文件；下方历史哈希不是当前版本。

```text
66bf90a8c1c1b1a2891a3d2763e7adc414ade65a9d1cea1d9325ae4863e9e760  coding_eval/api.py
07ac37aa4ce0cdcc39fccaf0a0a419ad5819c231c8f507467f16bccc58eaf5e7  coding_eval/runner.py
238363b06bf59fae31c8a3588edddb9edbfc46f3d480d3ffe0a93291c4865968  coding_eval/reports.py
e29295cb96612fd601f16ceb8b4dc83658f429ced93f7f880d1fa602fa7520c3  coding_eval/agent.py
961c23cee0b3d70a646f7ed7b95d69385723576a534850f041e0f4e55b9bdd30  coding_eval/container_check.py
489e43dad419f00f8507f1905b0e73de8c2e3d737b74ee11151f48c3c8deb3bf  coding_eval/judges.py
50c5c222c95ace6a803b26824482036341ea7f31dfa66e9a0ee1a7076ea8602b  coding_eval/models.py
bd7a88090b0b5ae3dabf8efd4e45579c9ff51c49a3c547d7cddd78ed82848c61  coding_eval/storage.py
efd63a61e63b5d5f6c19fe02fa9f77011b9a7bd5ac603cf520feb49528dec8ad  app/main.py
f66edac59ec5def3c9d325b3539454e96804d38b45a1ce90077dca131c90ef90  app/report_page.py
04d954207c13e176afd4dc8ee68c6fa8b6301ee52b7e92b299c42b16ff4fdfa8  app/model_page.py
74a109965cde87bd55e02ba90dced7071768188e9de42ffdb92d07da84b82324  app/run_page.py
b4e2619f49bfb37ece2d9d203922b66112ab5034d64ca0440fe0734c4771240d  pyproject.toml
e3f7bef44da8c2f92e3583eb208bd458d8499b1090e3184384cd36f1e0488c80  .trae/specs/build-api-coding-eval-app/test_acceptance_tmp.py
```

## 历史快照（非当前状态）

<details>
<summary>2026-10-21 修复前原始验收：旧计数、缺陷、行号、阻碍和哈希，仅供追溯；均由上方最终复验状态取代。</summary>

## 结论与边界

- 验收快照时间：2026-10-21 09:45 CST。结论：**未通过完整验收**，尚有 7 个必须修复项，以及明确未验证的真实容器、供应商和浏览器操作。
- 依据本目录 `spec.md`、`checklist.md` 的 R1-R8 和交付要求验收；没有修改规格、清单勾选、业务模块或现有测试。
- 本次仅新增本文件和自用临时测试 `test_acceptance_tmp.py`。测试数据库、锁及模拟判分产物使用 pytest 临时目录，没有写入业务数据目录。
- 未使用 review skill，未执行模型生成代码、参考实现或自定义测试代码。现有框架测试中的固定子进程只用于崩溃恢复、日志限长及超时检查；题目源代码只作为字符串或 AST 检查对象。
- 所有 API 请求均使用 MockTransport/测试替身，无真实 key、无供应商联网请求、无付费调用。合成 ready/判分夹具只验证框架，不能作为题目 ready 的验收证据。
- 工作区不是 Git 仓库，无法用 commit 标识快照，文件指纹附后。其他 agent 在本次验收期间完成了 main 集成、storage pointer 及 httpx 依赖修复，已按最新代码复跑；这些原有占位和已修项目不列为新缺陷。

## 执行结果

环境：macOS，Python 3.11.17，pytest 8.4.2，Streamlit 1.66.0，httpx 0.28.1，Pydantic 2.14.1。复用已有测试解释器，未安装依赖。

最终合并测试：

```sh
env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:tests CODING_EVAL_DOCKER_TESTS=0 \
  /Users/bytedance/.cache/uv/archive-v0/MRLaI56vqWjWjZD-/bin/python -B \
  -m pytest -q -p no:cacheprovider --tb=short --show-capture=no \
  tests .trae/specs/build-api-coding-eval-app/test_acceptance_tmp.py
```

```text
7 failed, 440 passed, 23 skipped in 16.61s
```

| 测试集合 | 通过 | 失败 | 跳过 | 含义 |
| --- | ---: | ---: | ---: | --- |
| 最新工作区 `tests/` | 434 | 0 | 23 | 框架、契约、模拟 API、存储、判分边界与 AppTest |
| 独立临时验收测试 | 6 | 7 | 0 | 包含新的跨模块反例，失败对应下列 B01-B07 |
| 合计 | 440 | 7 | 23 | 不把 skipped 计为通过 |

首次集成前基线为 348 passed / 23 skipped；这是较早快照，不与最终结果相加。独立验收最初发现的 `httpx` 依赖缺失已被其他 agent 修复，最终 `test_api_dependency_declared` 通过，撤出必须修复清单。

真实环境检查：

```text
docker info --format '{{.ServerVersion}}'
exit 1: failed to connect to the docker API at
unix:///Users/bytedance/.docker/run/docker.sock
connect: no such file or directory
```

只读检查既有 `data/t5-selfcheck/artifacts/judges/*/*/result.json`：20 份结果全部为 `blocked`，不是参考解通过日志。

## 必须修复项

以下均未由本次验收修改。P1 表示正式评分/证据/费用边界阻断；P2 表示验收前必须补齐的证据一致性或用户能力。

### B01 / P1：取消后尚有重试的样本被当作完整正式结果

- 路径：[reports.py:96-125](file:///Users/bytedance/Documents/AI-coding-eval/coding_eval/reports.py#L96-L125)、[reports.py:327-358](file:///Users/bytedance/Documents/AI-coding-eval/coding_eval/reports.py#L327-L358)、[report_page.py:189-192](file:///Users/bytedance/Documents/AI-coding-eval/app/report_page.py#L189-L192)。
- 具体触发：正式计划 1 模型 × 1 题 × 1 次，`max_retries=1/max_requests=2`；首请求 failed，判分结束时调用真实 `BatchRunner.cancel`，第二次请求尚未派发；在结果页生成报告。
- 实测：运行状态 `cancelled`，只派发 1 次；报告却为 `complete`、coverage=1，并给出 `rank=1, quality_score=0`。恢复后仍允许执行的重试可能改变成绩。
- 原因：报告仅以“每个样本存在可计分 attempt”判断完整，没有冻结样本终态、运行状态或待重试信息。API 失败应计零，但中断后的未终结样本不能据此形成完整排行。
- 修复验收：重试等待中、取消、崩溃恢复前均保持暂定；只有成功终结或耗尽已声明重试的失败样本才算终态，导出沿用同一完整性结论。
- 验证：**已通过真实 runner + SQLite + AppTest 复现**。失败测试 `test_cancelled_retry_is_not_formally_ranked`。涉及 R4/R6/R8。

### B02 / P1：流式取消丢失已经观测到的响应和指标

- 路径：[api.py:390-415](file:///Users/bytedance/Documents/AI-coding-eval/coding_eval/api.py#L390-L415)、[runner.py:308-327](file:///Users/bytedance/Documents/AI-coding-eval/coding_eval/runner.py#L308-L327)；控制调用同类路径为 [agent.py:114-122](file:///Users/bytedance/Documents/AI-coding-eval/coding_eval/agent.py#L114-L122)。
- 具体触发：SSE 已收到 `partial-answer` 和 `prompt_tokens=12`，流保持打开，此时停止批次。
- 实测：持久化 uncertain 是正确的，但 `raw_output=""`，已知 usage、首内容耗时、请求耗时及请求哈希均被新的空 AttemptResult 丢弃。不能将“最终计费未知”混同于“先前已知观测不存在”。
- 修复验收：取消应带回已脱敏的部分内容、已知 usage、计时及请求哈希，同时保持 uncertain、不自动重发、关闭响应流。
- 验证：**已用 APIClient + MockTransport SSE + 真实 runner/SQLite 复现**。失败测试 `test_api_cancellation_retains_observed_text_usage_and_timing`。涉及 R4/R5/R7。

### B03 / P1：正式批次没有 Docker 预检，先调用 API 后才 blocked

- 路径：[runner.py:132-141](file:///Users/bytedance/Documents/AI-coding-eval/coding_eval/runner.py#L132-L141)、[runner.py:181-189](file:///Users/bytedance/Documents/AI-coding-eval/coding_eval/runner.py#L181-L189)、[runner.py:300-328](file:///Users/bytedance/Documents/AI-coding-eval/coding_eval/runner.py#L300-L328)。
- 具体触发：已有合法 ready 快照且评分不变，预览之后 Docker 被关闭，或恢复一个历史正式批次时 Docker 不可用。
- 实测：将 Docker prepare 固定为 unavailable，正式批次仍发出 1 个模拟 API 请求，然后保存 blocked 判分。`create_plan` 仅在评分改变时重新验证，不能替代启动时环境预检。
- 影响：真实服务下会在已知无法判分的环境消耗请求预算/费用；当前不会回退宿主执行，这一点没有发现违规。
- 修复验收：正式批次派发前检查所有冻结任务的判分环境及运行契约，失败时零 API 请求；诊断生成需要独立、明确的用户授权语义。
- 验证：**已用 unavailable Docker 替身 + 真实 runner/判分包装层复现**。失败测试 `test_formal_preflight_blocks_before_requests_if_docker_unavailable`。涉及 R4/R7。

### B04 / P1：运行镜像没有随计划冻结

- 路径：[models.py:179-187](file:///Users/bytedance/Documents/AI-coding-eval/coding_eval/models.py#L179-L187)、[runner.py:55-72](file:///Users/bytedance/Documents/AI-coding-eval/coding_eval/runner.py#L55-L72)、[container_check.py:121-148](file:///Users/bytedance/Documents/AI-coding-eval/coding_eval/container_check.py#L121-L148)、[reports.py:334-340](file:///Users/bytedance/Documents/AI-coding-eval/coding_eval/reports.py#L334-L340)。
- 具体触发：使用默认 `python:3.11-slim` 验证任务及创建正式计划，随后本地该 tag 被更新，再执行后续样本或恢复运行。
- 实测契约：真实 `create_plan` 允许保存可变 tag，没有 digest。每个 `judge_attempt` 创建新的 DockerSandbox，重新解析当前 tag；单个 sandbox 内固定 ID 不等于批次冻结。
- 影响：同一任务/计划哈希、同一 judge_version 下可以使用不同 Python/SQLite/系统镜像，报告可比组没有运行时 image ID，无法保证复现和公平性。
- 修复验收：在批准/冻结阶段绑定可验证的镜像 digest/ID，运行和重判验证一致性；环境变化生成新的版本和可比组。
- 验证：**静态调用链与契约反例确认**，失败测试 `test_runtime_image_is_frozen_in_plan`；真实跨镜像运行因 Docker 不可用未验证，不声称已经观测到容器分数变化。涉及 R2/R6/R7/R8。

### B05 / P2：断言全失败但维度全满分的矛盾判分仍可正式排名

- 路径：[reports.py:133-156](file:///Users/bytedance/Documents/AI-coding-eval/coding_eval/reports.py#L133-L156)。
- 具体触发：读入同一冻结任务的判分，全部 check 都是 false，`all_required_passed=false`，无 critical gate，但保存的 `dimension_scores` 错为全 100。
- 实测：报告 `complete`、综合分 100。当前只校验 check 集合和通过标志，不根据冻结 rubric.points 重算或交叉核对维度分。
- 这是异常判分证据的防护缺口，不是断言正常时加权算法已算错，也不是已发现实际容器会产生这种数据。
- 修复验收：从断言及冻结 points 复算分数，或拒绝不一致证据为 judge_error；事后重评分同样校验且不改变父报告。
- 验证：**真实报告算法 + 合成矛盾证据复现**。失败测试 `test_report_recomputes_dimension_scores_from_frozen_checks`。涉及 R6/R8。

### B06 / P2：结果页没有可授权调用的 Agent 解释路径

- 路径：[report_page.py:244-247](file:///Users/bytedance/Documents/AI-coding-eval/app/report_page.py#L244-L247)。
- 具体触发：已有保存报告及启用模型，进入结果页的“可选 Agent 解释接口”。
- 实测：只有“尚未接入”说明和 JSON 摘要，没有控制模型选择、费用授权或解释按钮；`PlanningAgent.explain` 存在，但用户无法从页面调用。
- 此项是结果模块自身的缺失，不是用户要求暂缓报告的 `app/main.py` 路由占位；main 已接入该结果模块，缺口仍存在。
- 修复验收：显式选择控制模型并单次授权后解释保存报告，调用/用量单列，失败不影响确定性报表；不允许点击展示报告自动调用。
- 验证：**AppTest 复现**。失败测试 `test_report_ui_explanation_has_an_authorized_action`。涉及 R4/R8。

### B07 / P2：解释接口过滤掉实际报告的效率指标与区间

- 路径：[agent.py:177-198](file:///Users/bytedance/Documents/AI-coding-eval/coding_eval/agent.py#L177-L198)。
- 具体触发：把 `build_report` 生成并保存的正常报告交给 `PlanningAgent.explain`；其请求时延中位数为 2 秒，输出 token 为 10。
- 实测：发给控制模型的内容变成 `{"first_content_seconds":{},"input_tokens":{},"output_tokens":{},"total_seconds":{}}`。白名单缺少 `all_attempts/completed_attempts/noncompleted_attempts` 这些中间层，也没有 quality_interval/coverage 等必要证据字段。
- 影响：即使接通 B06，控制模型仍无法依据实际效率、样本完整性和区间作出要求的解释。原有测试用扁平 `{"metrics":{"mean":0.3}}`，未覆盖真实报告结构。
- 修复验收：使用明确对应真实报告 schema 的安全摘要，保留数值、分母、缺失率、区间和 ID，继续排除答案、参考解、隐藏测试与凭据。
- 验证：**真实报告/解释模块 + 控制调用替身抓取输入复现**。失败测试 `test_explanation_receives_actual_report_metric_structure`。涉及 R4/R5/R8。

## Checklist 逐项状态

编号对应读取时 checklist 的顺序；“离线通过”仅表示指定层面的验证，不意味着真实供应商或题目执行通过。

| # | 条目摘要 | 当前验证状态 |
| ---: | --- | --- |
| 1 | 模型表单、两种 URL 模式 | 离线通过，含主入口配置与编辑 |
| 2 | 双协议、独立探针、不支持参数 | 模拟响应通过；真实服务待验证 |
| 3 | key 不入库/导出/日志、unknown | 假 key 的环境变量/会话/错误/分片回显通过；容器日志真实隔离待验证 |
| 4 | C01-C10 完整元数据与评分项 | 结构检查通过，十题均 draft，不冒充已验证 |
| 5 | 十题参考/错误解、C10 变体实际通过 | **blocked**，10 个真实题目测试 skipped |
| 6 | 确定性交错与各题契约 | 公开文本/AST 结构通过；交错、死锁、实际契约执行待 Docker |
| 7 | 导入编辑复制导出、安全 ZIP | 框架 ZIP 与编辑/复制 AppTest 通过；真实浏览器上传和下载未执行 |
| 8 | 未验证不正式评分、历史快照 | 离线通过，含最新 pointer 回选及导入清除 ready |
| 9 | 控制模型自然语言计划、无控制表单 | 模拟 API/Agent 与 AppTest 通过；实际 LLM 指令理解待验证 |
| 10 | 控制模型不获密钥/隐藏测试、不改授权 | 目录白名单、超预算/改权重拒绝、未授权零调用通过 |
| 11 | 被测输入仅声明提示及公开文件 | 请求内容/哈希测试通过，双协议主入口链路已跑 |
| 12 | 交错、重复、尝试保留、不择优 | 固定种子和重试测试通过；取消已知响应丢失见 B02 |
| 13 | 取消、超时、恢复、uncertain、请求上限 | 计数/崩溃恢复/互斥离线通过；B01/B02 阻断完整验收 |
| 14 | 首内容/首回答/总耗时与空事件 | 正常、推理先返回、空 delta、心跳的协议测试通过；取消例外见 B02 |
| 15 | 非流式 null、断流失败 | 离线通过；取消证据见 B02 |
| 16 | OpenAI/Anthropic 原字段及归一化 | 固定协议样例通过，缓存分项无重复相加；真实网关映射未验证 |
| 17 | 缺 usage/cache 不补零、不估算 | 离线通过，0 与 null 分开，报告带缺失统计 |
| 18 | 请求/排队/重试/判分耗时分开 | 正常及重试聚合通过；取消请求时延丢失见 B02 |
| 19 | 默认权重、和为 1、模板校验 | 离线通过 |
| 20 | 每 check 单归属、证据复算 | 单归属通过；矛盾维度证据未拦截见 B05 |
| 21 | 格式/运行/关键不变量零分 | 判分包装层模拟通过；实际容器归因/门槛执行未验证 |
| 22 | API失败与judge_error分开、不补维度 | 常规分类通过；真实隐藏测试异常归因待 Docker |
| 23 | 事后改权重新报告、父版本不变 | 报告与主入口 AppTest 通过 |
| 24 | 非root/禁网/无凭据/无socket容器 | Docker 命令结构及 mock 通过；真实内核隔离未验证，镜像冻结 B04 |
| 25 | Docker不可用blocked、无宿主回退 | blocked 路径通过；正式派发前仍缺预检 B03 |
| 26 | 原文/提取/补丁/日志/版本追溯 | 模拟产物与严格提取通过；取消丢失 B02、运行环境版本 B04 |
| 27 | 多模型/场景/任务/重复与指标 UI | AppTest 通过矩阵、选择器、并排答案、图表数据和指标表；不代表浏览器视觉验收 |
| 28 | 可比组、暂定、缺失分母 | 普通 missing/blocked/uncertain/不同组通过；待重试中断 B01 |
| 29 | 探索性、任务聚类区间与复现 | 确定性统计测试通过；不验证真实模型统计优势 |
| 30 | JSON/CSV/HTML、一致转义、解释 | 导出/HTML 转义/CSV 公式保护通过；解释路径及证据缺失 B06/B07 |
| 31 | 完整离线闭环与合成标记 | 主入口双协议诊断链路通过（模拟容器）；真实十题完整评分闭环 **未验证** |
| 32 | 启动/模板/指标/权重/API说明及忽略规则 | ZIP 模板、页面提示及 `.gitignore` 存在；09:45 快照无独立 README/复现手册，交付文档待补齐与验收 |

## 已验证的关键路径

- `tests/test_app.py::test_main_mock_protocols_to_judge_report_reweight_and_exports`：最新 main 中双协议、授权预览、真实 runner、模拟 Docker 边界、报告、重评分及三种导出通过；诊断报告保持 invalid、不生成正式排名，没有执行参考实现。
- `tests/test_api.py`：流式/非流式、UTF-8 分片、空事件、公开推理先到、最终 usage、HTTP 错误、截断、断流、连接/总超时、参数拒绝和假 key 脱敏通过。
- `tests/test_runner.py`：随机调度复现、实际协程并发、intent 先落库、重复启动互斥、在途取消、进程崩溃、持久化失败恢复及 uncertain 不重发通过。新增测试揭示的 B01/B02 不被这些通过结果抵消。
- 临时 `test_budget_exhaustion_and_resume_never_issue_extra_requests`：2 模型 × 3 样本、每样本 1 次重试、并发 4，总计恰好 12 次请求；11 次预算被拒绝，12 次耗尽后再次 run 不增加请求。计数上限不是服务商实际账单金额保证。
- 临时自然语言页面测试：未授权零调用，授权后仅一次控制调用，编辑成超预算被拒绝，合法预览仍需单独确认，过程中零评测请求且隐藏测试未进入控制输入。
- 最新 foundation/app 测试已验证模型禁用再启用时复用旧 hash、task 回选、legacy pointer 迁移及历史内容不改写；不把旧 pointer 问题再次报告。

## 精确未验证项

1. **Docker 阻塞的 23 项**：`tests/test_judges.py:383` 十个 C01-C10 参考/负解验证；`:393` 十二个实际 runner 归因用例（正常/错误答案、模型异常、语法错误、缺 check、测试 setup 异常、生成器 check、judge RuntimeError、exit(0)、伪造 stdout、frame introspection、HMAC 篡改）；`:414` 一个不同 check 的 tmpfs/进程隔离用例。均为 skipped，不是 pass。
2. **真实判分与防护效果**：C02/C03 可控竞争和死锁、C04 补丁回归、C10 四类变体及重复性、关键安全不变量、真实 timeout/OOM/PID 限制、非 root/禁网/宿主不可见/凭据不可见、seccomp/audit 与 Python 标准库兼容、容器清理效果均未动态验证。
3. **隐藏测试错误归因的额外风险**：种子 `checks.py` 普遍用 `_case` 将所有 Exception 包成 AssertionError，而 `container_check.py:441-455` 把 AssertionError 当普通失败。独立的测试辅助代码 OSError/NameError 可能被当成模型失败；本次只读发现，未执行这些源代码。需在 Docker 中加真实异常归因回归，不能因 mock judge_error 测试通过而签收。
4. **真实供应商**：OpenAI、Anthropic 及用户企业网关的鉴权、精确 model ID、参数支持、公开模型版本、SSE 实际时序/代理缓冲、最终 usage、缓存/推理 token 计费口径、限流/超时/取消后的远端计费均未验证。未获得真实 key 或调用授权，不发起付费验证。
5. **真实模型 Agent 行为**：自然语言理解质量、生成计划稳定性、解释正确性/引用 task ID 的事实一致性不能由固定输出替身证明；目前解释 UI 本身还被 B06 阻断。
6. **真实浏览器操作**：AppTest 验证 Streamlit 控件和服务器逻辑，不含页面视觉/响应式布局、文件选择器实际上传、下载文件落盘、独立 HTML 在浏览器打开、真实浏览器跨会话 key 生命周期。ZIP/HTML/CSV 安全性已有函数层验证，但不可替代这些操作验收。
7. **安装与交付复现**：最新 pyproject 的 httpx 声明已通过静态测试；未从干净环境安装 wheel/检查安装后题库资源、未验证声明的所有 Streamlit/Pydantic 版本组合，独立启动/复现手册仍待交付。
8. **正式完整成绩闭环**：没有“真实十题 ready → 真实模型请求 → 受限容器判分 → 正式排行”证据；目前通过的是合成诊断闭环。初始十题不能据此给出任何真实模型分数或行业排名。

## 文件指纹

用于区分后续其他 agent 修复。以下为最终测试结束附近的 SHA256；文件改变后应复跑相关失败测试，而不是沿用本报告。

```text
spec.md                         e05364559d5b89480871925be098da362f29f3891a6f20475e67635e012b82da
checklist.md                    d6521b07fb1019c9394f2940461c2881e60e7c728f3a51048c9b970032181b1a
coding_eval/api.py              434d257e2eded3b3bba33ae3e5626e83352e333bc5ffb5edf4d81922b570b09d
coding_eval/runner.py           c22356ff9c3a43de31da11e40205a6964f793c63457ebabbb9d2def64e3e08e9
coding_eval/reports.py          66aad92553e9b50910fcbb672ffa1176a66dc494ce5405c30cd4feb79830b022
coding_eval/agent.py            a1b5aacb4b1dbaa5e2c325c2f41a23a8074322d9dfc16016ea761ce1300de9e8
coding_eval/container_check.py  a4a885ea5fae543ca749d74106b012e2a5b8db1157b8d855359ef501fbbe5131
coding_eval/judges.py           2ed00544b4c768f6566c0c4ed0724c47290ce7d3cbe362dbece8705613cd129f
coding_eval/models.py           79ea7aba5e5fe19d56d2449d34a3aeed4a25d2f68ecbc34b89c99ed13ebbdcf8
coding_eval/storage.py          bd7a88090b0b5ae3dabf8efd4e45579c9ff51c49a3c547d7cddd78ed82848c61
app/main.py                     efd63a61e63b5d5f6c19fe02fa9f77011b9a7bd5ac603cf520feb49528dec8ad
app/report_page.py              d6acac475b91eef927d937c1cfd32da3b6049d23aed76a3fec0fbd5e4f053f73
pyproject.toml                  b4e2619f49bfb37ece2d9d203922b66112ab5034d64ca0440fe0734c4771240d
```

</details>
