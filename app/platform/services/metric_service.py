class AcceptanceMetricService:
    def build_success_rate(self, success_count: int, total_count: int) -> float:
        if total_count <= 0:
            return 0.0
        return round(success_count * 100.0 / total_count, 2)

    def improvement_rate(self, fifo_millis: int, optimized_millis: int) -> float:
        if fifo_millis <= 0:
            return 0.0
        return round((fifo_millis - optimized_millis) * 100.0 / fifo_millis, 2)

