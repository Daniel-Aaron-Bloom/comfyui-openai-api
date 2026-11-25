import os
import ssl
from typing import Any
from urllib.parse import urlparse

from openai import AsyncOpenAI, DefaultAsyncHttpxClient, OpenAIError
from comfy_api.latest import io

from . import aioloop
from .logs import get_logger
from .apierrors import KEYLESS_API_KEY, has_no_api_prefix
from .iotypes import ParamClient, check_input

# API keys must never travel through widget inputs: ComfyUI serializes every
# widget value (even from bypassed nodes) into the `prompt` and `workflow`
# metadata chunks of generated images and exported workflows, which would leak
# them. The key instead comes from the OPENAI_API_KEY environment variable at
# execution time, with a placeholder for keyless servers (KEYLESS_API_KEY).
# The variable is read by the OpenAI SDK itself (its documented default when no
# api_key is given): this module only checks what the SDK picked up.

logger = get_logger(__name__)

# Base URLs already warned about, to log each warning only once
_warned_base_urls: set[str] = set()
_warned_unverified_base_urls: set[str] = set()


def clean_api_key(api_key: str) -> str:
    """Clean the API key read by the SDK from the OPENAI_API_KEY environment
    variable, the keyless placeholder when it is empty. The key ends up in the
    Authorization HTTP header, which only accepts printable ASCII: fail early
    with a clear message (never showing the value) instead of an encoding
    error deep inside the HTTP client."""
    # Surrounding whitespace (e.g. a trailing newline from a copy/paste) is
    # never part of a key
    api_key = api_key.strip()
    if api_key == "":
        return KEYLESS_API_KEY
    invalid_at = next((i for i, c in enumerate(api_key) if not " " <= c <= "~"), None)
    if invalid_at is not None:
        raise ValueError(
            "The OPENAI_API_KEY environment variable does not contain a valid API key: "
            f"it is {len(api_key)} characters long and has a non ASCII or control character "
            f"at position {invalid_at}, which an HTTP header can not carry (its value is not shown on purpose). "
            "It probably holds pasted text instead of a key: check where it is set "
            "(system or user environment variables, or the environment variables of your "
            "ComfyUI Desktop installation) and restart ComfyUI."
        )
    return api_key


def check_base_url(base_url: str) -> str | None:
    """The error message for an unusable Base URL, None if valid. Never
    let an empty one through: the SDK would silently replace it with the
    OPENAI_BASE_URL environment variable or https://api.openai.com/v1."""
    if base_url.strip() == "":
        return "the Base URL is empty"
    try:
        scheme = urlparse(base_url).scheme
    except ValueError as e:
        return f"invalid URL: {e}"
    if scheme not in ("http", "https"):
        return "URL scheme must be http or https"
    return None


def is_pem_text(ca_cert: str) -> bool:
    """Whether the CA Certificate input holds the PEM text itself (pasted)
    instead of the path of a file."""
    return "-----BEGIN " in ca_cert


def clean_ca_cert_path(ca_cert: str) -> str:
    """The path typed in the CA Certificate input: without the surrounding
    spaces or quotes (Windows "Copy as path" adds them), with ~ expanded.
    Empty when there is none."""
    path = ca_cert.strip()
    if len(path) >= 2 and path[0] == path[-1] and path[0] in "\"'":
        path = path[1:-1].strip()
    return os.path.expanduser(path)


def has_ca_cert(ca_cert: str) -> bool:
    """Whether the CA Certificate input holds anything (empty quotes are not)."""
    return is_pem_text(ca_cert) or clean_ca_cert_path(ca_cert) != ""


def load_ssl_context(ca_cert: str) -> ssl.SSLContext:
    """An SSL context trusting only the certificates of the CA Certificate
    input, a PEM text or the path of a PEM file (the self-signed server
    certificate itself, or the CA that signed it): the default trusted ones
    are replaced, as with curl --cacert. Raises a ValueError with a clear
    message when it is unusable."""
    if is_pem_text(ca_cert):
        # A private key pasted along (or instead): it would be saved in the
        # workflow and in the metadata of generated images, like an API key
        if "PRIVATE KEY-----" in ca_cert:
            raise ValueError(
                "the CA Certificate holds a private key: never paste one, it would be saved in the "
                "workflow and in the metadata of generated images. Only the public certificate is needed")
        # Pasted text often carries indentation or Windows line endings
        pem = "\n".join(line.strip() for line in ca_cert.strip().splitlines()) + "\n"
        try:
            return ssl.create_default_context(cadata=pem)
        except ssl.SSLError as e:
            raise ValueError(f"the pasted CA Certificate is not a valid PEM certificate ({e.reason or e})") from None
    path = clean_ca_cert_path(ca_cert)
    try:
        return ssl.create_default_context(cafile=path)
    except FileNotFoundError:
        raise ValueError(
            f"the CA Certificate file {path} does not exist (the path is read on the machine running ComfyUI)") from None
    except ssl.SSLError as e:
        raise ValueError(
            f"the CA Certificate file {path} holds no valid PEM certificate ({e.reason or e})") from None
    except OSError as e:
        raise ValueError(f"the CA Certificate file {path} can not be read ({e.strerror or e})") from None


