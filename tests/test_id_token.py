"""/request-access 鉴权:验证接入应用转交的 Casdoor id_token。
token 形状按 2026-11-16 对活 Casdoor 实测(casdoor-findings §7):RS256、kid=证书名、
aud=[client_id]、azp=client_id、sub=飞书 open_id、tokenType=id-token。"""
import asyncio, time
import httpx, jwt
import pytest
from even_auth_gov import id_token
from tests._jwt import KID, CID_B, KEY as _KEY, OTHER_KEY as _OTHER_KEY, APP_CLIENT_IDS, jwks as _jwks, token as _token


def _client(jwks_responses, seen=None):
    """按顺序返回 jwks_responses 里的 JWKS(用完重复最后一个);seen 记录 JWKS 拉取次数。"""
    calls = {"n": 0}
    def handler(req):
        if req.url.path == "/.well-known/jwks":
            calls["n"] += 1
            if seen is not None:
                seen["jwks_fetches"] = calls["n"]
            body = jwks_responses[min(calls["n"], len(jwks_responses)) - 1]
            if isinstance(body, httpx.Response):
                return body
            return httpx.Response(200, json=body)
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://casdoor")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("APP_CLIENT_IDS", APP_CLIENT_IDS)
    monkeypatch.delenv("CASDOOR_ISSUER", raising=False)
    id_token.clear_cache()
    yield
    id_token.clear_cache()


def _verify(token, client=None):
    return asyncio.run(id_token.verify(client or _client([_jwks()]), token))


def test_valid_token_yields_identity_and_app_from_signed_claims():
    info = _verify(_token())
    assert info == {"open_id": "ou_alice", "app": "app-b", "name": "Alice", "email": "alice@e.com"}


def test_app_is_derived_from_aud_not_from_anything_the_caller_says():
    info = _verify(_token(aud=["cid-app-c"], azp="cid-app-c"))
    assert info["app"] == "app-c"


def test_aud_as_plain_string_is_accepted():
    assert _verify(_token(aud=CID_B))["app"] == "app-b"


def test_name_falls_back_to_name_claim_when_no_display_name():
    assert _verify(_token(displayName=None))["name"] == "randname"


def test_expired_token_rejected():
    now = int(time.time())
    with pytest.raises(id_token.InvalidToken):
        _verify(_token(iat=now - 7200, nbf=now - 7200, exp=now - 3600))


def test_token_signed_by_another_key_rejected():
    with pytest.raises(id_token.InvalidToken):
        _verify(_token(key=_OTHER_KEY))


def test_symmetric_key_in_jwks_is_never_trusted():
    """JWKS 是公开的:里面要是混进对称密钥(oct/HS256),任何人都能用它签 token。只认非对称算法。"""
    secret = b"s" * 32
    jwks = {"keys": [{"kty": "oct", "kid": KID, "alg": "HS256",
                      "k": jwt.utils.base64url_encode(secret).decode()}]}
    now = int(time.time())
    forged = jwt.encode({"sub": "ou_evil", "aud": [CID_B], "iat": now, "exp": now + 3600,
                         "tokenType": "id-token"}, secret, algorithm="HS256", headers={"kid": KID})
    with pytest.raises(id_token.InvalidToken):
        _verify(forged, client=_client([jwks]))


def test_alg_mismatch_with_jwks_key_rejected():
    """header 声明 HS256 但 JWKS 里是 RSA 公钥:只能按 JWKS 密钥的算法验,不跟 header 走。"""
    now = int(time.time())
    forged = jwt.encode({"sub": "ou_evil", "aud": [CID_B], "iat": now, "exp": now + 3600,
                         "tokenType": "id-token"}, b"x" * 32, algorithm="HS256", headers={"kid": KID})
    with pytest.raises(id_token.InvalidToken):
        _verify(forged)


def test_garbage_rejected():
    with pytest.raises(id_token.InvalidToken):
        _verify("not-a-jwt")


def test_missing_sub_rejected():
    with pytest.raises(id_token.InvalidToken):
        _verify(_token(sub=None))


def test_refresh_and_access_tokens_rejected_only_id_token_accepted():
    with pytest.raises(id_token.InvalidToken):
        _verify(_token(tokenType="refresh-token"))
    with pytest.raises(id_token.InvalidToken):
        _verify(_token(tokenType="access-token"))


def test_unregistered_client_id_is_unknown_app():
    with pytest.raises(id_token.UnknownApp):
        _verify(_token(aud=["cid-unknown"], azp="cid-unknown"))


def test_no_registered_apps_fails_closed(monkeypatch):
    monkeypatch.delenv("APP_CLIENT_IDS", raising=False)
    with pytest.raises(id_token.UnknownApp):
        _verify(_token())


def test_issuer_checked_when_configured(monkeypatch):
    monkeypatch.setenv("CASDOOR_ISSUER", "https://auth.example.com")
    assert _verify(_token())["open_id"] == "ou_alice"
    id_token.clear_cache()
    with pytest.raises(id_token.InvalidToken):
        _verify(_token(iss="https://evil.example.com"))


def test_jwks_unreachable_is_unavailable_not_invalid():
    """拉不到公钥 ≠ token 无效:返回 503 让接入方重试,而不是告诉它"token 错了"。"""
    with pytest.raises(id_token.JWKSUnavailable):
        _verify(_token(), client=_client([httpx.Response(502)]))


def test_jwks_cached_between_calls():
    seen = {}
    client = _client([_jwks()], seen)
    async def run():
        await id_token.verify(client, _token())
        await id_token.verify(client, _token())
    asyncio.run(run())
    assert seen["jwks_fetches"] == 1


def test_unknown_kid_triggers_one_refetch_for_key_rotation():
    """Casdoor 换证书后 kid 变了:缓存里没有 → 强制重拉一次 JWKS。"""
    seen = {}
    client = _client([_jwks(), _jwks(key=_OTHER_KEY, kid="cert-new")], seen)
    async def run():
        await id_token.verify(client, _token())
        return await id_token.verify(client, _token(key=_OTHER_KEY, kid="cert-new"))
    info = asyncio.run(run())
    assert info["open_id"] == "ou_alice"
    assert seen["jwks_fetches"] == 2
