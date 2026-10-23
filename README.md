# API 编码评测

单用户本地 Streamlit 应用：配置原始模型 API、管理企业编码任务、冻结并执行评测计划、比较和导出结果。TRAE 不参与被测模型的提示或执行循环。初始任务为自建模拟题，不是客户生产数据。

## 当前验收状态

2026-10-23 已完成多轮产品体验优化：顶部导航与窄屏布局、运行进度卡片及独立刷新，
原文/原始响应/题目编辑按需加载，报告版本精简读取，JSON/CSV/HTML 点击时才生成。
当前真实数据的暖脚本重跑：结果页约 **1.19 → 0.18 秒**、运行页 **0.15 → 0.02 秒**；
默认结果页元素 **2.60 MB → 66 KB**，运行页 **895 KB → 18 KB**。
这些是服务器渲染基准，不是浏览器点击总时间。
新增14项查询/交互回归；完整合并离线验收 **587 passed、85 skipped**，
新增查询模块覆盖率100%（不代表全仓覆盖率）。
真实Chrome完成桌面/390px、答案切换、原始响应、运行详情、题目编辑入口和完整HTML下载。
当前服务不用重启；刷新即可加载新界面，后台worker资源保留。
实现与证据见 [产品体验验证](.trae/specs/build-api-coding-eval-app/product-experience/verification.md)。

2026-10-23 已修复“计划预览卡住/计划无效”：批次场景权重变化不再重复验证同维度的 ready 任务；
预算、选择及场景覆盖先检查，再执行真正需要的容器验证。页面显示当前题目和进度，
失败时明确指出任务、判分原因、未通过检查及日志路径，配置错误不回显原输入。
当前三个模型、十五题默认预览实测约 **0.02 秒**，原任务快照/证据完全保留；
未调用模型、未重复执行 Docker。长驻服务通过页面版本检查更新旧模块，刷新即可生效，
worker 资源保留，无需重启。设计与验收见 [plan-preview-fix](.trae/specs/build-api-coding-eval-app/plan-preview-fix/design.md)。
最新完整离线验收 **573 passed、85 skipped**。

2026-10-23 已优化运行失败诊断与报告概览：
报告首页展示质量总分、五维雷达图、平均输入/缓存命中/输出 Token；
默认统计全部实际请求（包含失败和重试），可切换仅完成或仅未完成，
每个指标显示有效请求数及缺失率，未上报不补零。HTML 导出包含离线 SVG 图表。
运行页区分思考超时、回答中断、截断、鉴权及容器判分失败；
新执行器遇到 401/403 后暂停同模型后续派发，修复后显式恢复，其他模型继续。
Seed 的当前配置已从 300 秒/8192 调整为 **600 秒/32768**，思考参数不变；
需重新生成计划，旧批次恢复仍使用冻结限额，增加额度不保证请求成功。
该次未调用模型 API、未重启服务。完整离线验收 **565 passed、85 skipped**，
运行服务已实测加载图表，独立 HTML 的雷达图已完成浏览器视觉检查。
最新批次的一条 Docker 临时判分失败已仅用保存答案补判成功，并统一当前判分器版本，批次恢复 completed，
没有新增请求或覆盖原始请求与旧判分版本；旧暂定报告仍保留，刷新后选择新版本。
增量规格见 [run-report-improvements](.trae/specs/build-api-coding-eval-app/run-report-improvements/design.md)，
现有真实证据的独立报告位于 `data/report_overviews/`。

2026-10-23 已补充飞书《基础模型评测》的五道参考题，编号 **C11–C15**。
五题均为明确标注的 Python 核心逻辑改编，记录原语言与 PR；
参考解每题五项检查全通过，十份负解均由预定检查检出。
本地数据库 **C01–C15 全部 ready**，导出和证据位于 `data/reference_tasks/`。
新任务加载默认 draft，导入到其他环境需重新容器验证；旧冻结计划需重新生成才能包含新增任务。
此前完整离线回归 **556 passed、85 skipped**；前次新增测试（含五题真实 Docker 验证）
**12 passed**。wheel/sdist资源及安装后异目录加载检查通过。
新增目录使用独立模块导入，兼容长驻Streamlit进程的旧模块缓存；页面遇到旧的导入错误时刷新即可，无需重启正在执行评测的服务。

原 C03/C05/C10 容器问题已修复：先初始化 tempfile 缓存再冻结依赖，精确允许 `/dev/null`，
没有修改原题或降低评分门槛。原 Docker 回归 182 passed、1 daemon unavailable；
受阻用例单独完整复跑 1 passed。真实供应商与浏览器完整验收仍保留。

