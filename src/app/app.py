"""Genie Conversation Explorer: browse Genie rooms, conversations, and message details.

All API + SQL calls run as the app's service principal. The list of conversations
per room is derived from `system.access.audit` (admin-style view across all users).
The signed-in user is surfaced from Databricks Apps forwarded identity headers.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
from typing import Any

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from backend import Backend


def _render_copy_button(text: str, key_suffix: str, label: str = "📋 Copy SQL") -> None:
    """Render a real clipboard-copy button via a tiny HTML component."""
    btn_id = f"copybtn_{key_suffix}"
    payload = json.dumps(text)  # properly JS-escaped string literal
    components.html(
        f"""
        <button id="{btn_id}" style="
            border: 1px solid #d1d5db;
            background: #f9fafb;
            border-radius: 6px;
            padding: 4px 10px;
            font-size: 13px;
            cursor: pointer;
        ">{html.escape(label)}</button>
        <script>
          const btn = document.getElementById({json.dumps(btn_id)});
          btn.addEventListener("click", () => {{
            navigator.clipboard.writeText({payload}).then(() => {{
              const old = btn.innerText;
              btn.innerText = "✅ Copied";
              setTimeout(() => (btn.innerText = old), 1500);
            }});
          }});
        </script>
        """,
        height=40,
    )

st.set_page_config(
    page_title="Genie Conversation Explorer",
    page_icon="✨",
    layout="wide",
)

# Monospace font for all st.dataframe tables.
st.markdown(
    """
    <style>
      [data-testid="stDataFrame"] [role="gridcell"],
      [data-testid="stDataFrame"] [role="columnheader"] {
          font-family: ui-monospace, SFMono-Regular, SF Mono, Menlo, Monaco, "Liberation Mono", "Courier New", monospace !important;
          font-size: 12.5px;
      }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def get_backend() -> Backend:
    return Backend.from_env()


@st.cache_data(ttl=60)
def cached_spaces() -> list[dict]:
    return get_backend().list_all_spaces()


@st.cache_data(ttl=30)
def cached_audit_conversations(space_id: str, warehouse_id: str) -> list[dict]:
    return get_backend().list_conversations_from_audit(space_id, warehouse_id=warehouse_id)


@st.cache_data(ttl=30)
def cached_messages(
    space_id: str, conversation_id: str
) -> tuple[list[dict], str | None]:
    try:
        return get_backend().list_conversation_messages(space_id, conversation_id), None
    except Exception as e:  # noqa: BLE001
        return [], str(e)


@st.cache_data(ttl=300)
def cached_warehouses() -> tuple[list[dict], list[str]]:
    b = get_backend()
    errors: list[str] = []
    for label, fn in [
        ("SDK", b.list_warehouses_via_sdk),
        ("system.compute.warehouses", b.list_warehouses_via_sql),
    ]:
        try:
            whs = fn()
            if whs:
                return whs, errors
        except Exception as e:  # noqa: BLE001
            errors.append(f"{label}: {e}")
    return b.default_warehouse_as_list(), errors


@st.cache_data(ttl=60)
def cached_audit_events(
    space_id: str, conversation_id: str, warehouse_id: str
) -> list[dict]:
    try:
        return get_backend().audit_events_for_conversation(
            space_id, conversation_id, warehouse_id=warehouse_id
        )
    except Exception as e:  # noqa: BLE001
        st.warning(f"Audit trail query failed: {e}")
        return []


@st.cache_data(ttl=60)
def cached_feedback_audit(
    space_id: str, conversation_id: str, warehouse_id: str
) -> list[dict]:
    try:
        return get_backend().feedback_events_for_conversation(
            space_id, conversation_id, warehouse_id=warehouse_id
        )
    except Exception as e:  # noqa: BLE001
        st.warning(f"Feedback audit query failed: {e}")
        return []


def _forwarded_user() -> dict[str, str]:
    try:
        h = st.context.headers
    except Exception:  # noqa: BLE001
        return {}
    return {
        "email": h.get("x-forwarded-email") or h.get("X-Forwarded-Email") or "",
        "user": h.get("x-forwarded-user") or h.get("X-Forwarded-User") or "",
        "preferred_username": (
            h.get("x-forwarded-preferred-username")
            or h.get("X-Forwarded-Preferred-Username")
            or ""
        ),
    }


