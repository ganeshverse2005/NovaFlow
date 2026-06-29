import os
import json
import base64
import asyncio
import re
from datetime import datetime
from typing import List, Dict, Any, Optional
from email.mime.text import MIMEText
import logging

import httpx
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from pydantic import BaseModel
from google_nodes import (
    create_calendar_event, save_to_drive, create_google_doc,
    create_contact, create_task, schedule_meet,
    get_form_responses, get_youtube_updates
)

# -------------------- CONFIG --------------------
CLIENT_SECRETS_FILE = "client_secret.json"
PROFILE_REDIRECT = "http://localhost:8000/auth/callback"
GMAIL_REDIRECT = "http://localhost:8000/gmail/callback"

LOGIN_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile"
]

# All Google service scopes in one OAuth flow
GMAIL_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    # Gmail
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    # Sheets + Drive + Docs
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/documents",
    # Calendar + Meet
    "https://www.googleapis.com/auth/calendar",
    # Contacts
    "https://www.googleapis.com/auth/contacts",
    # Tasks
    "https://www.googleapis.com/auth/tasks",
    # Forms (read)
    "https://www.googleapis.com/auth/forms.responses.readonly",
]

DB_FILE = "db.json"
RUN_HISTORY_FILE = "run_history.json"

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------- APP INIT --------------------
app = FastAPI()
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", os.urandom(32).hex())
)
templates = Jinja2Templates(directory="templates")

runtime_state: Dict[str, Dict] = {}

# -------------------- DB HELPERS --------------------
def load_db():
    if not os.path.exists(DB_FILE):
        return {}
    with open(DB_FILE, "r") as f:
        try: return json.load(f)
        except: return {}

def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=4)

def load_history():
    if not os.path.exists(RUN_HISTORY_FILE):
        return []
    with open(RUN_HISTORY_FILE, "r") as f:
        try: return json.load(f)
        except: return []

def save_history(entry: dict):
    history = load_history()
    history.insert(0, entry)
    history = history[:100]  # keep last 100
    with open(RUN_HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)

# -------------------- CREDS HELPERS --------------------
def creds_to_dict(creds: Credentials) -> dict:
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes) if creds.scopes else []
    }

def dict_to_creds(d: dict) -> Credentials:
    return Credentials(
        token=d.get("token"),
        refresh_token=d.get("refresh_token"),
        token_uri=d.get("token_uri"),
        client_id=d.get("client_id"),
        client_secret=d.get("client_secret"),
        scopes=d.get("scopes")
    )

def get_flow(scopes, redirect_uri, state=None):
    return Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE, scopes=scopes,
        redirect_uri=redirect_uri, state=state
    )

def has_gmail_scopes(creds_dict: dict) -> bool:
    scopes = creds_dict.get("scopes") or []
    return any("gmail" in s for s in scopes)

def has_sheets_scopes(creds_dict: dict) -> bool:
    scopes = creds_dict.get("scopes") or []
    return any("spreadsheets" in s or "drive" in s for s in scopes)

# -------------------- EMAIL HELPERS --------------------
def parse_email_content(service, msg_id: str) -> Dict:
    try:
        message = service.users().messages().get(userId='me', id=msg_id, format='full').execute()
        headers = message['payload']['headers']
        email_data = {'from':'','to':'','subject':'','content':'','time':'','message_id':msg_id}
        for h in headers:
            name = h['name'].lower()
            if name == 'from': email_data['from'] = h['value']
            elif name == 'to': email_data['to'] = h['value']
            elif name == 'subject': email_data['subject'] = h['value']
            elif name == 'date': email_data['time'] = h['value']

        def extract_text(payload):
            if payload.get('mimeType') == 'text/plain' and 'data' in payload.get('body', {}):
                return base64.urlsafe_b64decode(payload['body']['data']).decode(errors='replace')
            for part in payload.get('parts', []):
                result = extract_text(part)
                if result: return result
            return ''
        email_data['content'] = extract_text(message['payload'])
        return email_data
    except Exception as e:
        logger.error(f"Error parsing email {msg_id}: {e}")
        return {'from':'Unknown','to':'Unknown','subject':'Unknown','content':'','time':str(datetime.now()),'message_id':msg_id}