<details>
<summary>此前离线交付与浏览器补验快照（下述 draft/blocked 为历史状态）</summary>

2026-10-23 后续密钥优化已加入 macOS 系统钥匙串，新增 10 项离线验证；最新完整合并结果为 **548 passed、80 skipped**。真实钥匙串授权由用户首次保存时验证。本地现有服务健康检查正常；下方保留此前整体验收快照。

截至 2026-10-23，独立复验已确认 T1/T2/T4/T6 离线完成；T3/T5/T7 已实现部分能力，但真实 Docker、客户端和浏览器验收仍未完成，**不表示首版整体验收通过**。

- 四页已接入现有实现，不再显示主入口占位。
- 完整 `tests/`：`525 passed, 80 skipped`；独立验收：`13 passed`，其中原 B01-B07：`7 passed, 6 deselected`。最终合并：**`538 passed, 80 skipped`**。这些集合有包含关系，不重复累加；旧 `451/57` 为历史快照。
- B01-B07 均为 **closed offline**：取消与重试证据、派发前预检、镜像冻结、证据复算、解释授权与摘要通过。未以删除核心断言换取通过，真实动态验证仍待完成。
- 历史集成记录曾验证 sdist/wheel 构建、安装后十题资源/四页导入与本机健康检查；本轮未重建安装包或启动服务，不把历史结果冒充最新独立复验。
- 模拟 OpenAI/Anthropic 请求经过真实编排、提取、判分逻辑、SQLite、页面和报告导出；容器执行结果为测试夹具，不是真实模型成绩或种子验收证据。
- 页面测试覆盖导航、配置保存及禁用后启用、内置题加载、blocked 展示、确认后启动、报告与事后权重调整；既有测试覆盖停止/恢复和任务 ZIP 校验。未宣称完成真实浏览器文件上传验收。
- 本轮 sandbox 禁止启动 Docker；没有启动或探测真实 daemon，也未在宿主执行题目。历史记录曾报告 socket 不存在，不能当作本轮探测结果。
- **10 个数据集动态 validation blocked**：参考解/负解及 C10 缺陷检出没有真实容器通过证据，内置加载保持 `draft`。80 项 Docker 跳过：10 项种子验证、13 项执行隔离/归因、34 项数据集行为、23 项判分依赖防篡改/异常归因，均不计 pass。
- 未调用真实供应商 API，未产生本次真实测评成绩；真实双协议、流式 usage 与供应商扩展字段仍需凭据及明确调用额度验证。
- 报告页已接入控制模型选择、单次费用授权和“授权并解释此报告”按钮；请求/用量单列，刷新或导出不会自动调用。解释只接收已保存报告的安全数值摘要，失败不改分；真实 LLM 解释质量未验证。

详细证据与未完成项见 [.trae/specs/build-api-coding-eval-app/verification.md](.trae/specs/build-api-coding-eval-app/verification.md)。

### 本地交互补验

主流程随后使用 `.venv/bin/python` 启动 `http://127.0.0.1:8501`，浏览器成功加载模型配置页并保存一个无密钥的界面检查配置；该配置现已禁用，未发起请求。侧栏单选控件的浏览器自动化切换未成功，因此不宣称四页浏览器交互或文件上传下载全部通过。四页控件逻辑的 AppTest 结果仍有效。

已向本地数据库加载全部 10 个内置任务，状态均为 `draft`，没有执行任务源码。正式评测前需在用户终端启动 Docker Desktop，确认 `docker info` 成功，再在“数据集管理”执行参考解与负解容器验证。此前启动 Docker 的操作被工具沙箱拒绝写入宿主日志目录；不得通过宿主执行题目来绕过此限制。

</details>

## 本地启动

宿主需要 **Python >=3.11、Streamlit >=1.66**（按需下载与独立监控）；当前批次锁使用 `fcntl`，支持 macOS/Linux；Windows 请使用 Linux 环境。题目运行环境固定为 Python 3.11，与宿主版本分开。

在项目根目录执行：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m streamlit run app/main.py --server.address 127.0.0.1 --server.port 8501
```

浏览器访问 `http://127.0.0.1:8501`。项目 `.streamlit/config.toml` 默认仅监听本机，保留 CORS/XSRF 防护、关闭遥测，并将上传限制为 8 MiB。不要将此无账号应用直接暴露到公网。

