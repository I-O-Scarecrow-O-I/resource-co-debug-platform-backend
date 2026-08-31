from dataclasses import dataclass

from fastapi import APIRouter


@dataclass(frozen=True, slots=True)
class BackendModule:
    name: str
    route_prefix: str
    router: APIRouter
    version: str = "0.1.0"