def email_matches_filter(email: Dict, listener_data: Dict) -> bool:
    mode = listener_data.get('mode', 'all')
    if mode == 'all': return True
    if mode == 'sender': return listener_data.get('filter_from','').lower() in email['from'].lower()
    if mode == 'subject': return listener_data.get('filter_subject','').lower() in email['subject'].lower()
    if mode == 'strict':
        return (listener_data.get('filter_from','').lower() in email['from'].lower() and
                listener_data.get('filter_subject','').lower() in email['subject'].lower())
    return True

async def fetch_unread_emails(service) -> List[Dict]:
    result = service.users().messages().list(userId='me', q="is:unread", maxResults=5).execute()
    messages = result.get('messages', [])
    emails = []
    for m in messages:
        email = parse_email_content(service, m['id'])
        emails.append(email)
        service.users().messages().modify(userId='me', id=m['id'], body={"removeLabelIds": ["UNREAD"]}).execute()
    return emails

async def send_email_via_gmail(service, to_email: str, subject: str, body_text: str):
    message = MIMEText(body_text)
    message['to'] = to_email
    message['subject'] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    service.users().messages().send(userId='me', body={'raw': raw}).execute()
    logger.info(f"Email sent to {to_email}")

# -------------------- GROQ LLM --------------------
async def call_groq_llm(api_key: str, prompt: str, email: Dict, mode: str = "custom") -> str:
    system_message = "You are an intelligent email assistant. Follow the instructions precisely and be concise."

    if mode == "summarize":
        user_message = f"""Summarize this email in 3-5 bullet points. Be concise and extract key information.

From: {email['from']}
Subject: {email['subject']}
Content:
{email['content']}"""
    else:
        user_message = f"""Instructions: {prompt}

Email Data:
- From: {email['from']}
- To: {email['to']}
- Subject: {email['subject']}
- Time: {email['time']}
- Content:
{email['content']}

Follow the instructions above."""

    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message}
        ],
        "max_tokens": 1024,
        "temperature": 0.7
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post("https://api.groq.com/openai/v1/chat/completions", json=payload, headers=headers)
            if not resp.is_success:
                logger.error(f"Groq HTTP {resp.status_code}: {resp.text}")
                if resp.status_code == 400:
                    payload["model"] = "llama3-70b-8192"
                    resp = await client.post("https://api.groq.com/openai/v1/chat/completions", json=payload, headers=headers)
                    if not resp.is_success:
                        return f"[LLM Error {resp.status_code}: {resp.text[:200]}]"
            return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Groq API error: {e}", exc_info=True)
        return f"[LLM Error: {e}]"

# -------------------- GOOGLE SHEETS --------------------
async def append_to_sheet(user_email: str, spreadsheet_id: str, range_: str, values: List[List]):
    user_data = runtime_state.get(user_email, {})
    creds_dict = user_data.get("creds")
    if not creds_dict:
        logger.error("[SHEETS] No credentials found")
        return
    if not has_sheets_scopes(creds_dict):
        logger.error(
            "[SHEETS] Missing Sheets/Drive scopes. "
            "User must re-authorize via /connect_gmail to grant Sheets access."
        )
        raise PermissionError("Missing Google Sheets scopes. Please re-authorize via /connect_gmail.")
    creds = dict_to_creds(creds_dict)
    service = build('sheets', 'v4', credentials=creds)
    body = {'values': values}
    result = service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id, range=range_,
        valueInputOption='USER_ENTERED', body=body
    ).execute()
    logger.info(f"[SHEETS] Appended {len(values)} row(s) to {spreadsheet_id} → {result.get('updates',{})}")

