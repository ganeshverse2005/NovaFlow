 NovaFlow

NovaFlow is a workflow automation platform that monitors Gmail inboxes and executes customizable automation pipelines based on incoming emails. 
It combines Google Workspace APIs with AI-powered processing using Groq LLMs, enabling users to summarize emails, generate intelligent responses, update Google services, and automate repetitive workflows.



## Features

### Gmail Automation

* Monitor unread Gmail messages in real time.
* Automatically filter emails by sender or subject.
* Mark processed emails as read.
* Execute custom workflows for each matching email.

### AI-Powered Processing

* Generate intelligent responses using Groq LLM.
* Summarize emails into concise bullet points.
* Use custom prompts for domain-specific automation.
* Insert AI-generated outputs into downstream workflow nodes.

### Google Workspace Integration

* Gmail (read & send emails)
* Google Sheets (append processed data)
* Google Drive (save files)
* Google Docs (generate documents)
* Google Calendar (create events)
* Google Meet (schedule meetings)
* Google Contacts (create contacts)
* Google Tasks (create tasks)
* Google Forms (read responses)
* YouTube Data API (retrieve videos/comments)

### Workflow Engine

* Visual node-based workflow execution.
* Fan-out execution supporting multiple downstream nodes.
* Configurable workflow nodes:

  * Listener
  * Filter
  * LLM
  * Summarizer
  * Scheduler
  * Email Sender
  * Google Sheets
  * Calendar
  * Meet
  * Drive
  * Docs
  * Contacts
  * Tasks
  * Forms
  * YouTube

### Live Monitoring

* Workflow execution history.
* Live activity feed.
* Workflow deployment API.
* Start/stop workflow engine.
* Runtime status monitoring.

---

## Tech Stack

### Backend

* Python
* FastAPI
* AsyncIO
* HTTPX

### AI

* Groq API
* Llama 3 models

### Google APIs

* Gmail API
* Calendar API
* Drive API
* Docs API
* Sheets API
* People API
* Tasks API
* Forms API
* YouTube Data API

### Authentication

* Google OAuth 2.0

## Configuration

Create a Google Cloud project and enable the required APIs:

* Gmail API
* Google Drive API
* Google Docs API
* Google Sheets API
* Google Calendar API
* Google People API
* Google Tasks API
* Google Forms API
* YouTube Data API

Download your OAuth credentials and place them in:

```
client_secret.json
```


## API Endpoints

| Endpoint           | Description                        |
| ------------------ | ---------------------------------- |
| `/`                | Home page                          |
| `/login`           | Google Sign-In                     |
| `/connect_gmail`   | Connect Gmail with required scopes |
| `/api/deploy`      | Deploy workflow                    |
| `/api/load`        | Load workflow                      |
| `/api/status`      | Workflow status                    |
| `/api/feed`        | Live execution feed                |
| `/api/history`     | Execution history                  |
| `/api/toggle_node` | Enable/disable workflow nodes      |
| `/api/stop`        | Stop workflow engine               |
| `/logout`          | Logout                             |

---

## Security

The following files should **never** be committed:

```
client_secret.json
db.json
run_history.json
.session_secret
.env
```

Store secrets securely and use environment variables where possible.


## Future Improvements

* React-based workflow editor
* Drag-and-drop node builder
* Multiple workflow support
* Slack integration
* Discord integration
* Microsoft Outlook support
* Webhook nodes
* Database persistence (PostgreSQL)
* Docker deployment
* User authentication dashboard
* Workflow scheduling with cron
* Analytics dashboard

