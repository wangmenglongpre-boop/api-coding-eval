# LiveCodeBench 固定版本与每批抽样

将现有企业模拟题旁增加官方 LiveCodeBench code_generation_lite 累计 release_v6。
2026-10-27 查询到 release_latest == release_v6，固定 revision
0fe84c3912ea0c4d4a78037083943e8f0c4dd505；以后更新必须显式变更 pin。
默认 Easy/Medium/Hard 各 10 题，每题生成一次，模型共享样本。预算=模型数×题数×重复数×(1+重试数)。
本次不发任何付费模型请求。既有题库、批次和报告保持可读，旧内容哈希保持一致。
