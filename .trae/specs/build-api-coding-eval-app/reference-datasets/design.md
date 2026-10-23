# 参考题设计

| 场景 | 来源 | 保留考点 | 适用边界 |
| --- | --- | --- | --- |
| C11 | Element Plus PR130，Vue/TS | 200/500尺寸样例、双轴transform/webkitTransform、resize | 独立几何/状态模型，无DOM或主包集成 |
| C12 | Faker PR1946，Python | Windows randint与其他平台uniform、共享时间生成、亚秒精度 | 注入平台/RNG，无完整provider安装 |
| C13 | Prometheus PR15548，Go | k=9999999999不窄化、histogram int64计数 | 显式32位目标参数，无Go386/race/TSDB/原子对齐 |
| C14 | ShardingSphere PR13809，Java | 仅过滤None别名、保留空串/重复别名/顺序 | 字典分组，无Maven/Oracle执行 |
| C15 | aiohttp PR9726，Python | 3.12.7/3.13.0/3.13.1版本边界、警告和内部状态 | 独立Connector，无原172项集成测试 |

源码来源包括飞书题目描述、前三题评测附件及 Faker 题目附件。
ShardingSphere 和 aiohttp 的两个评测附件返回403；改编依据题目描述及公开PR补丁，
未读取其原隐藏测试，不修改授权或反复下载。

`scripts/build_reference_catalog.py` 只拼接与解析源码文本，生成
`datasets/reference_tasks.json`，不执行参考解/负解/隐藏测试。
`load_reference_tasks()` 按模块相对路径读取资源，校验恰好五个不同场景和任务，
返回新建 draft；原 `load_builtin_tasks()` 仍恰好返回十题。
页面新增“加载缺失参考题”，只插入缺失ID，不自动运行验证或模型请求。

长驻Streamlit进程可能保留更新前的 `coding_eval.datasets`，此时页面不能直接
从旧模块导入新增符号。独立 `coding_eval.reference_datasets` 实现参考目录加载，
页面直接导入该模块；`datasets.load_reference_tasks()` 保留为兼容委托入口。
不用重载既有业务模块或重启服务，避免中断正在运行的评测。
回归模拟删除旧模块中两个新增符号，重新导入页面/目录并完成加载五题。

每题按功能/健壮/安全数据完整性/兼容/维护代理五维检查，权重沿用0.5/0.2/0.15/0.1/0.05。
安全项为强制门槛；无跨输入/实例污染是独立的核心约束。
负解从完整参考解施加单点变异，检查本身保持不变。

`scripts/validate_reference_catalog.py` 运行真实判分器，检查参考解全通过、
两份负解对应检查确实失败、未因语法问题失败，五题全通过后保存缺失ID的ready快照。
证据绑定内容hash及不可变image ID。JSON/ZIP与结果索引在 `data/reference_tasks/`。
已有ID不覆盖；重新导入一律draft，必须在目标环境再验证。

C15警告位置测试不读取运行时调用栈。生成器从测试AST计算构造器调用行号，
真实容器内比较 warning.filename/lineno；保留stacklevel=2约束和防栈窥探。

验证分两层：宿主仅AST/合同/ZIP/AppTest；Docker显式启用后运行每题参考解和全部负解，
读取result.json确认预定负解检查失败。没有模型请求或真实密钥读取。
