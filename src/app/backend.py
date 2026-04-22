"""Backend client: Genie REST + Statement Execution API via the app's service principal."""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import requests
from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config

GENIE_API_BASE = "/api/2.0/genie/spaces"
AUDIT_TABLE = "system.access.audit"


@dataclass
class Backend:
    cfg: Config = field(default_factory=Config)
    default_warehouse_id: str | None = None

    @classmethod
    def from_env(cls) -> "Backend":
        return cls(
            cfg=Config(),
            default_warehouse_id=os.environ.get("DATABRICKS_WAREHOUSE_ID"),
        )

    @property
    def host(self) -> str:
        h = (self.cfg.host or os.environ.get("DATABRICKS_HOST") or "").rstrip("/")
        if h and not h.startswith(("http://", "https://")):
            h = f"https://{h}"
        return h

    # ---------- HTTP ----------

    def _headers(self) -> dict[str, str]:
        return {**self.cfg.authenticate(), "Content-Type": "application/json"}

    @staticmethod
    def _raise_with_body(r: requests.Response) -> None:
        if r.ok:
            return
        body = ""
        try:
            body = r.text[:2000]
        except Exception:  # noqa: BLE001
            pass
        print(
            f"[backend] HTTP {r.status_code} {r.reason} {r.request.method} {r.url} "
            f"body={body!r}",
            file=sys.stderr,
            flush=True,
        )
        raise requests.HTTPError(
            f"{r.status_code} {r.reason} for {r.request.method} {r.url} — body: {body}",
            response=r,
        )

    def _get(self, path: str, params: dict | None = None) -> dict:
        r = requests.get(
            f"{self.host}{path}", headers=self._headers(), params=params, timeout=30
        )
        self._raise_with_body(r)
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        r = requests.post(
            f"{self.host}{path}", headers=self._headers(), json=body, timeout=60
        )
        self._raise_with_body(r)
        return r.json()

    # ---------- Warehouses ----------

    def list_warehouses_via_sdk(self) -> list[dict]:
        w = WorkspaceClient(config=self.cfg)
        out: list[dict] = []
        for wh in w.warehouses.list():
            out.append(
                {
                    "id": wh.id,
                    "name": wh.name,
                    "state": wh.state.value if wh.state else None,
                    "size": wh.cluster_size,
                    "serverless": wh.enable_serverless_compute,
                }
            )
        out.sort(key=lambda x: (x["name"] or "").lower())
        return out

    def list_warehouses_via_sql(self) -> list[dict]:
        if not self.default_warehouse_id:
            raise RuntimeError("No default warehouse configured.")
        rows = self._query(
            """
            SELECT
                warehouse_id   AS id,
                warehouse_name AS name,
                warehouse_type AS type,
                warehouse_size AS size,
                warehouse_channel AS channel
            FROM system.compute.warehouses
            WHERE delete_time IS NULL
            QUALIFY row_number() OVER (
                PARTITION BY warehouse_id ORDER BY change_time DESC
            ) = 1
            ORDER BY lower(warehouse_name)
            """,
            warehouse_id=self.default_warehouse_id,
        )
        return [
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "state": None,
                "size": r.get("size"),
                "serverless": None,
            }
            for r in rows
        ]

    def default_warehouse_as_list(self) -> list[dict]:
        if not self.default_warehouse_id:
            return []
        return [
            {
                "id": self.default_warehouse_id,
                "name": "(default warehouse)",
                "state": None,
                "size": None,
                "serverless": None,
            }
        ]

    # ---------- Genie REST API (runs as the app SP) ----------

    def list_spaces(self, page_token: str | None = None) -> dict:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        return self._get(GENIE_API_BASE, params=params)

    def list_all_spaces(self) -> list[dict]:
        spaces: list[dict] = []
        token: str | None = None
        while True:
            resp = self.list_spaces(page_token=token)
            spaces.extend(resp.get("spaces", []))
            token = resp.get("next_page_token")
            if not token:
                break
        return spaces

    def get_space(self, space_id: str) -> dict:
        return self._get(f"{GENIE_API_BASE}/{space_id}")

    def list_conversation_messages(
        self, space_id: str, conversation_id: str
    ) -> list[dict]:
        """The Genie REST API exposes conversation detail as a message list only
        (there is no GET /conversations/{id} endpoint). Each message includes
        its attachments (SQL query, thoughts, text) inline."""
        resp = self._get(
            f"{GENIE_API_BASE}/{space_id}/conversations/{conversation_id}/messages"
        )
        return resp.get("messages", [])

    # ---------- Statement Execution API ----------

    def _resolve_warehouse(self, warehouse_id: str | None) -> str:
        wid = warehouse_id or self.default_warehouse_id
        if not wid:
            raise RuntimeError(
                "No SQL warehouse selected and DATABRICKS_WAREHOUSE_ID is unset."
            )
        return wid

    def _query(
        self,
        statement: str,
        params: dict[str, Any] | None = None,
        warehouse_id: str | None = None,
        poll_timeout_s: int = 60,
    ) -> list[dict]:
        wid = self._resolve_warehouse(warehouse_id)
        body: dict[str, Any] = {
            "warehouse_id": wid,
            "statement": statement,
            "wait_timeout": "30s",
            "on_wait_timeout": "CONTINUE",
            "format": "JSON_ARRAY",
            "disposition": "INLINE",
        }
        if params:
            body["parameters"] = [
                {"name": k, "value": str(v), "type": "STRING"} for k, v in params.items()
            ]
        resp = self._post("/api/2.0/sql/statements", body)
        statement_id = resp["statement_id"]
        state = (resp.get("status") or {}).get("state")

        deadline = time.time() + poll_timeout_s
        while state in ("PENDING", "RUNNING") and time.time() < deadline:
            time.sleep(1)
            resp = self._get(f"/api/2.0/sql/statements/{statement_id}")
            state = (resp.get("status") or {}).get("state")

        if state != "SUCCEEDED":
            err = (resp.get("status") or {}).get("error") or {}
            raise RuntimeError(
                f"SQL statement {state}: {err.get('message') or err or 'unknown error'}"
            )

        manifest = resp.get("manifest") or {}
        schema = manifest.get("schema") or {}
        cols = [c["name"] for c in schema.get("columns", [])]
        result = resp.get("result") or {}
        data = result.get("data_array") or []
        return [dict(zip(cols, row)) for row in data]

    @staticmethod
    def audit_conversations_sql(
        space_id: str | None = None, limit: int = 500, literal: bool = False
    ) -> str:
        """The SQL used to list conversations from system.access.audit.

        When `literal=True`, substitutes `space_id` directly (for display / copy).
        When `literal=False`, returns the parameterized form with `:space_id`.
        """
        space_expr = f"'{space_id}'" if literal and space_id else ":space_id"
        return (
            f"WITH events AS (\n"
            f"    SELECT\n"
            f"        request_params['conversation_id'] AS conversation_id,\n"
            f"        user_identity.email                AS user_email,\n"
            f"        event_time,\n"
            f"        action_name\n"
            f"    FROM {AUDIT_TABLE}\n"
            f"    WHERE service_name IN ('genie', 'aibiGenie', 'dataRoom')\n"
            f"      AND request_params['conversation_id'] IS NOT NULL\n"
            f"      AND (\n"
            f"          request_params['space_id'] = {space_expr}\n"
            f"          OR request_params['room_id'] = {space_expr}\n"
            f"      )\n"
            f")\n"
            f"SELECT\n"
            f"    conversation_id,\n"
            f"    min_by(user_email, event_time)   AS created_by,\n"
            f"    min(event_time)                  AS created,\n"
            f"    max(event_time)                  AS last_activity,\n"
            f"    count(*)                         AS event_count,\n"
            f"    count(DISTINCT action_name)      AS distinct_actions\n"
            f"FROM events\n"
            f"GROUP BY conversation_id\n"
            f"ORDER BY last_activity DESC\n"
            f"LIMIT {int(limit)}"
        )

    def list_conversations_from_audit(
        self, space_id: str, warehouse_id: str | None = None, limit: int = 500
    ) -> list[dict]:
        """Reconstruct conversation list for a space from system.access.audit.

        Groups audit events by request_params['conversation_id'] to produce one
        row per conversation, with its creator (first-seen user), timestamps,
        and event count.
        """
        query = self.audit_conversations_sql(limit=limit)
        return self._query(query, {"space_id": space_id}, warehouse_id=warehouse_id)

    def audit_events_for_space(
        self, space_id: str, limit: int = 200, warehouse_id: str | None = None
    ) -> list[dict]:
        query = f"""
            SELECT event_time, user_identity.email AS user_email, action_name,
                   request_params, response
            FROM {AUDIT_TABLE}
            WHERE service_name IN ('genie', 'aibiGenie', 'dataRoom')
              AND (
                  request_params['space_id'] = :space_id
                  OR request_params['room_id'] = :space_id
              )
            ORDER BY event_time DESC
            LIMIT {int(limit)}
        """
        return self._query(query, {"space_id": space_id}, warehouse_id=warehouse_id)

    def audit_events_for_conversation(
        self,
        space_id: str,
        conversation_id: str,
        limit: int = 200,
        warehouse_id: str | None = None,
    ) -> list[dict]:
        query = f"""
            SELECT event_time, user_identity.email AS user_email, action_name,
                   request_params, response
            FROM {AUDIT_TABLE}
            WHERE service_name IN ('genie', 'aibiGenie', 'dataRoom')
              AND request_params['conversation_id'] = :conv_id
              AND (
                  request_params['space_id'] = :space_id
                  OR request_params['room_id'] = :space_id
              )
            ORDER BY event_time DESC
            LIMIT {int(limit)}
        """
        return self._query(
            query,
            {"conv_id": conversation_id, "space_id": space_id},
            warehouse_id=warehouse_id,
        )

    def feedback_events_for_conversation(
        self,
        space_id: str,
        conversation_id: str,
        limit: int = 200,
        warehouse_id: str | None = None,
    ) -> list[dict]:
        query = f"""
            SELECT event_time, user_identity.email AS user_email, action_name,
                   request_params, response
            FROM {AUDIT_TABLE}
            WHERE service_name IN ('genie', 'aibiGenie', 'dataRoom')
              AND request_params['conversation_id'] = :conv_id
              AND (
                  lower(action_name) LIKE '%feedback%'
                  OR lower(action_name) LIKE '%rating%'
                  OR lower(action_name) LIKE '%thumb%'
              )
            ORDER BY event_time DESC
            LIMIT {int(limit)}
        """
        return self._query(
            query, {"conv_id": conversation_id}, warehouse_id=warehouse_id
        )
