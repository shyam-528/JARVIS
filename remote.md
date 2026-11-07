# Remote Access Guide for JARVIS MARK 3

## Overview

This guide covers setting up remote access to your JARVIS instance from anywhere.

## Option 1: Telegram Bot (Recommended)

**Best for: Mobile access, push notifications, two-way communication**

No tunneling required — Telegram relays through their global infrastructure.

### Setup

1. **Create a Telegram Bot**
   - Message [@BotFather](https://t.me/BotFather) on Telegram
   - Send `/newbot`
   - Choose a name (e.g., "My JARVIS")
   - Choose a username (must end in `bot`, e.g., `myjarvis_bot`)
   - Copy the **bot token** provided

2. **Get your Chat ID**
   - Start a chat with your new bot
   - Send any message
   - Visit: `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
   - Find `"chat":{"id":123456789,...}` — that's your chat ID

3. **Configure**
   ```yaml
   # config.yaml
   telegram:
     enabled: true
     bot_token: "YOUR_BOT_TOKEN"
     chat_id: "123456789"
   ```

3. **Set Environment Variables**
   ```bash
   # .env
   TELEGRAM_BOT_TOKEN=your_bot_token
   TELEGRAM_CHAT_ID=your_chat_id
   ```

4. **Start JARVIS**
   ```bash
   .\.venv\Scripts\python.exe -m uvicorn api.main:app --host 0.0.0.0 --port 8080
   ```

### Features Available via Telegram
- `/start` — Register your chat
- `/status` — System status
- `/chat <text>` — Chat with JARVIS
- `/screenshot` — Capture and send screen
- `/analyze <prompt>` — AI vision analysis
- `/tasks` — List scheduled tasks
- `/remember <fact>` — Store memory
- `/screenshot` — Capture screen
- `/analyze <prompt>` — Vision analysis
- Voice notes → Transcribe + process
- Photos → OCR + AI analysis

---

## Option 2: Tailscale (VPN Mesh)

**Best for: Full LAN-like access, direct HUD access, no port forwarding**

### Setup
1. **Install Tailscale** on both server and phone
   ```bash
   # Windows (server)
   winget install Tailscale.Tailscale
   # Or download from https://tailscale.com/download
   
   # iOS/Android
   # App Store / Play Store: Tailscale
   ```

2. **Authenticate** on both devices with same account

4. **Access HUD**
   - Phone browser: `http://<tailscale-ip>:8080/app`
   - Install as PWA: browser menu → "Install app"

### Tailscale Funnel (Optional - public HTTPS)
```bash
tailscale funnel 8080
# Gives you a stable https://<name>.tailnet.ts.net/app
```

---

## Option 3: Cloudflare Tunnel (cloudflared)

**Best for: Public HTTPS without open ports**

### Setup
```bash
# Install
winget install Cloudflare.cloudflared

# Authenticate
cloudflared tunnel login

# Create tunnel
cloudflared tunnel create jarvis

# Route to local port
cloudflared tunnel route dns <tunnel-id> jarvis.yourdomain.com

# Run
cloudflared tunnel run <tunnel-id> --url http://localhost:8080
```

### systemd service (Linux)
```ini
[Unit]
Description=JARVIS Cloudflare Tunnel
After=network.target

[Service]
Type=simple
User=krith
WorkingDirectory=/home/krith/mark3
ExecStart=/usr/bin/cloudflared tunnel run --url http://localhost:8080 jarvis
Restart=always

[Install]
WantedBy=multi-user.target
```

---

## Comparison

| Feature | Telegram | Tailscale | Cloudflare |
|---------|----------|-----------|------------|
| **Setup time** | 2 min | 3 min | 10 min |
| **Cost** | Free | Free | Free (1 tunnel) |
| **Mobile app** | ✅ Native | ✅ Native | ❌ Web only |
| **Two-way** | ✅ Full | ✅ Full | ✅ Web only |
| **Images/Voice** | ✅ Full | ✅ Full | ⚠️ Web only |
| **Firewall friendly** | ✅ | ✅ (mesh) | ✅ |
| **Reliability** | 99.99% | 99.9% | 99.9% |
| **Setup effort** | ⭐ | ⭐⭐ | ⭐⭐⭐ |

## Recommendation

**Start with Telegram** — it works instantly, requires no port forwarding, gives you two-way control from anywhere, and handles media natively.

---

## PWA Installation (All Methods)

Once you have any remote access working:

1. Open `http://<your-ip>:8080/app` on phone browser
2. Browser menu → "Install app" / "Add to Home Screen"
3. JARVIS installs as a standalone app with offline caching
4. Works offline for cached content

---

## Troubleshooting

### Telegram bot not responding
```bash
# Check bot token
curl "https://api.telegram.org/bot<TOKEN>/getMe"

# Check webhook (should be empty for polling)
curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
```

### HUD not loading on mobile
- Ensure `Cache-Control: no-cache` headers (already configured)
- Try `http://<ip>:8080/app` directly
- Check firewall: port 8080 open on server

### Notifications not arriving
- Check `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`
- Ensure bot is started: `journalctl -u jarvis -f` (if systemd)
- Test: `curl -X POST "https://api.telegram.org/bot<TOKEN>/sendMessage" -d "chat_id=<ID>&text=test"`

---

## Quick Reference Card

| Task | Command |
|------|---------|
| Start server | `.\.venv\Scripts\python.exe -m uvicorn api.main:app --host 0.0.0.0 --port 8080` |
| Test Telegram | `curl "https://api.telegram.org/bot<TOKEN>/getMe"` |
| Send test msg | `curl -X POST "https://api.telegram.org/bot<TOKEN>/sendMessage" -d "chat_id=<ID>&text=test"` |
| View logs | `Get-Content jarvis.log -Wait -Tail 50` |
| Restart | `Stop-Process -Name python; .\.venv\Scripts\python.exe -m uvicorn api.main:app --host 0.0.0.0 --port 8080` |