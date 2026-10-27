# 常驻前端 ImportError 修复

用户反馈所有前端页面 ImportError。两个进程日志均证实：
app.main 在刷新旧 coding_eval.report_views 之前导入 report_page，
后者经 report_charts 导入新增 difficulty_rows，导致入口失败。
修复依赖顺序并保留后台worker与数据契约，不重启运行中的评测。
