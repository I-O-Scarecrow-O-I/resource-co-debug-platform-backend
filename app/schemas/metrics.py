from pydantic import BaseModel


class ImprovementResult(BaseModel):
    fifo_millis: int
    optimized_millis: int
    improvement_rate: float
