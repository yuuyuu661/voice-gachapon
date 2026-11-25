import ast
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "server.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def function_source(name: str) -> str:
    node = next(n for n in TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    return ast.get_source_segment(SOURCE, node) or ""


class VoiceGachaSecurityTests(unittest.TestCase):
    def test_no_currency_tables_are_touched(self):
        lowered = SOURCE.lower()
        self.assertNotIn("casino_transactions", lowered)
        self.assertNotIn("casino_bets", lowered)
        self.assertNotIn("casino_balance", lowered)
        self.assertNotIn("update users", lowered)
        self.assertNotIn("casino_sessions", lowered)

    def test_standalone_sessions_are_hashed(self):
        self.assertIn("voice_gacha_sessions", SOURCE)
        self.assertIn("token_hash TEXT PRIMARY KEY", SOURCE)
        self.assertIn("hashlib.sha256(token.encode", SOURCE)

    def test_serial_codes_are_hashed_and_single_use(self):
        self.assertIn("hashlib.sha256", SOURCE)
        self.assertIn("code_hash TEXT PRIMARY KEY", SOURCE)
        self.assertIn("FOR UPDATE", function_source("redeem"))
        self.assertNotIn("code_plain", SOURCE)

    def test_spin_has_server_side_credit_lock_and_idempotency(self):
        spin = function_source("spin")
        self.assertIn("FOR UPDATE", spin)
        self.assertIn("spin_credits=spin_credits-1", spin)
        self.assertIn("request_id", spin)
        self.assertIn("Request ID is already in use", spin)

    def test_media_requires_collection_ownership(self):
        media = function_source("prize_media")
        self.assertIn("voice_gacha_collections", media)
        self.assertIn("owned.guild_id=$1", media)
        self.assertIn("owned.user_id=$2", media)
        self.assertIn("prize.id=$3", media)

    def test_delete_preserves_obtained_prizes(self):
        delete = function_source("delete_prize")
        self.assertIn("voice_gacha_pulls", delete)
        self.assertIn("pull_count", delete)
        self.assertIn("FOR UPDATE", delete)

    def test_only_expected_rarities_and_gacha_types(self):
        self.assertIn('Literal["R", "SSR"]', SOURCE)
        self.assertIn('Literal["boys", "girls"]', SOURCE)

    def test_configured_rates_are_persistent_and_used_by_spin(self):
        self.assertIn("voice_gacha_settings", SOURCE)
        spin = function_source("spin")
        self.assertIn("ssr_rate_bps", spin)
        self.assertIn("choose_rarity", spin)
        update = function_source("update_rates")
        self.assertIn("req.r_percent + req.ssr_percent", update)
        self.assertIn("ON CONFLICT(gacha_type) DO UPDATE", update)

    def test_current_rates_have_a_public_read_only_endpoint(self):
        public_rates = function_source("public_rates")
        self.assertIn("voice_gacha_settings", public_rates)
        self.assertNotIn("UPDATE", public_rates)
        self.assertNotIn("INSERT", public_rates)


if __name__ == "__main__":
    unittest.main()
