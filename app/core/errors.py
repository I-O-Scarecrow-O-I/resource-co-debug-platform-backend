# 可公开的业务错误带 HTTP 状态；调用点应提供适合前端展示的消息。
# 内部异常仍交由应用的通用错误处理，不在这里猜测分类。
class AppError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# 缺少项目、任务或其他已约定资源时用固定的不存在状态。
class NotFoundError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=404)


# 取消信号跨模块传播控制意图；托管执行层据此结束任务和释放资源。
class CancellationRequested(Exception):
    """Raised when module A asks a long-running module to stop."""

