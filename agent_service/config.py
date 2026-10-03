"""Configuration for the Python Agent Service.

The service reads configuration from two places, in this order:

1. the application's ``config.json`` (the Go tool's settings file), under its
   ``agent`` section -- this is what the Agent panel on the frontend writes;
2. environment variables, which stay supported for headless runs and for
   ``.env``-style deployments.

Go continues to own the provider credentials for the digital-human capability;
the model fields here are the small runtime seam the Agent roles need.  Both
``config.json`` and the environment are re-read when the file changes, so a
model key saved in the browser takes effect without restarting this process.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import tempfile
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ModelSettings(BaseModel):
    """Configuration for one logical model role."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    provider: str
    model: str
    api_base_url: str = ""
    api_key: SecretStr | None = None
    temperature: float = 0.0
    max_retries: int = 2
    #: Whether this role may keep the provider's thinking mode on.  Off means
    #: the faster answer; on means the model reasons first and the UI can show
    #: that reasoning.  Structured-output roles ignore it: thinking mode rejects
    #: the forced tool choice they depend on.
    thinking: bool = False

    def safe_dict(self) -> dict[str, object]:
        """Return model settings without the provider secret."""

        return {
            "provider": self.provider,
            "model": self.model,
            "api_base_url": self.api_base_url,
            "temperature": self.temperature,
            "max_retries": self.max_retries,
            "thinking": self.thinking,
        }


class AgentSettings(BaseModel):
    """Validated runtime settings shared by the HTTP application.

    Secret values are represented by :class:`SecretStr`, so accidental model
    or debug output cannot expose an API key.  Application code that genuinely
    needs the value must explicitly call ``get_secret_value()`` later.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    go_capability_base_url: str = Field(default="http://127.0.0.1:8909")

    # Model provider seam for later tasks.  No model client is created here.
    model_provider: str = Field(default="openai_compatible")
    model_api_base_url: str = Field(default="")
    model_name: str = Field(default="")
    model_api_key: SecretStr | None = Field(default=None)

    # Logical roles keep provider configuration out of Agent implementations.
    supervisor: ModelSettings = Field(
        default_factory=lambda: ModelSettings(
            provider="openai_compatible",
            model="",
        )
    )
    script: ModelSettings = Field(
        default_factory=lambda: ModelSettings(
            provider="deepseek",
            model="deepseek-chat",
        )
    )

    # Business DB path is configured now; database creation belongs to Task 1.3.
    # The launcher overrides this. The fallback is outside the source tree so
    # one-shot module execution cannot write into the copied legacy assets.
    db_path: Path = Field(
        default_factory=lambda: Path(tempfile.gettempdir())
        / "koubo-video-tool-agent-upgrade"
        / "agent.sqlite"
    )

    log_level: str = Field(default="INFO")
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8090, ge=1, le=65535)

    # Remote tracing is opt-in: without a key the service runs untraced and
    # sends nothing anywhere, which is what a local checkout should do.
    langsmith_api_key: SecretStr | None = Field(default=None)
    langsmith_project: str = Field(default="koubo-agent-upgrade")

    @property
    def langsmith_tracing(self) -> bool:
        return self.langsmith_api_key is not None

    def safe_dict(self) -> dict[str, object]:
        """Return non-secret settings suitable for diagnostics or tests."""

        return {
            "go_capability_base_url": self.go_capability_base_url,
            "model_provider": self.model_provider,
            "model_api_base_url": self.model_api_base_url,
            "model_name": self.model_name,
            "supervisor": self.supervisor.safe_dict(),
            "script": self.script.safe_dict(),
            "db_path": str(self.db_path),
            "log_level": self.log_level,
            "host": self.host,
            "port": self.port,
        }


def _env(environ: Mapping[str, str], name: str, default: str = "") -> str:
    """Read a string environment variable, treating whitespace as empty."""

    value = environ.get(name, default).strip()
    return value if value else default


#: The Go settings file, if the application is configured through the UI.  The
#: launcher may point elsewhere; the default matches where the upgrade
#: workspace keeps the Go tool's data directory.
DEFAULT_CONFIG_PATH = (
    Path(tempfile.gettempdir())
    / "koubo-video-tool-agent-upgrade"
    / "go"
    / "config.json"
)

#: A settings file is re-read only when it changed, so a turn does not stat and
#: parse it for every tool call.
_CONFIG_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def config_file_path(environ: Mapping[str, str] | None = None) -> Path:
    """Return the settings file path the Agent panel would have written."""

    values = os.environ if environ is None else environ
    configured = _env(values, "KOUBO_CONFIG_PATH")
    if configured:
        return Path(configured)
    data_dir = _env(values, "KOUBO_DATA_DIR")
    if data_dir:
        return Path(data_dir) / "config.json"
    return DEFAULT_CONFIG_PATH


def load_app_config(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Read the application settings file, cached by modification time.

    A missing, unreadable, or malformed file yields an empty mapping: an
    installation configured purely through the environment is normal, and a
    half-written file must not stop the service from starting.
    """

    resolved = Path(path) if path is not None else config_file_path(environ)
    key = str(resolved)
    try:
        mtime = resolved.stat().st_mtime
    except OSError:
        _CONFIG_CACHE.pop(key, None)
        return {}

    cached = _CONFIG_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    try:
        loaded = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    _CONFIG_CACHE[key] = (mtime, loaded)
    return loaded


