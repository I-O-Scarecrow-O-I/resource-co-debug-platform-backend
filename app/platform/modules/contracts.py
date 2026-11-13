from dataclasses import dataclass

from fastapi import APIRouter


@dataclass(frozen=True, slots=True)
class BackendModule:
    # 模块注册描述包含公开名称、相对路由前缀和路由对象。
    # 发布版本供模块清单展示，不参与任务生命周期。
    name: str
    route_prefix: str
    router: APIRouter
    version: str = "0.1.0"
