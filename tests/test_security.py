import base64
import hashlib
import hmac

from app import config, security


def sign(token: str, url: str, params: dict) -> str:
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(token.encode(), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


class TestSignature:
    def test_matches_twilio_algorithm(self, monkeypatch):
        monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "secret")

        url = "https://example.com/webhook/whatsapp"
        params = {"From": "whatsapp:+911234567890", "Body": "hello"}

        assert security.expected_signature(url, params) == sign("secret", url, params)

    def test_param_order_does_not_matter(self, monkeypatch):
        monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "secret")
        url = "https://example.com/x"

        a = security.expected_signature(url, {"a": "1", "b": "2"})
        b = security.expected_signature(url, {"b": "2", "a": "1"})

        assert a == b

    def test_tampered_body_changes_signature(self, monkeypatch):
        monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "secret")
        url = "https://example.com/x"

        original = security.expected_signature(url, {"Body": "call mum"})
        tampered = security.expected_signature(url, {"Body": "call someone else"})

        assert original != tampered


class TestAllowlist:
    def test_empty_allowlist_permits_everyone(self, monkeypatch):
        monkeypatch.setattr(config, "ALLOWED_WHATSAPP_NUMBERS", set())
        assert security.is_allowed_sender("whatsapp:+910000000000")

    def test_allows_listed_number_with_prefix(self, monkeypatch):
        monkeypatch.setattr(config, "ALLOWED_WHATSAPP_NUMBERS", {"+918837557003"})
        assert security.is_allowed_sender("whatsapp:+918837557003")

    def test_blocks_unlisted_number(self, monkeypatch):
        monkeypatch.setattr(config, "ALLOWED_WHATSAPP_NUMBERS", {"+918837557003"})
        assert not security.is_allowed_sender("whatsapp:+910000000000")
