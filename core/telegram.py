"""Telegram bot surface: owner-locked, long-polling, two-way.

Contract: the first chat to message an unlinked bot becomes the owner
(state persisted to state_path); every later chat gets a refusal and is
never processed. Command handlers report errors back to the chat; the
poll loop logs failures to the console, backs off, and never dies.
_call returns None on any API failure (logged), so send_* return False
instead of raising into the chat loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import Any

import httpx

from utils.json_utils import load_json_store, save_json_atomic

API_BASE = "https://api.telegram.org/bot{token}"


class TelegramBot:
    """Two-way Telegram bot: owner-locked, long-polling, commands, photos, voice notes."""

    def __init__(
        self,
        bot_token: str,
        orch: Any,
        state_path: Path,
        poll_timeout: int = 30,
        chat_stream_factory=None,
    ):
        self.token = bot_token
        self.orch = orch
        self.state_path = state_path
        self.poll_timeout = poll_timeout
        # Unified brain: optional factory returning an async (message) ->
        # stream of (label, chunk). When set, chat goes to the CLI brain.
        self._chat_stream_factory = chat_stream_factory
        self.chat_id: str | None = None
        self.running = False
        self._task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None
        self._offset = 0
        self._load_state()

    # ---------- state ----------
    def _load_state(self):
        # P0: shared corrupt-file contract — a corrupt link state quarantines
        # aside (re-link on next message, announced once) instead of printing
        # on every boot.
        data = load_json_store(self.state_path, default={}, owner="telegram", expect=dict)
        self.chat_id = data.get("chat_id")
        try:
            self._offset = int(data.get("offset", 0))
        except (TypeError, ValueError):
            self._offset = 0

    def _save_state(self):
        # P0: atomic + never raises (this runs inside the poll loop — a disk
        # hiccup must not take down polling; the failure is still loud).
        save_json_atomic(self.state_path, {"chat_id": self.chat_id, "offset": self._offset})

    # ---------- low level ----------
    def _url(self, method: str) -> str:
        return f"{API_BASE.format(token=self.token)}/{method}"

    async def _call(self, method: str, **kwargs) -> dict | None:
        try:
            resp = await self._client.post(self._url(method), timeout=40.0, **kwargs)
            data = resp.json()
            if not data.get("ok"):
                print(f"[Telegram] API {method} failed: {str(data.get('description', '?'))[:150]}")
                return None
            return data.get("result")
        except Exception as exc:
            print(f"[Telegram] API {method} error: {type(exc).__name__}: {str(exc)[:150]}")
            return None

    async def send_message(
        self, text: str, chat_id: str | None = None, parse_mode: str | None = None
    ) -> bool:
        """Send a message. parse_mode=None is plain text — use for LLM output
        that may contain <angle brackets> which would break HTML parsing."""
        cid = chat_id or self.chat_id
        if not cid:
            return False
        for chunk_start in range(0, len(text), 4000):
            payload = {"chat_id": cid, "text": text[chunk_start : chunk_start + 4000]}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            result = await self._call("sendMessage", json=payload)
            if result is None:
                return False
        return True

    async def send_photo(self, photo_bytes: bytes, caption: str = "") -> bool:
        if not self.chat_id:
            return False
        try:
            files = {"photo": ("shot.jpg", photo_bytes, "image/jpeg")}
            result = await self._call(
                "sendPhoto",
                data={"chat_id": self.chat_id, "caption": caption[:1000]},
                files=files,
            )
            return result is not None
        except Exception:
            return False

    async def send_typing(self):
        await self._call("sendChatAction", json={"chat_id": self.chat_id, "action": "typing"})

    # ---------- commands ----------
    async def _handle_update(self, update: dict):
        msg = update.get("message") or update.get("edited_message") or {}
        chat = str(msg.get("chat", {}).get("id", ""))
        if not chat:
            return

        if self.chat_id is None:
            self.chat_id = chat
            self._save_state()
            await self.send_message(
                "JARVIS MARK 3 linked to this chat, sir. You are the sole authorized user.\n\n"
                "Commands:\n/status - system status\n/chat &lt;text&gt; - talk to me\n"
                "/screenshot - screen capture to your phone\n/analyze &lt;prompt&gt; - AI vision\n"
                "/tasks - scheduled tasks\n/remember &lt;fact&gt; - store memory\n"
                "/prefs - learned preferences · /anniversary - a year ago today\n"
                "/project - per-project memory · /storm - queued storm answers\n"
                "/morning - starting-work routine · /evening - winding-down routine\n"
                "/demo [apps] · /downloads - organize · /snap [prompt] - screen note · /open <app>\n"
                "/proactive - push status · mute/unmute · digest now\n"
                "Voice notes: hold mic, speak - I transcribe and answer.\nPlain text = chat."
            )
            return

        if chat != self.chat_id:
            await self._call(
                "sendMessage",
                json={
                    "chat_id": chat,
                    "text": "Unauthorized. This JARVIS belongs to someone else.",
                },
            )
            return

        text = (msg.get("text") or "").strip()
        voice = msg.get("voice") or msg.get("audio")
        photo = msg.get("photo")

        if photo:
            await self._handle_photo(photo)
        elif voice:
            await self._handle_voice(voice)
        elif text.startswith("/"):
            await self._handle_command(text)
        elif text:
            await self._handle_chat(text)

    async def _handle_command(self, text: str):
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().split("@")[0]
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/start":
            await self.send_message("At your service, sir.")
        elif cmd == "/briefing":
            await self.send_typing()
            try:
                from skills.briefing import generate_briefing

                kind = "evening" if "evening" in arg.lower() else "morning"
                await self.send_message(await generate_briefing(kind))
            except Exception as exc:
                await self.send_message(f"Briefing error: {exc}")
        elif cmd in ("/morning", "/evening"):
            # One-tap routines (on-demand): starting work / winding down.
            await self.send_typing()
            try:
                tool = "morning_start" if cmd == "/morning" else "evening_wrap"
                res = await self.orch.toolbox.execute(tool, "{}")
                await self.send_message(res)
            except Exception as exc:
                await self.send_message(f"Routine error: {exc}")
        elif cmd in ("/demo", "/downloads", "/snap", "/open"):
            # PC deep control (free policy: acts immediately).
            await self.send_typing()
            try:
                import json as _pj2

                if cmd == "/demo":
                    res = await self.orch.toolbox.execute("demo_prep", _pj2.dumps({"apps": arg}))
                elif cmd == "/downloads":
                    res = await self.orch.toolbox.execute("downloads_organize", "{}")
                elif cmd == "/snap":
                    res = await self.orch.toolbox.execute(
                        "screenshot_note", _pj2.dumps({"prompt": arg})
                    )
                else:
                    if not arg:
                        await self.send_message("Usage: /open <app>")
                        return
                    res = await self.orch.toolbox.execute("open_app", _pj2.dumps({"name": arg}))
                await self.send_message(res[:3000])
            except Exception as exc:
                await self.send_message(f"PC error: {exc}")
        elif cmd == "/mail":
            await self.send_typing()
            try:
                from core import mail as _mail

                parts = (arg or "").split(None, 1)
                sub = parts[0].lower() if parts else ""
                rest = parts[1] if len(parts) > 1 else ""
                if sub == "send" and rest:
                    import json as _jj
                    import re as _re

                    _bits = rest.strip().split()
                    _mid = _bits[0] if _bits else ""
                    _mto = next(
                        (b for b in _bits[1:] if _re.fullmatch(r"[\w.+-]+@[\w-]+\.[\w.]+", b)),
                        "",
                    )
                    res = await self.orch.toolbox.execute(
                        "mail_send_draft", _jj.dumps({"id": _mid, "to": _mto})
                    )
                    await self.send_message(res)
                    return
                if sub == "send":
                    res = await self.orch.toolbox.execute("mail_send_draft", "{}")
                    await self.send_message(res)
                    return
                if sub == "triage":
                    res = await self.orch.toolbox.execute("mail_triage", "{}")
                    await self.send_message(res)
                    return
                if sub == "draft" and rest:
                    res = await self.orch.toolbox.execute(
                        "mail_draft", f'{{"uid": "{rest.strip()}"}}'
                    )
                    await self.send_message(res[:1500])
                    return
                if not arg or sub == "unread":
                    res = _mail.unread(limit=8)
                elif arg.lower().startswith("read "):
                    res = _mail.read(arg[5:].strip())
                    if "error" in res:
                        await self.send_message(f"Mail error: {res['error']}")
                    else:
                        await self.send_message(f"[{res['id']}]\n{res['body'][:1500]}")
                    return
                else:
                    res = _mail.search(arg, limit=8)
                if "error" in res:
                    await self.send_message(f"Mail error: {res['error']}")
                elif not res.get("emails"):
                    await self.send_message("No matching mail.")
                else:
                    lines = [f"Mail ({res['total']} matching):"]
                    for m in res["emails"]:
                        lines.append(
                            f"[{m['id']}] {m['date']} | {m['from'][:40]} | {m['subject'][:70]}"
                        )
                    await self.send_message("\n".join(lines))
            except Exception as exc:
                await self.send_message(f"Mail error: {exc}")
        elif cmd == "/wa":
            # /wa triage | /wa chats | /wa send <id>  (needs paired bridge)
            await self.send_typing()
            try:
                parts = (arg or "").split(None, 1)
                sub = parts[0].lower() if parts else "triage"
                rest = parts[1] if len(parts) > 1 else ""
                if sub == "send":
                    import json as _jj

                    res = await self.orch.toolbox.execute(
                        "wa_send_draft", _jj.dumps({"id": rest.strip()})
                    )
                elif sub == "sendto" and rest:
                    import json as _jj2
                    import re as _re2

                    _m2 = _re2.match(r"(\+?\d[\d\s\-]{7,})\s+(.*)", rest.strip(), _re2.DOTALL)
                    if not _m2:
                        await self.send_message("Usage: /wa sendto <number> <text>")
                        return
                    res = await self.orch.toolbox.execute(
                        "wa_send", _jj2.dumps({"chat": _m2.group(1), "text": _m2.group(2)})
                    )
                elif sub == "chats":
                    res = await self.orch.toolbox.execute("wa_chats", "{}")
                elif sub == "contact":
                    import json as _jj3

                    _cp = (rest or "").split(None, 2)
                    _ca = _cp[0].lower() if _cp else "list"
                    _cn = _cp[1] if len(_cp) > 1 else ""
                    _cc = _cp[2] if len(_cp) > 2 else ""
                    res = await self.orch.toolbox.execute(
                        "wa_contact", _jj3.dumps({"action": _ca, "name": _cn, "chat": _cc})
                    )
                else:
                    res = await self.orch.toolbox.execute("wa_triage", "{}")
                await self.send_message(res[:1500])
            except Exception as exc:
                await self.send_message(f"WA error: {exc}")
        elif (
            cmd == "/remind"
        ):  # /remind <minutes> <message>  |  /remind tomorrow 9pm <message> (best-effort)
            if not arg:
                await self.send_message("Usage: /remind <minutes|HH:MM|tomorrow HHpm> <message>")
                return
            try:
                import re as _re
                from datetime import datetime as _dt
                from datetime import timedelta as _td

                mins = None
                m = _re.match(r"(\d+)\s+(.*)", arg)
                if m and int(m.group(1)) <= 24 * 60 * 7:
                    mins = int(m.group(1))
                    msg = m.group(2)
                    run_at = (_dt.now() + _td(minutes=mins)).replace(second=0, microsecond=0)
                    tid = self.orch.scheduler.add_task(
                        f"tg-remind: {msg[:40]}",
                        "once",
                        {"run_at": run_at.isoformat()},
                        "notify_reminder",
                        {"message": msg},
                    )
                    await self.send_message(
                        f"Reminder set for {run_at.strftime('%Y-%m-%d %H:%M')} (id {tid})."
                    )
                else:
                    await self.send_message(
                        "Try: /remind 30 take pills  |  or chat 'remind me at 9pm ...' for full parsing."
                    )
            except Exception as exc:
                await self.send_message(f"Remind error: {exc}")
        elif cmd == "/status":
            chain = self.orch.chain_status()
            ready = sum(1 for c in chain if c[3].startswith("READY"))
            s = self.orch.stats()
            lines = [
                "<b>JARVIS MARK 3</b>",
                f"Brains: {ready}/{len(chain)} ready",
                f"Hops today: {s['total_hops']}",
                f"Memory: {self.orch.memory.count()} facts",
                f"Tasks: {len(self.orch.scheduler.list_tasks())}",
            ]
            await self.send_message("\n".join(lines), parse_mode="HTML")
        elif cmd == "/chat":
            if not arg:
                await self.send_message("Usage: /chat <message>")
                return
            await self._process_and_reply(arg)
        elif cmd == "/screenshot":
            await self.send_typing()
            try:
                from core.vision import ImageAnalyzer, ScreenCaptureManager

                mgr = ScreenCaptureManager()
                cap = mgr.capture_monitor(1)
                mgr.close()
                buf_path = cap.image
                import io

                buf = io.BytesIO()
                buf_path.convert("RGB").save(buf, format="JPEG", quality=80)
                ok = await self.send_photo(buf.getvalue(), caption="Screen capture, sir.")
                if not ok:
                    await self.send_message("Screenshot failed to send.")
            except Exception as exc:
                await self.send_message(f"Screenshot error: {exc}")
        elif cmd == "/analyze":
            prompt = arg or "Describe what is on this screen"
            await self.send_typing()
            try:
                from core.vision import ImageAnalyzer, OCREngine, ScreenCaptureManager

                mgr = ScreenCaptureManager()
                cap = mgr.capture_monitor(1)
                mgr.close()
                ocr = ""
                with contextlib.suppress(Exception):
                    ocr = OCREngine().extract_text(cap.image)
                analyzer = ImageAnalyzer(self.orch)
                result = await analyzer.analyze_with_model(cap.image, prompt, ocr_text=ocr)
                if "error" in result:
                    await self.send_message(f"Vision error: {result['error']}")
                else:
                    await self.send_message(
                        f"<b>[{result['provider']}]</b>\n{result['analysis']}", parse_mode="HTML"
                    )
            except Exception as exc:
                await self.send_message(f"Analyze error: {exc}")
        elif cmd == "/proactive":
            # /proactive [status] | mute <kind> | unmute <kind> | digest now
            #   | auto on|off|status | dry on|off | acts [n] | sweep
            try:
                from core.proactive_push import KINDS, PushGate

                gate = PushGate(self.orch.cfg.db_path.parent)
                parts = (arg or "").split(None, 1)
                sub = parts[0].lower() if parts else "status"
                rest = parts[1].strip().lower() if len(parts) > 1 else ""
                if sub in ("mute", "unmute") and rest in KINDS:
                    gate.set_muted(rest, sub == "mute")
                    await self.send_message(
                        f"Proactive '{rest}' {'muted — no more pushes of this kind.' if sub == 'mute' else 'unmuted.'}"
                    )
                elif sub == "digest":
                    res = await self.orch.toolbox.execute("proactive_digest", '{"force": true}')
                    await self.send_message(res[:1500])
                elif sub == "sweep":
                    res = await self.orch.toolbox.execute("proactive_nudge_sweep", "{}")
                    await self.send_message(res[:1500])
                elif sub == "auto" and rest in ("on", "off"):
                    try:
                        auto, dry = self.orch.set_auto_state(auto=(rest == "on"))
                        mode = "dry-run (no execution)" if dry else "LIVE (executes)"
                        await self.send_message(
                            f"Auto-act {'ON' if auto else 'OFF'} — {mode}. "
                            f"{'First 48h: keep dry-run ON, review /proactive acts.' if auto and dry else ''}".strip()
                        )
                    except Exception as exc:
                        await self.send_message(f"Auto toggle failed: {exc}")
                elif sub == "dry" and rest in ("on", "off", "live"):
                    try:
                        want_dry = rest != "off" and rest != "live"
                        auto, dry = self.orch.set_auto_state(dry=want_dry)
                        await self.send_message(
                            f"Dry-run {'ON (announces only)' if dry else 'OFF — LIVE execution'} (auto={'on' if auto else 'off'})."
                        )
                    except Exception as exc:
                        await self.send_message(f"Dry toggle failed: {exc}")
                elif sub == "acts":
                    try:
                        n = int(rest) if rest.isdigit() else 5
                        rows = self.orch.recent_acts(min(max(n, 1), 10))
                        if not rows:
                            await self.send_message(
                                "No auto-acts logged yet (data/proactive_acts.jsonl)."
                            )
                        else:
                            lines = [f"Recent acts ({len(rows)}):"]
                            for r in rows[-5:]:
                                lines.append(
                                    f"- {r.get('ts', '?')} [{r.get('mode', '?')}] "
                                    f"{r.get('suggestion', '?')}→{r.get('action', '?')} "
                                    f"{str(r.get('detail', ''))[:80]}"
                                )
                            await self.send_message("\n".join(lines)[:1500])
                    except Exception as exc:
                        await self.send_message(f"Acts read failed: {exc}")
                else:
                    st = gate.status()
                    try:
                        auto, dry = self.orch._auto_state()
                        pending = self.orch._auto_schedule_count()
                    except Exception:
                        auto, dry, pending = False, True, 0
                    lines = [
                        f"Proactive pushes today: {st['today']}/{st['cap']}",
                        "Kinds: "
                        + ", ".join(
                            f"{k} ({'muted' if m else 'on'})" for k, m in st["muted"].items()
                        ),
                        f"Auto-act: {'on' if auto else 'off'} (default off) · "
                        f"dry-run: {'on' if dry else 'OFF-LIVE'} · pending auto: {pending}/3",
                        "Usage: /proactive mute|unmute <digest|meeting|nudge> · /proactive digest · /proactive sweep",
                        "· /proactive auto on|off|status · /proactive dry on|off · /proactive acts [n]",
                    ]
                    await self.send_message("\n".join(lines))
            except Exception as exc:
                await self.send_message(f"Proactive error: {exc}")
        elif cmd == "/storm":
            # /storm [status] | list | drain | drop <id> | clear
            try:
                from core.storm_queue import StormQueue

                q = StormQueue(self.orch.cfg.db_path.parent)
                parts = (arg or "").split(None, 1)
                sub = parts[0].lower() if parts else "status"
                rest = parts[1].strip() if len(parts) > 1 else ""
                if sub == "list":
                    items = q.pending()
                    if not items:
                        await self.send_message("Storm queue empty — no missed questions.")
                    else:
                        lines = [f"Storm queue ({len(items)}):"]
                        for it in items[:10]:
                            lines.append(
                                f"- {it['id']} [{it.get('surface', '?')}] "
                                f"{it.get('ts', '?')} · {it['text'][:80]}"
                            )
                        await self.send_message("\n".join(lines)[:1500])
                elif sub == "drain":
                    res = await self.orch.toolbox.execute("storm_drain", "{}")
                    await self.send_message(res[:1500])
                elif sub == "drop" and rest:
                    ok = q.drop(rest.split()[0])
                    await self.send_message(
                        f"Dropped {rest.split()[0]}."
                        if ok
                        else f"No queued item {rest.split()[0]}."
                    )
                elif sub == "clear":
                    n = q.clear()
                    await self.send_message(f"Cleared {n} queued question(s).")
                else:
                    st = q.status()
                    if not st["pending"]:
                        await self.send_message("Storm queue empty — no missed questions.")
                    else:
                        await self.send_message(
                            f"⛈ {st['pending']} question(s) queued (oldest {st['oldest']}). "
                            f"They deliver here + voice when quota returns.\n"
                            f"Usage: /storm list · /storm drain · /storm drop <id> · /storm clear"
                        )
            except Exception as exc:
                await self.send_message(f"Storm error: {exc}")
        elif cmd == "/tasks":
            tasks = self.orch.scheduler.list_tasks()
            if not tasks:
                await self.send_message("No scheduled tasks.")
                return
            lines = [
                f"- {t.name} [{t.trigger_type}] next: {t.next_run or 'never'}" for t in tasks[:15]
            ]
            await self.send_message("Scheduled:\n" + "\n".join(lines))
        elif cmd == "/ask":
            if not arg:
                await self.send_message("Usage: /ask <question> - search your indexed documents")
                return
            await self.send_typing()
            try:
                results = self.orch.knowledge_store.search(arg, k=3)
                if not results:
                    await self.send_message(
                        "No results in knowledge base. Index files first with /index <path>."
                    )
                    return
                context = "\n\n".join(f"[{r['source']}]\n{r['text'][:500]}" for r in results)
                reply_prompt = (
                    f"Based on the following context, answer the question: '{arg}'\n\n"
                    f"Context:\n{context}\n\nAnswer concisely."
                )
                await self._process_and_reply(reply_prompt)
            except Exception as exc:
                await self.send_message(f"Knowledge search error: {exc}")
        elif cmd == "/index":
            if not arg:
                await self.send_message("Usage: /index <path> - index a directory of files")
                return
            await self.send_typing()
            try:
                result = self.orch.knowledge_indexer.index_directory(arg)
                if "error" in result:
                    await self.send_message(f"Index error: {result['error']}")
                else:
                    await self.send_message(
                        f"Indexed {result['files_indexed']} files ({result['files_skipped']} skipped) "
                        f"-> {result['total_chunks']} chunks"
                    )
            except Exception as exc:
                await self.send_message(f"Index error: {exc}")
        elif cmd == "/remember":
            if not arg:
                await self.send_message("Usage: /remember <fact>")
                return
            result = self.orch.memory.add(arg)
            await self.send_message(f"Stored: {result}")
        elif cmd == "/prefs":
            # /prefs [forget <key>] — visible learned preferences
            try:
                bits = (arg or "").split(None, 1)
                if len(bits) == 2 and bits[0].lower() == "forget":
                    ok = self.orch.preferences.remove(bits[1].strip())
                    await self.send_message(
                        f"Forgot '{bits[1].strip()}'."
                        if ok
                        else f"No preference '{bits[1].strip()}'."
                    )
                else:
                    res = await self.orch.toolbox.execute("prefs_list", "{}")
                    await self.send_message(res[:1500])
            except Exception as exc:
                await self.send_message(f"Prefs error: {exc}")
        elif cmd == "/anniversary":
            try:
                res = await self.orch.toolbox.execute("memory_anniversary", "{}")
                await self.send_message(res[:1500])
            except Exception as exc:
                await self.send_message(f"Anniversary error: {exc}")
        elif cmd == "/project":
            # /project list | show <name> | note|thread|convention|standup <name> <text> | forget <name> [text]
            try:
                import json as _pj

                bits = (arg or "").split(None, 2)
                sub = bits[0].lower() if bits else "list"
                if sub == "list" or not arg:
                    res = await self.orch.toolbox.execute("project_list", "{}")
                    await self.send_message(res[:1500])
                elif sub == "show" and len(bits) > 1:
                    res = await self.orch.toolbox.execute(
                        "project_recall", _pj.dumps({"project": bits[1]})
                    )
                    await self.send_message(res[:1500])
                elif sub in ("note", "thread", "convention", "standup") and len(bits) > 2:
                    res = await self.orch.toolbox.execute(
                        "project_remember",
                        _pj.dumps({"project": bits[1], "kind": sub, "text": bits[2]}),
                    )
                    await self.send_message(res[:500])
                elif sub == "forget" and len(bits) > 1:
                    from core.project_memory import ProjectMemory

                    out = ProjectMemory(self.orch.cfg.db_path.parent).forget(
                        bits[1], bits[2] if len(bits) > 2 else ""
                    )
                    await self.send_message(
                        "Forgotten." if out.get("ok") else f"error: {out.get('error')}"
                    )
                else:
                    await self.send_message(
                        "Usage: /project list · /project show <name> · "
                        "/project <note|thread|convention|standup> <name> <text> · "
                        "/project forget <name> [text]"
                    )
            except Exception as exc:
                await self.send_message(f"Project error: {exc}")
        elif cmd == "/id":
            await self.send_message(f"chat_id: <code>{self.chat_id}</code>", parse_mode="HTML")
        else:
            await self.send_message(
                "Unknown command. Try /status, /chat <text>, /screenshot, /analyze, /tasks, /remember, /prefs, /anniversary, /project, /morning, /evening, /demo, /downloads, /snap, /open"
            )

    async def _process_and_reply(self, text: str):
        await self.send_typing()
        collected = []
        try:
            if self._chat_stream_factory is not None:
                stream = self._chat_stream_factory(text)
            else:
                stream = self.orch.process(text, surface="telegram")
            async for provider, chunk in stream:
                if provider in (
                    "tool",
                    "proactive",
                    "ml_proactive",
                    "auto",
                    "bg_dispatch",
                    "skill",
                ):
                    continue
                collected.append(chunk)
        except Exception as exc:
            partial = "".join(collected).strip()
            queued = ""
            try:
                from core.storm_queue import StormQueue, should_queue

                if should_queue(exc) and not partial:
                    res = StormQueue(self.orch.cfg.db_path.parent).enqueue("telegram", text)
                    if res.get("queued"):
                        queued = (
                            f"\n\n⛈ All brains are down — queued ({res['id']}). "
                            f"I'll answer here + voice when quota returns."
                        )
            except Exception:
                pass
            try:
                draft = self.orch.offline_draft(text)
            except Exception:
                draft = ""
            note = f"(brain error: {str(exc)[:200]})"
            await self.send_message(
                (partial + "\n\n" + note + queued).strip() if partial else (note + queued).strip()
            )
            if draft:
                await self.send_message(draft[:1500])
            return
        reply = "".join(collected).strip() or "(no response)"
        await self.send_message(reply)

    async def _handle_photo(self, photo_sizes: list):
        await self.send_typing()
        try:
            largest = photo_sizes[-1]
            file_info = await self._call("getFile", json={"file_id": largest["file_id"]})
            if not file_info:
                await self.send_message("Could not fetch photo.")
                return
            file_path = file_info.get("file_path", "")
            resp = await self._client.get(
                f"https://api.telegram.org/file/bot{self.token}/{file_path}", timeout=30.0
            )
            img_bytes = resp.content
            import io

            from PIL import Image

            from core.vision import ImageAnalyzer

            img = Image.open(io.BytesIO(img_bytes))
            analyzer = ImageAnalyzer(self.orch)
            result = await analyzer.analyze_with_model(img, "Describe this image in detail")
            if "error" in result:
                await self.send_message(f"Vision error: {result['error']}")
            else:
                await self.send_message(
                    f"<b>[{result['provider']}]</b>\n{result['analysis']}", parse_mode="HTML"
                )
        except Exception as exc:
            await self.send_message(f"Photo error: {exc}")

    async def _handle_voice(self, voice: dict):
        await self.send_typing()
        try:
            file_info = await self._call("getFile", json={"file_id": voice["file_id"]})
            if not file_info:
                await self.send_message("Could not fetch voice note.")
                return
            file_path = file_info.get("file_path", "")
            resp = await self._client.get(
                f"https://api.telegram.org/file/bot{self.token}/{file_path}", timeout=30.0
            )
            audio_ogg = resp.content

            import tempfile

            from voice.stt import Transcriber

            tmp = Path(tempfile.gettempdir()) / f"tg_voice_{int(time.time())}.ogg"
            tmp.write_bytes(audio_ogg)
            try:
                transcriber = Transcriber(model_size="tiny")
                text = await asyncio.to_thread(transcriber.transcribe_file, str(tmp))
            finally:
                tmp.unlink(missing_ok=True)

            if not text.strip():
                await self.send_message("(could not transcribe voice note)")
                return
            await self.send_message(f'Heard: "{text}"')
            await self._process_and_reply(text)
        except Exception as exc:
            await self.send_message(f"Voice error: {exc}")

    async def _handle_chat(self, text: str):
        await self._process_and_reply(text)

    # ---------- loop ----------
    async def start(self):
        if self.running:
            return
        self._client = httpx.AsyncClient(timeout=45.0)

        # Retry getMe a few times — transient network blips shouldn't kill Telegram
        me = None
        last_err: str = ""
        for attempt in range(3):
            try:
                me = await self._call("getMe")
                if me:
                    break
                last_err = "getMe returned not-ok (token rejected)"
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            if attempt < 2:
                await asyncio.sleep(2.0 * (attempt + 1))

        if not me:
            await self._client.aclose()
            raise RuntimeError(f"Telegram unavailable after 3 attempts — {last_err}")
        self.running = True
        self._task = asyncio.create_task(self._poll_loop())
        return me.get("username", "bot")

    async def _poll_loop(self):
        consecutive_errors = 0
        # P1 single-owner: track lock dir so 409 surrenders immediately.
        try:
            lock_dir = self.state_path.parent
        except Exception:
            lock_dir = None
        while self.running:
            try:
                resp = await self._client.post(
                    self._url("getUpdates"),
                    json={
                        "offset": self._offset,
                        "timeout": self.poll_timeout,
                        "allowed_updates": ["message", "edited_message"],
                    },
                    timeout=self.poll_timeout + 15.0,
                )
                data = resp.json()
                if data.get("ok"):
                    consecutive_errors = 0
                    for update in data.get("result", []):
                        self._offset = update["update_id"] + 1
                        self._save_state()
                        # One bad update must never break the poll loop or
                        # silently swallow the reply — report and continue.
                        try:
                            await self._handle_update(update)
                        except Exception as exc:
                            print(f"[Telegram] update handler error: {exc}")
                            with contextlib.suppress(Exception):
                                await self.send_message(
                                    f"(internal error handling that message: {str(exc)[:200]})"
                                )
                else:
                    # 409 Conflict: another getUpdates poller (second instance?)
                    desc = str(data.get("description", ""))
                    is_conflict = (
                        "Conflict" in desc
                        or "terminated by other" in desc
                        or "conflict" in desc.lower()
                    )
                    consecutive_errors += 1
                    if is_conflict:
                        # P1: surrender lock immediately so winner keeps polling.
                        try:
                            if lock_dir is not None:
                                from core.single_owner import release

                                release(lock_dir)
                        except Exception:
                            pass
                        print(
                            f"[Telegram] 409 conflict (another poller) — surrendering, retry in 30s: {desc[:120]}"
                        )
                        await asyncio.sleep(30)
                        continue
                    if consecutive_errors % 6 == 1:
                        print(
                            f"[Telegram] getUpdates not-ok (attempt {consecutive_errors}): "
                            f"{desc[:120]}"
                        )
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                consecutive_errors += 1
                if consecutive_errors % 6 == 1:
                    print(f"[Telegram] poll error (attempt {consecutive_errors}): {exc}")
                await asyncio.sleep(5)

    async def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._client:
            await self._client.aclose()
