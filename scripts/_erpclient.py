"""Phase 1 hand-run ERPNext REST helper.

This is the scripted form of "drive the buying chain by hand via curl/Postman"
(PRD §7 Phase 1). It hits the **real** ERPNext REST API — no mock (Rule 1).
Superseded by the typed client in ``erp/`` in Phase 2; do not build agent logic
on top of this.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent


class ERPError(RuntimeError):
    pass


class ERP:
    def __init__(self, url: str | None = None) -> None:
        self.url = (url or os.environ.get("ERPNEXT_URL", "http://localhost:8080")).rstrip("/")
        self.s = requests.Session()
        self.s.headers["Accept"] = "application/json"

    # --- auth ---------------------------------------------------------------
    def login_admin(self, password: str | None = None) -> None:
        pwd = password or os.environ.get("ERPNEXT_ADMIN_PASSWORD", "admin")
        r = self.s.post(f"{self.url}/api/method/login", data={"usr": "Administrator", "pwd": pwd})
        if r.status_code != 200:
            raise ERPError(f"admin login failed: {r.status_code} {r.text[:400]}")

    def use_api_key(self, key: str | None = None, secret: str | None = None) -> None:
        key = key or os.environ["ERPNEXT_API_KEY"]
        secret = secret or os.environ["ERPNEXT_API_SECRET"]
        self.s.headers["Authorization"] = f"token {key}:{secret}"

    # --- low level -------------------------------------------------------
    def _check(self, r: requests.Response) -> Any:
        if not r.ok:
            raise ERPError(f"{r.request.method} {r.request.url} -> {r.status_code}\n{r.text[:800]}")
        try:
            body = r.json()
        except ValueError:
            return r.text
        return body.get("data", body.get("message", body))

    def get(self, doctype: str, name: str) -> dict:
        return self._check(self.s.get(f"{self.url}/api/resource/{doctype}/{name}"))

    def list(
        self,
        doctype: str,
        filters: list | None = None,
        fields: list[str] | None = None,
        limit: int = 0,
    ) -> list[dict]:
        params: dict[str, Any] = {"limit_page_length": limit}
        if filters:
            params["filters"] = json.dumps(filters)
        if fields:
            params["fields"] = json.dumps(fields)
        return self._check(self.s.get(f"{self.url}/api/resource/{doctype}", params=params))

    def exists(self, doctype: str, filters: list) -> str | None:
        rows = self.list(doctype, filters=filters, fields=["name"], limit=1)
        return rows[0]["name"] if rows else None

    def insert(self, doctype: str, doc: dict) -> dict:
        doc = {**doc, "doctype": doctype}
        return self._check(
            self.s.post(
                f"{self.url}/api/resource/{doctype}",
                data={"data": json.dumps(doc)},
            )
        )

    def update(self, doctype: str, name: str, doc: dict) -> dict:
        return self._check(
            self.s.put(
                f"{self.url}/api/resource/{doctype}/{name}",
                data={"data": json.dumps(doc)},
            )
        )

    def submit(self, doc: dict) -> dict:
        return self._check(
            self.s.post(
                f"{self.url}/api/method/frappe.client.submit",
                data={"doc": json.dumps(doc)},
            )
        )

    def insert_and_submit(self, doctype: str, doc: dict) -> dict:
        created = self.insert(doctype, doc)
        return self.submit(created)

    def method(self, dotted_path: str, **args: Any) -> Any:
        payload = {k: (json.dumps(v) if isinstance(v, list | dict) else v) for k, v in args.items()}
        return self._check(self.s.post(f"{self.url}/api/method/{dotted_path}", data=payload))

    def ping(self) -> bool:
        try:
            return self.s.get(f"{self.url}/api/method/ping", timeout=5).status_code == 200
        except requests.RequestException:
            return False


def write_env(updates: dict[str, str], path: Path | None = None) -> None:
    """Rewrite matching KEY= lines in .env (create from .env.example if missing)."""
    path = path or ROOT / ".env"
    if not path.exists():
        path.write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    lines = path.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    for i, line in enumerate(lines):
        for k, v in updates.items():
            if line.startswith(f"{k}="):
                lines[i] = f"{k}={v}"
                seen.add(k)
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
