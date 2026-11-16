import unittest

from hummin_assistant.errors import friendly

OPENROUTER_402 = ('402: {"message":"This request requires more credits, or fewer max_tokens. You requested up to '
                  '131072 tokens, but can only afford 37899. To increase, visit https://openrouter.ai/settings/credits",'
                  '"code":402}')
ZAI_BALANCE = '429: {"code":"1113","message":"Insufficient balance or no resource package. Please recharge."}'
MODEL = "openrouter/z-ai/glm-5.3-flash"


class FriendlyTests(unittest.TestCase):
    def test_credit_errors(self):
        self.assertTrue(friendly(OPENROUTER_402, MODEL).startswith("OpenRouter is out of credit for this request."))
        self.assertIn("can only afford 37899", friendly(OPENROUTER_402, MODEL))
        # Z.ai reports an empty balance as 429, which is not a rate limit
        self.assertTrue(friendly(ZAI_BALANCE, "zai/glm-5.3-flash").startswith("Z.ai is out of credit"))

    def test_status_codes(self):
        self.assertTrue(friendly('429: {"error":{"message":"slow down"}}', MODEL).startswith(
            "OpenRouter is rate-limiting requests."))
        self.assertIn("slow down", friendly('429: {"error":{"message":"slow down"}}', MODEL))
        self.assertTrue(friendly("401: invalid key", MODEL).startswith("OpenRouter rejected the API key"))
        self.assertEqual(friendly("503: upstream", MODEL), "OpenRouter is having trouble right now (503). Try again shortly.")
        self.assertEqual(friendly("418: teapot", "local/x"), "local error 418: teapot")

    def test_brief_and_colon_less_forms(self):
        self.assertEqual(friendly('429: {"error":{"message":"slow down"}}', MODEL, brief=True),
                         "OpenRouter is rate-limiting requests.")
        self.assertEqual(friendly("529 overloaded", MODEL),
                         "OpenRouter is having trouble right now (529). Try again shortly.")

    def test_odd_input_never_raises(self):
        self.assertEqual(friendly(None, None), "Something went wrong.")
        self.assertTrue(friendly("429: " + "[" * 100000, "zai/x").startswith("Z.ai is rate-limiting"))
        self.assertEqual(friendly({"message": "x"}, MODEL), "{'message': 'x'}")
        for body in ("[1, 2]", "3", "null", '"s"', "true", '{"error": null}', '{"error": "x"}',
                     '{"error": {"message": null}}', '{"message": 5}'):
            friendly("400: " + body, MODEL)

    def test_other_errors(self):
        self.assertTrue(friendly("TypeError: fetch failed", MODEL).startswith("Can't reach OpenRouter."))
        self.assertEqual(friendly("boom", MODEL), "boom")
        self.assertEqual(friendly("", MODEL), "Something went wrong.")
        self.assertLessEqual(len(friendly("429: " + "x" * 1000, MODEL)), 300)


if __name__ == "__main__":
    unittest.main()
