import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openclaw_runtime.config import Settings


ALLOWED_ROLES = {
    "general",
    "classification",
    "vision",
    "code_review",
    "architecture_review",
    "synthesis",
}


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    base_url: str
    model: str
    roles: tuple[str, ...]
    timeout: int
    enabled: bool = True
    fallback: str | None = None

    @classmethod
    def from_dict(cls, model_id: str, value: dict[str, Any], *, default_timeout: int) -> "ModelSpec":
        if not isinstance(value, dict):
            raise ValueError(f"model {model_id!r} must be an object")
        base_url = str(value.get("base_url", "")).strip().rstrip("/")
        model = str(value.get("model", "")).strip()
        raw_roles = value.get("roles", [])
        if not model_id.strip():
            raise ValueError("model ID must not be empty")
        if not base_url:
            raise ValueError(f"model {model_id!r} requires base_url")
        if not model:
            raise ValueError(f"model {model_id!r} requires model")
        if not isinstance(raw_roles, list) or not raw_roles:
            raise ValueError(f"model {model_id!r} requires at least one role")
        roles = tuple(str(role).strip() for role in raw_roles)
        unknown_roles = sorted(set(roles) - ALLOWED_ROLES)
        if unknown_roles:
            raise ValueError(f"model {model_id!r} has unknown roles: {', '.join(unknown_roles)}")
        timeout = value.get("timeout", default_timeout)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise ValueError(f"model {model_id!r} timeout must be a positive integer")
        enabled = value.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"model {model_id!r} enabled must be a boolean")
        fallback = value.get("fallback")
        if fallback is not None:
            fallback = str(fallback).strip() or None
        return cls(model_id.strip(), base_url, model, roles, timeout, enabled, fallback)


class ModelRegistry:
    def __init__(self, models: list[ModelSpec]) -> None:
        self._models: dict[str, ModelSpec] = {}
        for model in models:
            if model.model_id in self._models:
                raise ValueError(f"duplicate model ID: {model.model_id}")
            self._models[model.model_id] = model
        if not self._models:
            raise ValueError("model catalog must contain at least one model")
        for model in self._models.values():
            if model.fallback and model.fallback not in self._models:
                raise ValueError(f"model {model.model_id!r} references unknown fallback {model.fallback!r}")
            if model.fallback == model.model_id:
                raise ValueError(f"model {model.model_id!r} cannot fall back to itself")

    @property
    def models(self) -> list[ModelSpec]:
        return list(self._models.values())

    def resolve(self, model_policy: str, *, allow_disabled: bool = False) -> ModelSpec:
        try:
            model = self._models[model_policy]
        except KeyError as exc:
            raise LookupError(f"unknown model policy: {model_policy}") from exc
        if not model.enabled and not allow_disabled:
            raise LookupError(f"model policy is disabled: {model_policy}")
        return model

    def fallback_for(self, model_policy: str) -> ModelSpec | None:
        model = self.resolve(model_policy, allow_disabled=True)
        if not model.fallback:
            return None
        fallback = self.resolve(model.fallback)
        return fallback

    def for_role(self, role: str) -> list[ModelSpec]:
        return [model for model in self._models.values() if model.enabled and role in model.roles]


def default_model_spec(settings: Settings) -> ModelSpec:
    return ModelSpec(
        model_id="local_default",
        base_url=settings.vllm_base_url,
        model=settings.vllm_model,
        roles=("general",),
        timeout=settings.request_timeout,
    )


def default_vision_spec(settings: Settings) -> ModelSpec:
    """Synthesised 'vision' model from OPENCLAW_VLM_* so image features work
    without a full catalog. Defaults back to the main text model."""
    return ModelSpec(
        model_id="vision",
        base_url=settings.vlm_base_url,
        model=settings.vlm_model,
        roles=("vision",),
        timeout=settings.request_timeout,
        fallback="local_default",
    )


def load_model_registry(settings: Settings) -> ModelRegistry:
    path = settings.model_catalog_path
    if not path.exists():
        return ModelRegistry([default_model_spec(settings), default_vision_spec(settings)])
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid model catalog JSON at {path}: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("models"), dict):
        raise ValueError("model catalog requires a top-level models object")
    models = [
        ModelSpec.from_dict(model_id, value, default_timeout=settings.request_timeout)
        for model_id, value in document["models"].items()
    ]
    have_vision = any("vision" in spec.roles for spec in models)
    if not have_vision and not any(spec.model_id == "vision" for spec in models):
        models.append(default_vision_spec(settings))
    registry = ModelRegistry(models)
    registry.resolve("local_default")
    return registry
