import os
import pty
import tty
import asyncio
import signal
import subprocess

from aiohttp import web, WSMsgType


PORT = int(os.environ.get("PORT", "7681"))
PASSWORD = os.environ.get("TWIN_PASSWORD", "changeme")


async def index(request):
    return web.FileResponse("/app/index.html")


async def terminal(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # Authentication
    auth = request.headers.get("X-Twin-Password", "")

    if auth != PASSWORD:
        await ws.send_json({
            "type": "auth",
            "success": False
        })
        await ws.close()
        return ws

    await ws.send_json({
        "type": "auth",
        "success": True
    })

    loop = asyncio.get_running_loop()

    master_fd, slave_fd = pty.openpty()

    env = os.environ.copy()

    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env["HOME"] = "/data"
    env["USER"] = "root"

    shell = "/bin/bash"

    process = subprocess.Popen(
        [shell, "--login"],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd="/data",
        env=env,
        preexec_fn=os.setsid
    )

    os.close(slave_fd)

    queue = asyncio.Queue()

    def read_pty():
        try:
            data = os.read(master_fd, 4096)
            if data:
                loop.call_soon_threadsafe(queue.put_nowait, data)
        except OSError:
            pass

    loop.add_reader(master_fd, read_pty)

    async def sender():
        while True:
            data = await queue.get()

            if data is None:
                break

            try:
                await ws.send_bytes(data)
            except Exception:
                break

    sender_task = asyncio.create_task(sender())

    try:
        async for msg in ws:

            if msg.type == WSMsgType.TEXT:

                try:
                    data = msg.json()
                except Exception:
                    data = None

                if isinstance(data, dict):

                    if data.get("type") == "input":
                        value = data.get("data", "")

                        if value:
                            os.write(
                                master_fd,
                                value.encode(
                                    "utf-8",
                                    errors="ignore"
                                )
                            )

                    elif data.get("type") == "resize":

                        rows = int(data.get("rows", 24))
                        cols = int(data.get("cols", 80))

                        try:
                            import fcntl
                            import struct
                            import termios

                            size = struct.pack(
                                "HHHH",
                                rows,
                                cols,
                                0,
                                0
                            )

                            fcntl.ioctl(
                                master_fd,
                                termios.TIOCSWINSZ,
                                size
                            )

                        except Exception:
                            pass

            elif msg.type == WSMsgType.ERROR:
                break

    finally:

        loop.remove_reader(master_fd)

        try:
            os.killpg(
                os.getpgid(process.pid),
                signal.SIGTERM
            )
        except Exception:
            pass

        try:
            process.wait(timeout=1)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

        try:
            os.close(master_fd)
        except Exception:
            pass

        await queue.put(None)

        try:
            await sender_task
        except Exception:
            pass

    return ws


async def stats(request):

    memory = "N/A"
    cpu = "N/A"

    try:
        with open("/proc/meminfo") as f:
            values = {}

            for line in f:
                parts = line.split()

                if len(parts) >= 2:
                    values[parts[0].rstrip(":")] = int(parts[1])

            total = values.get("MemTotal", 0)
            available = values.get("MemAvailable", 0)

            if total:
                used = total - available
                memory = f"{used / 1024:.0f} MB"

    except Exception:
        pass

    try:
        load = os.getloadavg()[0]
        cpu = f"{load:.2f}"
    except Exception:
        pass

    return web.json_response({
        "memory": memory,
        "cpu": cpu,
        "status": "online"
    })


app = web.Application()

app.router.add_get("/", index)
app.router.add_get("/ws", terminal)
app.router.add_get("/api/stats", stats)

web.run_app(
    app,
    host="0.0.0.0",
    port=PORT
)
