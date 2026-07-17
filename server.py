"""
TraceDownloader — web server (FastAPI REST + SSE).

Run:  uvicorn server:app --host 127.0.0.1 --port 8686
The engine package owns every download/queue/DB decision; this file is only the
HTTP layer on top of it.
"""

import asyncio
import json
import os
import signal
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse

import engine as eng

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

engine: eng.Engine = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine
    engine = eng.Engine()
    yield
    engine.shutdown()


app = FastAPI(title="TraceDownloader", version=eng.APP_VERSION, lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/state")
def state(q: str = "", expanded: str = ""):
    exp = [e for e in expanded.split(",") if e]
    return engine.snapshot(q=q, expanded=exp)


@app.get("/api/events")
async def events(request: Request):
    """SSE: pushes a change signal (version) plus toasts. The client
    throttles /api/state re-fetches off of the signal rather than polling."""
    async def gen():
        last_ver   = -1
        # Skip toasts that happened before this client connected, so a new
        # tab doesn't get flooded with old notifications.
        old = engine.toasts_since(0)
        last_toast = old[-1][0] if old else 0
        while True:
            if await request.is_disconnected():
                break
            ver = await asyncio.to_thread(engine.wait_change, last_ver, 25.0)
            toasts = engine.toasts_since(last_toast)
            if toasts:
                last_toast = toasts[-1][0]
            payload = json.dumps({"version": ver, "toasts": [m for _, m in toasts]},
                                 ensure_ascii=False)
            yield f"data: {payload}\n\n"
            last_ver = ver
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/add")
def add(body: dict):
    # Sync def -> runs in FastAPI's thread pool. If this were async, waiting
    # on the engine lock would freeze the whole event loop (and every other
    # request with it).
    n = engine.add_urls(body.get("urls", ""))
    return {"added": n}


@app.post("/api/start_all")
def start_all():
    engine._start_all()
    return {"ok": True}


@app.post("/api/stop_all")
def stop_all():
    engine._stop_all()
    return {"ok": True}


@app.post("/api/save")
def save():
    ok = engine._save_all(silent=False)
    return {"ok": ok}


@app.post("/api/action")
def action(body: dict):
    ids    = body.get("ids") or []
    act    = body.get("action") or ""
    try:
        engine.apply_action(ids, act)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True}


@app.post("/api/reorder")
def reorder(body: dict):
    engine.reorder_groups(body.get("group_ids") or [])
    return {"ok": True}


@app.post("/api/delete")
def delete(body: dict):
    ids        = body.get("ids") or []
    with_files = bool(body.get("with_files"))
    token, files = engine.delete_tasks(ids, with_files=with_files)
    return {"ok": True, "token": token, "files": files}


@app.post("/api/delete_files")
def delete_files(body: dict):
    ok = engine.confirm_delete_files(body.get("token") or "")
    return {"ok": ok}


@app.post("/api/res_check")
def res_check(body: dict):
    if body.get("all_done"):
        engine._res_check_all_done()
    else:
        groups = engine._groups_for_ids(body.get("ids") or [])
        if groups:
            engine._res_check_groups(groups)
    return {"ok": True}


@app.post("/api/size_check")
def size_check(body: dict):
    if body.get("all_done"):
        engine._size_check_all_done()
    else:
        groups = engine._groups_for_ids(body.get("ids") or [])
        if groups:
            engine._size_check_groups(groups)
    return {"ok": True}


@app.post("/api/missing_check")
def missing_check(body: dict):
    """Find videos marked completed in the DB whose file is actually gone —
    returns a result plus a confirmation token (2-step, see /api/missing_confirm)."""
    if body.get("all_done"):
        return engine.missing_check()
    return engine.missing_check(body.get("ids") or [])


@app.post("/api/missing_confirm")
def missing_confirm(body: dict):
    n = engine.confirm_missing_redownload(body.get("token") or "")
    return {"ok": n > 0, "count": n}


@app.post("/api/done_ops")
def done_ops(body: dict):
    op = body.get("op") or ""
    if op == "recheck_all":
        engine._recheck_all_done()
    elif op == "retry_all":
        engine._retry_all_errors_skipped()
    elif op == "redownload_all":
        engine._redownload_all_done()
    else:
        return JSONResponse({"error": f"unknown op: {op}"}, status_code=400)
    return {"ok": True}


@app.post("/api/locate")
def locate(body: dict):
    """Real file/folder path for a video or group (consumed by the optional
    desktop "open" helper — see client/)."""
    r = engine.locate_path(body.get("id") or "")
    return r or JSONResponse({"error": "not found"}, status_code=404)


@app.post("/api/shutdown")
def shutdown():
    """Save and exit cleanly (exit code 0), so a process manager configured
    with restart-on-failure won't bring it back automatically."""
    engine._save_all(silent=True)

    def _term():
        time.sleep(0.4)  # let the HTTP response reach the client before SIGTERM
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_term, daemon=True).start()
    return {"ok": True}


@app.get("/api/settings")
def get_settings():
    return engine.get_settings()


@app.post("/api/settings")
def set_settings(body: dict):
    engine.set_settings(body)
    return {"ok": True}


@app.post("/api/check_updates")
def check_updates():
    """Manual "check now" for yt-dlp/gallery-dl/ffmpeg (also runs on its own
    on a timer - see engine._update_check_loop)."""
    return engine.check_tool_updates()


@app.post("/api/check_app_update")
def check_app_update():
    """Is there a newer TraceDownloader release? Returns version info; the
    web server updates itself with `git pull` (deploy/update.sh), so this is
    informational."""
    return engine.check_app_update()


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("APP_HOST", "127.0.0.1")
    port = int(os.environ.get("APP_PORT", "8686"))
    uvicorn.run(app, host=host, port=port)
