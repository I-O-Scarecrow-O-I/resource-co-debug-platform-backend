from datetime import UTC, datetime


# 统一生成带时区的 UTC 时间，以便日志、状态记录和 SQLite 序列化使用同一口径。
def utc_now() -> datetime:
    return datetime.now(tz=UTC)