def check_ca_cert(ca_cert: str) -> str | None:
    """The error message for an unusable CA Certificate, None if valid or empty."""
    if not has_ca_cert(ca_cert):
        return None
    try:
        load_ssl_context(ca_cert)
    except ValueError as e:
        return str(e)
    return None


def build_verify(verify_ssl: bool, ca_cert: str) -> ssl.SSLContext | bool:
    """What the HTTP client gets as `verify`: False (certificate not checked),
    an SSL context trusting only the CA Certificate, or True (the default
    trusted certificates)."""
    if not verify_ssl:
        return False
    if not has_ca_cert(ca_cert):
        return True
    return load_ssl_context(ca_cert)


async def _build_client(base_url: str, max_retries: int, timeout: int,
                        verify: ssl.SSLContext | bool = True) -> AsyncOpenAI:
    # A coroutine only to run on the API client loop
    options: dict[str, Any] = {"base_url": base_url, "max_retries": max_retries, "timeout": timeout}
    if verify is not True:
        # The default HTTP client of the SDK (same limits, redirects and
        # proxies), only with another way to check the server certificate.
        # Left to the SDK otherwise.
        options["http_client"] = DefaultAsyncHttpxClient(verify=verify)
    try:
        # No api_key: the SDK reads the OPENAI_API_KEY environment variable
        client = AsyncOpenAI(**options)
    except OpenAIError:
        # Raised only when the variable is unset: keyless server
        return AsyncOpenAI(api_key=KEYLESS_API_KEY, **options)
    # Set but empty (the SDK would then send no Authorization header at all),
    # padded with whitespace or invalid: replaced, trimmed or rejected
    api_key = clean_api_key(client.api_key)
    if api_key != client.api_key:
        # Shares the HTTP client (and its connections) of the original one
        client = client.with_options(api_key=api_key)
    return client


