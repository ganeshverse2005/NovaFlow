
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload
from datetime import datetime, timedelta
import logging, json, re

logger = logging.getLogger(__name__)

def get_service(creds, api: str, version: str):
    return build(api, version, credentials=creds)

# -------------------- CALENDAR --------------------
async def create_calendar_event(creds, data: dict, email: dict, llm_response: str) -> str:
    service = get_service(creds, 'calendar', 'v3')
    title    = _res(data.get('title',    'Meeting from Email'), email, llm_response)
    desc     = _res(data.get('desc',     email.get('content','')[:500]), email, llm_response)
    location = _res(data.get('location', ''), email, llm_response)
    date_str = data.get('date', '')          # e.g. "2024-12-25" or blank = tomorrow
    time_str = data.get('time', '10:00')     # e.g. "14:30"
    duration = int(data.get('duration_min', 60))
    attendees= [e.strip() for e in data.get('attendees','').split(',') if e.strip()]

    # Parse start datetime
    try:
        if date_str:
            start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        else:
            start_dt = datetime.now().replace(hour=int(time_str.split(':')[0]),
                                               minute=int(time_str.split(':')[1])) + timedelta(days=1)
    except Exception:
        start_dt = datetime.now() + timedelta(days=1)

    end_dt = start_dt + timedelta(minutes=duration)

    event = {
        'summary': title,
        'description': desc,
        'location': location,
        'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'UTC'},
        'end':   {'dateTime': end_dt.isoformat(),   'timeZone': 'UTC'},
        'attendees': [{'email': a} for a in attendees],
        'conferenceData': {'createRequest': {'requestId': f"meet-{int(datetime.now().timestamp())}"}} if data.get('create_meet') else None,
    }
    # Remove None keys
    event = {k: v for k, v in event.items() if v is not None}

    conf_version = 1 if data.get('create_meet') else 0
    result = service.events().insert(
        calendarId='primary', body=event,
        conferenceDataVersion=conf_version
    ).execute()
    meet_link = result.get('conferenceData', {}).get('entryPoints', [{}])[0].get('uri', '')
    logger.info(f"[CALENDAR] Created event: {result.get('id')} meet={meet_link}")
    return meet_link or result.get('htmlLink', '')

# -------------------- DRIVE --------------------
async def save_to_drive(creds, data: dict, email: dict, llm_response: str) -> str:
    service = get_service(creds, 'drive', 'v3')
    filename  = _res(data.get('filename', 'Email_{{email.subject}}'), email, llm_response)
    content   = _res(data.get('content',  '{{email.content}}'),       email, llm_response)
    folder_id = data.get('folder_id', '')  # optional Drive folder ID
    mime      = 'text/plain'

    metadata = {'name': filename, 'mimeType': mime}
    if folder_id:
        metadata['parents'] = [folder_id]

    media = MediaInMemoryUpload(content.encode('utf-8'), mimetype=mime)
    result = service.files().create(body=metadata, media_body=media, fields='id,webViewLink').execute()
    link = result.get('webViewLink', '')
    logger.info(f"[DRIVE] Saved file: {filename} → {link}")
    return link

# -------------------- DOCS --------------------
async def create_google_doc(creds, data: dict, email: dict, llm_response: str) -> str:
    docs_service  = get_service(creds, 'docs', 'v1')
    drive_service = get_service(creds, 'drive', 'v3')
    title   = _res(data.get('title',   'Doc from Email: {{email.subject}}'), email, llm_response)
    content = _res(data.get('content', '{{llm.response}}'), email, llm_response)
    folder_id = data.get('folder_id', '')

    # Create blank doc
    doc = docs_service.documents().create(body={'title': title}).execute()
    doc_id = doc.get('documentId')

    # Insert content
    docs_service.documents().batchUpdate(
        documentId=doc_id,
        body={'requests': [{'insertText': {'location': {'index': 1}, 'text': content}}]}
    ).execute()

    # Move to folder if specified
    if folder_id:
        file = drive_service.files().get(fileId=doc_id, fields='parents').execute()
        drive_service.files().update(
            fileId=doc_id,
            addParents=folder_id,
            removeParents=','.join(file.get('parents', [])),
            fields='id,parents'
        ).execute()

    link = f"https://docs.google.com/document/d/{doc_id}/edit"
    logger.info(f"[DOCS] Created doc: {title} → {link}")
    return link

# -------------------- CONTACTS --------------------
async def create_contact(creds, data: dict, email: dict, llm_response: str) -> str:
    service = get_service(creds, 'people', 'v1')
    # Extract name from "Name <email@>" format
    raw_from = email.get('from', '')
    name_match = re.match(r'^"?([^"<]+)"?\s*<', raw_from)
    name = name_match.group(1).strip() if name_match else raw_from.split('@')[0]
    email_addr = re.search(r'<(.+?)>', raw_from)
    email_addr = email_addr.group(1) if email_addr else raw_from

    given = name.split()[0] if name.split() else name
    family = ' '.join(name.split()[1:]) if len(name.split()) > 1 else ''

    note = _res(data.get('note', 'Added from email: {{email.subject}}'), email, llm_response)

    body = {
        'names': [{'givenName': given, 'familyName': family}],
        'emailAddresses': [{'value': email_addr, 'type': 'work'}],
        'biographies': [{'value': note, 'contentType': 'TEXT_PLAIN'}],
    }
    phone = data.get('phone_placeholder', '')
    if phone:
        body['phoneNumbers'] = [{'value': _res(phone, email, llm_response)}]

    result = service.people().createContact(body=body).execute()
    resource = result.get('resourceName', '')
    logger.info(f"[CONTACTS] Created contact: {name} <{email_addr}>")
    return resource

