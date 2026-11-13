# 统一返回百分数而非比例小数，由调用方决定是否满足验收阈值。
class AcceptanceMetricService:
    # 三项常量来自调度验收口径；它们不是命令执行时的资源上限。
    REQUIRED_WORKLOAD_COUNT = 3
    REQUIRED_DURATION_SPREAD_RATE = 200.0
    REQUIRED_IMPROVEMENT_RATE = 15.0

    # 分母无样本时返回可序列化的零值；样本是否有效需另行判断。
    def build_success_rate(self, success_count: int, total_count: int) -> float:
        # 没有样本时不做除法，零值不能作为验收通过证据。
        if total_count <= 0:
            return 0.0
        return round(success_count * 100.0 / total_count, 2)

    # FIFO 实测完成时间是基准；优化较慢时保留负值。
    def improvement_rate(self, fifo_millis: int, optimized_millis: int) -> float:
        # 基线时长缺失或为零时没有有效分母，返回约定值而不是无限收益。
        if fifo_millis <= 0:
            return 0.0
        return round((fifo_millis - optimized_millis) * 100.0 / fifo_millis, 2)

    # 耗时离散程度以最短任务为分母，用于判断工作负载是否具有足够差异。
    # 最短耗时无法作为基准时，不产生无穷大或缺失数值。
    def duration_spread_rate(self, durations_millis: list[int]) -> float:
        if not durations_millis:
            return 0.0
        # 同一份基线负载的最大最小值描述任务差异，与跨轮总耗时无关。
        minimum = min(durations_millis)
        maximum = max(durations_millis)
        if minimum <= 0:
            return 0.0
        return round((maximum - minimum) * 100.0 / minimum, 2)

    # 每份对比结果等权，不按任务数或耗时给某份工作负载加权。
    def average_improvement_rate(self, improvement_rates: list[float]) -> float:
        # 没有可聚合样本时返回零，样本数量仍由对比服务显式检查。
        if not improvement_rates:
            return 0.0
        return round(sum(improvement_rates) / len(improvement_rates), 2)