# -------------------- PLACEHOLDER RESOLUTION --------------------
def resolve_placeholders(template: str, email: Dict, llm_response: str, summary: str = "") -> str:
    result = template
    # Support BOTH literal {{x}} AND JS unicode-escaped \x7B\x7Bx\x7D\x7D forms
    # (frontend stores placeholders as unicode escapes to avoid Jinja2 rendering them)
    def sub(tpl, old_lit, old_esc, value):
        return tpl.replace(old_lit, value).replace(old_esc, value)

    result = sub(result, "{{llm.response}}",   r"{{llm.response}}",   llm_response)
    result = sub(result, "{{summary}}",         r"{{summary}}",         summary or llm_response)
    result = sub(result, "{{email.from}}",      r"{{email.from}}",      email.get('from', ''))
    result = sub(result, "{{email.to}}",        r"{{email.to}}",        email.get('to', ''))
    result = sub(result, "{{email.subject}}",   r"{{email.subject}}",   email.get('subject', ''))
    result = sub(result, "{{email.content}}",   r"{{email.content}}",   email.get('content', ''))
    result = sub(result, "{{email.time}}",      r"{{email.time}}",      email.get('time', ''))
    result = sub(result, "{{now}}",             r"{{now}}",             datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return result

# -------------------- WORKFLOW ENGINE --------------------
async def execute_node(node_id: int, nodes: List[Dict], adj: Dict[int, List[int]],
                       email: Dict, service, user_email: str,
                       llm_response: str, summary: str, run_log: dict,
                       visited: set):
    """Recursively execute a node and all its downstream nodes (supports fan-out)."""
    if node_id in visited:
        return llm_response, summary
    visited.add(node_id)

    node = next((n for n in nodes if n['id'] == node_id), None)
    if not node:
        return llm_response, summary

    if not node.get('enabled', True):
        logger.info(f"Skipping disabled node: {node['type']} (id={node_id})")
    else:
        node_type = node['type']
        node_data = node.get('data', {})
        logger.info(f"[NODE] Executing: {node_type} (id={node_id})")

        try:
            if node_type == 'llm':
                api_key = node_data.get('api_key', '')
                prompt  = node_data.get('prompt', '')
                if not api_key:
                    llm_response = "[No API key configured]"
                else:
                    llm_response = await call_groq_llm(api_key, prompt, email, mode="custom")
                run_log["steps"].append({"node": "llm", "result": llm_response[:100]})

            elif node_type == 'summarizer':
                api_key = node_data.get('api_key', '')
                if not api_key:
                    summary = "[No API key for summarizer]"
                else:
                    summary = await call_groq_llm(api_key, "", email, mode="summarize")
                llm_response = llm_response or summary
                run_log["steps"].append({"node": "summarizer", "result": summary[:100]})

            elif node_type == 'filter':
                field     = node_data.get('field', 'subject')
                operator  = node_data.get('operator', 'contains')
                value     = node_data.get('value', '').lower()
                field_val = email.get(field, '').lower()
                matched   = False
                if operator == 'contains':     matched = value in field_val
                elif operator == 'not_contains': matched = value not in field_val
                elif operator == 'equals':     matched = field_val == value
                elif operator == 'starts_with': matched = field_val.startswith(value)
                run_log["steps"].append({"node": "filter", "matched": matched})
                if not matched:
                    logger.info(f"[FILTER] Condition not met — branch stopped.")
                    return llm_response, summary   # stop this branch only

            elif node_type == 'scheduler':
                delay = int(node_data.get('delay_seconds', 60))
                logger.info(f"[SCHEDULER] Waiting {delay}s")
                run_log["steps"].append({"node": "scheduler", "delay": delay})
                await asyncio.sleep(delay)

            elif node_type == 'sender':
                raw_to   = node_data.get('to_email', '')
                raw_subj = node_data.get('subject', '')
                raw_body = node_data.get('body', '')
                to_email = resolve_placeholders(raw_to,   email, llm_response, summary)
                subject  = resolve_placeholders(raw_subj, email, llm_response, summary)
                body     = resolve_placeholders(raw_body, email, llm_response, summary)
                logger.info(f"[SENDER] to={repr(to_email)} subject={repr(subject)}")
                if not to_email.strip():
                    logger.error("[SENDER] to_email is empty — check Sender node config")
                    run_log["steps"].append({"node": "sender", "error": "to_email empty"})
                    run_log["status"] = "partial_error"
                else:
                    await send_email_via_gmail(service, to_email.strip(), subject, body)
                    run_log["steps"].append({"node": "sender", "to": to_email})

            elif node_type == 'sheets':
                spreadsheet_id = node_data.get('spreadsheet_id', '')
                sheet_range    = node_data.get('range', 'Sheet1!A1')
                cols           = node_data.get('columns', 'from,subject,summary,time')
                col_list       = [c.strip() for c in cols.split(',')]
                row = []
                for col in col_list:
                    if col == 'from':         row.append(email.get('from', ''))
                    elif col == 'to':         row.append(email.get('to', ''))
                    elif col == 'subject':    row.append(email.get('subject', ''))
                    elif col == 'content':    row.append(email.get('content', '')[:500])
                    elif col == 'time':       row.append(email.get('time', ''))
                    elif col == 'summary':    row.append(summary or llm_response)
                    elif col == 'llm_response': row.append(llm_response)
                    elif col == 'now':        row.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    else:                     row.append(col)
                if spreadsheet_id:
                    await append_to_sheet(user_email, spreadsheet_id, sheet_range, [row])
                    run_log["steps"].append({"node": "sheets", "sheet": spreadsheet_id})

            # -------- NEW GOOGLE SERVICE NODES --------
            elif node_type == 'calendar':
                creds = dict_to_creds(runtime_state.get(user_email, {}).get("creds", {}))
                meet_link = await create_calendar_event(creds, node_data, email, llm_response)
                run_log["steps"].append({"node": "calendar", "meet_link": meet_link})
                logger.info(f"[CALENDAR] Event created. Meet: {meet_link}")

            elif node_type == 'meet':
                creds = dict_to_creds(runtime_state.get(user_email, {}).get("creds", {}))
                meet_link = await schedule_meet(creds, node_data, email, llm_response)
                run_log["steps"].append({"node": "meet", "link": meet_link})
                logger.info(f"[MEET] Scheduled: {meet_link}")

            elif node_type == 'drive':
                creds = dict_to_creds(runtime_state.get(user_email, {}).get("creds", {}))
                file_link = await save_to_drive(creds, node_data, email, llm_response)
                run_log["steps"].append({"node": "drive", "link": file_link})
                logger.info(f"[DRIVE] File saved: {file_link}")

            elif node_type == 'docs':
                creds = dict_to_creds(runtime_state.get(user_email, {}).get("creds", {}))
                doc_link = await create_google_doc(creds, node_data, email, llm_response)
                run_log["steps"].append({"node": "docs", "link": doc_link})
                logger.info(f"[DOCS] Doc created: {doc_link}")

            elif node_type == 'contacts':
                creds = dict_to_creds(runtime_state.get(user_email, {}).get("creds", {}))
                resource = await create_contact(creds, node_data, email, llm_response)
                run_log["steps"].append({"node": "contacts", "resource": resource})

            elif node_type == 'tasks':
                creds = dict_to_creds(runtime_state.get(user_email, {}).get("creds", {}))
                task_id = await create_task(creds, node_data, email, llm_response)
                run_log["steps"].append({"node": "tasks", "task_id": task_id})

            elif node_type == 'forms':
                creds = dict_to_creds(runtime_state.get(user_email, {}).get("creds", {}))
                responses = await get_form_responses(creds, node_data)
                run_log["steps"].append({"node": "forms", "response_count": len(responses)})

            elif node_type == 'youtube':
                items = await get_youtube_updates(None, node_data)
                run_log["steps"].append({"node": "youtube", "items": len(items)})

        except Exception as e:
            logger.error(f"[NODE] {node_type} error: {e}", exc_info=True)
            run_log["steps"].append({"node": node_type, "error": str(e)})
            run_log["status"] = "partial_error"

    # Execute ALL downstream nodes (fan-out support)
    for next_id in adj.get(node_id, []):
        llm_response, summary = await execute_node(
            next_id, nodes, adj, email, service, user_email,
            llm_response, summary, run_log, visited
        )

    return llm_response, summary


async def process_email_through_workflow(email: Dict, nodes: List[Dict], edges: List[Dict], service, user_email: str):
  
    adj: Dict[int, List[int]] = {}
    for edge in edges:
        src, tgt = edge['source'], edge['target']
        adj.setdefault(src, []).append(tgt)
        logger.info(f"[GRAPH] Edge: {src} → {tgt}")

    logger.info(f"[GRAPH] Full adjacency: {adj}")

    listener_nodes = [n for n in nodes if n['type'] == 'listener' and n.get('enabled', True)]
    if not listener_nodes:
        logger.warning("[WORKFLOW] No enabled listener node found")
        return

    listener = listener_nodes[0]
    if not email_matches_filter(email, listener.get('data', {})):
        logger.info(f"[FILTER] Email skipped: {email['subject']}")
        return

    logger.info(f"[WORKFLOW] Starting for email: {email['subject']}")

    run_log = {
        "timestamp": datetime.now().isoformat(),
        "email_from": email['from'],
        "email_subject": email['subject'],
        "steps": [],
        "status": "success"
    }

    llm_response, summary = await execute_node(
        listener['id'], nodes, adj, email, service, user_email,
        "", "", run_log, set()
    )

    save_history(run_log)

  
    if user_email in runtime_state:
        feed = runtime_state[user_email].get("feed", [])
        feed.insert(0, {
            "email": email,
            "llm_response": llm_response,
            "summary": summary,
            "timestamp": datetime.now().isoformat(),
            "status": run_log["status"]
        })
        runtime_state[user_email]["feed"] = feed[:20]

async def workflow_engine(user_email: str):
    logger.info(f"[ENGINE] Started for {user_email}")
    while runtime_state.get(user_email, {}).get("active", False):
        try:
            user_data = runtime_state.get(user_email, {})
            creds_dict = user_data.get("creds")

            if not creds_dict:
                runtime_state[user_email]["active"] = False
                runtime_state[user_email]["error"] = "no_credentials"
                break

            if not has_gmail_scopes(creds_dict):
                runtime_state[user_email]["active"] = False
                runtime_state[user_email]["error"] = "insufficient_scopes"
                break

            creds = dict_to_creds(creds_dict)
            service = build('gmail', 'v1', credentials=creds)

            db = load_db()
            workflow = db.get(user_email, {"nodes": [], "edges": []})
            nodes = workflow.get("nodes", [])
            edges = workflow.get("edges", [])

            if not nodes:
                await asyncio.sleep(10)
                continue

            emails = await fetch_unread_emails(service)
            runtime_state[user_email]['emails'] = emails
            runtime_state[user_email].pop("error", None)

            if emails:
                for email in emails:
                    await process_email_through_workflow(email, nodes, edges, service, user_email)
            else:
                logger.info(f"[ENGINE] No new emails for {user_email}")

        except Exception as e:
            err_str = str(e)
            if "insufficientPermissions" in err_str or "403" in err_str:
                runtime_state[user_email]["active"] = False
                runtime_state[user_email]["error"] = "insufficient_scopes"
                break
            logger.error(f"[ENGINE] Error: {e}", exc_info=True)

        await asyncio.sleep(10)

    logger.info(f"[ENGINE] Stopped for {user_email}")


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    user = request.session.get("user")
    has_gmail = request.session.get("has_gmail_access", False)
    return templates.TemplateResponse("index.html", {
        "request": request, "user": user, "has_gmail": has_gmail
    })

@app.get("/login")
def login():
    flow = get_flow(LOGIN_SCOPES, PROFILE_REDIRECT)
    auth_url, _ = flow.authorization_url(prompt="consent")
    return RedirectResponse(auth_url)

@app.get("/auth/callback")
def auth_callback(request: Request, code: str):
    flow = get_flow(LOGIN_SCOPES, PROFILE_REDIRECT)
    flow.fetch_token(code=code)
    creds = flow.credentials
    service = build("oauth2", "v2", credentials=creds)
    user_info = service.userinfo().get().execute()
    request.session["user"] = user_info
    request.session["creds"] = creds_to_dict(creds)
    runtime_state[user_info["email"]] = {"active": False, "creds": request.session["creds"], "emails": [], "feed": []}
    return RedirectResponse("/")

@app.get("/connect_gmail")
def connect_gmail(request: Request):
    flow = get_flow(GMAIL_SCOPES, GMAIL_REDIRECT)
    auth_url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
    request.session["auth_state"] = state
    return RedirectResponse(auth_url)

@app.get("/gmail/callback")
async def gmail_callback(request: Request, code: str, state: Optional[str] = None, background_tasks: BackgroundTasks = None):
    flow = get_flow(GMAIL_SCOPES, GMAIL_REDIRECT, state=state)
    flow.fetch_token(code=code)
    creds = flow.credentials
    user = request.session.get("user")
    if not user:
        return HTMLResponse("Session expired. <a href='/login'>Login again</a>")
    request.session["has_gmail_access"] = True
    creds_dict = creds_to_dict(creds)
    request.session["creds"] = creds_dict
    if user["email"] not in runtime_state:
        runtime_state[user["email"]] = {"emails": [], "feed": []}
    runtime_state[user["email"]]["creds"] = creds_dict
    runtime_state[user["email"]]["active"] = True
    if background_tasks:
        background_tasks.add_task(workflow_engine, user["email"])
    else:
        asyncio.create_task(workflow_engine(user["email"]))
    return RedirectResponse("/")

@app.post("/api/stop")
def stop_engine(request: Request):
    user = request.session.get("user")
    if user and user["email"] in runtime_state:
        runtime_state[user["email"]]["active"] = False
        return {"status": "stopped"}
    return {"status": "error"}

@app.post("/api/deploy")
async def deploy_workflow(request: Request, background_tasks: BackgroundTasks):
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")
    data = await request.json()
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])
    db = load_db()
    db[user["email"]] = {"nodes": nodes, "edges": edges}
    save_db(db)
    email = user["email"]
    if email not in runtime_state:
        runtime_state[email] = {"emails": [], "active": False, "feed": []}
    if not runtime_state[email].get("active", False):
        runtime_state[email]["active"] = True
        background_tasks.add_task(workflow_engine, email)
    else:
        runtime_state[email]["active"] = True
    return {"status": "deployed", "node_count": len(nodes), "edge_count": len(edges)}

