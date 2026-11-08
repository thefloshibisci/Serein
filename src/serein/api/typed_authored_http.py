"""Authenticated HTTP counterparts; import/apply stays local and reviewable."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from .typed_authored import tools_for
from ..core.store import Conflict


class Action(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tool: str
    arguments: dict = Field(default_factory=dict)


def routes(settings, auth):
    router = APIRouter(dependencies=auth)
    tools = tools_for(settings)

    @router.post('/api/authored-types')
    def invoke(body: Action):
        if body.tool not in tools:
            raise HTTPException(404, 'Authored tool unavailable')
        try:
            return tools[body.tool](**body.arguments)
        except Conflict as error:
            raise HTTPException(409, str(error))
        except (ValueError, TypeError) as error:
            raise HTTPException(422, str(error))

    return router
