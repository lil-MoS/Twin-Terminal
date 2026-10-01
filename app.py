import os
import pty
import tty
import asyncio
import signal
import subprocess
import fcntl
import struct
import termios

from aiohttp import web, WSMsgType


# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

PORT = int(os.environ.get("PORT", "7681"))
PASSWORD = os.environ.get("TWIN_PASSWORD", "changeme")

INDEX_FILE = "/app/index.html"


# ─────────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────────

async def index(request):
    return web.FileResponse(INDEX_FILE)


# ─────────────────────────────────────────────
# Terminal WebSocket
# ─────────────────────────────────────────────

async def terminal(request):

    ws = web.WebSocketResponse(
        heartbeat=30
    )

    await ws.prepare(request)

    process = None
    master_fd = None
    sender_task = None

    try:

        # ─────────────────────────────────────
        # Authentication
        # ─────────────────────────────────────

        first_message = await ws.receive()

        if first_message.type != WSMsgType.TEXT:
            await ws.close()
            return ws

        try:
            auth = first_message.json()
        except Exception:
            await ws.close()
            return ws

        if auth.get("type") != "auth":

            await ws.send_json({
                "type": "auth",
                "success": False
            })

            await ws.close()

            return ws

        supplied_password = auth.get(
            "password",
            ""
        )

        if supplied_password != PASSWORD:

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


        # ─────────────────────────────────────
        # Create PTY
        # ─────────────────────────────────────

        master_fd, slave_fd = pty.openpty()


        # ─────────────────────────────────────
        # Environment
        # ─────────────────────────────────────

        env = os.environ.copy()

        env.update({

            "TERM": "xterm-256color",

            "COLORTERM": "truecolor",

            "LANG": "C.UTF-8",

            "LC_ALL": "C.UTF-8",

            "HOME": "/data",

            "USER": "root",

            "SHELL": "/bin/bash",

            "PATH":
                "/usr/local/sbin:"
                "/usr/local/bin:"
                "/usr/sbin:"
                "/usr/bin:"
                "/sbin:"
                "/bin"
        })


        # ─────────────────────────────────────
        # Start Bash
        # ─────────────────────────────────────

        process = subprocess.Popen(

            [
                "/bin/bash",
                "--login"
            ],

            stdin=slave_fd,

            stdout=slave_fd,

            stderr=slave_fd,

            cwd="/data",

            env=env,

            preexec_fn=os.setsid
        )


        os.close(slave_fd)

        slave_fd = None


        # ─────────────────────────────────────
        # Async output queue
        # ─────────────────────────────────────

        loop = asyncio.get_running_loop()

        output_queue = asyncio.Queue()


        def read_pty():

            try:

                data = os.read(
                    master_fd,
                    8192
                )

                if data:

                    loop.call_soon_threadsafe(
                        output_queue.put_nowait,
                        data
                    )

            except (OSError, IOError):

                try:

                    loop.call_soon_threadsafe(
                        output_queue.put_nowait,
                        None
                    )
                except Exception:
                    pass


        loop.add_reader(
            master_fd,
            read_pty
        )


        # ─────────────────────────────────────
        # Send terminal output
        # ─────────────────────────────────────

        async def sender():

            while True:

                data = await output_queue.get()

                if data is None:
                    break

                try:

                    await ws.send_bytes(data)

                except Exception:

                    break


        sender_task = asyncio.create_task(
            sender()
        )


        # ─────────────────────────────────────
        # Main WebSocket loop
        # ─────────────────────────────────────

        async for message in ws:

            # ─────────────────────────────────
            # Text message
            # ─────────────────────────────────

            if message.type == WSMsgType.TEXT:

                try:

                    data = message.json()

                except Exception:

                    continue


                message_type = data.get(
                    "type"
                )


                # ─────────────────────────────
                # Terminal input
                # ─────────────────────────────

                if message_type == "input":

                    value = data.get(
                        "data",
                        ""
                    )

                    if value and master_fd is not None:

                        try:

                            os.write(
                                master_fd,
                                value.encode(
                                    "utf-8",
                                    errors="ignore"
                                )
                            )

                        except OSError:

                            break


                # ─────────────────────────────
                # Terminal resize
                # ─────────────────────────────

                elif message_type == "resize":

                    if master_fd is None:
                        continue

                    try:

                        rows = int(
                            data.get(
                                "rows",
                                24
                            )
                        )

                        cols = int(
                            data.get(
                                "cols",
                                80
                            )
                        )

                        # Safety limits

                        rows = max(
                            2,
                            min(rows, 200)
                        )

                        cols = max(
                            10,
                            min(cols, 300)
                        )


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


            # ─────────────────────────────────
            # Ping
            # ─────────────────────────────────

            elif message.type == WSMsgType.PING:

                await ws.pong()


            # ─────────────────────────────────
            # Error
            # ─────────────────────────────────

            elif message.type == WSMsgType.ERROR:

                break


    except asyncio.CancelledError:

        pass

    except Exception as error:

        print(
            "Terminal error:",
            repr(error)
        )


    finally:

        # ─────────────────────────────────────
        # Remove PTY reader
        # ─────────────────────────────────────

        if master_fd is not None:

            try:

                loop = asyncio.get_running_loop()

                loop.remove_reader(
                    master_fd
                )

            except Exception:

                pass


        # ─────────────────────────────────────
        # Stop sender
        # ─────────────────────────────────────

        if sender_task:

            try:

                await output_queue.put(
                    None
                )

            except Exception:

                pass

            try:

                await asyncio.wait_for(
                    sender_task,
                    timeout=1
                )

            except Exception:

                sender_task.cancel()


        # ─────────────────────────────────────
        # Stop shell process
        # ─────────────────────────────────────

        if process:

            try:

                if process.poll() is None:

                    os.killpg(
                        os.getpgid(
                            process.pid
                        ),
                        signal.SIGTERM
                    )

            except Exception:

                pass


            try:

                process.wait(
                    timeout=1
                )

            except Exception:

                try:

                    process.kill()

                except Exception:

                    pass


        # ─────────────────────────────────────
        # Close PTY
        # ─────────────────────────────────────

        if master_fd is not None:

            try:

                os.close(
                    master_fd
                )

            except Exception:

                pass


        if slave_fd is not None:

            try:

                os.close(
                    slave_fd
                )

            except Exception:

                pass


    return ws


