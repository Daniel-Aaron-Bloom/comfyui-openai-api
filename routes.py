import asyncio
import json
from typing import Any

from aiohttp import web
from server import PromptServer

from . import memoryedit
from .logs import get_logger
from .memory import MEMORY, SHARED, normalize_key


logger = get_logger(__name__)

# The HTTP routes behind the Memory panel of the UI (web/memory_editor.js),
# which lists, reads and edits the values stored by the Save Memory nodes.
# ComfyUI mirrors them under /api, like every route registered here:
#
#   GET /openai_api/memory?client_id=...                       the keys holding a value
#   GET /openai_api/memory/value?key=...&scope=...&client_id=...   one value, in its editable form
#   PUT /openai_api/memory/value            edit it (JSON body: key, scope, client_id, version, value)
#
# The memory is split in sessions (see memory.py). The client id of the page is
# the session it speaks for: a route only reaches that one and the shared
# memory, never the memory of another session. The scope tells which of the
# two a key is in. This is no authentication though: the client id is only
# a secret nobody else is told. Anyone who can reach ComfyUI can read and edit
# the shared memory here, as they can already run any workflow that loads it.
routes = PromptServer.instance.routes

PATH = "/openai_api/memory"

SCOPE_SESSION = "session"
SCOPE_SHARED = "shared"


def _error(status: int, message: str, **extra: Any) -> web.Response:
    return web.json_response({"error": message, **extra}, status=status)


def _slot(scope: Any, client_id: Any) -> str | None:
    """The session the request speaks of: SHARED for the shared memory, the
    client id for the memory of the page. None when the request is not valid
    (no such scope, or a page without client id yet)."""
    if scope == SCOPE_SHARED:
        return SHARED
    if scope == SCOPE_SESSION and isinstance(client_id, str) and client_id != "":
        return client_id
    return None


def _bad_slot() -> web.Response:
    return _error(400, f"The scope must be '{SCOPE_SESSION}' (with the client id of the page) or '{SCOPE_SHARED}'")


async def _json_response(data: Any) -> web.Response:
    """The JSON response, serialized in a worker thread: a history holding
    images is several megabytes, and the event loop also serves the prompts
    and the UI."""
    try:
        text = await asyncio.to_thread(json.dumps, data)
    except (TypeError, ValueError) as e:
        return _error(422, f"This value can not be shown ({type(e).__name__}: {e})")
    return web.Response(text=text, content_type="application/json")


@routes.get(PATH)
async def list_memory(request: web.Request) -> web.Response:
    slots = [(SCOPE_SHARED, SHARED)]
    # A page without client id (its socket is not connected yet) has no session
    client_id = request.rel_url.query.get("client_id")
    if client_id:
        slots.insert(0, (SCOPE_SESSION, client_id))
    return web.json_response({
        "entries": [
            {"scope": scope, "key": key, "version": version, "kind": memoryedit.kind_of(value),
             "summary": memoryedit.summarize(value)}
            for scope, session in slots
            for key, value, version in MEMORY.entries(session)
        ],
    })


@routes.get(f"{PATH}/value")
async def get_memory_value(request: web.Request) -> web.Response:
    query = request.rel_url.query
    scope = query.get("scope")
    session = _slot(scope, query.get("client_id"))
    if session is None:
        return _bad_slot()
    key = normalize_key(query.get("key"))
    value, version = MEMORY.peek(session, key)
    if value is None:
        return _error(404, "Nothing is stored under this key (anymore)")
    return await _json_response({
        "scope": scope,
        "key": key,
        "version": version,
        "kind": memoryedit.kind_of(value),
        "summary": memoryedit.summarize(value),
        "value": memoryedit.to_editable(value),
    })


def _save(session: str, scope: str, key: str, version: int, raw: Any) -> tuple[int, dict[str, Any]]:
    current, current_version = MEMORY.peek(session, key)
    if current is None:
        return 404, {"error": "Nothing is stored under this key (anymore)"}
    if current_version != version:
        return 409, {"error": "This key was saved again since it was loaded", "version": current_version}
    try:
        value = memoryedit.parse_edit(current, raw)
    except memoryedit.EditError as e:
        return 400, {"error": str(e)}
    new_version = MEMORY.save_if_version(session, key, value, version)
    if new_version is None:
        # Saved by a run between the check above and now
        return 409, {"error": "This key was saved again since it was loaded", "version": MEMORY.version(session, key)}
    logger.info("Memory: the value of the key '%s' (%s) was edited from the UI", key, scope)
    return 200, {"version": new_version}


@routes.put(f"{PATH}/value")
async def put_memory_value(request: web.Request) -> web.Response:
    try:
        body = await asyncio.to_thread(json.loads, await request.read())
    except ValueError:
        return _error(400, "The request is not valid JSON")
    version = body.get("version") if isinstance(body, dict) else None
    if not isinstance(version, int) or isinstance(version, bool) or "value" not in body:
        return _error(400, "The request needs a key, a version (the one read with the value) and a value")
    scope = body.get("scope")
    session = _slot(scope, body.get("client_id"))
    if session is None:
        return _bad_slot()
    key = normalize_key(body.get("key"))
    if key == "":
        return _error(400, "The key must not be empty")
    # Validated and stored in a worker thread, like the parsing above: the
    # deep copy of a big history must not stall the event loop either
    status, payload = await asyncio.to_thread(_save, session, scope, key, version, body["value"])
    return web.json_response(payload, status=status)
