import copy
import threading
from typing import Any

from comfy_api.latest import io
from server import PromptServer

from .logs import get_logger


# The memory of the shared slot, which every session reads and writes. The
# other slots are the sessions: a ComfyUI browser tab (its client id)
SHARED = ""

# How many sessions may hold values at once: each tab opened over the days is a
# new session, and a history can hold images. When one more comes in, the least
# recently used one is dropped.
MAX_SESSIONS = 32

logger = get_logger(__name__)


# In-process memory: a helper to carry a value (an analysis, a conversation
# history...) from one queued run to the next. It is kept in RAM only and lost
# when ComfyUI restarts. A value lives in a slot, the shared one (SHARED) or the
# one of a session, so that two sessions using the same key do not meet.
class _MemoryStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[tuple[str, str], Any] = {}
        # Modification number of every (session, key), taken from one counter
        # shared by all of them, and 0 for a key never written. A number is
        # handed out once, so it identifies a value: a counter going back to a
        # previous number could make ComfyUI serve a stale cached Load Memory
        # output. Wiped keys keep theirs for the same reason, and only the
        # eviction of a whole session removes them (the numbers of its keys
        # are then gone for good, and 0 is right: it holds nothing anymore).
        self._versions: dict[tuple[str, str], int] = {}
        self._clock = 0
        # The sessions holding something (SHARED excepted), least recently used first
        self._sessions: dict[str, None] = {}

    def _bump(self, session: str, key: str) -> int:
        """Under the lock: the new version of the key."""
        self._clock += 1
        self._versions[(session, key)] = self._clock
        return self._clock

    def _use(self, session: str) -> None:
        """Under the lock: the session is the most recently used one. Makes
        room by dropping the least recently used sessions."""
        if session == SHARED:
            return
        self._sessions.pop(session, None)
        self._sessions[session] = None
        while len(self._sessions) > MAX_SESSIONS:
            oldest = next(iter(self._sessions))
            del self._sessions[oldest]
            for slot in [slot for slot in self._versions if slot[0] == oldest]:
                del self._versions[slot]
                self._values.pop(slot, None)
            logger.warning("Memory: more than %d sessions hold values, dropped the memory of the least recently used one", MAX_SESSIONS)

    def save(self, session: str, key: str, value: Any) -> None:
        stored = copy.deepcopy(value)
        with self._lock:
            self._values[(session, key)] = stored
            self._bump(session, key)
            self._use(session)

    def wipe(self, session: str, key: str) -> None:
        with self._lock:
            self._values.pop((session, key), None)
            self._bump(session, key)
            self._use(session)

    def load(self, session: str, key: str) -> Any:
        """A copy of the value stored under key, None if there is none."""
        with self._lock:
            stored = self._values.get((session, key))
            if stored is not None:
                self._use(session)
        # Stored values are never mutated in place (save replaces them), so
        # the copy can be made outside of the lock
        return copy.deepcopy(stored)

    def version(self, session: str, key: str) -> int:
        with self._lock:
            return self._versions.get((session, key), 0)

    def peek(self, session: str, key: str) -> tuple[Any, int]:
        """The value stored under key (None if there is none) and its version,
        read together. The value is the stored one, not a copy: read only."""
        with self._lock:
            return self._values.get((session, key)), self._versions.get((session, key), 0)

    def entries(self, session: str) -> list[tuple[str, Any, int]]:
        """The key, value and version of every key of the session holding a
        value (a stored None counts as nothing), sorted by key. The values are
        the stored ones, not copies: read only."""
        with self._lock:
            found = [(slot[1], value, self._versions.get(slot, 0))
                     for slot, value in self._values.items() if slot[0] == session and value is not None]
        return sorted(found, key=lambda entry: entry[0])

    def save_if_version(self, session: str, key: str, value: Any, version: int) -> int | None:
        """Like save, but only if the key was neither saved nor wiped since
        the version returned by peek: None (nothing stored) otherwise, so an
        edit made on an old value never overwrites a newer one. Returns the
        new version."""
        stored = copy.deepcopy(value)
        with self._lock:
            if self._versions.get((session, key), 0) != version:
                return None
            self._values[(session, key)] = stored
            new_version = self._bump(session, key)
            self._use(session)
            return new_version


MEMORY = _MemoryStore()


def current_session(shared: bool | None) -> str:
    """The slot of the memory nodes of the prompt being executed: the shared
    one when they are set to it, otherwise the one of the session the prompt
    was queued from (its client id, which ComfyUI sets before it checks which
    nodes changed). A prompt queued without client id (a script calling the
    API) has no session: it uses the shared slot."""
    if shared:
        return SHARED
    client_id = getattr(PromptServer.instance, "client_id", None)
    return client_id if isinstance(client_id, str) else SHARED


def normalize_key(key: str | None) -> str:
    return key.strip() if isinstance(key, str) else ""


