# 设计

- 独立 plan_preview 模块供 UI 直接导入，runner.create_plan 保留兼容委托，避免旧进程缓存旧实现。
- 首先以仅供内部检查的 draft 副本验证 RunPlan 参数、预算、场景覆盖与非空唯一选择；不持久化，不伪造 ready。
- ready 任务模板与维度权重相同则复用原快照、证书及 hash，批次场景权重独立冻结在 RunPlan。RunPlan 原验证器已只要求任务维度/模板一致。
- 真实维度/模板变化继续清除证书、执行参考解/负解验证并核对内容与固定镜像不被替换；draft 行为保持原约束。
- 回调呈现任务数、任务 ID、复用或容器验证阶段。验证异常保留任务 ID、结果和证据路径；Pydantic 仅展示 loc/msg，不回显 input。
- 手动预览不因控制模型授权勾选而调用 API；编辑 agent 计划仍检查授权范围，使用同一新预览入口。
- 实测长驻服务仍缓存旧 run_page；主入口检测 PLAN_PREVIEW_REVISION，不匹配时只 reload 页面模块一次。worker_registry 的缓存函数体不变，资源保留，后台模块与已存在 worker 不重载。刷新后 DOM 已显示新提示，无 ImportError。
