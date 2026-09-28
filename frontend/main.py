"""Minimal FastAPI proxy for a deployed A2A agent (Agent Runtime, agents-cli 1.1.0+).

The browser talks ONLY to this proxy (same origin, no CORS, no GCP creds in the
browser). The proxy authenticates with Application Default Credentials and
forwards chat to the deployed agent over the A2A protocol, returning replies as
structured parts the chat UI knows how to show:

  * {"kind": "text", "text": ...}  -> a normal chat bubble
  * {"kind": "a2ui", "data": ...}  -> one A2UI message (beginRendering /
    surfaceUpdate); static/index.html renders these as a card.

Why A2A: agents-cli 1.1.0 (GA) deploys ADK agents to Agent Runtime as A2A agents
and no longer registers the reasoning-engine operation schema the old
`agent_engines.get(...).stream_query()` path relied on (operation_schemas() comes
back empty). The container serves the A2A protocol over the Agent Engine HTTP
passthrough, so this proxy fetches the agent's card and sends messages with the
a2a-sdk client (the same path `agents-cli run --mode a2a` uses). This works for
both A2A and plain ADK 1.1.0 deployments (the container serves A2A either way).

Run:
  pip install -r requirements.txt
  export AGENT_ENGINE_RESOURCE_NAME="projects/.../locations/.../reasoningEngines/..."
  export AGENT_DIRECTORY="app"   # your agent's app directory (agents-cli-manifest.yaml)
  python main.py                 # -> http://localhost:8080
"""

import os
import uuid

import google.auth
import google.auth.transport.requests
import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.types import (
    AgentCard,
    Message,
    Part,
    Role,
    TaskArtifactUpdateEvent,
)
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

RESOURCE = os.environ["AGENT_ENGINE_RESOURCE_NAME"]
# The agent's app directory (matches agent_directory in agents-cli-manifest.yaml).
AGENT_DIRECTORY = os.environ.get("AGENT_DIRECTORY", "app")
# Location is embedded in the resource name: projects/<p>/locations/<loc>/reasoningEngines/<id>.
LOCATION = RESOURCE.split("/locations/")[1].split("/")[0]

# A2A endpoint for an Agent Runtime deployment, via the Agent Engine HTTP
# passthrough. The card lives at the well-known path under this base.
A2A_BASE = (
    f"https://{LOCATION}-aiplatform.googleapis.com/reasoningEngines/v1/"
    f"{RESOURCE}/api/a2a/{AGENT_DIRECTORY}"
)
A2A_CARD_URL = f"{A2A_BASE}/.well-known/agent-card.json"

# The agent tags its A2UI data parts with this mime type.
_A2UI_MIME = "application/json+a2ui"

# One set of ADC credentials, refreshed per request (access tokens expire ~1h).
_creds, _ = google.auth.default(
    scopes=["https://www.googleapis.com/auth/cloud-platform"]
)


def _auth_headers() -> dict[str, str]:
    _creds.refresh(google.auth.transport.requests.Request())
    return {
        "Authorization": f"Bearer {_creds.token}",
        "Content-Type": "application/json",
    }


app = FastAPI()


@app.exception_handler(Exception)
async def _json_errors(request: Request, exc: Exception):
    # Always return JSON so the browser never receives a plain-text 500 page
    # (which shows up in the chat as "Unexpected token 'I', "Internal S"... is
    # not valid JSON"). Any server-side failure now surfaces as a readable
    # message in the chat bubble instead.
    return JSONResponse(
        status_code=200,
        content={
            "parts": [{"kind": "text", "text": f"Error: {type(exc).__name__}: {exc}"}]
        },
    )


# Reuse ONE A2A context per user so the agent remembers the conversation.
_contexts: dict[str, str] = {}
# Cache the agent card after the first fetch.
_card: AgentCard | None = None


from google.protobuf.json_format import ParseDict


async def _get_card(client: httpx.AsyncClient) -> AgentCard:
    global _card
    if _card is None:
        resp = await client.get(A2A_CARD_URL)
        resp.raise_for_status()
        try:
            from google.protobuf.json_format import ParseDict
            _card = ParseDict(resp.json(), AgentCard(), ignore_unknown_fields=True)
        except Exception:
            try:
                _card = AgentCard.model_validate(resp.json())
            except Exception:
                _card = AgentCard(**resp.json())
    return _card


