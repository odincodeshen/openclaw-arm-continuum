import json
import tempfile
import unittest
from pathlib import Path

from openclaw_runtime.model_catalog import ModelRegistry, ModelSpec, load_model_registry
from openclaw_runtime.model_client_factory import ModelClientFactory
from support import build_settings


class ModelCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "models.json"

    def write(self, models: dict) -> None:
        self.path.write_text(json.dumps({"models": models}), encoding="utf-8")

    def test_missing_catalog_uses_legacy_local_default(self) -> None:
        settings = build_settings(
            model_catalog_path=self.path,
            vllm_base_url="http://legacy:8000/v1",
            vllm_model="legacy-model",
            request_timeout=17,
        )
        model = load_model_registry(settings).resolve("local_default")
        self.assertEqual(model.base_url, "http://legacy:8000/v1")
        self.assertEqual(model.model, "legacy-model")
        self.assertEqual(model.timeout, 17)

    def test_loads_catalog_and_resolves_fallback(self) -> None:
        self.write(
            {
                "local_default": {
                    "base_url": "http://general/v1/",
                    "model": "general",
                    "roles": ["general"],
                },
                "local_coder": {
                    "base_url": "http://coder/v1",
                    "model": "coder",
                    "roles": ["code_review"],
                    "timeout": 90,
                    "fallback": "local_default",
                },
            }
        )
        registry = load_model_registry(build_settings(model_catalog_path=self.path))
        coder = registry.resolve("local_coder")
        self.assertEqual(coder.base_url, "http://coder/v1")
        self.assertEqual(coder.timeout, 90)
        self.assertEqual(registry.fallback_for("local_coder").model_id, "local_default")
        self.assertEqual([item.model_id for item in registry.for_role("code_review")], ["local_coder"])

    def test_rejects_invalid_document(self) -> None:
        self.path.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "top-level models"):
            load_model_registry(build_settings(model_catalog_path=self.path))

    def test_rejects_unknown_role(self) -> None:
        self.write(
            {
                "local_default": {
                    "base_url": "http://general/v1",
                    "model": "general",
                    "roles": ["magic"],
                }
            }
        )
        with self.assertRaisesRegex(ValueError, "unknown roles"):
            load_model_registry(build_settings(model_catalog_path=self.path))

    def test_rejects_unknown_fallback(self) -> None:
        self.write(
            {
                "local_default": {
                    "base_url": "http://general/v1",
                    "model": "general",
                    "roles": ["general"],
                    "fallback": "missing",
                }
            }
        )
        with self.assertRaisesRegex(ValueError, "unknown fallback"):
            load_model_registry(build_settings(model_catalog_path=self.path))

    def test_rejects_duplicate_model_ids(self) -> None:
        model = ModelSpec("same", "http://one/v1", "one", ("general",), 10)
        with self.assertRaisesRegex(ValueError, "duplicate model ID"):
            ModelRegistry([model, model])

    def test_disabled_model_cannot_be_resolved(self) -> None:
        registry = ModelRegistry([ModelSpec("disabled", "http://one/v1", "one", ("general",), 10, False)])
        with self.assertRaisesRegex(LookupError, "disabled"):
            registry.resolve("disabled")


class ModelClientFactoryTest(unittest.TestCase):
    def test_returns_cached_client_with_resolved_endpoint(self) -> None:
        spec = ModelSpec("local_coder", "http://coder/v1", "coder", ("code_review",), 44)
        registry = ModelRegistry([spec])
        factory = ModelClientFactory(build_settings(), registry)
        first = factory.get("local_coder")
        second = factory.get("local_coder")
        self.assertIs(first, second)
        self.assertEqual(first.endpoint_id, "local_coder")
        self.assertEqual(first.base_url, "http://coder/v1")
        self.assertEqual(first.model, "coder")
        self.assertEqual(first.timeout, 44)


if __name__ == "__main__":
    unittest.main()
