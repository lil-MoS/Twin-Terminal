import os
import pty
import asyncio
import signal
import subprocess
import fcntl
import struct
import termios
import json
import logging
import time
import shutil
import platform
from datetime import datetime
from aiohttp import web, WSMsgType
import psutil

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("twin-terminal")

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────
PORT = int(os.environ.get("PORT", "7681"))
HOST = os.environ.get("HOST", "0.0.0.0")
PASSWORD = os.environ.get("TWIN_PASSWORD", "changeme")
INDEX_FILE = os.environ.get("INDEX_FILE", "/app/index.html")
WORK_DIR = os.environ.get("WORK_DIR", "/data")
SHELL = os.environ.get("SHELL_PATH", "/bin/bash")
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "10"))
SESSION_TIMEOUT = int(os.environ.get("SESSION_TIMEOUT", "3600"))

# Active sessions counter
active_sessions = 0
session_lock = asyncio.Lock()

# ─────────────────────────────────────────────
# HTTP Routes
# ─────────────────────────────────────────────
async def index(request):
    """Serve the main HTML page."""
    if not os.path.exists(INDEX_FILE):
        return web.Response(text="index.html not found", status=404)
    return web.FileResponse(INDEX_FILE)


async def favicon(request):
    """Serve a simple favicon to avoid 404 spam."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<rect width="64" height="64" rx="12" fill="#0d1117"/>'
        '<text x="32" y="44" font-size="36" text-anchor="middle" fill="#58a6ff">⚡</text>'
        '</svg>'
    )
    return web.Response(text=svg, content_type="image/svg+xml")


async def health(request):
    """Health check endpoint."""
    return web.json_response({
        "status": "ok",
        "service": "Twin Terminal",
        "version": "1.0.0",
        "time": datetime.utcnow().isoformat() + "Z",
        "active_sessions": active_sessions,
    })


# ─────────────────────────────────────────────
# System Stats Endpoint
# ─────────────────────────────────────────────
async def stats(request):
    """
    Return real system stats:
      - CPU usage percentage (real)
      - Available (usable) RAM in GB
      - Total / used RAM in GB
      - Disk usage
      - Network traffic since boot in GB
      - Uptime
      - Load average
    """
    try:
        # ── CPU ──
        cpu_percent = psutil.cpu_percent(interval=0.1)
        cpu_count = psutil.cpu_count(logical=True)

        # ── RAM (use `available` = actually usable by applications) ──
        mem = psutil.virtual_memory()
        mem_total_gb = mem.total / (1024 ** 3)
        mem_available_gb = mem.available / (1024 ** 3)
        mem_used_gb = mem.used / (1024 ** 3)
        mem_percent = mem.percent

        # ── Disk ──
        try:
            disk = psutil.disk_usage("/")
            disk_total_gb = disk.total / (1024 ** 3)
            disk_used_gb = disk.used / (1024 ** 3)
            disk_percent = disk.percent
        except Exception:
            disk_total_gb = 0.0
            disk_used_gb = 0.0
            disk_percent = 0.0

        # ── Network traffic (total since boot) ──
        net = psutil.net_io_counters()
        bytes_sent = net.bytes_sent
        bytes_recv = net.bytes_recv
        total_bytes = bytes_sent + bytes_recv
        traffic_gb = total_bytes / (1024 ** 3)
        sent_gb = bytes_sent / (1024 ** 3)
        recv_gb = bytes_recv / (1024 ** 3)

        # ── Uptime ──
        boot_time = psutil.boot_time()
        uptime_seconds = int(time.time() - boot_time)
        hours, remainder = divmod(uptime_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{hours}h {minutes}m"

        # ── Load average ──
        try:
            load1, load5, load15 = os.getloadavg()
        except Exception:
            load1 = load5 = load15 = 0.0

        # ── Python / OS info ──
        python_version = platform.python_version()
        os_name = platform.system()

        return web.json_response({
            "cpu": {
                "percent": round(cpu_percent, 1),
                "count": cpu_count,
                "load1": round(load1, 2),
                "load5": round(load5, 2),
                "load15": round(load15, 2),
            },
            "memory": {
                "total_gb": round(mem_total_gb, 2),
                "available_gb": round(mem_available_gb, 2),
                "used_gb": round(mem_used_gb, 2),
                "percent": mem_percent,
            },
            "disk": {
                "total_gb": round(disk_total_gb, 2),
                "used_gb": round(disk_used_gb, 2),
                "percent": disk_percent,
            },
            "network": {
                "total_gb": round(traffic_gb, 2),
                "sent_gb": round(sent_gb, 2),
                "recv_gb": round(recv_gb, 2),
                "limit_gb": 100.0,
                "remaining_gb": round(max(0.0, 100.0 - traffic_gb), 2),
            },
            "system": {
                "uptime": uptime_str,
                "os": os_name,
                "python": python_version,
            },
            "sessions": {
                "active": active_sessions,
                "max": MAX_SESSIONS,
            },
            "status": "online",
            "timestamp": int(time.time()),
        })
    except Exception as error:
        logger.exception("Stats error")
        return web.json_response({"error": str(error)}, status=500)


# ─────────────────────────────────────────────
# File Listing Endpoint (for Files panel)
# ─────────────────────────────────────────────
async def list_files(request):
    """List files in a given path (defaults to WORK_DIR)."""
    path = request.query.get("path", WORK_DIR)
    # Prevent path traversal
    real_path = os.path.realpath(path)
    if not real_path.startswith(os.path.realpath(WORK_DIR)):
        return web.json_response({"error": "Access denied"}, status=403)
    if not os.path.isdir(real_path):
        return web.json_response({"error": "Not a directory"}, status=400)

    entries = []
    try:
        for name in sorted(os.listdir(real_path)):
            full = os.path.join(real_path, name)
            try:
                st = os.stat(full)
                entries.append({
                    "name": name,
                    "is_dir": os.path.isdir(full),
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                    "permissions": oct(st.st_mode)[-3:],
                })
            except OSError:
                continue
    except OSError as e:
        return web.json_response({"error": str(e)}, status=500)

    return web.json_response({
        "path": real_path,
        "parent": os.path.dirname(real_path) if real_path != os.path.realpath(WORK_DIR) else None,
        "entries": entries,
    })


async def read_file(request):
    """Read a file's content (max 1 MB)."""
    path = request.query.get("path", "")
    real_path = os.path.realpath(path)
    if not real_path.startswith(os.path.realpath(WORK_DIR)):
        return web.json_response({"error": "Access denied"}, status=403)
    if not os.path.isfile(real_path):
        return web.json_response({"error": "Not a file"}, status=400)
    try:
        size = os.path.getsize(real_path)
        if size > 1024 * 1024:
            return web.json_response({"error": "File too large"}, status=413)
        with open(real_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return web.json_response({"path": real_path, "content": content, "size": size})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def write_file(request):
    """Write content to a file."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    path = data.get("path", "")
    content = data.get("content", "")
    real_path = os.path.realpath(path)
    if not real_path.startswith(os.path.realpath(WORK_DIR)):
        return web.json_response({"error": "Access denied"}, status=403)
    try:
        os.makedirs(os.path.dirname(real_path), exist_ok=True)
        with open(real_path, "w", encoding="utf-8") as f:
            f.write(content)
        return web.json_response({"success": True, "path": real_path})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ─────────────────────────────────────────────
# Settings Endpoint
# ─────────────────────────────────────────────
SETTINGS_FILE = os.path.join(WORK_DIR, ".twin-settings.json")


async def get_settings(request):
    """Get current settings."""
    defaults = {
        "fontSize": 14,
        "fontFamily": "'Cascadia Code', 'Fira Code', 'JetBrains Mono', monospace",
        "cursorBlink": True,
        "theme": "dark",
        "bellStyle": "none",
        "scrollback": 5000,
        "statsRefreshMs": 2000,
    }
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                saved = json.load(f)
            defaults.update(saved)
        except Exception:
            pass
    return web.json_response(defaults)


async def save_settings(request):
    """Save settings."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=2)
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ─────────────────────────────────────────────
# Terminal WebSocket
# ─────────────────────────────────────────────
async def terminal(request):
    global active_sessions

    async with session_lock:
        if active_sessions >= MAX_SESSIONS:
            return web.Response(text="Max sessions reached", status=503)
        active_sessions += 1

    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=1024 * 1024)
    await ws.prepare(request)

    process = None
    master_fd = None
    slave_fd = None
    sender_task = None
    output_queue = None
    loop = asyncio.get_running_loop()
    client_addr = request.remote

    logger.info(f"New WS connection from {client_addr} (sessions: {active_sessions})")

    try:
        # ── Authentication ──
        first_message = await asyncio.wait_for(ws.receive(), timeout=15.0)
        if first_message.type != WSMsgType.TEXT:
            await ws.close()
            return ws
        try:
            auth = json.loads(first_message.data)
        except Exception:
            await ws.close()
            return ws
        if auth.get("type") != "auth" or auth.get("password") != PASSWORD:
            await ws.send_json({"type": "auth", "success": False})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth", "success": True})
        logger.info(f"Client {client_addr} authenticated")

        # ── Create PTY ──
        master_fd, slave_fd = pty.openpty()

        # ── Environment ──
        env = os.environ.copy()
        env.update({
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "HOME": WORK_DIR,
            "USER": "root",
            "SHELL": SHELL,
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "PS1": "\\[\\e[38;5;39m\\]\\u@\\h\\[\\e[0m\\]:\\[\\e[38;5;214m\\]\\w\\[\\e[0m\\]$ ",
        })

        # ── Start shell ──
        process = subprocess.Popen(
            [SHELL, "--login"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=WORK_DIR,
            env=env,
            preexec_fn=os.setsid,
            close_fds=True,
        )
        os.close(slave_fd)
        slave_fd = None

        # ── Async output queue ──
        output_queue = asyncio.Queue(maxsize=1024)

        def read_pty():
            try:
                data = os.read(master_fd, 65536)
                if data:
                    try:
                        loop.call_soon_threadsafe(output_queue.put_nowait, data)
                    except asyncio.QueueFull:
                        pass
                else:
                    loop.call_soon_threadsafe(output_queue.put_nowait, None)
            except (OSError, IOError):
                try:
                    loop.call_soon_threadsafe(output_queue.put_nowait, None)
                except Exception:
                    pass

        loop.add_reader(master_fd, read_pty)

        # ── Sender task ──
        async def sender():
            try:
                while True:
                    data = await output_queue.get()
                    if data is None:
                        break
                    try:
                        await ws.send_bytes(data)
                    except Exception:
                        break
            except asyncio.CancelledError:
                pass

        sender_task = asyncio.create_task(sender())

        # ── Main receive loop ──
        async for message in ws:
            if message.type == WSMsgType.TEXT:
                try:
                    data = json.loads(message.data)
                except Exception:
                    continue
                msg_type = data.get("type")

                if msg_type == "input":
                    value = data.get("data", "")
                    if value and master_fd is not None:
                        try:
                            os.write(master_fd, value.encode("utf-8", errors="ignore"))
                        except OSError:
                            break

                elif msg_type == "resize":
                    if master_fd is None:
                        continue
                    try:
                        rows = int(data.get("rows", 24))
                        cols = int(data.get("cols", 80))
                        rows = max(2, min(rows, 500))
                        cols = max(10, min(cols, 500))
                        size = struct.pack("HHHH", rows, cols, 0, 0)
                        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, size)
                    except Exception:
                        pass

                elif msg_type == "ping":
                    await ws.send_json({"type": "pong", "t": int(time.time())})

            elif message.type == WSMsgType.BINARY:
                if master_fd is not None:
                    try:
                        os.write(master_fd, message.data)
                    except OSError:
                        break

            elif message.type == WSMsgType.ERROR:
                logger.warning(f"WS error: {ws.exception()}")
                break

    except asyncio.TimeoutError:
        logger.warning(f"Client {client_addr} auth timeout")
    except asyncio.CancelledError:
        pass
    except Exception as error:
        logger.exception(f"Terminal error: {error}")
    finally:
        # ── Cleanup ──
        if master_fd is not None:
            try:
                loop.remove_reader(master_fd)
            except Exception:
                pass

        if sender_task:
            try:
                if output_queue is not None:
                    await output_queue.put(None)
                await asyncio.wait_for(sender_task, timeout=1.0)
            except Exception:
                sender_task.cancel()

        if process:
            try:
                if process.poll() is None:
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                    try:
                        process.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except Exception:
                pass

        if master_fd is not None:
            try:
                os.close(master_fd)
            except Exception:
                pass
        if slave_fd is not None:
            try:
                os.close(slave_fd)
            except Exception:
                pass

        async with session_lock:
            active_sessions -= 1

        logger.info(f"Client {client_addr} disconnected (sessions: {active_sessions})")

    return ws


# ─────────────────────────────────────────────
# App setup
# ─────────────────────────────────────────────
def create_app():
    app = web.Application(client_max_size=10 * 1024 * 1024)
    app.router.add_get("/", index)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_get("/health", health)
    app.router.add_get("/api/stats", stats)
    app.router.add_get("/api/files", list_files)
    app.router.add_get("/api/files/read", read_file)
    app.router.add_post("/api/files/write", write_file)
    app.router.add_get("/api/settings", get_settings)
    app.router.add_post("/api/settings", save_settings)
    app.router.add_get("/ws", terminal)
    return app


if __name__ == "__main__":
    logger.info(f"Starting Twin Terminal on {HOST}:{PORT}")
    logger.info(f"Work dir: {WORK_DIR}")
    logger.info(f"Shell: {SHELL}")
    app = create_app()
    web.run_app(app, host=HOST, port=PORT, access_log=logger)
