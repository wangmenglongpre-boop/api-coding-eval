# 产品体验验证（2026-10-23）

## 实现与范围

`coding_eval/ui_queries.py` 使用 object_heads 查询当前版本；当前批次判分批量读取。
运行摘要移除原文和 SSE 响应，详情再加载；报告索引只投影元数据，所选版本才解析。
页面默认不渲染原始响应、任务 JSON 编辑、探针历史或 Agent 解释。
HTML/CSV/JSON 下载使用 Streamlit 1.66 callable 延迟生成，绑定不可变报告，下载不触发全页 rerun。
运行监控用 fragment，默认仅活跃批次自动刷新，表单不参与定时 rerun。
顶部与侧栏导航/URL 同步，卡片、表单与窄屏布局已调整。
主入口仅热更新 UI 和 UI 查询模块，明确保留 worker_registry 函数与资源身份；后台类不重载。

## 真实数据性能

只读已有 45 MB 数据库、4 个 completed 批次、30 条请求、6 个报告。
基准使用同一 AppTest 主入口和现有默认批次/报告；统计服务端脚本时间及元素 protobuf 字节，
字节不包括浏览器首次下载的 JS，脚本时间不等于点击到浏览器完成重绘。

| 页面 | 优化前暖重跑 | 优化后暖重跑 | 默认元素字节前 / 后 |
| --- | ---: | ---: | ---: |
| 数据集 | 0.0077 s | 0.0061 s | 16,480 / 8,613 |
| 运行 | 0.1534 s | 0.0223 s | 894,666 / 18,001 |
| 报告 | 1.1895 s | 0.1791 s | 2,602,802 / 66,395 |

报告服务端重跑减少约 85%，默认元素减少约 97%；运行页默认元素减少约 98%。
日志 `.tools/product-baseline.log`、`.tools/product-final-profile.log`。
这是当前数据集的局部观测，未声称所有数据规模的时延保证。

## 单测流程

Step1 已运行 prepare_test.sh，TMP_ROOT 已建立，EXEC_SOURCE 为空；utree 下载 API 未返回包地址。
Step2 Python 3.11/pytest、临时 SQLite、MockTransport、禁真实 OS 凭据、AppTest 风格已读取。
Step3 限定新查询模块；Step4 无已证实正确性 BUG_MAP，纯性能候选按技能降噪规则排除。
范围与过滤记录 `.tools/product-unit-scope.json`。
Step5 第一轮生成8例，`pytest tests/test_ui_queries.py -q` 8 passed；
真实数据集成随后发现旧 intent 缺 purpose 的兼容问题，修复并补足真实 intent 夹具，第二轮8 passed。
没有删除断言或把历史证据修改成测试数据。
Step6 技能 CI 覆盖率门禁跳过（无 CI 阈值/flux 来源）；额外产品验证的新增模块覆盖率
36条可执行语句全部覆盖，100%，日志 `.tools/product-query-coverage.log`，不冒充全仓覆盖率。
Step7 已执行 utree flush，CLI 因下载失败不存在，exit127；本文件保存可复验的替代记录。

新增 `tests/test_product_experience.py` 集成覆盖默认无导出/原文、按需完整响应、
运行详情无 API、顶部/侧栏/URL同步、热升级资源身份、陈旧 package alias。
更新历史页面测试仅增加新详情入口操作；评分、缺失、授权、预算等原断言保留。
独立验收中旧 JSON 编辑/解释入口同样更新。
一次并发运行发现旧凭据 AppTest 的3秒启动超时，改为10秒测试等待；未修改产品请求时限。
最终完整合并命令 `.venv/bin/python -m pytest -q tests .trae/specs/build-api-coding-eval-app/test_acceptance_tmp.py`：
**587 passed、85 skipped（17.37s）**，日志 `.tools/product-final-suite3.log`。
查询单测8例与产品交互6例为本次新增14项，未对历史计数重复求和。

## 浏览器

使用独立临时 Chrome profile、现有服务 8501 和真实已保存数据，不读取密钥，不点击付费操作。
已验证桌面1440px总览、答案切换、按需原始响应、完整HTML真实下载落盘、
运行详情、题目编辑入口，以及390px页面/答案切换。
HTML下载17,941,445字节，原文和证据仍完整；默认页面不提前生成此文件。
桌面首次打开约2.52秒，首次答案切换约1.90秒（包括浏览器加载/重绘），与脚本基准口径分开。
390px文档宽度390、三张卡片各364px、无异常、无整页横向溢出。
视觉检查发现窄屏顶部导航与固定页眉重叠，增加顶间距后再验。
最终专项窄屏验证：导航top80px、固定页眉bottom60px，卡片364px×3，
页面宽390px、无异常、答案切换通过，日志`.tools/product-mobile-final.log`。
整页最后复跑`.tools/product-browser-final2.log`：桌面冷打开4.34秒、答案切换1.16秒、
HTML下载/运行详情/任务编辑通过；该次窄屏初始检查仅等待标题，卡片尚未挂载，
由上述等待三张卡片的专项验证补足，未把空卡片数组当作布局通过。
`.tools/product_browser_check.py` 为可重跑的只读验证脚本；
`.tools/product-report-desktop.png`、`product-report-mobile.png`、`product-run-desktop.png` 保存截图。
内置浏览器桥接存在点击拦截/截图不可用，因此采用独立Chrome完成真实交互。

## 保留边界

本轮未新增供应商请求、未读取/写入真实密钥，未重跑与UI改动无关的85项Docker测试。
当前题目15个ready、运行/请求/报告数量保持原样；服务PID51232没有重启。
尚未验证供应商在新限额下的成功率或任意大规模历史库；现有原始评分规则与费用授权继续生效。
CLI工具限制不影响pytest、真实浏览器、分发包验证。
最终wheel/sdist重建成功，日志`.tools/product-package-final.log`；
安装到`.tools/product-final-wheel`，在`/tmp`从安装目录导入四页、新查询和15题资源通过。
最终health=ok、PID51232未变；只读核对4个批次、30个请求、6个报告、15个ready任务，与基线一致。
