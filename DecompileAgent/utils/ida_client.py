import requests

class IDAClient:
    server = "http://127.0.0.1:8888"
    timeout = 2.0  # 秒

    def _fetch_from_ida_full(self, func: str) -> str | None:
        try:
            r = requests.get(
                f"{self.server}/full",
                params={"func": func},
                timeout=self.timeout,
            )
            if r.status_code != 200:
                return None
            j = r.json()
            if not j.get("ok"):
                return None
            pseudocode = j.get("pseudocode")
            if not pseudocode:
                return None
            # pseudocode 是 list[str]
            return "\n".join(pseudocode)
        except Exception:
            return None

    def _fetch_from_ida_by_ea(self, address: str, ctx: int = 10) -> str | None:
        try:
            r = requests.get(
                f"{self.server}/",
                params={"ea": address, "n": ctx},
                timeout=self.timeout,
            )
            if r.status_code != 200:
                return None
            j = r.json()
            if not j.get("ok"):
                return None
            ctx_lines = j.get("context")
            if not ctx_lines:
                return None
            header = f"[IDA live pseudocode @ {address} | function {j.get('function')}]\n"
            return header + "\n".join(ctx_lines)
        except Exception:
            return None
