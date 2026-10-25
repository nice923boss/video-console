"""The in-app assistant (vc/agent.py): one chat turn per request, and stopping a running one."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..agent import Agent
from ..server import Request, Router

if TYPE_CHECKING:
    from ..app import App


def register(router: Router, app: App) -> None:
    agent = Agent(app)

    @router.route("POST", "/api/projects/{pid}/chat")
    def chat(req: Request) -> dict:
        """{id?, messages: [{role, content}], attachment?: {name, text}} -> {id, reply, steps, stopped}. Blocks
        until the model is done (minutes on a slow model); each step is also sent as a "chat" event."""
        return agent.run(req.params["pid"], req.json_object())

    @router.route("POST", "/api/projects/{pid}/chat/cancel")
    def chat_cancel(req: Request) -> dict:
        """{id}: stop that turn before its next model round."""
        return {"stopped": agent.cancel(str(req.json_object().get("id") or ""))}
