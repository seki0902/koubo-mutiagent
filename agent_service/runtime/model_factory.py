"""Single model construction entry point for all Agent roles."""

from __future__ import annotations

import importlib
from typing import Any

from agent_service.config import AgentSettings, ModelSettings, load_settings


class ModelFactoryError(RuntimeError):
    """Raised when a logical model role cannot be constructed."""


class ModelFactory:
    """Create configured LangChain chat models without hardcoded Agent models."""

    def __init__(self, settings: AgentSettings):
        self.settings = settings

    def get_model(self, role: str, *, disable_thinking: bool = False) -> Any:
        """Return the chat model configured for ``supervisor`` or ``script``.

        ``disable_thinking`` belongs to the agents that ask the provider for a
        forced tool call, which is how their structured-output schema is bound:
        DeepSeek's thinking mode rejects every forced tool choice, so those
        calls have to turn thinking off.  The Supervisor binds its tools with
        ``auto``, so it keeps thinking and the routing that comes with it.
        """

        if role not in {"supervisor", "script"}:
            raise ModelFactoryError(
                f"unsupported model role {role!r}; expected 'supervisor' or 'script'"
            )

        spec = getattr(self.settings, role)
        self._validate_spec(role, spec)
        provider = self._normalize_provider(spec.provider)

        if provider == "openai":
            return self._create_openai(role, spec)
        if provider == "deepseek":
            # A role that asks for the provider's thinking mode keeps it; every
            # other deepseek call turns it off, because the structured-output
            # roles bind a forced tool choice that thinking mode rejects.
            return self._create_deepseek(
                role, spec, disable_thinking=disable_thinking or not spec.thinking
            )

        raise ModelFactoryError(
            f"{role} model provider {spec.provider!r} is unsupported; "
            "supported providers are 'openai_compatible' and 'deepseek'"
        )

    @staticmethod
    def _normalize_provider(provider: str) -> str:
        normalized = provider.strip().lower().replace("-", "_")
        if normalized in {"openai", "openai_compatible", "openai_compat"}:
            return "openai"
        if normalized in {"deepseek", "deep_seek"}:
            return "deepseek"
        return normalized

    @staticmethod
    def _validate_spec(role: str, spec: ModelSettings) -> None:
        if not spec.provider.strip():
            raise ModelFactoryError(
                f"{role} model provider is missing; set {role.upper()}_MODEL_PROVIDER"
            )
        if not spec.model.strip():
            raise ModelFactoryError(
                f"{role} model name is missing; set {role.upper()}_MODEL"
            )
        if spec.api_key is None or not spec.api_key.get_secret_value().strip():
            raise ModelFactoryError(
                f"{role} model API key is missing; set "
                f"{role.upper()}_MODEL_API_KEY or MODEL_API_KEY"
            )

    @staticmethod
    def _load_constructor(
        role: str, provider: str, module_name: str, class_name: str
    ) -> Any:
        try:
            module = importlib.import_module(module_name)
            return getattr(module, class_name)
        except (ImportError, AttributeError) as exc:
            package = module_name.replace("_", "-")
            raise ModelFactoryError(
                f"{role} model provider '{provider}' requires optional package "
                f"'{package}'; install it before starting the Agent Service"
            ) from exc

    @classmethod
    def _create_openai(cls, role: str, spec: ModelSettings) -> Any:
        constructor = cls._load_constructor(
            role, "openai_compatible", "langchain_openai", "ChatOpenAI"
        )
        kwargs: dict[str, Any] = {
            "model": spec.model,
            "api_key": spec.api_key.get_secret_value(),
            "temperature": spec.temperature,
            "max_retries": spec.max_retries,
        }
        if spec.api_base_url:
            kwargs["base_url"] = spec.api_base_url
        return constructor(**kwargs)

    @classmethod
    def _create_deepseek(
        cls, role: str, spec: ModelSettings, *, disable_thinking: bool = False
    ) -> Any:
        constructor = cls._load_constructor(
            role, "deepseek", "langchain_deepseek", "ChatDeepSeek"
        )
        kwargs: dict[str, Any] = {
            "model": spec.model,
            "api_key": spec.api_key.get_secret_value(),
            "temperature": spec.temperature,
            "max_retries": spec.max_retries,
        }
        if disable_thinking:
            # DeepSeek's thinking mode rejects every forced tool choice:
            #   400 Thinking mode does not support this tool_choice
            # Only the body field turns it off; ``model_kwargs`` would reach the
            # SDK call as an unknown argument instead.
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        if spec.api_base_url:
            kwargs["api_base"] = spec.api_base_url
        return constructor(**kwargs)


def get_model(
    role: str,
    settings: AgentSettings | None = None,
    *,
    disable_thinking: bool = False,
) -> Any:
    """Construct the configured model for a logical role."""

    return ModelFactory(settings or load_settings()).get_model(
        role, disable_thinking=disable_thinking
    )