def _hostname_key() -> bytes:
    """Derive the settings file's encryption key exactly as Go does."""

    hostname = socket.gethostname()
    return hashlib.sha256(f"{hostname}:koubo-video-tool:key-v1".encode("utf-8")).digest()


def _decrypt_secret(value: str) -> str:
    """Decrypt one ``ENC:`` settings value, or return it unchanged.

    Go writes ``ENC:`` + base64(nonce || ciphertext || tag) with AES-256-GCM.
    Values without the prefix are legacy plaintext and are used as-is, which
    mirrors the Go side and keeps an un-migrated settings file working.
    """

    if not value:
        return ""
    if not value.startswith("ENC:"):
        return value
    try:
        from Crypto.Cipher import AES

        raw = base64.b64decode(value[4:])
        nonce, body = raw[:12], raw[12:]
        if len(body) < 16:
            return ""
        # Go's Seal appends the 16-byte tag to the ciphertext; pycryptodome
        # wants the two separately.
        ciphertext, tag = body[:-16], body[-16:]
        cipher = AES.new(_hostname_key(), AES.MODE_GCM, nonce=nonce)
        plaintext = cipher.decrypt_and_verify(ciphertext, tag)
    except Exception:  # noqa: BLE001 - an unreadable secret is reported as absent
        return ""
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _agent_model_from_file(
    entry: Any,
) -> tuple[str, str, str, str]:
    """Read one ``agent`` model entry as (provider, model, base_url, api_key)."""

    if not isinstance(entry, Mapping):
        return "", "", "", ""
    provider = str(entry.get("provider") or "").strip()
    model = str(entry.get("model") or "").strip()
    base_url = str(entry.get("api_base_url") or "").strip().rstrip("/")
    api_key = _decrypt_secret(str(entry.get("api_key") or "").strip())
    return provider, model, base_url, api_key


def _shared_model_from_file(
    section: Any,
) -> tuple[str, str, str, str, bool]:
    """Read the shared ``llm`` section as provider, model, url, key, thinking.

    This is the section the settings page owns, and it is the only place a user
    is asked for a model, so both Agent roles take their defaults from it.  The
    section carries no provider, so one is inferred from the URL: DeepSeek must
    be recognised because its thinking mode rejects the forced tool choice that
    the structured-output agents bind.
    """

    if not isinstance(section, Mapping):
        return "", "", "", "", False
    base_url = str(section.get("api_url") or "").strip().rstrip("/")
    model = str(section.get("model") or "").strip()
    api_key = _decrypt_secret(str(section.get("api_key") or "").strip())
    provider = ""
    if base_url:
        provider = "deepseek" if "deepseek" in base_url.lower() else "openai_compatible"
    thinking = bool(section.get("thinking"))
    return provider, model, base_url, api_key, thinking


def _prefer(*values: str) -> str:
    """Return the first non-blank value, matching the settings precedence."""

    for value in values:
        if value and value.strip():
            return value.strip()
    return ""


def _secret(value: str) -> SecretStr | None:
    return SecretStr(value) if value else None