def _fmt_ts(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        try:
            return dt.datetime.fromtimestamp(
                v / 1000 if v > 1e12 else v, tz=dt.timezone.utc
            ).isoformat()
        except (OSError, ValueError):
            return str(v)
    return str(v)


def _space_label(space: dict) -> str:
    return space.get("title") or space.get("display_name") or space.get("space_id", "unknown")


# ---------- UI ----------

st.title("Genie Conversation Explorer")
st.caption(
    "Conversations listed here come from `system.access.audit` — covers every user. "
    "Genie API calls (space list + conversation detail) run as the app's service principal."
)

with st.sidebar:
    st.header("Signed in as")
    user = _forwarded_user()
    label = user.get("preferred_username") or user.get("email") or "(unknown)"
    st.markdown(f"**{label}**")
    if user.get("email") and user["email"] != label:
        st.caption(user["email"])
    if user:
        with st.expander("Forwarded identity headers", expanded=False):
            st.json(user, expanded=False)

    st.divider()
    st.header("SQL warehouse")
    try:
        warehouses, wh_errors = cached_warehouses()
    except Exception as e:  # noqa: BLE001
        st.error(f"Failed to list warehouses: {e}")
        warehouses, wh_errors = [], [str(e)]
    if wh_errors:
        with st.expander(f"⚠️ {len(wh_errors)} warehouse-list fallback(s) used", expanded=False):
            for err in wh_errors:
                st.caption(err)

    default_wid = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")
    if warehouses:
        wh_ids = [w["id"] for w in warehouses]
        try:
            default_idx = wh_ids.index(default_wid) if default_wid in wh_ids else 0
        except ValueError:
            default_idx = 0
        selected_warehouse_id = st.selectbox(
            "Select warehouse",
            options=wh_ids,
            index=default_idx,
            format_func=lambda wid: next(
                (
                    f"{w['name']}  ({w['state']}{', serverless' if w['serverless'] else ''})"
                    for w in warehouses
                    if w["id"] == wid
                ),
                wid,
            ),
            key="warehouse_id",
        )
    elif default_wid:
        st.caption(f"Using configured warehouse: `{default_wid}`")
        selected_warehouse_id = default_wid
    else:
        st.warning("No warehouses visible and DATABRICKS_WAREHOUSE_ID is unset.")
        selected_warehouse_id = None

    st.divider()
    st.header("Rooms")
    if st.button("Refresh", use_container_width=True):
        cached_spaces.clear()
        cached_audit_conversations.clear()
        cached_warehouses.clear()

    try:
        spaces = cached_spaces()
    except Exception as e:  # noqa: BLE001
        st.error(f"Failed to list Genie spaces: {e}")
        st.stop()

    if not spaces:
        st.info("No Genie spaces visible to the app's service principal.")
        st.stop()

    space_options = {s["space_id"]: _space_label(s) for s in spaces}
    selected_space_id = st.selectbox(
        "Select a room",
        options=list(space_options.keys()),
        format_func=lambda sid: space_options[sid],
        key="space_id",
    )

st.subheader("Conversations (from audit)")
with st.expander("SQL used to build this list", expanded=False):
    audit_sql = Backend.audit_conversations_sql(space_id=selected_space_id, literal=True)
    st.code(audit_sql, language="sql")
    _render_copy_button(
        audit_sql,
        key_suffix=f"auditsql_{selected_space_id}",
        label="📋 Copy audit SQL",
    )

if not selected_warehouse_id:
    st.info("Pick a SQL warehouse to load conversations.")
    conversations = []
else:
    try:
        conversations = cached_audit_conversations(
            selected_space_id, selected_warehouse_id
        )
    except Exception as e:  # noqa: BLE001
        st.error(f"Failed to list conversations from audit: {e}")
        conversations = []

if conversations:
    conv_df = pd.DataFrame(
        [
            {
                "id": c["conversation_id"],
                "created_by": c.get("created_by"),
                "created": _fmt_ts(c.get("created")),
                "last_activity": _fmt_ts(c.get("last_activity")),
                "events": c.get("event_count"),
            }
            for c in conversations
        ]
    )
    event = st.dataframe(
        conv_df,
        use_container_width=True,
        hide_index=True,
        height=340,
        on_select="rerun",
        selection_mode="single-row",
        key=f"conv_table_{selected_space_id}",
    )
    selected_rows = event.selection.rows if event and event.selection else []
    if selected_rows:
        selected_conv_id = conversations[selected_rows[0]]["conversation_id"]
    else:
        selected_conv_id = None
        st.caption("Click a row above to select a conversation.")

    if selected_conv_id:
        _render_copy_button(
            selected_conv_id,
            key_suffix=f"convid_{selected_conv_id}",
            label="📋 Copy conversation_id",
        )
else:
    selected_conv_id = None
    if selected_warehouse_id:
        st.info("No conversations found in audit for this room.")

st.divider()
st.subheader("Conversation detail")
if not selected_conv_id:
    st.info("Pick a conversation to see its details.")
else:
    summary_sql = Backend.conversation_summary_sql(
        space_id=selected_space_id,
        conversation_id=selected_conv_id,
        literal=True,
    )
    with st.expander("SQL used to build this summary", expanded=False):
        st.code(summary_sql, language="sql")
        _render_copy_button(
            summary_sql,
            key_suffix=f"summarysql_{selected_conv_id}",
            label="📋 Copy summary SQL",
        )

    # Header metadata from the audit row we already have.
    audit_row = next(
        (c for c in conversations if c["conversation_id"] == selected_conv_id),
        None,
    )
    if audit_row:
        st.json(
            {
                "conversation_id": audit_row["conversation_id"],
                "created_by": audit_row.get("created_by"),
                "created": _fmt_ts(audit_row.get("created")),
                "last_activity": _fmt_ts(audit_row.get("last_activity")),
                "event_count": audit_row.get("event_count"),
                "distinct_actions": audit_row.get("distinct_actions"),
            },
            expanded=False,
        )

    messages_curl = Backend.messages_request_curl(
        selected_space_id, selected_conv_id
    )
    with st.expander("REST call used to fetch messages", expanded=False):
        st.caption(
            "Messages come from the Genie REST API — there is no equivalent SQL. "
            "Paste this into a shell with `DATABRICKS_HOST` and `DATABRICKS_TOKEN` set."
        )
        st.code(messages_curl, language="bash")
        _render_copy_button(
            messages_curl,
            key_suffix=f"msgcurl_{selected_conv_id}",
            label="📋 Copy messages REST call",
        )

    messages, msg_err = cached_messages(selected_space_id, selected_conv_id)
    if msg_err:
        st.warning("Could not load messages from the Genie API.")
        with st.expander("API error detail", expanded=False):
            st.code(msg_err)
    elif messages:
        st.markdown(f"**{len(messages)} message(s)**")
        for i, msg in enumerate(messages):
            role = (msg.get("user") or {}).get("email") or msg.get("role") or "user"
            content = msg.get("content") or ""
            status = msg.get("status") or ""
            created = _fmt_ts(msg.get("created_timestamp"))
            header = f"#{i + 1}  ·  {role}  ·  {status}  ·  {created}"
            with st.expander(header, expanded=(i == 0)):
                if content:
                    st.markdown(f"**User prompt**\n\n{content}")

                for att_idx, att in enumerate(msg.get("attachments", []) or []):
                    query = att.get("query") or {}
                    text = att.get("text") or {}
                    suggested = att.get("suggested_questions") or {}
                    att_id = att.get("attachment_id") or f"att{att_idx}"
                    if query:
                        sql_text = query.get("query", "")
                        st.markdown("**Generated SQL**")
                        st.code(sql_text, language="sql")
                        _render_copy_button(sql_text, key_suffix=f"{selected_conv_id}_{i}_{att_id}")
                        if query.get("description"):
                            st.caption(query["description"])
                        if query.get("statement_id"):
                            stmt_id = query["statement_id"]
                            history_url = (
                                f"{get_backend().host}/sql/history?queryId={stmt_id}"
                            )
                            st.caption(
                                f"statement_id: `{stmt_id}` · "
                                f"[open in Query History ↗]({history_url})"
                            )
                        for thought in query.get("thoughts") or []:
                            tt = thought.get("thought_type", "").replace(
                                "THOUGHT_TYPE_", ""
                            ).title()
                            st.markdown(f"*{tt}:* {thought.get('content','')}")
                    if text:
                        st.markdown("**Assistant text**")
                        st.markdown(text.get("content", ""))
                    if suggested:
                        st.markdown("**Suggested follow-ups**")
                        for q in suggested.get("questions", []):
                            st.markdown(f"- {q}")

                feedback = (
                    msg.get("feedback")
                    or msg.get("rating")
                    or msg.get("user_feedback")
                )
                if feedback:
                    st.markdown("**Feedback (from API)**")
                    st.json(feedback)

                if st.toggle(
                    "Show raw message JSON",
                    key=f"raw_{selected_conv_id}_{msg.get('id') or msg.get('message_id') or i}",
                ):
                    st.json(msg, expanded=False)
    else:
        st.caption("No messages returned by the Genie API for this conversation.")

    st.markdown("---")
    st.markdown("**Audit trail (`system.access.audit`)**")
    audit_events_sql = Backend.audit_events_for_conversation_sql(
        space_id=selected_space_id,
        conversation_id=selected_conv_id,
        literal=True,
    )
    with st.expander("SQL used to build this trail", expanded=False):
        st.code(audit_events_sql, language="sql")
        _render_copy_button(
            audit_events_sql,
            key_suffix=f"auditsql_trail_{selected_conv_id}",
            label="📋 Copy audit-trail SQL",
        )
    if not selected_warehouse_id:
        st.caption("Pick a SQL warehouse to enable audit queries.")
    else:
        events = cached_audit_events(
            selected_space_id, selected_conv_id, selected_warehouse_id
        )
        if events:
            st.dataframe(
                pd.DataFrame(events), use_container_width=True, hide_index=True
            )
        else:
            st.caption("No audit events found for this conversation.")

    st.markdown("**Feedback events**")
    feedback_sql = Backend.feedback_events_for_conversation_sql(
        conversation_id=selected_conv_id, literal=True
    )
    with st.expander("SQL used to build this list", expanded=False):
        st.code(feedback_sql, language="sql")
        _render_copy_button(
            feedback_sql,
            key_suffix=f"feedbacksql_{selected_conv_id}",
            label="📋 Copy feedback SQL",
        )
    if selected_warehouse_id:
        feedback_rows = cached_feedback_audit(
            selected_space_id, selected_conv_id, selected_warehouse_id
        )
        if feedback_rows:
            st.dataframe(
                pd.DataFrame(feedback_rows),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.caption("No feedback events for this conversation.")
