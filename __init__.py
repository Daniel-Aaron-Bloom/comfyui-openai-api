from typing_extensions import override
from comfy_api.latest import ComfyExtension, io

from . import routes  # noqa: F401 (registers the HTTP routes of the Memory panel)
from .client import Client
from .completions import ChatCompletion
from .history import TrimHistory
from .memory import SaveMemory, LoadMemory
from .unload import UnloadModel
from .options import OPTION_NODES

# The Memory panel of the UI (a sidebar tab)
WEB_DIRECTORY = "./web"


class OpenAIAPIExtension(ComfyExtension):
    @override
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [
            Client,
            ChatCompletion,
            *OPTION_NODES,
            TrimHistory,
            SaveMemory,
            LoadMemory,
            UnloadModel,
        ]


async def comfy_entrypoint() -> OpenAIAPIExtension:
    return OpenAIAPIExtension()
