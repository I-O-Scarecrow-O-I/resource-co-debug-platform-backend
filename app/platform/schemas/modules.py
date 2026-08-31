from pydantic import BaseModel


class BackendModuleResponse(BaseModel):
    name: str
    route_prefix: str
    version: str
