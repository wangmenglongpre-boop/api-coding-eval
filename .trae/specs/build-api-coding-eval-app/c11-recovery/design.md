# 设计

不把任意测试 KeyError 全局改成候选失败：它也可能代表真正的测试缺陷。
新增 check_migrations 模块，仅匹配原始 C11 checks.py 精确 SHA256
15836d688c22b2102140b012a5c494d432bfc11c5b98ba79b0f2b42dcb8633f0。
在测试读取 geometry/scroll/update/snapshot 返回字段前断言结果为 dict，
具有 size/move/style、style 为 dict 且含当前轴 height/width。
参考解的 scroll/update 本身复用 snapshot，必须覆盖这些返回入口。
它们均来自题目已声明的 geometry 返回契约；
原独立性、实例隔离、非法输入原子性断言全部保留。
未知或已编辑测试不迁移；重复迁移幂等。

原题库、数据库任务及冻结计划源码与 hash 不变。
每次判分保存 effective_checks.py、check_migration.json，包含原始/有效源码 hash；
这是一项显式、可复验的判分器修订，不冒充冻结测试逐字不变。
判分版本升级 docker-python-v4。
仅在隔离 Docker 内运行候选；宿主只处理字符串和元数据。

恢复使用持有同批次 OS 锁的独立进程，硬性禁止 API generate，
确认已全派发且 max_retries=0，重判全部 135 份使报告版本一致。
追加判分版本并保留旧版本；用现有状态转换恢复 completed，
核对 attempts、request_intents、plan_hash 不变，追加新报告。
服务升级前确认无活跃批次，避免打断任务。
