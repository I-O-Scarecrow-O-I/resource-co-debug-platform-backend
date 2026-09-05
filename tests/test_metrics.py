from app.modules.co_debug.services.metric_service import AcceptanceMetricService


def test_build_success_rate() -> None:
    service = AcceptanceMetricService()

    assert service.build_success_rate(10, 10) == 100.0
    assert service.build_success_rate(9, 10) == 90.0


def test_improvement_rate() -> None:
    service = AcceptanceMetricService()

    assert service.improvement_rate(1000, 850) == 15.0


def test_duration_spread_rate() -> None:
    service = AcceptanceMetricService()

    assert service.duration_spread_rate([100, 300]) == 200.0
    assert service.duration_spread_rate([100, 400, 200]) == 300.0


def test_average_improvement_rate() -> None:
    service = AcceptanceMetricService()

    assert service.average_improvement_rate([10.0, 20.0, 30.0]) == 20.0

