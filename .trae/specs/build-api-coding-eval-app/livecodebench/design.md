# 设计

- 六个分片按上游 LFS SHA256 验证后写入本地压缩 SQLite catalog，原始 JSON 保留，不执行远程 loader。
- 全题池1055题，Easy322/Medium383/Hard350；原始公开测试可为空，合并公开/隐藏测试必须非空。计划中保留原始压缩private字符串，避免展开后数GB的序列化与内存开销。
- 官方 private cases 的 base64/zlib/pickle 只用禁止所有全局类加载的受限解码器，结果必须是 JSON 字符串；限制解压大小。
- 按难度分别在排序后的题池用固定 seed 无放回抽样，冻结题号、题目与测试哈希、数据 revision、抽样数量与 seed。
- 新增可选 benchmark/sampling 契约；为空时序列化不输出字段，保留旧任务/计划内容哈希。
- benchmark readiness 表示原始测试结构、来源、容器环境验证，不伪造不存在的参考解或负解证明。
- readiness 再与本地已验SHA256的原题比对，拒绝改写提示、测试、来源或运行限制后冒用官方pin；只允许冻结镜像ID与计划场景权重变化。
- 原始 public+private tests 全部用于单个 all_tests 必过评分项，功能正确性权重 1。场景对应难度，可分别报告。
- 候选只接收题意、starter、公开示例。Docker 内独立子进程只接收当前 input，禁止读取测试目录、进程内存、网络及替换判分进程；签名 verdict 由父进程生成。
- stdin 对齐上游逐行 strip 与精确 Decimal 比较，functional 对齐 JSON 实参、Solution 或顶层函数、顶层 tuple→list 与精确相等。
- 明确这是本应用的 Docker 适配评测：Python 3.11 标准库、每测试 6 秒、整题容器最多 120 秒、512MB、输出最多16MB；函数题每测试重置实例，不是官方榜单得分，受超时/包装差异影响。
- UI 独立入口：准备本地 catalog、每批生成 seed，可手动复现；正式开始沿用现有费用确认。
- 报告/导出保留 pin、sampling 与难度；只有一个评分维度时不画雷达。
- 运行中的8501旧进程保留；新版8502共用数据库并保留跨进程批次锁，旧进程只提示新版入口，不重载后端类。可独立重载报告展示模块。