class Client(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="OAIAPI_Client",
            display_name="OpenAI API - Client",
            category="OpenAI API",
            description="The OpenAI API client used to perform chat completions. The API key is read from the OPENAI_API_KEY environment variable at execution time and must never be stored in the workflow: ComfyUI embeds widget values into the metadata of generated images, which would leak it.",
            inputs=[
                io.String.Input(
                    id="base_url",
                    display_name="Base URL",
                    tooltip="Base URL of the OpenAI-compatible API, including its path prefix (usually /v1): the part before /chat/completions, nothing is added automatically. Examples: http://localhost:8000/v1 (vLLM, SGLang, tabbyAPI...), http://localhost:11434/v1 (ollama), https://api.openai.com/v1. Some providers use another prefix, and llama.cpp also answers without /v1.",
                    placeholder="http(s)://host[:port][/URI]",
                    default="https://api.openai.com/v1"
                ),
                io.Int.Input(
                    id="max_retries",
                    display_name="Max Retries",
                    tooltip="Max number of retries for failed requests",
                    default=2,
                    min=0,
                    max=10,
                    advanced=True,
                ),
                io.Int.Input(
                    id="timeout",
                    display_name="Timeout",
                    tooltip="Request timeout in seconds",
                    default=600,
                    min=1,
                    max=86400,  # explicit, otherwise the frontend caps it at 2048
                    advanced=True,
                ),
                # Optional: workflows and API prompts saved before these inputs
                # existed have no value for them (a missing required input is
                # a validation error), the defaults of execute apply
                io.Boolean.Input(
                    id="verify_ssl",
                    display_name="Verify SSL",
                    tooltip="Check the certificate of an https:// server (the default, keep it on). Turn it off to accept any certificate (self-signed, expired, wrong host name): the connection stays encrypted, but the server is no longer authenticated, so anything able to intercept it could read your requests and your API key. Prefer CA Certificate for a self-signed server. Has no effect on http:// URLs.",
                    default=True,
                    optional=True,
                    advanced=True,
                ),
                io.String.Input(
                    id="ca_cert",
                    display_name="CA Certificate",
                    tooltip="The certificate to trust for an https:// server: either the path of a PEM file on the machine running ComfyUI, or the PEM text itself (-----BEGIN CERTIFICATE----- ...) pasted here. Give the self-signed certificate itself, or the certificate of the (private) CA that signed it, never a private key. It replaces the default trusted certificates and must list the host name or IP address of the Base URL. Empty: use the default trusted certificates. Ignored when Verify SSL is off.",
                    placeholder="Path of a PEM file, or the PEM certificate itself",
                    multiline=True,
                    default="",
                    optional=True,
                    advanced=True,
                ),
            ],
            outputs=[
                ParamClient.Output(
                    id="client",
                    display_name="API Client",
                    tooltip="The initialized and ready to query OpenAI API client"
                )
            ],
        )

    @classmethod
    def validate_inputs(cls, base_url: str | None, verify_ssl: bool | None = True,
                        ca_cert: str | None = "") -> bool | str:
        # None when coming from another node: checked at execution
        if base_url is not None:
            error = check_base_url(base_url)
            if error is not None:
                return error
        # Not read when the certificate check is off
        if verify_ssl is not False and ca_cert is not None:
            error = check_ca_cert(ca_cert)
            if error is not None:
                return error
        return True

    @classmethod
    def fingerprint_inputs(cls, ca_cert: str | None = "", **kwargs) -> str:
        # A certificate file is read when the node runs, its content is not an
        # input: a replaced file (a renewed certificate, same path) must
        # rebuild the cached client, whose connections trust the old one.
        # A pasted PEM is an input itself.
        if not isinstance(ca_cert, str) or ca_cert.strip() == "" or is_pem_text(ca_cert):
            return ""
        try:
            stat = os.stat(clean_ca_cert_path(ca_cert))
        except OSError:
            return ""
        return f"{stat.st_mtime_ns}:{stat.st_size}"

    @classmethod
    async def execute(cls, base_url: str, max_retries: int, timeout: int,
                      verify_ssl: bool = True, ca_cert: str = "") -> io.NodeOutput:
        # A linked Base URL is not checked by validate_inputs (None there)
        if not isinstance(base_url, str) or base_url.strip() == "":
            raise ValueError(
                "Client: the Base URL is empty (or not a text). If it comes from a "
                "Load Memory node, connect its Default input for the first run.")
        # Pasted with a trailing newline or space: the SDK would send it as
        # %20 in the URL (a 404) or as a relative URL (a connection error)
        base_url = base_url.strip()
        # Linked numbers: a None timeout would mean no timeout at all
        check_input(max_retries, int, "Max Retries", "a whole number", "Client", optional=False)
        check_input(timeout, (int, float), "Timeout", "a number", "Client", optional=False)
        check_input(verify_ssl, bool, "Verify SSL", "true or false", "Client", optional=False)
        check_input(ca_cert, str, "CA Certificate", "a path or a PEM text", "Client", optional=False)
        error = check_base_url(base_url)
        if error is not None:
            raise ValueError(f"Client: {error}")
        # No API prefix at all: fine for llama.cpp, a 404/405 for most other
        # servers. Only a warning (once per URL), never added automatically.
        if has_no_api_prefix(base_url) and base_url not in _warned_base_urls:
            _warned_base_urls.add(base_url)
            logger.warning(
                "Client: the base URL %s has no API path prefix (usually /v1). "
                "This works with llama.cpp, most other servers expect .../v1", base_url)
        # One async client shared by every node connected to this one, and
        # reused across prompts while ComfyUI caches it: its connections are
        # kept alive between requests. Built and used on the API client loop
        # only (see aioloop), never on the per prompt ComfyUI one.
        try:
            verify = build_verify(verify_ssl, ca_cert)
        except ValueError as e:
            raise ValueError(f"Client: {e}") from None
        if verify is False and urlparse(base_url).scheme == "https" and base_url not in _warned_unverified_base_urls:
            _warned_unverified_base_urls.add(base_url)
            logger.warning(
                "Client: the SSL certificate of %s is not verified (Verify SSL is off): anything able to "
                "intercept the connection can read the requests, including the API key. For a self-signed "
                "server, the CA Certificate input is safer", base_url)
        client = await aioloop.submit(_build_client(base_url, max_retries, timeout, verify))
        aioloop.close_when_collected(client)
        return io.NodeOutput(client)
