import unittest

from openclaw_runtime.intent_router import IntentRouter


class FakeClient:
    def __init__(self, response="", raises=None):
        self.response = response
        self.raises = raises
        self.prompts = []

    def chat_json(self, prompt, schema, *, schema_name, max_tokens=None):
        self.prompts.append(prompt)
        if self.raises:
            raise self.raises
        return self.response


class FakeFactory:
    def __init__(self, client, has_router=True):
        self.client = client
        self.has_router = has_router

    def get(self, policy):
        if policy == "local_router" and not self.has_router:
            raise LookupError("no local_router")
        return self.client


class IntentRouterBuildTest(unittest.TestCase):
    def test_disabled_returns_none(self) -> None:
        self.assertIsNone(IntentRouter.build(FakeFactory(FakeClient()), enabled=False, min_confidence=0.6))

    def test_no_factory_returns_none(self) -> None:
        self.assertIsNone(IntentRouter.build(None, enabled=True, min_confidence=0.6))

    def test_no_router_model_returns_none(self) -> None:
        factory = FakeFactory(FakeClient(), has_router=False)
        self.assertIsNone(IntentRouter.build(factory, enabled=True, min_confidence=0.6))

    def test_built_when_router_model_present(self) -> None:
        factory = FakeFactory(FakeClient())
        self.assertIsInstance(IntentRouter.build(factory, enabled=True, min_confidence=0.6), IntentRouter)


class IntentRouterClassifyTest(unittest.TestCase):
    def _router(self, response="", raises=None, min_confidence=0.6):
        return IntentRouter(FakeFactory(FakeClient(response, raises)), min_confidence=min_confidence)

    def test_high_confidence_intent(self) -> None:
        r = self._router('{"intent":"knowledge_base","confidence":0.9,"reason":"asks about saved notes"}')
        self.assertEqual(r.classify("我存的筆記"), "knowledge_base")

    def test_low_confidence_defers(self) -> None:
        r = self._router('{"intent":"web_search","confidence":0.3}')
        self.assertIsNone(r.classify("something"))

    def test_fenced_json_is_tolerated(self) -> None:
        r = self._router('```json\n{"intent":"web_search","confidence":0.8}\n```')
        self.assertEqual(r.classify("latest news"), "web_search")

    def test_bad_json_defers(self) -> None:
        self.assertIsNone(self._router("not json at all").classify("x"))

    def test_model_error_defers(self) -> None:
        self.assertIsNone(self._router(raises=RuntimeError("endpoint down")).classify("x"))

    def test_unknown_intent_defers(self) -> None:
        r = self._router('{"intent":"cook_dinner","confidence":0.99}')
        self.assertIsNone(r.classify("x"))


if __name__ == "__main__":
    unittest.main()
