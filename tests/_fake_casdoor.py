"""测试用内存 Casdoor:get-user(userId / email)、get-users、add-user、update-user(按 columns 落列)、get-roles。
生命周期类测试用它代替逐个 monkeypatch casdoor_admin,让读改写的真实代码路径都跑到。"""
import copy
import json

import httpx


class FakeCasdoor:
    def __init__(self, org: str = "even-test"):
        self.org = org
        self.users: dict[str, dict] = {}
        self.fail: set[str] = set()   # 命中的路径返回 status=error

    def add(self, **user) -> dict:
        user.setdefault("owner", self.org)
        user.setdefault("name", user["id"])
        user.setdefault("groups", [])
        user.setdefault("isForbidden", False)
        user.setdefault("properties", {})
        self.users[user["id"]] = user
        return user

    def props(self, user_id: str) -> dict:
        return self.users[user_id]["properties"]

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle), base_url="http://casdoor")

    @staticmethod
    def _ok(data=None):
        return httpx.Response(200, json={"status": "ok", "data": data})

    def _handle(self, req: httpx.Request) -> httpx.Response:
        path, q = req.url.path, req.url.params
        if path in self.fail:
            return httpx.Response(200, json={"status": "error", "msg": "boom", "data": None})
        if path == "/api/get-user":
            if "email" in q:
                user = next((u for u in self.users.values() if u.get("email") == q["email"]), None)
            else:
                user = self.users.get(q.get("userId"))
            return self._ok(copy.deepcopy(user))
        if path == "/api/get-users":
            return self._ok([copy.deepcopy(u) for u in self.users.values()])
        if path == "/api/get-roles":
            return self._ok([])
        if path == "/api/add-user":
            body = json.loads(req.content)
            if body["id"] in self.users:
                return httpx.Response(200, json={"status": "error", "msg": "exists"})
            self.add(**body)
            return self._ok("Affected")
        if path == "/api/update-user":
            owner, name = q["id"].split("/", 1)
            target = next((u for u in self.users.values() if u["owner"] == owner and u["name"] == name), None)
            if target is None:
                return httpx.Response(200, json={"status": "error", "msg": "no such user"})
            body = json.loads(req.content)
            for col in q.get("columns", "").split(","):
                target[col] = copy.deepcopy(body.get(col))
            return self._ok("Affected")
        return httpx.Response(404)