默认数据目录为当前目录下 `data/`；可在启动前设置：

```bash
export CODING_EVAL_DATA_DIR="$HOME/.local/share/coding-eval"
```

SQLite 保存配置版本、冻结计划、每次请求和报告；`artifacts/` 保存答案、提取结果、测试日志及证据。备份时停止 worker 并备份整个数据目录。模型输出、隐藏题目也可能包含业务敏感信息，不应提交仓库。

## 使用流程

1. **模型配置**：填写协议、地址、精确 model ID 和参数；保存后可禁用、重新启用。在 macOS 填入密钥并点击“保存密钥到系统钥匙串”，刷新或重启后可自动使用；也支持仅会话输入及环境变量名称，密钥不写入模型配置。
2. **连接探针**：选择流式/非流式并确认费用后才请求；探针与评分隔离，记录实际 usage，不估算货币价格。
3. **数据集管理**：点击“加载缺失内置题”加载 C01–C10，“加载缺失参考题”加载 C11–C15；不覆盖已有任务。可编辑 JSON、复制场景、下载 ZIP 模板、导入导出任务。
4. **验证题目**：Docker 就绪后点击“容器验证参考解与负解”。验证成功才追加绑定内容 hash 的 `ready` 版本。编辑、导入、修改评分会清除或重新验证证据。
5. **评测运行**：选择模型、任务、重复次数、并发、预算与权重，生成冻结预览，再确认费用并开始。已验证且维度/模板不变时复用任务证据，场景权重在计划中单独冻结；真实维度变更才重新验证，显示每题进度。预算不足在容器执行前报错。没有控制模型也可完整使用表单；agent 计划仍需服务端校验和用户确认。
6. **停止/恢复**：停止派发并取消在途请求；可能计费但丢失响应的请求标为 `uncertain`，恢复不会重发。默认不重试，显式重试保留每个 attempt，不择优覆盖。
7. **结果对比**：从当前证据生成独立报告版本，查看矩阵、维度、原文、指标及缺失率，导出 JSON/CSV/HTML。事后重评分追加报告，不覆盖原证据；可另选控制模型并单次授权解释，调用/用量与评测分开。

顶部可直接切换四页，导航同步到URL。结果页“答案对比”查看任务/样本/重试，
原始响应再勾选加载；运行页勾选“查看原始结果与判分”查看完整记录。
数据集 JSON 编辑与探针历史使用显式开关。下载按钮在点击时生成文件，不重跑页面；
活跃批次默认每3秒独立刷新监控，可手动关闭，不定时刷新上方计划表单。

诊断模式允许 draft，但仍可能产生 API 费用；它不能绕过 Docker 隔离，且不能形成正式排名。未完成报告是暂定；`blocked`/`judge_error` 不计能力分，并阻止完整正式排名。

## API 与 Usage

本项目提供 Python 接口，不提供额外的 REST 服务。

| 配置 | OpenAI Chat Completions | Anthropic Messages |
| --- | --- | --- |
| `protocol` | `openai` | `anthropic` |
| `url_mode="base"` 示例 | `https://api.openai.com/v1` | `https://api.anthropic.com/v1` |
| 自动接口路径 | `/chat/completions` | `/messages` |
| `url_mode="full"` | 原样使用完整请求 URL | 原样使用完整请求 URL |
| 输出限制 | `max_tokens_field` 默认 `max_tokens`，可选 `max_completion_tokens` | 固定 `max_tokens` |
| usage 映射版本 | `openai-chat-v1` | `anthropic-messages-v1` |

参数以 `parameters` JSON 保存；不允许覆盖模型、提示、流式开关、输出上限或注入工具/历史。未知参数交由服务明确校验，不静默丢弃。`capabilities.stream=false` 使用非流式；`capabilities.stream_usage=false` 不请求 OpenAI 最终流式 usage。能力未知不能声称已对齐。

`max_tokens_field` 是新增的顶层模型配置字段，页面可选择并持久化；实际限额仍取 `max_output_tokens`。仅按服务声明显式选择字段，不自动换字段重试或额外消耗请求。该默认值不保证所有兼容网关均接受；缺少新字段的旧冻结 ModelConfig 快照/数据库升级兼容尚未验证。

密钥优先级为当前会话 > 系统钥匙串 > 指定环境变量。macOS 用户可在模型配置页输入后点击“保存密钥到系统钥匙串”；已有会话密钥可直接点击保存，无需再次输入。关闭页面、重启应用后，探针、批次和控制模型自动读取。首次保存/读取可能需要允许 macOS 钥匙串访问。更换服务地址后需要重新保存，修改采样参数不影响密钥。