# -------------------- TASKS --------------------
async def create_task(creds, data: dict, email: dict, llm_response: str) -> str:
    service   = get_service(creds, 'tasks', 'v1')
    title     = _res(data.get('title', '{{email.subject}}'), email, llm_response)
    notes     = _res(data.get('notes', 'From: {{email.from}}\n\n{{llm.response}}'), email, llm_response)
    due_days  = int(data.get('due_days', 3))
    list_id   = data.get('tasklist_id', '@default')

    due_dt = (datetime.utcnow() + timedelta(days=due_days)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    task = {'title': title, 'notes': notes, 'due': due_dt, 'status': 'needsAction'}

    result = service.tasks().insert(tasklist=list_id, body=task).execute()
    logger.info(f"[TASKS] Created task: {title} due in {due_days} days")
    return result.get('id', '')

# -------------------- MEET (via Calendar) --------------------
async def schedule_meet(creds, data: dict, email: dict, llm_response: str) -> str:
    """Creates a Calendar event WITH a Google Meet link."""
    data_with_meet = dict(data)
    data_with_meet['create_meet'] = True
    data_with_meet.setdefault('title', 'Meeting: {{email.subject}}')
    data_with_meet.setdefault('desc',  'Meeting requested via email from {{email.from}}\n\n{{llm.response}}')
    meet_link = await create_calendar_event(creds, data_with_meet, email, llm_response)
    logger.info(f"[MEET] Scheduled meeting: {meet_link}")
    return meet_link

# -------------------- FORMS (read responses) --------------------
async def get_form_responses(creds, data: dict) -> list:
    """Fetch latest responses from a Google Form."""
    service = get_service(creds, 'forms', 'v1')
    form_id = data.get('form_id', '')
    if not form_id:
        logger.warning("[FORMS] No form_id configured")
        return []
    result = service.forms().responses().list(formId=form_id).execute()
    responses = result.get('responses', [])
    logger.info(f"[FORMS] Got {len(responses)} responses from form {form_id}")
    return responses

# -------------------- YOUTUBE --------------------
async def get_youtube_updates(creds_or_key: str, data: dict) -> list:
    """Fetch latest videos or comments from a YouTube channel."""
    import httpx
    channel_id = data.get('channel_id', '')
    mode       = data.get('mode', 'videos')  # 'videos' or 'comments'
    api_key    = data.get('api_key', '')
    if not api_key or not channel_id:
        logger.warning("[YOUTUBE] Missing api_key or channel_id")
        return []

    async with httpx.AsyncClient(timeout=15) as client:
        if mode == 'videos':
            resp = await client.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={'key': api_key, 'channelId': channel_id,
                        'part': 'snippet', 'order': 'date', 'maxResults': 5, 'type': 'video'}
            )
        else:
            video_id = data.get('video_id', '')
            resp = await client.get(
                "https://www.googleapis.com/youtube/v3/commentThreads",
                params={'key': api_key, 'videoId': video_id,
                        'part': 'snippet', 'maxResults': 10, 'order': 'time'}
            )
        if resp.is_success:
            return resp.json().get('items', [])
        logger.error(f"[YOUTUBE] API error: {resp.status_code} {resp.text[:200]}")
        return []

# -------------------- PLACEHOLDER HELPER --------------------
def _res(template: str, email: dict, llm_response: str, summary: str = "") -> str:
    """Inline placeholder resolver for Google node helpers."""
    result = str(template)
    def sub(t, lit, esc, val):
        return t.replace(lit, val).replace(esc, val)
    result = sub(result, "{{llm.response}}",  r"\x7B\x7Bllm.response\x7D\x7D",  llm_response)
    result = sub(result, "{{summary}}",        r"\x7B\x7Bsummary\x7D\x7D",        summary or llm_response)
    result = sub(result, "{{email.from}}",     r"\x7B\x7Bemail.from\x7D\x7D",     email.get('from',''))
    result = sub(result, "{{email.to}}",       r"\x7B\x7Bemail.to\x7D\x7D",       email.get('to',''))
    result = sub(result, "{{email.subject}}",  r"\x7B\x7Bemail.subject\x7D\x7D",  email.get('subject',''))
    result = sub(result, "{{email.content}}",  r"\x7B\x7Bemail.content\x7D\x7D",  email.get('content',''))
    result = sub(result, "{{email.time}}",     r"\x7B\x7Bemail.time\x7D\x7D",     email.get('time',''))
    result = sub(result, "{{now}}",            r"\x7B\x7Bnow\x7D\x7D",            datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return result