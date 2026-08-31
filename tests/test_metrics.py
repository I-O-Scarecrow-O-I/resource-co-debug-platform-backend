from app.services.metric_service import AcceptanceMetricService


def test_build_success_rate() -> None:
    service = AcceptanceMetricService()

    assert service.build_success_rate(10, 10) == 100.0
    assert service.build_success_rate(9, 10) == 90.0


def test_improvement_rate() -> None:
    service = AcceptanceMetricService()

    assert service.improvement_rate(1000, 850) == 15.0
