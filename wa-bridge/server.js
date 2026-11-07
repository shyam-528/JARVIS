// JARVIS wa-bridge: WhatsApp-Web (Baileys) <-> localhost HTTP API.
// Unofficial client: use a secondary number first (small ban risk).
// API (127.0.0.1:3101):
//   GET  /status                    {connected, user}
//   GET  /chats?limit=20            [{id, name, unread}]
//   GET  /messages?chat=<jid>&limit=20  [{id, from, text, ts, mine}]
//   POST /send {chat, text}         {ok}
//   POST /logout                    clears session (re-pair via QR)
import express from "express";
import qrcode from "qrcode-terminal";
import {
  default as makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
} from "@whiskeysockets/baileys";

const PORT = process.env.WA_BRIDGE_PORT || 3101;
const app = express();
app.use(express.json({ limit: "1mb" }));

// In-memory recent-message cache per chat (cap 100 each).
const recent = new Map(); // jid -> [{id, from, text, ts, mine}]
function remember(jid, m) {
  if (!recent.has(jid)) recent.set(jid, []);
  const arr = recent.get(jid);
  if (!arr.some((x) => x.id === m.id)) arr.push(m);
  while (arr.length > 100) arr.shift();
}
function fmt(stanza, mine) {
  const key = stanza.key || {};
  const msg = stanza.message || {};
  const text =
    msg.conversation ||
    msg.extendedTextMessage?.text ||
    msg.imageMessage?.caption ||
    "";
  return {
    id: key.id || "",
    from: mine ? "me" : key.participant || key.remoteJid || "",
    text: String(text).slice(0, 1000),
    ts: Number(stanza.messageTimestamp) || 0,
    mine: !!mine,
  };
}

let sock = null;
let status = { connected: false, user: null, qr: false };

async function connect() {
  const { state, saveCreds } = await useMultiFileAuthState("./wa-bridge/auth");
  const { version } = await fetchLatestBaileysVersion();
  sock = makeWASocket({ version, auth: state, printQRInTerminal: false });
  sock.ev.on("creds.update", saveCreds);
  sock.ev.on("connection.update", (u) => {
    const { connection, lastDisconnect, qr } = u;
    if (qr) {
      status.qr = true;
      console.log("SCAN THIS QR WITH WHATSAPP (Linked devices):");
      qrcode.generate(qr, { small: true });
    }
    if (connection === "open") {
      status = { connected: true, user: sock.user?.id || null, qr: false };
      console.log("wa-bridge connected as", status.user);
    }
    if (connection === "close") {
      status.connected = false;
      const code = lastDisconnect?.error?.output?.statusCode;
      console.log("wa-bridge disconnected:", code);
      if (code === DisconnectReason.loggedOut) {
        console.log("Logged out. POST /logout then restart to re-pair.");
      } else {
        setTimeout(connect, 5000);
      }
    }
  });
  sock.ev.on("messages.upsert", ({ messages }) => {
    for (const s of messages || []) {
      const jid = s.key?.remoteJid;
      if (!jid || jid === "status@broadcast") continue;
      remember(jid, fmt(s, s.key?.fromMe));
    }
  });
}

app.get("/status", (req, res) => res.json(status));
app.get("/chats", async (req, res) => {
  try {
    const limit = Math.min(50, parseInt(req.query.limit || "20", 10));
    const out = [];
    for (const [jid, arr] of recent) {
      const unread = arr.filter((m) => !m.mine).length;
      out.push({ id: jid, messages: arr.length, unread, last: arr[arr.length - 1] || null });
    }
    out.sort((a, b) => (b.last?.ts || 0) - (a.last?.ts || 0));
    res.json(out.slice(0, limit));
  } catch (e) {
    res.status(500).json({ error: String(e).slice(0, 200) });
  }
});
app.get("/messages", (req, res) => {
  const arr = recent.get(req.query.chat || "") || [];
  const limit = Math.min(50, parseInt(req.query.limit || "20", 10));
  res.json(arr.slice(-limit));
});
app.post("/send", async (req, res) => {
  try {
    if (!sock || !status.connected) return res.status(503).json({ error: "not connected" });
    let { chat, text } = req.body || {};
    if (!chat || !text) return res.status(400).json({ error: "chat and text required" });
    // LID resolution: plain phone numbers must be verified via onWhatsApp.
    // Some accounts only deliver to the LID (xxx@lid); PN sends return ok
    // but never arrive. Prefer LID whenever the server provides one.
    if (!String(chat).includes("@")) {
      return res.status(400).json({ error: "chat must be a JID (resolve via /check first)" });
    }
    const sent = await sock.sendMessage(chat, { text: String(text).slice(0, 4000) });
    res.json({ ok: true, id: sent?.key?.id || null });
  } catch (e) {
    res.status(500).json({ error: String(e).slice(0, 200) });
  }
});
app.get("/check", async (req, res) => {
  // ?number=7904269352 -> {exists, jid, lid} — call BEFORE first send.
  try {
    if (!sock || !status.connected) return res.status(503).json({ error: "not connected" });
    const number = String(req.query.number || "").replace(/\D/g, "");
    if (!number) return res.status(400).json({ error: "number required" });
    const [info] = await sock.onWhatsApp(number);
    if (!info || !info.exists) return res.status(404).json({ exists: false });
    res.json({ exists: true, jid: info.jid || `${number}@s.whatsapp.net`, lid: info.lid || null });
  } catch (e) {
    res.status(500).json({ error: String(e).slice(0, 200) });
  }
});
app.post("/logout", async (req, res) => {
  try {
    await sock?.logout();
  } catch {}
  res.json({ ok: true });
});

app.listen(PORT, "127.0.0.1", () => {
  console.log(`wa-bridge on http://127.0.0.1:${PORT}`);
  connect();
});
