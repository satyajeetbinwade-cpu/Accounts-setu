"""Diagnostic: how many `access-control-allow-origin` headers does `/_event` send?

Run:  ./venv/bin/python verify_cors_stack.py

This is EVIDENCE, not a pass/fail gate — it exits 0 whatever it finds. It
exists because `rxconfig.py` carries a long note about cross-origin realtime
and a claim like that should be reproducible rather than folklore.

Reflex builds this stack (reflex/app.py):

    Starlette(top_asgi_app)                      # CORSMiddleware(allow_origins=X)
      └── Mount /_event -> engineio.ASGIApp(AsyncServer(cors_allowed_origins=Y))

where Y is `"*"` (or `list(X)`) when `config.transport == "websocket"`, else `[]`.
`/_event` is the realtime endpoint, so it is the one that must be correct:
engineio writes its own ACAO header, and Starlette's middleware appends a
second one. Browsers reject a duplicated ACAO, so a CROSS-ORIGIN client cannot
complete the handshake.

Re-run this after changing `cors_allowed_origins`, changing the `transport`, or
upgrading Reflex: if variant A starts reporting a single header, the bug is
fixed upstream and the note in `rxconfig.py` can go away.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import httpx
import uvicorn
from socketio import ASGIApp as EngineIOApp
from socketio import AsyncServer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route

ORIGIN = "http://localhost:3100"

# (name, starlette allow_origins, engineio cors_allowed_origins, engineio credentials)
VARIANTS: list[tuple[str, object, object, bool]] = [
    ('A. cors_allowed_origins="*"  (what rxconfig uses)', "*", ["*"], True),
    ('B. cors_allowed_origins=("*",)  (Reflex\'s own branch)', ("*",), "*", True),
    (
        "C. transport != websocket  (engineio CORS off)",
        "*",
        [],
        False,
    ),
    (
        "D. an explicit origin list",
        ["http://localhost:3100", "http://localhost:3000"],
        ["http://localhost:3100", "http://localhost:3000"],
        True,
    ),
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _build(starlette_origins, engineio_origins, engineio_credentials) -> Starlette:
    sio = AsyncServer(
        async_mode="asgi",
        cors_allowed_origins=engineio_origins,
        cors_credentials=engineio_credentials,
    )
    inner = Starlette(
        routes=[
            Route("/ping", lambda _r: PlainTextResponse("pong")),
            Mount("/_event", app=EngineIOApp(sio, socketio_path="")),
        ]
    )
    return Starlette(
        routes=[Mount("/", app=inner)],
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
                allow_origins=starlette_origins,
            )
        ],
    )


async def _probe(name, starlette_origins, engineio_origins, engineio_credentials) -> bool:
    app = _build(starlette_origins, engineio_origins, engineio_credentials)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)

    print(f"  {name}")
    print(
        f"    starlette={starlette_origins!r} engineio={engineio_origins!r} "
        f"engineio_credentials={engineio_credentials}"
    )
    single = True
    async with httpx.AsyncClient() as client:
        for path in ("/ping", "/_event/?EIO=4&transport=polling"):
            resp = await client.get(
                f"http://127.0.0.1:{port}{path}", headers={"Origin": ORIGIN}
            )
            acao = resp.headers.get_list("access-control-allow-origin")
            if len(acao) != 1:
                single = False
            print(
                f"    {'OK  ' if len(acao) == 1 else str(len(acao)) + '   '}"
                f"{path.split('?')[0]:9} status={resp.status_code} "
                f"ACAO={acao}"
            )
    server.should_exit = True
    thread.join(timeout=5)
    return single


async def _main() -> int:
    results = {}
    for name, s_origins, e_origins, e_creds in VARIANTS:
        results[name] = await _probe(name, s_origins, e_origins, e_creds)
        print()
    print("=== exactly one ACAO header on BOTH /ping and /_event? ===")
    for name, ok in results.items():
        print(f"  {'YES' if ok else 'no '}  {name}")
    print(
        "\nExpected today: only variant C is clean, and it is not usable — it\n"
        "means transport != \"websocket\", i.e. HTTP polling instead of realtime.\n"
        "Production is unaffected because Caddy serves ONE origin (no CORS)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
