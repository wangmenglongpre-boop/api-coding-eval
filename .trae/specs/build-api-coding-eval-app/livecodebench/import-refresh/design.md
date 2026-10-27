# 设计

- 独立 ui_bootstrap 模块使用进程内 RLock 串行刷新展示模块。
- 逐个刷新依赖后再导入消费者：report_views → report_charts → report_page。
  不预先 import 全部消费者后才循环 reload。
- 依赖变更传播到消费者，更新 from-import 的函数引用；
  缺少 difficulty_rows 时即使时间戳相同也必须刷新。
- 保留旧 worker_registry 资源函数及所有 backend 模块/类；
  不重载 models、runner、API，不发模型请求。
- 回归模拟 sys.modules 中旧依赖缺失新增导出、消费者导入失败后被移除，
  验证真实入口四页可加载，以及 registry/RunPlan/BatchRunner 身份保持一致。
