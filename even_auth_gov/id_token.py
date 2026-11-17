"""/request-access 鉴权:验证接入应用转交的、用户刚登录拿到的 Casdoor id_token。

为什么不用共享密钥:申请人姓名/邮箱会显示在审批卡片上,拿到共享密钥的人可以用自己的
open_id 冒填别人的名字骗批准,也能替任意应用伪造申请。改为验 Casdoor 签发的 id_token 后:
- 是谁(sub = 飞书 open_id)、叫什么(displayName/email)都来自 Casdoor 签名过的 claim;
- 申请哪个应用由 aud(该应用的 client_id)决定,对照 APP_CLIENT_IDS 登记表,调用方说了不算;
- 接入应用不再持有任何网关密钥,Casdoor webhook 密钥只留在 Casdoor 与网关两处。

token 形状按对活 Casdoor 实测(docs/casdoor-findings.md §7):RS256、header kid = 证书名、
aud = [client_id]、sub = open_id、tokenType = id-token;公钥在 <CASDOOR>/.well-known/jwks。
"""
from __future__ import annotations
import logging
import time
import httpx
import jwt
from even_auth_gov import settings

logger = logging.getLogger(__name__)

_JWKS_PATH = "/.well-known/jwks"
_JWKS_TTL = 3600.0
_LEEWAY = 30
# 只认非对称签名。对称算法的密钥就在公开的 JWKS 里,接受它等于谁都能签。
_ALLOWED_ALGS = {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}

_cache: dict = {"keys": {}, "at": 0.0}


class TokenRejected(Exception):
    status = 401


class InvalidToken(TokenRejected):
    """签名/过期/格式/类型不对 → 401。"""
    status = 401


class UnknownApp(TokenRejected):
    """token 有效,但签给的应用(aud)没在 APP_CLIENT_IDS 登记 → 403。"""
    status = 403


class JWKSUnavailable(TokenRejected):
    """拉不到 Casdoor 公钥,无法判断真伪 → 503,接入方稍后重试。"""
    status = 503


def clear_cache() -> None:
    _cache.update(keys={}, at=0.0)


async def _fetch_jwks(client: httpx.AsyncClient) -> dict:
    try:
        resp = await client.get(_JWKS_PATH, timeout=10.0)
        resp.raise_for_status()
        keyset = jwt.PyJWKSet.from_dict(resp.json())
    except Exception as e:
        raise JWKSUnavailable(f"拉取 Casdoor JWKS 失败: {e}") from e
    keys = {k.key_id or "": k for k in keyset.keys}
    _cache.update(keys=keys, at=time.monotonic())
    return keys


def _pick(keys: dict, kid):
    if kid:
        return keys.get(kid)
    return next(iter(keys.values())) if len(keys) == 1 else None


async def _signing_key(client: httpx.AsyncClient, kid):
    keys = _cache["keys"]
    fresh = bool(keys) and time.monotonic() - _cache["at"] < _JWKS_TTL
    if not fresh:
        keys = await _fetch_jwks(client)
    key = _pick(keys, kid)
    if key is None and fresh:
        # 缓存里没有这个 kid:Casdoor 可能换了证书,强制重拉一次。
        key = _pick(await _fetch_jwks(client), kid)
    if key is None:
        raise InvalidToken(f"未知签名密钥 kid={kid!r}")
    return key


async def verify(client: httpx.AsyncClient, token: str) -> dict:
    """验证 id_token,返回 {"open_id", "app", "name", "email"};不通过抛 TokenRejected 子类。
    只证明"Casdoor 给这个人签过这个应用的 token";账号此刻是否在职由调用方再查 Casdoor。
    client 的 base_url 指向 Casdoor(取 JWKS 用)。"""
    clients = settings.app_clients()
    if not clients:
        raise UnknownApp("网关未登记任何接入应用(APP_CLIENT_IDS)")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as e:
        raise InvalidToken(f"不是合法的 JWT: {e}") from e
    key = await _signing_key(client, header.get("kid"))
    if key.algorithm_name not in _ALLOWED_ALGS:
        raise InvalidToken(f"不接受的签名算法 {key.algorithm_name}")
    try:
        claims = jwt.decode(
            token, key.key, algorithms=[key.algorithm_name], audience=list(clients),
            issuer=settings.casdoor_issuer() or None, leeway=_LEEWAY,
            options={"require": ["exp", "iat", "sub", "aud"]},
        )
    except jwt.InvalidAudienceError as e:
        raise UnknownApp(f"token 签给的应用未登记: {e}") from e
    except jwt.PyJWTError as e:
        raise InvalidToken(str(e)) from e

    token_type = claims.get("tokenType")
    if token_type and token_type != "id-token":
        raise InvalidToken(f"只接受 id_token,收到 {token_type}")
    sub = str(claims.get("sub") or "").strip()
    if not sub:
        raise InvalidToken("token 缺少 sub")
    auds = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    apps = {clients[a] for a in auds if a in clients}
    if len(apps) != 1:
        raise UnknownApp(f"无法从 aud 唯一确定应用: {auds}")
    return {
        "open_id": sub,
        "app": apps.pop(),
        "name": claims.get("displayName") or claims.get("name") or "",
        "email": claims.get("email") or "",
    }