“清除会话密钥”只清临时覆盖；“删除系统钥匙串密钥”删除当前配置对应的持久密钥并清除会话覆盖。保存失败会提示并保留临时输入供重试。持久保存目前仅支持本机 macOS；Linux/容器使用会话输入或环境变量。钥匙串不可用时不会降级为明文文件。

环境变量字段填写**名称**（例如 `OPENAI_API_KEY`），其值通过进程环境提供。`.env` 不会被自动加载。公网服务默认要求密钥，批次启动/恢复前检查全部模型；缺失时不会派发请求。匿名服务可在能力声明中填写 `{"api_key_required": false}`。禁止将密钥写入 URL、参数、文件或版本库；`.gitignore` 已忽略 `.env*`、Streamlit secrets 和本地数据。

以下示例**会产生一次真实请求及可能的费用，仅在明确授权后执行**，不是离线演示：

```python
import asyncio

from coding_eval.api import APIClient
from coding_eval.models import ModelConfig

config = ModelConfig(
    id="gateway", display_name="我的模型", protocol="openai",
    url="https://api.openai.com/v1", url_mode="base",
    model_id="替换为服务实际支持的精确模型标识",
    api_key_env="OPENAI_API_KEY", max_output_tokens=64,
    capabilities={"stream": False},
)
result = asyncio.run(APIClient().generate(
    config, "Reply only with OK.", "Connection probe.",
    run_id="manual-probe", task_id="probe", purpose="probe",
))
print(result.status, result.error_code, result.metrics.model_dump())
```

`APIClient.generate` 本身不持久化；正式评测使用 `create_plan`、`Storage.save_run`、`BatchRunner`。单独探针应保存为 `probe` 对象，控制模型调用单列为 `control`，不要混入评测样本。

| 指标 | 口径 |
| --- | --- |
| `first_content_seconds` | 发出请求至首个非空生成事件，包含服务公开返回的推理内容；不保存推理正文 |
| `first_answer_seconds` | 发出请求至首个非空最终回答文本 |
| `total_seconds` | 请求至正常流结束及最终 usage；失败时为截至失败时长，不能当作成功耗时 |
| `input_tokens` | OpenAI `prompt_tokens`；Anthropic 非重叠的 input、cache_read、cache_creation 合计 |
| `cache_read_tokens` | 服务明确提供的缓存读取量；不可再次加到归一化输入总量 |
| `output_tokens` | 服务端输出用量，是否含推理由 `output_includes_reasoning` 标注 |
| `reasoning_tokens` | 仅服务公开报告的计数，未知为 null |
| `raw_usage` / `usage_mapping_version` | 原始字段及映射版本；未知供应商字段不猜测 |

缺失为 `null`，不是 0；非流式两种首 token 时间均不可测。SSE 时间是客户端可观测近似，不是服务器内部逐 token 时间。排队、逻辑任务（含重试间隔）及判分耗时另列；不以文本长度估算计费量。

## 题库与隔离

| 场景 | 任务 | 本地当前状态 |
| --- | --- | --- |
| C01 | 金额与折扣计算 | ready |
| C02 | 库存并发扣减 | ready |
| C03 | 支付回调幂等 | ready |
| C04 | SEARCH/REPLACE 精确重构 | ready |
| C05 | 遗留接口兼容 | ready |
| C06 | HTTP 客户端重试 | ready |
| C07 | SQL 查询优化 | ready |
| C08 | 租户权限隔离 | ready |
| C09 | ETL 清洗与去重 | ready |
| C10 | 单测补充与缺陷检出 | ready |
| C11 | Element Plus 滚动条几何与状态改编 | ready |
| C12 | Faker 跨平台亚秒时间改编 | ready |
| C13 | Prometheus 32位 k/int64 histogram 改编 | ready |
| C14 | ShardingSphere 无别名分组改编 | ready |
| C15 | aiohttp cleanup_closed 版本与警告改编 | ready |

每个场景初始只有一题。种子文件 `datasets/seed_tasks.json` 通过 setuptools package data 随 sdist/wheel 安装，安装后仍在 `coding_eval` 同级目录读取；不会依赖启动时的工作目录。`load_builtin_tasks()` 只读取文本，不执行代码。

