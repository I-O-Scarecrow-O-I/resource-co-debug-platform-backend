class AcceptanceMetricService:
    # 验收比较在服务层集中使用同一组阈值；前端不再重算判定口径。
    # 样本数用于整组判断，差异率和改进率分别约束输入和实验结果。
    REQUIRED_WORKLOAD_COUNT = 3
    REQUIRED_DURATION_SPREAD_RATE = 200.0
    REQUIRED_IMPROVEMENT_RATE = 15.0

    def build_success_rate(self, success_count: int, total_count: int) -> float:
        # 没有构建样本时返回 0，避免将空样本误报为全部成功。
        if total_count <= 0:
            return 0.0
        return round(success_count * 100.0 / total_count, 2)

    def improvement_rate(self, fifo_millis: int, optimized_millis: int) -> float:
        # 以 FIFO 的实测完工时间为基准；负值表示优化方案反而更慢。
        # 基准为零时无法求相对比例，按当前接口约定返回 0。
        if fifo_millis <= 0:
            return 0.0
        return round((fifo_millis - optimized_millis) * 100.0 / fifo_millis, 2)

    def duration_spread_rate(self, durations_millis: list[int]) -> float:
        # 用组内最短任务作为分母；比较的是同组任务耗时差，而非两轮完工差。
        # 缺少样本或最短耗时无效时，返回 0 表示不能达到差异门槛。
        if not durations_millis:
            return 0.0
        minimum = min(durations_millis)
        maximum = max(durations_millis)
        if minimum <= 0:
            return 0.0
        return round((maximum - minimum) * 100.0 / minimum, 2)

    def average_improvement_rate(self, improvement_rates: list[float]) -> float:
        # 各工作负载等权平均，不让任务较多或更耗时的工程占更大权重。
        if not improvement_rates:
            return 0.0
        return round(sum(improvement_rates) / len(improvement_rates), 2)