def _float_env(environ: Mapping[str, str], name: str, default: str) -> float:
    value = _env(environ, name, default)
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _int_env(environ: Mapping[str, str], name: str, default: str) -> int:
    value = _env(environ, name, default)
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def load_settings(environ: Mapping[str, str] | None = None) -> AgentSettings:
    """Load settings from the application settings file, then the environment.

    An explicit mapping makes the environment half deterministic in tests while
    the default keeps normal process startup simple.  The settings file is only
    consulted when no explicit mapping was passed, so a test can never pick up
    whatever the machine happens to have saved in the UI.
    """

    values = os.environ if environ is None else environ
    file_config = load_app_config(environ=environ) if environ is None else {}
    agent_section = file_config.get("agent")
    if not isinstance(agent_section, Mapping):
        agent_section = {}
    file_supervisor = _agent_model_from_file(agent_section.get("supervisor"))
    file_script = _agent_model_from_file(agent_section.get("script"))
    # The settings page's shared model section comes first for both roles: it is
    # the one place the UI asks for a model, and keeping one source is what stops
    # the Agent page and the settings page from disagreeing.
    file_llm = _shared_model_from_file(file_config.get("llm"))

    raw_key = _env(values, "MODEL_API_KEY")

    base_provider = _env(values, "MODEL_PROVIDER", "openai_compatible")
    base_model = _env(values, "MODEL_NAME")
    base_url = _env(values, "MODEL_API_BASE_URL").rstrip("/")

    supervisor_provider = _prefer(
        file_llm[0],
        file_supervisor[0],
        _env(values, "SUPERVISOR_MODEL_PROVIDER", base_provider),
    )
    supervisor_model = _prefer(
        file_llm[1],
        file_supervisor[1],
        _env(values, "SUPERVISOR_MODEL", base_model),
    )
    supervisor_url = _prefer(
        file_llm[2],
        file_supervisor[2],
        _env(values, "SUPERVISOR_MODEL_API_BASE_URL", base_url).rstrip("/"),
    )
    supervisor_key = _prefer(
        file_llm[3],
        file_supervisor[3],
        _env(values, "SUPERVISOR_MODEL_API_KEY", raw_key),
    )

    script_provider = _prefer(
        file_llm[0],
        file_script[0],
        _env(values, "SCRIPT_MODEL_PROVIDER", "deepseek"),
    )
    script_model = _prefer(
        file_llm[1],
        file_script[1],
        _env(values, "SCRIPT_MODEL", "deepseek-chat"),
    )
    script_url = _prefer(
        file_llm[2],
        file_script[2],
        _env(
            values,
            "SCRIPT_MODEL_API_BASE_URL",
            _env(values, "DEEPSEEK_API_BASE"),
        ).rstrip("/"),
    )
    script_key = _prefer(
        file_llm[3],
        file_script[3],
        _env(
            values,
            "SCRIPT_MODEL_API_KEY",
            _env(values, "DEEPSEEK_API_KEY"),
        ),
    )

    return AgentSettings(
        go_capability_base_url=_env(
            values, "GO_CAPABILITY_BASE_URL", "http://127.0.0.1:8909"
        ).rstrip("/"),
        model_provider=base_provider,
        model_api_base_url=base_url,
        model_name=base_model,
        model_api_key=_secret(raw_key),
        supervisor=ModelSettings(
            provider=supervisor_provider,
            model=supervisor_model,
            api_base_url=supervisor_url,
            api_key=_secret(supervisor_key),
            temperature=_float_env(values, "SUPERVISOR_TEMPERATURE", "0"),
            max_retries=_int_env(values, "SUPERVISOR_MAX_RETRIES", "2"),
            # Off by default: thinking costs 10-20s per turn on this workload.
            # The settings page owns the switch.
            thinking=file_llm[4],
        ),
        script=ModelSettings(
            provider=script_provider,
            model=script_model,
            api_base_url=script_url,
            api_key=_secret(script_key),
            temperature=_float_env(values, "SCRIPT_TEMPERATURE", "0.7"),
            max_retries=_int_env(values, "SCRIPT_MAX_RETRIES", "2"),
        ),
        langsmith_api_key=_secret(_env(values, "LANGSMITH_API_KEY")),
        langsmith_project=_env(
            values, "LANGSMITH_PROJECT", "koubo-agent-upgrade"
        ),
        db_path=Path(
            _env(
                values,
                "DB_PATH",
                str(
                    Path(tempfile.gettempdir())
                    / "koubo-video-tool-agent-upgrade"
                    / "agent.sqlite"
                ),
            )
        ),
        log_level=_env(values, "LOG_LEVEL", "INFO").upper(),
        host=_env(values, "AGENT_HOST", "127.0.0.1"),
        port=int(_env(values, "AGENT_PORT", "8090")),
    )
