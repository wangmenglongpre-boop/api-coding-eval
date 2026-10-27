# 验证（2026-10-27）

## 根因

批次 97aaa0492b8b47d09e4a4d7a1c689179 共135次请求，127 completed、8 timeout。
5份异常都来自C11状态检查；独立Docker复现保留了外层AssertionError，
内层KeyError来自tests/checks.py。候选snapshot只返回viewport/content/offset/horizontal，
不满足题目要求的geometry字典。v3单独重判仍复现，不能靠重启Docker解决。

## 检查修订

精确匹配原C11测试源码SHA256后，在原检查读取返回值字段前验证结构。
snapshot以及复用snapshot的scroll/update返回入口均覆盖。
不修改异常通用归因、不执行宿主题目、不更改题库或冻结计划。
每份C11判分归档有效源码和原始/有效hash，修订归属docker-python-v4。

## 测试

最终完整离线：
`pytest -q tests .trae/specs/build-api-coding-eval-app/test_acceptance_tmp.py`
588 passed、91 skipped，11.58s，日志`.tools/c11-offline-final.log`。
91个Docker跳过不计pass。

开启CODING_EVAL_DOCKER_TESTS=1的C11专项：
9 passed、11 deselected，19.80s，日志`.tools/c11-docker-regression.log`。
包含正常snapshot、缺字段、空style、style错误类型、非字典返回、
真正的未知测试KeyError、原C11参考解及两份负解验证。
第一次专项发现参考解scroll/update也复用snapshot，补全这些入口后通过；
未删除失败断言。

wheel构建成功，已检查check_migrations模块和v4 harness包含在包内，
日志`.tools/c11-package.log`。

## 本地运行环境

原.venv指向/tmp中的Python副本，启动时报encodings缺失。
使用已安装的同版本Python3.11.17稳定路径恢复home与python符号链接，
.venv能导入Streamlit1.66与pydantic；不更改依赖版本、不读取密钥。

## 批次恢复

恢复脚本`.tools/recover_c11_batch.py`先保存SQLite备份，持有同批次OS锁，
并发2统一判分；客户端generate硬性拒绝任何API请求。
最终135份判分全部completed/v4，批次completed。
127条API completed、8条timeout保留；5份原C11判分异常现在正常完成，
按原critical检查规则质量0分，其余4份C11质量100分。
没有把completed状态解释成代码全部正确。

恢复前后逐条比较attempts、所有request_intents、冻结plan_hash；
逐条确认原judge不可变版本仍在，旧报告列表保持不变。
追加新报告162789e9d760408f9d4c8e90e0c2a3da，状态complete，
HTML存于data/report_overviews/report-162789e9d760408f9d4c8e90e0c2a3da.html。
原请求135、新增API请求0；未读取系统钥匙串，恢复预检使用不可派发占位凭据。
证据摘要`.tools/c11-recovery-evidence.json`，过程日志`.tools/c11-batch-recovery.log`，
备份`.tools/c11-before-recovery.sqlite3`，均由.gitignore排除。

确认全库没有queued/running批次，并核实旧服务PID51232工作目录及确切命令后，
正常停止该项目服务并以修复后的.venv重启，未停止其他进程。
新服务PID7520，http://127.0.0.1:8501/_stcore/health返回ok。
无需用户重新调用模型；刷新页面，选择该批次与新报告即可查看。
