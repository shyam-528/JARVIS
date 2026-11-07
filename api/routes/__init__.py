"""Domain routers for the JARVIS API (split out of api/main.py — arch step 4)."""

from api.routes import agents, chat, memory, system, tasks, vision, voice, web, workflows

ALL_ROUTERS = [
    system.router,
    voice.router,
    tasks.router,
    memory.router,
    workflows.router,
    agents.router,
    web.router,
    vision.router,
    chat.router,
]
