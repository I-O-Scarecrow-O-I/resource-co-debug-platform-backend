from functools import lru_cache
from pathlib import Path
from typing import Annotated, Self
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


# 应用设置描述平台存储、接口和 NaturalCC 应用地址；模型路由由上游负责。
class Settings(BaseSettings):
    # 应用身份和监听设置属于平台 HTTP 服务，与上游应用地址独立。
    app_name: str = "resource-co-debug-platform-backend"
    app_env: str = "local"
    # 监听配置只描述本应用；NaturalCC 地址是应用间调用的另一条连接。
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    # 默认存储根含工程目录，任务数据库可以显式放到其他目录。
    storage_root: Path = Path("data/workspaces")
    task_database_path: Path | None = None
    # 默认任务预算与上游单次 HTTP 请求预算独立。
    default_task_timeout_seconds: int = 300
    # 保留行数按任务计，用于裁剪持久日志窗口而非限制任务执行。
    max_log_lines_per_task: int = 2000
    # 连接超时与请求预算独立，不能用缩短请求预算替代连接探测。
    naturalcc_base_url: str = "http://127.0.0.1:7860"
    # 连接超时用于建立 HTTP 连接；请求预算还覆盖等待应用响应。
    naturalcc_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    naturalcc_request_timeout_seconds: float = Field(default=30.0, gt=0)
    # 远端工具执行缺省不预授权，代码写入与执行许可按不同策略处理。
    naturalcc_approve_execute: bool = False
    # CORS 来源使用逗号列表兼容环境变量；不要求运营方写 JSON 数组。
    allowed_cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]
    )

    # 普通启动读取 UTF-8 的仓库环境文件，变量大小写不敏感。
    # 服务装配通过 BaseSettings 读取环境；不在这里对部署方式作全局离线限制。
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @field_validator("allowed_cors_origins", mode="before")
    @classmethod
    # 逗号分隔的环境变量转换为来源列表，剔除空项目后交给配置类型校验。
    def parse_cors_origins(cls, value: object) -> object:
        # 环境字符串按逗号拆分并去空项；程序直接传列表时保留列表形状。
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("naturalcc_base_url")
    @classmethod
    # 校验上游应用 URL 形式并规范尾部分隔符，供不同 HTTP 客户端共用。
    def normalize_naturalcc_base_url(cls, value: str) -> str:
        # 规范化空白及尾斜杠后再判断 URL 是否具备 HTTP(S) 结构。
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        # 这里只检查 HTTP(S) 应用地址形状，连通性与上游健康由探测接口给出。
        if not normalized or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("naturalcc_base_url must be a non-empty HTTP(S) URL")
        return normalized

    @model_validator(mode="after")
    # 未显式配置数据库时跟随存储根，测试或部署可指定独立位置。
    def set_default_task_database_path(self) -> Self:
        # 根据最终解析的存储根推导数据库位置；显式路径不受默认规则覆盖。
        if self.task_database_path is None:
            self.task_database_path = self.storage_root.parent / "tasks.sqlite3"
        return self


# 缓存设置实例避免每次请求重新解释环境；运行期变更通常通过重启生效。
@lru_cache
# 设置由应用依赖复用，避免每个请求重新读取环境变量。
def get_settings() -> Settings:
    return Settings()

