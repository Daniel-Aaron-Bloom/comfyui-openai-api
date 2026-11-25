import asyncio
import json
from typing import Any

from aiohttp import web
from server import PromptServer

from . import memoryedit
from .logs import get_logger
from .memory import MEMORY, normalize_key


logger = get_logger(__name__)

# The HTTP routes behind the Memory panel of the UI (web/memory_editor.js),
# which lists, reads and edits the values stored by the Save Memory nodes.
# ComfyUI mirrors them under /api, like every route registered here:
#
#   GET /openai_api/memory                  the keys holding a value
#   GET /openai_api/memory/value?key=...    one value, in its editable form
#   PUT /openai_api/memory/value            edit it (JSON body: key, version, value)
#
# Anyone who can reach ComfyUI can read and edit the memory here, as they can
# already run any workflow that loads it.
routes = PromptServer.instance.routes

PATH = "/openai_api/memory"


def _error(status: int, message: str, **extra: Any) -> web.Response:
    return web.json_response({"error": message, **extra}, status=status)


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
    return web.json_response({
        "entries": [
            {"key": key, "version": version, "kind": memoryedit.kind_of(value),
             "summary": memoryedit.summarize(value)}
            for key, value, version in MEMORY.entries()
        ],
    })


@routes.get(f"{PATH}/value")
async def get_memory_value(request: web.Request) -> web.Response:
    key = normalize_key(request.rel_url.query.get("key"))
    value, version = MEMORY.peek(key)
    if value is None:
        return _error(404, "Nothing is stored under this key (anymore)")
    return await _json_response({
        "key": key,
        "version": version,
        "kind": memoryedit.kind_of(value),
        "summary": memoryedit.summarize(value),
        "value": memoryedit.to_editable(value),
    })


def _save(key: str, version: int, raw: Any) -> tuple[int, dict[str, Any]]:
    current, current_version = MEMORY.peek(key)
    if current is None:
        return 404, {"error": "Nothing is stored under this key (anymore)"}
    if current_version != version:
        return 409, {"error": "This key was saved again since it was loaded", "version": current_version}
    try:
        value = memoryedit.parse_edit(current, raw)
    except memoryedit.EditError as e:
        return 400, {"error": str(e)}
    if not MEMORY.save_if_version(key, value, version):
        # Saved by a run between the check above and now
        return 409, {"error": "This key was saved again since it was loaded", "version": MEMORY.version(key)}
    logger.info("Memory: the value of the key '%s' was edited from the UI", key)
    return 200, {"version": version + 1}


@routes.put(f"{PATH}/value")
async def put_memory_value(request: web.Request) -> web.Response:
    try:
        body = await asyncio.to_thread(json.loads, await request.read())
    except ValueError:
        return _error(400, "The request is not valid JSON")
    version = body.get("version") if isinstance(body, dict) else None
    if not isinstance(version, int) or isinstance(version, bool) or "value" not in body:
        return _error(400, "The request needs a key, a version (the one read with the value) and a value")
    key = normalize_key(body.get("key"))
    if key == "":
        return _error(400, "The key must not be empty")
    # Validated and stored in a worker thread, like the parsing above: the
    # deep copy of a big history must not stall the event loop either
    status, payload = await asyncio.to_thread(_save, key, version, body["value"])
    return web.json_response(payload, status=status)