五道参考改编题在 `datasets/reference_tasks.json`，独立使用 `load_reference_tasks()`。
`scripts/build_reference_catalog.py` 可重建惰性 JSON；在 Docker 就绪时执行
`PYTHONPATH=. .venv/bin/python scripts/validate_reference_catalog.py` 验证、导出并保存缺失任务。
本地导出包含五份单题 JSON/ZIP 和十五份判分结果索引，导入 ZIP 会清除验证状态。
完整来源、适用范围和五道公开接口见
[参考题设计](.trae/specs/build-api-coding-eval-app/reference-datasets/design.md)。
它们不覆盖 Vue DOM/包集成、Go386/race/TSDB、Java/Maven/Oracle 或 aiohttp 原仓库集成测试。

ZIP 使用 `task.json` 加 `public/`、`reference/`、`tests/`、`negative/<名称>/` 精确 UTF-8 文件；以页面下载模板为准。限制压缩包 8 MiB、展开 16 MiB、单文件 2 MiB、256 文件，拒绝穿越、链接、重复/大小写别名及元数据不一致。导入不在宿主执行代码。

在允许 Docker 的环境中，就绪须以 `docker info` 和镜像准备成功为准。draft 默认允许官方 `python:3.11-slim`，也可指定允许的版本/digest；参考解验证时解析并绑定不可变 `sha256:<64 位十六进制>` 本地 image ID，验证成功后发布 ready。正式 `create_plan` 拒绝仅有可变 tag 或尚未验证为本地 ID 的快照，不在冻结时偷偷换镜像；每次正式启动/恢复检查全部任务契约和冻结 ID，失败时零 API 请求。旧 tag 任务需重新容器验证，不能手改证据冒充 ready。

首版拒绝额外依赖，不能临时联网安装。未 pin 镜像的拉取仅发生在宿主准备阶段；已 pin ID 缺失则阻断，不回退 tag。候选运行命令始终禁网、非 root、只读根目录，限制 CPU/内存/进程数/时间，不挂载密钥或 Docker socket。原80项动态门禁已经实际执行并取得通过证据。

**严禁为了验证而在宿主导入或运行答案、参考实现、隐藏测试或负解。** Docker 不可用时只允许配置、导入、预览及离线框架测试，动态判分为 blocked。

## 评分与版本

默认维度权重为功能 50%、健壮性 20%、安全 15%、遵循兼容 10%、可维护性代理 5%，不是官方行业权重。单次质量分为加权维度和，关键门槛失败为零；任务分对所有独立样本取平均，场景分对任务取平均，综合分按冻结场景权重加权。全通过率以有效评测样本为分母，API 失败计零且单列；评测器故障不计能力分。

只有相同题库、覆盖、评分配置和预算组可正式比较。时延和 token 不混入质量分，不推算价格；当前15题结论仅具探索性，区间按任务聚类重采样，不将重复样本当作独立题。

`Storage.save_object` 保留不可变内容版本，用 `object_heads` 保存当前 hash。旧内容再次保存会重新成为 latest，不改历史 payload/首次创建时间；旧数据库自动补指针，重开不重置。`list_objects` 返回**全部版本**，历史在前、active 在后，现有页面按 ID 去重得到最新；报告仍可读取全部 judge 历史。冻结计划、attempt 和报告独立追加，不能覆盖。

## 复验

离线完整测试（不请求真实模型、不执行题目代码）：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:tests CODING_EVAL_DOCKER_TESTS=0 \
  python -B -m pytest -q -p no:cacheprovider tests \
  .trae/specs/build-api-coding-eval-app/test_acceptance_tmp.py
```

仅在 sandbox 允许且 Docker 完全就绪的环境中启用真实隔离测试。以下覆盖原80项和新增5项动态验证及相关离线测试，会在容器执行参考解、负解及错误归类，不调用付费模型：

```bash
docker info
CODING_EVAL_DOCKER_TESTS=1 python -m pytest -q \
  tests/test_judges.py tests/test_dataset_integrity.py tests/test_judge_trust.py \
  tests/test_reference_datasets.py
```

测试在临时目录保存证据，不会自动将应用题库发布为 ready；在页面按题验证并保存就绪版本。本地15题已保存ready，完整T7仍待真实浏览器上传与有授权的供应商冒烟。

构建分发包：

```bash
python -m pip install build
python -m build
```

离线测试通过不能替代 Docker、真实浏览器或供应商验证；spec/tasks/checklist/verification 已同步真实状态，T3/T5/T7 仍未整体完成。