@app.get("/api/load")
def load_workflow(request: Request):
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")
    db = load_db()
    return JSONResponse(db.get(user["email"], {"nodes": [], "edges": []}))

@app.get("/api/status")
def get_status(request: Request):
    user = request.session.get("user")
    if user and user["email"] in runtime_state:
        state = runtime_state[user["email"]]
        return {
            "active": state.get("active", False),
            "email_count": len(state.get("emails", [])),
            "error": state.get("error"),
            "has_gmail": request.session.get("has_gmail_access", False)
        }
    return {"active": False, "email_count": 0, "error": None, "has_gmail": False}

@app.get("/api/feed")
def get_feed(request: Request):
    user = request.session.get("user")
    if user and user["email"] in runtime_state:
        return runtime_state[user["email"]].get("feed", [])
    return []

@app.get("/api/history")
def get_history(request: Request):
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401)
    return load_history()

@app.post("/api/toggle_node")
async def toggle_node(request: Request):
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401)
    data = await request.json()
    node_id = data.get("node_id")
    enabled = data.get("enabled", True)
    db = load_db()
    workflow = db.get(user["email"], {"nodes": [], "edges": []})
    for node in workflow["nodes"]:
        if node["id"] == node_id:
            node["enabled"] = enabled
            break
    db[user["email"]] = workflow
    save_db(db)
    return {"status": "ok", "node_id": node_id, "enabled": enabled}

@app.get("/api/groq_models")
async def list_groq_models(api_key: str):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://api.groq.com/openai/v1/models",
                                    headers={"Authorization": f"Bearer {api_key}"})
            if resp.is_success:
                return {"models": sorted([m["id"] for m in resp.json().get("data", [])])}
            return {"error": resp.text}
    except Exception as e:
        return {"error": str(e)}

@app.get("/logout")
def logout(request: Request):
    user = request.session.get("user")
    if user and user["email"] in runtime_state:
        runtime_state[user["email"]]["active"] = False
    request.session.clear()
    return RedirectResponse("/")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)