def _extract_parts(parts) -> list[dict]:
    """Turn A2A response parts into structured parts for the chat UI.

    Text parts pass through as {"kind": "text"}. A2UI data parts (tagged
    application/json+a2ui) become {"kind": "a2ui", "data": <message>} so the UI
    renders the card; each data part is one A2UI message (beginRendering or
    surfaceUpdate).
    """
    import json
    out: list[dict] = []
    for p in parts:
        root = getattr(p, "root", p)

        # Skip explicit function call and response objects
        if getattr(root, "function_call", None) is not None or getattr(root, "function_response", None) is not None:
            continue

        meta = getattr(root, "metadata", None)
        adk_type = None
        mime = None
        if isinstance(meta, dict):
            adk_type = meta.get("adk_type")
            mime = meta.get("mimeType") or meta.get("mime_type")
        elif hasattr(meta, "fields"):
            fields = meta.fields
            if "adk_type" in fields:
                adk_type = fields["adk_type"].string_value
            if "mimeType" in fields:
                mime = fields["mimeType"].string_value
            elif "mime_type" in fields:
                mime = fields["mime_type"].string_value

        if adk_type in ("function_call", "function_response"):
            continue

        text = getattr(root, "text", None)
        if text:
            if text.startswith("struct_value {") or "function_call" in text or "function_response" in text:
                continue
            out.append({"kind": "text", "text": text})
            continue

        raw_data = getattr(root, "data", None)
        if raw_data is not None:
            dict_data = None
            if hasattr(raw_data, "DESCRIPTOR") or type(raw_data).__name__ == "Value":
                try:
                    from google.protobuf.json_format import MessageToDict
                    dict_data = MessageToDict(raw_data)
                except Exception:
                    pass

            if dict_data is not None and isinstance(dict_data, dict):
                inner_meta = dict_data.get("metadata", {})
                inner_mime = inner_meta.get("mimeType") if isinstance(inner_meta, dict) else None
                effective_mime = mime or inner_mime

                inner_data = dict_data.get("data", dict_data)
                is_a2ui = (effective_mime == _A2UI_MIME) or (
                    isinstance(inner_data, dict) and ("beginRendering" in inner_data or "surfaceUpdate" in inner_data)
                ) or (
                    isinstance(inner_data, str) and ("beginRendering" in inner_data or "surfaceUpdate" in inner_data)
                )

                if is_a2ui:
                    if isinstance(inner_data, str):
                        try:
                            inner_data = json.loads(inner_data)
                        except Exception:
                            pass
                    out.append({"kind": "a2ui", "data": inner_data})
                    continue
                else:
                    text_content = dict_data.get("text") or dict_data.get("literalString")
                    if text_content and not str(text_content).startswith("struct_value"):
                        out.append({"kind": "text", "text": str(text_content)})
                        continue

            if isinstance(raw_data, bytes):
                try:
                    raw_data = raw_data.decode("utf-8")
                except Exception:
                    pass

            if not isinstance(raw_data, str) or len(raw_data) == 0:
                continue

            if raw_data.startswith("struct_value {"):
                continue

            is_a2ui = (mime == _A2UI_MIME) or ("beginRendering" in raw_data or "surfaceUpdate" in raw_data)

            if is_a2ui:
                try:
                    parsed_json = json.loads(raw_data)
                    out.append({"kind": "a2ui", "data": parsed_json})
                except Exception:
                    out.append({"kind": "a2ui", "data": raw_data})
            else:
                out.append({"kind": "text", "text": raw_data})

    return out


@app.post("/chat")
async def chat(req: Request):
    body = await req.json()
    message = body.get("message", "")
    user_id = body.get("user_id") or "web-user"
    parts: list[dict] = []

    async with httpx.AsyncClient(headers=_auth_headers(), timeout=120) as client:
        card = await _get_card(client)
        factory = ClientFactory(ClientConfig(httpx_client=client))
        a2a_client = factory.create(card)

        try:
            user_role = Role.user
        except AttributeError:
            user_role = getattr(Role, "ROLE_USER", "ROLE_USER")

        try:
            part_obj = Part(text=message)
        except Exception:
            part_obj = Part(root={"text": message})

        msg = Message(
            message_id=str(uuid.uuid4()),
            role=user_role,
            parts=[part_obj],
            context_id=_contexts.get(user_id),
        )

        try:
            from a2a.types import SendMessageRequest
            send_req = SendMessageRequest(message=msg)
        except Exception:
            send_req = msg

        async for event in a2a_client.send_message(send_req):
            payload_type = event.WhichOneof("payload") if hasattr(event, "WhichOneof") else None
            print(f"[CHAT] Received event payload_type={payload_type}")

            if payload_type == "task" or (hasattr(event, "task") and event.HasField("task")):
                t = event.task
                if t.context_id:
                    _contexts[user_id] = t.context_id

            if payload_type == "status_update" or (hasattr(event, "status_update") and event.HasField("status_update")):
                su = event.status_update
                if su.context_id:
                    _contexts[user_id] = su.context_id
                if su.HasField("status") and su.status.HasField("message"):
                    state_val = getattr(su.status, "state", None)
                    is_failed = (state_val == 3 or str(state_val) == "TASK_STATE_FAILED")
                    if not (is_failed and parts):
                        if su.status.message.parts:
                            p_list = _extract_parts(su.status.message.parts)
                            print(f"[CHAT] Extracted {len(p_list)} parts from status_update message")
                            parts.extend(p_list)

            if payload_type == "artifact_update" or (hasattr(event, "artifact_update") and event.HasField("artifact_update")):
                au = event.artifact_update
                if getattr(au, "context_id", None):
                    _contexts[user_id] = au.context_id
                if au.HasField("artifact") and au.artifact.parts:
                    p_list = _extract_parts(au.artifact.parts)
                    print(f"[CHAT] Extracted {len(p_list)} parts from artifact_update")
                    parts.extend(p_list)
            elif hasattr(event, "task_artifact_update") and event.HasField("task_artifact_update"):
                tau = event.task_artifact_update
                if tau.HasField("artifact") and tau.artifact.parts:
                    p_list = _extract_parts(tau.artifact.parts)
                    print(f"[CHAT] Extracted {len(p_list)} parts from task_artifact_update")
                    parts.extend(p_list)
            elif isinstance(event, tuple):
                task, update = event
                if task and getattr(task, "context_id", None):
                    _contexts[user_id] = task.context_id
                if isinstance(update, TaskArtifactUpdateEvent):
                    parts.extend(_extract_parts(update.artifact.parts))

    if not parts:
        # The turn produced no text or UI (e.g. the agent only ran tools, or a
        # tool stalled). Be honest rather than silent.
        parts = [{"kind": "text", "text": "(The agent didn't return a reply.)"}]
    return JSONResponse({"parts": parts})


# Serve the chat UI (keep this mount last so /chat wins).
_static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