# Optional, so that the API format prompts written before it existed stay
# valid (they miss it: per session, the default)
def shared_input() -> io.Boolean.Input:
    return io.Boolean.Input(
        id="shared",
        display_name="Shared",
        tooltip="Off: this key is private to the session (browser tab) running the workflow, so two tabs using the same key do not meet. On: the key is shared by every session. Save Memory and Load Memory must agree. A prompt queued without client id (a script calling the API) has no session and always uses the shared memory.",
        default=False,
        optional=True,
        advanced=True,
    )


class SaveMemory(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="OAIAPI_SaveMemory",
            display_name="OpenAI API - Save Memory",
            category="OpenAI API/Memory",
            description="Stores a copy of any value (text, history...) in memory under a key, so a Load Memory node can return it on the next queued run. The memory is kept in RAM only, separate for each session (browser tab) unless Shared is on, and lost when ComfyUI restarts. The value is passed through unchanged.",
            is_output_node=True,
            inputs=[
                io.String.Input(
                    id="key",
                    display_name="Key",
                    tooltip="The name under which the value is stored. Leading and trailing spaces are ignored.",
                    default="memory",
                ),
                io.AnyType.Input(
                    id="value",
                    display_name="Value",
                    tooltip="The value to store (any type)",
                ),
                shared_input(),
            ],
            outputs=[
                io.AnyType.Output(
                    id="value_out",
                    display_name="Value",
                    tooltip="The input value, passed through unchanged",
                ),
            ],
        )

    @classmethod
    def validate_inputs(cls, key: str | None) -> bool | str:
        # key is None when it comes from another node: checked at execution
        if key is not None and normalize_key(key) == "":
            return "key must not be empty"
        return True

    @classmethod
    def fingerprint_inputs(cls, **kwargs) -> float:
        # Always execute: after a reset of the key, unchanged (cached) upstream
        # nodes must still store their value again
        return float("nan")

    @classmethod
    def execute(cls, key: str, value: Any, shared: bool = False) -> io.NodeOutput:
        normalized_key = normalize_key(key)
        if normalized_key == "":
            raise ValueError("Save Memory: key must not be empty")
        try:
            MEMORY.save(current_session(shared), normalized_key, value)
        except Exception as e:
            raise ValueError(
                f"Save Memory: this value can not be copied into memory ({type(e).__name__}: {e})") from e
        return io.NodeOutput(value)


class LoadMemory(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="OAIAPI_LoadMemory",
            display_name="OpenAI API - Load Memory",
            category="OpenAI API/Memory",
            description="Returns a copy of the value stored under a key by a Save Memory node of the same session (or a shared one), usually during the previous queued run. Returns the Default input, or None (as if nothing was connected) when the key is empty or nothing (or None) is stored under it.",
            inputs=[
                io.String.Input(
                    id="key",
                    display_name="Key",
                    tooltip="The name under which the value was stored. Leading and trailing spaces are ignored.",
                    default="memory",
                ),
                io.Boolean.Input(
                    id="reset",
                    display_name="Reset",
                    tooltip="Wipe the value stored under this key and return nothing for this run. Untick it afterwards, otherwise the key is reset on every run.",
                    default=False,
                ),
                shared_input(),
                io.AnyType.Input(
                    id="default",
                    display_name="Default",
                    optional=True,
                    tooltip="Optional value returned instead of None when the key is empty, missing, reset or holds None, e.g. on the first run of a loop when the output feeds a required input",
                ),
            ],
            outputs=[
                io.AnyType.Output(
                    id="value",
                    display_name="Value",
                    tooltip="A copy of the stored value, or the default input (None if not connected) if there is none",
                ),
            ],
        )

    @classmethod
    def fingerprint_inputs(cls, **kwargs) -> float | int:
        # ComfyUI evaluates the fingerprints of the whole prompt before
        # executing any node: the version is the one left by the previous run.
        # A linked input (None here) is not known yet: always re-run then.
        reset = kwargs.get("reset")
        key = kwargs.get("key")
        # Missing from the API format prompts written before it existed, which
        # use the default. A linked input is there, as None.
        shared = kwargs.get("shared", False)
        if reset or reset is None or key is None or shared is None:
            return float("nan")
        # Unchanged version: the cached output (and everything downstream)
        # is reused. A version is never given to two keys, so it also tells
        # which session the cached value came from.
        return MEMORY.version(current_session(shared), normalize_key(key))

    @classmethod
    def execute(cls, key: str, reset: bool, shared: bool = False, default: Any = None) -> io.NodeOutput:
        # default is None when not connected: same as returning nothing
        normalized_key = normalize_key(key)
        if normalized_key == "":
            return io.NodeOutput(default)
        session = current_session(shared)
        if reset:
            MEMORY.wipe(session, normalized_key)
            return io.NodeOutput(default)
        # A stored None counts as nothing stored, so default applies to it too
        value = MEMORY.load(session, normalized_key)
        return io.NodeOutput(default if value is None else value)
