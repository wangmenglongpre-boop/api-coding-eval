# LiveCodeBench 验收记录 — 2026-10-27

## 数据

官方 livecodebench/code_generation_lite，release_latest == 累计 release_v6，
固定 revision 0fe84c3912ea0c4d4a78037083943e8f0c4dd505。
test.jsonl 与 test2..test6.jsonl 全部完成下载、LFS SHA256匹配、事务导入；
最终1055题：Easy322 / Medium383 / Hard350。本地压缩catalog约3.1GB，未加入Git。

`python -m scripts.validate_livecodebench --all --docker-smoke`：
全部1055题的原始行哈希、public/private安全解码、输入输出类型/函数实参及测试契约通过，
固定seed20261027抽30道唯一题。公开测试为空的官方题也保留全部隐藏测试。
没有删减原始测试，没有上游参考解，未制造参考/负解证据。

## 自动验证

- 完整离线回归：595 passed，101 skipped（真实Docker门禁，未当作通过）。
- 新增真实Docker十项：10 passed，覆盖stdin正/负解、函数类与顶层函数、调试输出、隐藏测试读取拒绝、超时、伪造verdict、路径函数篡改与判分hook自省拒绝。
- 共用runner此前专项：20 passed（初始八项与12项旧runner异常归因），最终守卫改动再次验证全部十项。
- 官方Codeforces 1873_A 全部5测试：手写正解100，错误解0。
- 官方LeetCode 2727 全部14测试：手写正解100，错误解0。
- compileall、git diff --check 通过；7个已有批次所有计划/题目哈希保持一致。
- wheel构建成功，确认包含新benchmark/解码器/容器/页面模块及原有种子资源。
- 没有调用付费模型；数据库LiveCodeBench请求数为0。运行中旧批次的用户请求独立保留。

## 浏览器与部署

TRAE-browseruse真实页面验证http://127.0.0.1:8502/?page=评测运行：
选LiveCodeBench，默认Easy/Medium/Hard各10、重复1、并发1、重试0、四模型预算120；
生成计划8b04ca539c50的冻结预览，seed1492174677，30题/120次生成；
无页面错误，计划下载按钮存在，费用确认未勾选时“开始冻结计划”禁用。
真实题库预览约13秒，压缩后服务RSS约158MB；先前全量展开导致约2GB内存和长预览的问题已修复。

8501/PID7520的旧批次71d4c335b3b9仍运行，未重启；
新版8502独立进程共用同一数据目录。报告展示模块可热更新，后端数据契约不在旧worker运行时重载。
新功能代码、SPEC、README与测试通过Git发布，不包含数据、凭据或本地产物。

## 适用边界

这是本应用的Docker适配评测，不能当作官方榜单结果：
Python3.11标准库；每测试6秒，整题120秒，512MB，输出最多16MB；
函数题逐测试重置实例，公开与隐藏测试完整保留。
对模型的真实端到端成绩需用户在页面确认预算后运行，不把手写夹具当作模型成绩。
持续优化：以后可显式升级数据pin、支持更多语言与更接近上游的进程复用/时间统计；
所有升级需新的任务/判分器版本，避免与旧结果混算。
