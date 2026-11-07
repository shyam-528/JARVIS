"""HTTP chat endpoint + the /ws streaming websocket."""

import json

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from api import state
from api.state import get_auth_token
from core.router import AllProvidersDown

router = APIRouter()


class ChatRequest(BaseModel):
    message: str
    stream: bool = False


class ChatResponse(BaseModel):
    response: str
    provider: str
    tool_calls: list = []


@router.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, token: str = Depends(get_auth_token)):
    collected = []
    provider_name = ""
    try:
        # Unified brain: stream through the CLI's live pipeline when the
        # proxy is up; otherwise the child's local brain answers.
        # wait_bg=True: the turn stays open, so background-dispatched jobs
        # are awaited (bounded) and answered inline for show + voice.
        stream = (
            state.brain_proxy.chat_stream(request.message, wait_bg=True, surface="web")
            if state.brain_proxy is not None
            else state.orch.process(request.message, wait_bg=True, surface="web")
        )
        async for provider, chunk in stream:
            if provider in ("tool", "proactive", "ml_proactive", "auto", "skill"):
                continue
            if provider == "bg_dispatch":
                # Surface the dispatch note so the website shows the job card.
                collected.append(f"\n\n> _{chunk}_\n\n")
                continue
            if not provider_name:
                provider_name = provider
            collected.append(chunk)
    except AllProvidersDown as exc:
        try:
            from core.storm_queue import StormQueue

            res = StormQueue(state.orch.cfg.db_path.parent).enqueue("web", request.message)
            if res.get("queued"):
                raise HTTPException(
                    status_code=503,
                    detail=f"{exc} — queued ({res['id']}): answer lands on Telegram + voice when quota returns.",
                )
        except HTTPException:
            raise
        except Exception:
            pass
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return ChatResponse(response="".join(collected), provider=provider_name)


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str | None = None):
    # P0: require valid token when any active token exists (was: empty token bypassed auth).
    try:
        needs_auth = any(t.get("active") for t in state.auth_manager.list_tokens())
    except Exception:
        needs_auth = False
    if needs_auth and (not token or not state.auth_manager.verify_token(token)):
        await websocket.close(code=4001)
        return
    if token and not state.auth_manager.verify_token(token):
        await websocket.close(code=4001)
        return
    await state.ws_manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            if msg.get("type") == "chat":
                # Unified brain: stream through the CLI's live pipeline when
                # the proxy is connected; local brain otherwise. Identical
                # event contract either way. wait_bg keeps the turn open so
                # background-dispatched jobs answer inline (shown + spoken).
                try:
                    stream = (
                        state.brain_proxy.chat_stream(msg["message"], wait_bg=True, surface="web")
                        if state.brain_proxy is not None
                        else state.orch.process(msg["message"], wait_bg=True, surface="web")
                    )
                    provider_name = ""
                    async for provider, chunk in stream:
                        if provider in (
                            "tool",
                            "proactive",
                            "ml_proactive",
                            "auto",
                            "bg_dispatch",
                            "skill",
                        ):
                            await websocket.send_json({"type": "tool", "content": chunk})
                            continue
                        if not provider_name:
                            provider_name = provider
                        await websocket.send_json(
                            {"type": "token", "content": chunk, "provider": provider}
                        )
                    await websocket.send_json({"type": "done", "provider": provider_name})
                except AllProvidersDown as exc:
                    queued = ""
                    try:
                        from core.storm_queue import StormQueue

                        res = StormQueue(state.orch.cfg.db_path.parent).enqueue(
                            "web", msg.get("message", "")
                        )
                        if res.get("queued"):
                            queued = (
                                f" Queued ({res['id']}): answer lands on "
                                f"Telegram + voice when quota returns."
                            )
                    except Exception:
                        pass
                    await websocket.send_json({"type": "error", "content": f"{exc}.{queued}"})
            elif msg.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        state.ws_manager.disconnect(websocket)
