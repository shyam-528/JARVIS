"""Memory, preferences, conversations and knowledge-base endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from api import state
from api.state import get_auth_token

router = APIRouter()


class MemoryItem(BaseModel):
    id: int
    timestamp: str
    text: str


# NOTE: literal routes (/search, /current, /new) MUST precede /{session_id} —
# FastAPI matches in order, otherwise "search" becomes a session id (404).
@router.get("/api/conversations/search")
async def search_conversations(q: str, limit: int = 10, token: str = Depends(get_auth_token)):
    return state.orch.persistence.search_sessions(q, limit=limit)


@router.get("/api/conversations/current")
async def current_conversation(token: str = Depends(get_auth_token)):
    if not state.orch.session_id:
        return {"session_id": None}
    session = state.orch.persistence.get_session(state.orch.session_id)
    messages = state.orch.persistence.get_last_n_messages(state.orch.session_id, 50)
    return {"session": session, "messages": messages}


@router.get("/api/conversations")
async def list_conversations(limit: int = 50, token: str = Depends(get_auth_token)):
    return state.orch.persistence.list_sessions(limit=limit)


@router.get("/api/conversations/{session_id}")
async def get_conversation(session_id: str, token: str = Depends(get_auth_token)):
    session = state.orch.persistence.get_session(session_id)
    if not session:
        raise HTTPException(404, "session not found")
    messages = state.orch.persistence.get_messages(session_id)
    return {"session": session, "messages": messages}


@router.delete("/api/conversations/{session_id}")
async def delete_conversation(session_id: str, token: str = Depends(get_auth_token)):
    ok = state.orch.persistence.delete_session(session_id)
    return {"ok": ok}


@router.post("/api/conversations/{session_id}/resume")
async def resume_conversation(session_id: str, token: str = Depends(get_auth_token)):
    msgs = state.orch.resume_session(session_id)
    return {"resumed": True, "session_id": session_id, "messages_loaded": len(msgs)}


@router.post("/api/conversations/new")
async def new_conversation(token: str = Depends(get_auth_token)):
    sid = state.orch.start_new_session()
    return {"session_id": sid}


class KnowledgeIndexRequest(BaseModel):
    path: str
    recursive: bool = True
    force: bool = False


class KnowledgeSearchRequest(BaseModel):
    query: str
    k: int = 5
    source: str = ""


@router.post("/api/knowledge/index")
async def knowledge_index(request: KnowledgeIndexRequest, token: str = Depends(get_auth_token)):
    try:
        from pathlib import Path as P

        p = P(request.path)
        if p.is_dir():
            result = state.orch.knowledge_indexer.index_directory(
                request.path, recursive=request.recursive, force=request.force
            )
        else:
            result = state.orch.knowledge_indexer.index_file(p, force=request.force)
        return result
    except Exception as exc:
        return {"error": str(exc)}


@router.post("/api/knowledge/search")
async def knowledge_search(request: KnowledgeSearchRequest, token: str = Depends(get_auth_token)):
    try:
        results = state.orch.knowledge_store.search(
            request.query, k=request.k, source_filter=request.source
        )
        return results
    except Exception as exc:
        return {"error": str(exc)}


@router.get("/api/knowledge/stats")
async def knowledge_stats(token: str = Depends(get_auth_token)):
    try:
        return state.orch.knowledge_store.stats()
    except Exception as exc:
        return {"error": str(exc)}


@router.get("/api/knowledge/sources")
async def knowledge_sources(token: str = Depends(get_auth_token)):
    try:
        return state.orch.knowledge_store.list_sources()
    except Exception as exc:
        return {"error": str(exc)}


@router.delete("/api/knowledge/clear")
async def knowledge_clear(token: str = Depends(get_auth_token)):
    try:
        n = state.orch.knowledge_store.clear()
        return {"cleared": n}
    except Exception as exc:
        return {"error": str(exc)}


@router.get("/api/memory", response_model=list[MemoryItem])
async def list_memory(token: str = Depends(get_auth_token)):
    rows = state.orch.memory.all(50)
    return [{"id": r[0], "timestamp": r[1], "text": r[2]} for r in rows]


@router.post("/api/memory")
async def add_memory(text: str, kind: str = "fact", token: str = Depends(get_auth_token)):
    result = state.orch.memory.add(text, kind)
    return {"result": result}


@router.delete("/api/memory")
async def forget_memory(query: str, token: str = Depends(get_auth_token)):
    n = state.orch.memory.forget(query)
    return {"forgotten": n}


@router.get("/api/preferences")
async def list_preferences(token: str = Depends(get_auth_token)):
    """Learned preferences (visible/editable in Settings)."""
    try:
        all_prefs = state.orch.preferences.list_all()
    except Exception:
        return []
    return [
        {
            "key": k,
            "value": p.value,
            "category": p.category,
            "confidence": p.confidence,
            "source": p.source,
        }
        for k, p in sorted(all_prefs.items())
    ]


@router.put("/api/preferences")
async def set_preference(request: Request, token: str = Depends(get_auth_token)):
    body = await request.json()
    try:
        ok = state.orch.preferences.set_value(
            body.get("key", ""), body.get("value", ""), body.get("category", "general") or "general"
        )
    except Exception:
        ok = False
    if not ok:
        raise HTTPException(status_code=400, detail="key and value required")
    return {"ok": True}


@router.delete("/api/preferences")
async def delete_preference(key: str, token: str = Depends(get_auth_token)):
    try:
        ok = state.orch.preferences.remove(key)
    except Exception:
        ok = False
    return {"forgotten": key if ok else ""}
