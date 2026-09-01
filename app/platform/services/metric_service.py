class AcceptanceMetricService:
    REQUIRED_WORKLOAD_COUNT = 3
    REQUIRED_DURATION_SPREAD_RATE = 200.0
    REQUIRED_IMPROVEMENT_RATE = 15.0

    def build_success_rate(self, success_count: int, total_count: int) -> float:
        if total_count <= 0:
            return 0.0
        return round(success_count * 100.0 / total_count, 2)

    def improvement_rate(self, fifo_millis: int, optimized_millis: int) -> float:
        if fifo_millis <= 0:
            return 0.0
        return round((fifo_millis - optimized_millis) * 100.0 / fifo_millis, 2)

    def duration_spread_rate(self, durations_millis: list[int]) -> float:
        if not durations_millis:
            return 0.0
        minimum = min(durations_millis)
        maximum = max(durations_millis)
        if minimum <= 0:
            return 0.0
        return round((maximum - minimum) * 100.0 / minimum, 2)

    def average_improvement_rate(self, improvement_rates: list[float]) -> float:
        if not improvement_rates:
            return 0.0
        return round(sum(improvement_rates) / len(improvement_rates), 2)

