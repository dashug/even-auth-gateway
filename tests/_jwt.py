"""测试用:模拟 Casdoor 签发的 id_token + 对应 JWKS(形状见 casdoor-findings §7)。"""
import time
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

KID = "cert-built-in"
CID_B = "cid-app-b"
CID_C = "cid-app-c"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
APP_CLIENT_IDS = f"app-b:{CID_B}, app-c:{CID_C}"


def jwks(key=KEY, kid=KID):
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return {"keys": [jwk]}


def token(key=KEY, kid=KID, **over):
    """默认:alice 登录 app-b 拿到的 id_token。传 key=None 的 claim 会被删掉。"""
    now = int(time.time())
    claims = {"iss": "https://auth.example.com", "aud": [CID_B], "azp": CID_B, "sub": "ou_alice",
              "owner": "even-test", "name": "randname", "displayName": "Alice", "email": "alice@e.com",
              "groups": [], "roles": [], "tokenType": "id-token", "iat": now, "nbf": now, "exp": now + 3600}
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})