# ─────────────────────────────────────────────
# System statistics
# ─────────────────────────────────────────────

async def stats(request):

    memory = "N/A"
    cpu = "N/A"

    # Memory

    try:

        values = {}

        with open(
            "/proc/meminfo",
            "r"
        ) as file:

            for line in file:

                parts = line.split()

                if len(parts) >= 2:

                    key = parts[0].rstrip(":")

                    try:

                        values[key] = int(
                            parts[1]
                        )

                    except ValueError:

                        pass


        total = values.get(
            "MemTotal",
            0
        )

        available = values.get(
            "MemAvailable",
            0
        )


        if total:

            used = total - available

            memory = (
                f"{used / 1024:.0f} MB"
            )


    except Exception:

        pass


    # Load

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


# ─────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────

async def health(request):

    return web.json_response({

        "status": "ok",

        "service": "Twin Terminal",

        "version": "1.0"

    })


# ─────────────────────────────────────────────
# Application
# ─────────────────────────────────────────────

app = web.Application(

    client_max_size=1024 * 1024

)


app.router.add_get(
    "/",
    index
)

app.router.add_get(
    "/ws",
    terminal
)

app.router.add_get(
    "/api/stats",
    stats
)

app.router.add_get(
    "/health",
    health
)


# ─────────────────────────────────────────────
# Startup
# ─────────────────────────────────────────────

if __name__ == "__main__":

    print(
        "╭──────────────────────────────────────╮"
    )

    print(
        "│        ⚡ TWIN TERMINAL              │"
    )

    print(
        "╰──────────────────────────────────────╯"
    )

    print(
        f"Listening on 0.0.0.0:{PORT}"
    )

    print(
        "Web terminal: /"
    )

    print(
        "Health check: /health"
    )

    web.run_app(

        app,

        host="0.0.0.0",

        port=PORT,

        access_log=None
    )
