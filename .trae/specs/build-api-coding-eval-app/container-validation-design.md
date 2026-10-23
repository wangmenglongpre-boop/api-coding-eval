# C03/C05/C10 容器验证修复

日期：2026-10-23。用户反馈三个场景无法发布为 ready。

## 问题与证据

- C03：参考解的 check_concurrent 被标记 candidate_runtime；C10：需要临时目录执行变体的四个检查被同样标记。容器日志均为 Trusted dependency mutation。
- 容器内临时诊断确认变化的是标准库 `tempfile.tempdir`：它首次调用 gettempdir/TemporaryDirectory 时会从 None 变为临时目录字符串。判分器在初始化前冻结了这个公开绑定，后续合法初始化被当作篡改。
- 修复缓存误判后，C10 的 subprocess.DEVNULL 仍触发文件访问限制：Python 子进程启动会打开 `/dev/null`，原先允许路径列表未覆盖该输出丢弃设备。
- C05 历史记录为 Docker daemon unavailable。当前 daemon 就绪后，参考解与 shallow_copy 负解能通过验证，无需改题。

## 修复

在容器加载常用标准库后、安装依赖身份保护前调用 `tempfile.gettempdir()`，冻结初始化后的真实绑定。保持所有候选的模块赋值、类/函数身份检查及受限执行边界，不把 tempdir 添加到忽略名单，不修改题目、参考解、负解或评分门槛。

文件访问仅增加精确的 `/dev/null`，不允许整个 `/dev`。候选输出仍不作为 C10 子进程判分依据；子进程退出码来自可信 runner，原有 AST 白名单与生产文件完整性检查保留。

判分版本由 docker-python-v2 升为 docker-python-v3，历史记录保留。临时诊断输出已移除。对三个已有任务再次执行参考解和全部负解，只有完整通过才追加 ready 版本到应用数据库；已有冻结评测计划不变。

## 验证策略

使用现存真实 Docker 测试验证十题参考解/负解、C03 针对性事务负例、C10 缺陷变体检出以及依赖防篡改和异常归因。运行全量离线回归确认 API、密钥、批次、报告未受影响。真实题目代码仅在受限容器执行，不调用模型 API。
