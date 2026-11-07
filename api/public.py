"""Standalone multi-user website: uvicorn api.public:app --port 8090.

Never imports the desktop orchestrator, its tools, private config, or event bus.
Only authenticated, owner-scoped conversations are sent to a dedicated provider.
"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import threading
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from api.public_store import SESSION_SECONDS, WebStore, token_hash

ROOT = Path(__file__).resolve().parent.parent
COOKIE = "jarvis_web_session"
SYSTEM_PROMPT = (
    "You are JARVIS, a helpful, clear and thoughtful AI assistant. "
    "You can answer questions, explain concepts, and help draft text and code. "
    "You have no tools, live web access, computer access, or access to other users. "
    "Do not claim to perform actions or access information outside this conversation."
)


@dataclass
class Settings:
    db_path: Path
    origin: str = "http://localhost:8090"
    secure_cookies: bool = True
    signup_enabled: bool = True
    daily_limit: int = 100
    provider_url: str = "https://api.openai.com/v1"
    provider_key: str = ""
    provider_model: str = ""
    database_url: str = ""
    serverless: bool = False
    deployment_origin: str = ""

    @classmethod
    def from_env(cls):
        vercel = os.getenv("VERCEL") == "1"
        deployed_host = os.getenv("VERCEL_URL", "")
        production_host = os.getenv("VERCEL_PROJECT_PRODUCTION_URL", deployed_host)
        default_origin = (
            f"https://{production_host}" if vercel and production_host else "http://localhost:8090"
        )
        return cls(
            db_path=Path(os.getenv("JARVIS_WEB_DB", str(ROOT / "data" / "public_web.db"))),
            origin=os.getenv("JARVIS_WEB_ORIGIN", default_origin).rstrip("/"),
            secure_cookies=os.getenv("JARVIS_WEB_SECURE_COOKIES", "1") != "0",
            signup_enabled=os.getenv("JARVIS_WEB_SIGNUP", "1") != "0",
            daily_limit=max(1, int(os.getenv("JARVIS_WEB_DAILY_LIMIT", "100"))),
            provider_url=os.getenv("JARVIS_WEB_PROVIDER_URL", "https://api.openai.com/v1"),
            provider_key=os.getenv("JARVIS_WEB_PROVIDER_KEY", ""),
            provider_model=os.getenv("JARVIS_WEB_MODEL", ""),
            database_url=os.getenv("JARVIS_WEB_DATABASE_URL")
            or os.getenv("POSTGRES_URL")
            or os.getenv("DATABASE_URL", ""),
            serverless=vercel,
            deployment_origin=f"https://{deployed_host}" if vercel and deployed_host else "",
        )


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        value = value.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Enter a valid email address")
        return value


class Signup(Credentials):
    name: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=12, max_length=128)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        value = " ".join(value.split())
        if not value:
            raise ValueError("Enter your name")
        return value


class Message(BaseModel):
    message: str = Field(min_length=1, max_length=8000)

    @field_validator("message")
    @classmethod
    def valid_message(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Enter a message")
        return value


async def provider_reply(settings: Settings, messages: list[dict]) -> str:
    """Stateless provider call, bounded in time, output size and context length."""
    if not settings.provider_key or not settings.provider_model:
        raise HTTPException(503, "AI is not configured yet. Please contact the site administrator.")
    if not settings.provider_key.isascii() or any(
        ord(char) < 33 or ord(char) == 127 for char in settings.provider_key
    ):
        raise HTTPException(
            503,
            "The server's AI key contains invalid characters. "
            "The administrator needs to replace it and restart the server.",
        )
    try:
        async with (
            httpx.AsyncClient(timeout=60, follow_redirects=False) as client,
            client.stream(
                "POST",
                settings.provider_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {settings.provider_key}"},
                json={
                    "model": settings.provider_model,
                    "messages": messages,
                    "max_tokens": 2048,
                    "stream": False,
                },
            ) as response,
        ):
            response.raise_for_status()
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > 1_000_000:
                    raise ValueError("Provider response too large")
            import json

            answer = json.loads(chunks)["choices"][0]["message"]["content"]
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("Empty response")
            return answer[:32000]
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in (401, 403):
            detail = "The AI provider rejected the server's credentials. Please contact the administrator."
        elif status == 429:
            detail = "The AI provider's usage limit has been reached. Please try again later."
        elif status in (400, 404, 422):
            detail = "The AI provider rejected the model or request configuration. Please contact the administrator."
        else:
            detail = "The AI provider is temporarily unavailable. Please try again shortly."
        raise HTTPException(503, detail) from None
    except httpx.TimeoutException:
        raise HTTPException(504, "The AI provider timed out. Please try again.") from None
    except httpx.RequestError:
        raise HTTPException(
            503, "The server could not connect to the AI provider. Please try again shortly."
        ) from None
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        # Never expose provider URLs, credentials, or upstream bodies to browsers.
        raise HTTPException(
            503, "AI is temporarily unavailable. Please try again shortly."
        ) from None


def context_messages(history: list[dict], message: str) -> list[dict]:
    selected = []
    remaining = 24000 - len(message)
    # Whole turns, newest first. Avoid orphaning an assistant response.
    for index in range(len(history) - 2, -1, -2):
        pair = history[index : index + 2]
        size = sum(len(item["content"]) for item in pair)
        if size > remaining or len(selected) >= 20:
            break
        selected[0:0] = [{"role": item["role"], "content": item["content"]} for item in pair]
        remaining -= size
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        *selected,
        {"role": "user", "content": message},
    ]


def create_app(settings: Settings | None = None, responder=None) -> FastAPI:
    settings = settings or Settings.from_env()
    origin = urlparse(settings.origin)
    if origin.scheme not in ("http", "https") or not origin.netloc or origin.path:
        raise ValueError("JARVIS_WEB_ORIGIN must be an exact origin without a path")
    if not settings.secure_cookies and origin.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Insecure cookies are allowed only for local development")
    if origin.scheme != "https" and origin.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Public deployments require an HTTPS origin")
    if urlparse(settings.provider_url).scheme != "https":
        raise ValueError("The AI provider URL must use HTTPS")

    allowed_origins = {settings.origin}
    if settings.deployment_origin:
        allowed_origins.add(settings.deployment_origin)
    allowed_hosts = {urlparse(value).netloc for value in allowed_origins}
    store_lock = threading.Lock()

    def initialize_store(app):
        with store_lock:
            if app.state.store is not None:
                return app.state.store
            if settings.serverless and not settings.database_url:
                raise HTTPException(
                    503,
                    "Database setup is required. The administrator must connect a PostgreSQL database.",
                )
            if settings.database_url and urlparse(settings.database_url).scheme not in (
                "postgres",
                "postgresql",
            ):
                raise HTTPException(
                    503,
                    "The server database configuration is invalid. Please contact the administrator.",
                )
            try:
                app.state.store = WebStore(settings.db_path, database_url=settings.database_url)
            except (OSError, sqlite3.Error, ImportError):
                raise HTTPException(
                    503,
                    "The server could not connect to its database. Please contact the administrator.",
                ) from None
            return app.state.store

    @asynccontextmanager
    async def lifespan(app):
        # A missing database must not prevent the page or diagnostic endpoint
        # from loading. Vercel may also invoke ASGI without lifespan startup.
        with suppress(HTTPException):
            await asyncio.to_thread(initialize_store, app)
        yield

    app = FastAPI(
        title="JARVIS Web", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.settings = settings
    app.state.store = None
    app.state.responder = responder or provider_reply

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host")
        if host not in allowed_hosts:
            return JSONResponse({"detail": "Invalid host"}, status_code=400)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            request_origin = request.headers.get("origin")
            if request_origin not in allowed_origins or urlparse(request_origin).netloc != host:
                return JSONResponse({"detail": "Request origin rejected"}, status_code=403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "Use application/json"}, status_code=415)
            # Bound the actual body, including requests without Content-Length.
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 40000:
                    return JSONResponse({"detail": "Request too large"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; font-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'; object-src 'none'"
        )
        if origin.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    def store(request: Request) -> WebStore:
        return initialize_store(request.app)

    store_dependency = Depends(store)

    def user(request: Request, db: WebStore = store_dependency):
        found = db.authenticate(request.cookies.get(COOKIE, ""))
        if not found:
            raise HTTPException(401, "Please sign in to continue.")
        return found

    user_dependency = Depends(user)

    def auth_limit(request: Request, db: WebStore):
        # Trust proxy headers only when uvicorn has an explicitly trusted proxy.
        host = request.client.host if request.client else "unknown"
        if not db.allow("auth:" + token_hash(host), 20, 900):
            raise HTTPException(429, "Too many sign-in attempts. Try again in 15 minutes.")

    def set_session(response: Response, db: WebStore, user_id: str):
        response.set_cookie(
            COOKIE,
            db.session(user_id),
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=settings.secure_cookies,
            samesite="strict",
            path="/",
        )

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "Conversation not found"}, status_code=404)

    @app.exception_handler(sqlite3.OperationalError)
    async def database_unavailable(request, exc):
        return JSONResponse(
            {"detail": "The database is temporarily unavailable. Please try again shortly."},
            status_code=503,
        )

    @app.get("/api/health")
    def health():
        database_available = True
        setup_message = ""
        try:
            with initialize_store(app).connect() as db:
                db.execute("SELECT 1")
        except (HTTPException, sqlite3.Error):
            database_available = False
            setup_message = "Database setup is incomplete or the database is unavailable. Please contact the site administrator."
        return {
            "status": "ok" if database_available else "setup_required",
            "database_available": database_available,
            "setup_message": setup_message,
            "ai_configured": bool(settings.provider_key and settings.provider_model),
            "signup_enabled": settings.signup_enabled and database_available,
        }

    @app.post("/api/auth/signup", status_code=201)
    def signup(body: Signup, request: Request, response: Response, db: WebStore = store_dependency):
        auth_limit(request, db)
        if not settings.signup_enabled:
            raise HTTPException(403, "New registrations are currently closed.")
        try:
            account = db.create_user(body.email, body.name, body.password)
        except sqlite3.IntegrityError:
            raise HTTPException(
                409, "Unable to register this email. Try signing in instead."
            ) from None
        set_session(response, db, account["id"])
        return {"user": account}

    @app.post("/api/auth/login")
    def login(
        body: Credentials, request: Request, response: Response, db: WebStore = store_dependency
    ):
        auth_limit(request, db)
        account = db.login(body.email, body.password)
        if account is None:
            raise HTTPException(401, "Email or password is incorrect.")
        set_session(response, db, account["id"])
        return {"user": account}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response, db: WebStore = store_dependency):
        db.logout(request.cookies.get(COOKIE, ""))
        response.delete_cookie(
            COOKIE, path="/", secure=settings.secure_cookies, httponly=True, samesite="strict"
        )
        return {"ok": True}

    @app.get("/api/auth/me")
    def me(account=user_dependency, db: WebStore = store_dependency):
        return {"user": account, "usage": db.usage(account["id"]), "limit": settings.daily_limit}

    @app.get("/api/conversations")
    def conversations(account=user_dependency, db: WebStore = store_dependency):
        return db.list_conversations(account["id"])

    @app.post("/api/conversations", status_code=201)
    def new_conversation(account=user_dependency, db: WebStore = store_dependency):
        if not db.allow("create:" + account["id"], 100, 86400):
            raise HTTPException(429, "Daily conversation creation limit reached.")
        return db.create_conversation(account["id"])

    @app.get("/api/conversations/{conversation_id}/messages")
    def messages(conversation_id: str, account=user_dependency, db: WebStore = store_dependency):
        return db.messages(account["id"], conversation_id)

    @app.delete("/api/conversations/{conversation_id}")
    def delete_conversation(
        conversation_id: str, account=user_dependency, db: WebStore = store_dependency
    ):
        db.delete_conversation(account["id"], conversation_id)
        return {"ok": True}

    @app.post("/api/conversations/{conversation_id}/messages")
    async def send(
        conversation_id: str,
        body: Message,
        request: Request,
        account=user_dependency,
        db: WebStore = store_dependency,
    ):
        try:
            lease = db.reserve(account["id"], conversation_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        try:
            if not settings.provider_key or not settings.provider_model:
                raise HTTPException(
                    503, "AI is not configured yet. Please contact the site administrator."
                )
            if not db.allow("chat:" + account["id"], settings.daily_limit, 86400):
                raise HTTPException(429, "Daily message limit reached. It resets at midnight UTC.")
            if not db.allow("pace:" + account["id"], 10, 60):
                raise HTTPException(429, "Please wait a minute before sending more messages.")
            history = db.messages(account["id"], conversation_id)
            try:
                answer = await asyncio.wait_for(
                    request.app.state.responder(settings, context_messages(history, body.message)),
                    timeout=75,
                )
            except TimeoutError:
                raise HTTPException(504, "The reply took too long. Please try again.") from None
            try:
                db.save_turn(account["id"], conversation_id, lease, body.message, answer)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            return {
                "response": answer,
                "usage": db.usage(account["id"]),
                "limit": settings.daily_limit,
            }
        finally:
            db.release(account["id"], conversation_id, lease)

    app.mount("/assets", StaticFiles(directory=ROOT / "web" / "public"), name="public-assets")

    @app.get("/")
    @app.get("/app")
    def index():
        return FileResponse(ROOT / "web" / "public" / "index.html")

    return app


app = create_app()
