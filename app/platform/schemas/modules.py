from pydantic import BaseModel


# 公开模块清单只暴露注册信息，不包含依赖对象或服务内部状态。
class BackendModuleResponse(BaseModel):
    name: str
    route_prefix: str
    version: str
