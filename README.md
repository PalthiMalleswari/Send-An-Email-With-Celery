# Send-An-Email-With-Celery

# Celery + Redis + Email — Complete Reference Notes

A personal reference for the Django + Celery + Redis background-task stack.
Covers concepts, setup, tasks, retries, timeouts, resource monitoring, and a
full troubleshooting section with every error and its fix.

---

## Table of contents

1. [Core concepts & mental model](#1-core-concepts--mental-model)
2. [The three (really four) actors](#2-the-three-really-four-actors)
3. [Project setup from scratch](#3-project-setup-from-scratch)
4. [Writing and calling tasks](#4-writing-and-calling-tasks)
5. [Running the processes](#5-running-the-processes)
6. [Emailing a workbook (the main feature)](#6-emailing-a-workbook-the-main-feature)
7. [Tracking status (for React polling)](#7-tracking-status-for-react-polling)
8. [Retries](#8-retries)
9. [Timeouts](#9-timeouts)
10. [Execution order: how retries + timeouts chain](#10-execution-order-how-retries--timeouts-chain)
11. [Broker connection failures & limits](#11-broker-connection-failures--limits)
12. [Monitoring resources with htop](#12-monitoring-resources-with-htop)
13. [Troubleshooting — every error & fix](#13-troubleshooting--every-error--fix)
14. [Settings cheat-sheet](#14-settings-cheat-sheet)
15. [Golden rules](#15-golden-rules)

---

## 1. Core concepts & mental model

**The problem Celery solves:** Django requests are meant to be fast
(milliseconds). Slow work — sending email, building a 40-second Excel file,
calling a slow API — should NOT run inside the request, because it blocks the
web worker from serving anyone else.

**The idea:** don't do slow work during the request. Write down "this needs
doing" somewhere, respond to the user instantly, and let a *separate* program
do the actual work in the background.

**Restaurant analogy:**
- **Waiter** writes your order on a ticket, doesn't cook, goes back to serving → **Django view**
- **Ticket rail** where orders hang waiting → **broker (Redis)**
- **Cook** takes tickets and makes the food → **Celery worker**

The waiter and cook work at the same time; the waiter never waits for cooking.
That's how Celery frees your web workers.

---

## 2. The three (really four) actors

| Actor | What it is | Role |
|-------|-----------|------|
| **Django view** | your web code | hands off slow work, responds instantly |
| **Broker (Redis)** | a separate server | holds the queue of pending tasks |
| **Celery worker** | a separate program | picks up tasks and runs them |
| **Result backend (Redis)** | a store | keeps task outcomes so you can poll status |

**Key mental shift:** the web server and the Celery worker are **two separate
running programs**. You start them separately. They only talk through the
broker. A slow task in the worker can never block the web server.

```
React/Browser
      │  click
      ▼
Django (runserver) ──► Redis (broker) ──► Celery worker
 returns instantly       holds job          does slow work
      ▲                                          │
      └────── React polls status ◄── Redis (result backend) ◄┘
```

**Worker internals:** starting one worker actually creates multiple OS
processes — one **MainProcess** (manager: talks to Redis, hands out work) and
several **child processes** (e.g. `ForkPoolWorker-8`) that run your task code.
By default one child per CPU core. Tasks run in the *children*, not the main.

---

## 3. Project setup from scratch

```bash
# 1. Folder + virtual env
mkdir report_mailer && cd report_mailer
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# 2. Install
pip install django celery redis openpyxl

# 3. Redis SERVER (the pip 'redis' is only the client!)
docker run -d -p 6379:6379 --name my-redis redis
redis-cli ping                    # must return PONG

# 4. Django project + app
django-admin startproject config .
python manage.py startapp reports
```

Register the app in `config/settings.py`:
```python
INSTALLED_APPS = [
    # ...defaults...
    "reports",
]
```

**`config/celery.py`:**
```python
import os
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
app = Celery("config")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
```

**`config/__init__.py`:**
```python
from .celery import app as celery_app
__all__ = ("celery_app",)
```

**Settings** (`config/settings.py`):
```python
CELERY_BROKER_URL = "redis://localhost:6379/0"
CELERY_RESULT_BACKEND = "redis://localhost:6379/0"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"

# Email — console backend prints emails to the worker terminal (great for learning)
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = "reports@myapp.com"
```

---

## 4. Writing and calling tasks

**A task** is a normal function wrapped with `@shared_task`:
```python
# reports/tasks.py
from celery import shared_task

@shared_task
def add(x, y):
    return x + y
```

**Calling it — the single most important distinction:**
```python
add(2, 3)          # runs NORMALLY, here and now, BLOCKING. Returns 5.
add.delay(2, 3)    # sends to broker, returns INSTANTLY with a tracking object.
```

`.delay()` = "run this in the background." Returns an `AsyncResult` (a receipt).

**`bind=True` and `self`** — these ALWAYS go together:
```python
@shared_task(bind=True)
def my_task(self, email):     # self is REQUIRED when bind=True
    ...
```
`bind=True` tells Celery to pass the task instance as the first argument
(`self`), which you need to call `self.retry(...)`. If you set `bind=True` but
forget `self` in the signature, you get argument-collision errors.

**Arguments must be JSON-serializable** — numbers, strings, lists, dicts.
```python
build_report.delay(user)         # ✗ model object — fails to serialize
build_report.delay(user.id)      # ✓ pass the id, load the object inside the task
```

---

## 5. Running the processes

Three (or four) terminals:

```bash
# Terminal 1 — Redis (already running from docker, or:)
redis-server

# Terminal 2 — Django
python manage.py runserver

# Terminal 3 — Celery worker
celery -A config worker --loglevel=info
#   Windows: add  --pool=solo
#   Limit CPU/RAM use: add  --concurrency=2
```

**CRITICAL: Celery does NOT auto-reload.** Every time you edit a task, you must
**stop (Ctrl+C) and restart the worker**, or it keeps running old code.

**Firing tasks from a shell — use Django's shell, not plain python:**
```bash
python manage.py shell        # ✓ loads settings → uses Redis
# NOT: python              →  ✗ no settings → Celery defaults to RabbitMQ!
```
```python
>>> from reports.tasks import add
>>> add.delay(2, 3)
```

---

## 6. Emailing a workbook (the main feature)

```python
# reports/tasks.py
import io
from celery import shared_task
from openpyxl import Workbook
from django.core.mail import EmailMessage

@shared_task(bind=True, max_retries=3)
def build_and_email_workbook(self, recipient_email, title="Report"):
    # 1) build workbook in memory
    wb = Workbook()
    ws = wb.active
    ws.title = "Sales"
    ws.append(["Product", "Region", "Units", "Revenue"])
    for row in [["Widget A", "North", 120, 2400],
                ["Widget B", "South", 90, 1800]]:
        ws.append(row)

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    # 2) attach to email
    email = EmailMessage(
        subject=f"Your {title} is ready",
        body="Hi,\n\nYour report is attached.",
        to=[recipient_email],
    )
    email.attach(
        filename=f"{title}.xlsx",
        content=buffer.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    # 3) send
    email.send()
    return f"Sent {title}.xlsx to {recipient_email}"
```

Notes:
- `io.BytesIO()` = in-memory file, so nothing clutters the disk. `getvalue()`
  gives the raw bytes to attach.
- With the console email backend, the whole email (incl. base64 attachment)
  **prints in the worker terminal** — proof it worked.

**Switching to real email** — only settings change, task code stays identical:
```python
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = "smtp.sendgrid.net"
EMAIL_PORT = 587
EMAIL_USE_TLS = True
EMAIL_HOST_USER = "apikey"
EMAIL_HOST_PASSWORD = os.environ["EMAIL_PASSWORD"]   # secrets in env vars!
```

---

## 7. Tracking status (for React polling)

**Views:**
```python
import json
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt   # local testing only
from celery.result import AsyncResult
from kombu.exceptions import OperationalError
from .tasks import build_and_email_workbook

@csrf_exempt
def start_report(request):
    data = json.loads(request.body or "{}")
    email = data.get("email")
    if not email:
        return JsonResponse({"error": "email is required"}, status=400)
    try:
        task = build_and_email_workbook.delay(email, title="Sales Report")
    except OperationalError:                 # broker down at enqueue time
        return JsonResponse({"error": "Service unavailable, retry"}, status=503)
    return JsonResponse({"task_id": task.id}, status=202)

def report_status(request, task_id):
    result = AsyncResult(task_id)
    payload = {"task_id": task_id, "state": result.state}
    if result.ready():
        if result.successful():
            payload["result"] = result.result
        else:
            payload["error"] = str(result.result)
    return JsonResponse(payload)
```

**AsyncResult states:** `PENDING` → `STARTED` → `RETRY` → `SUCCESS` / `FAILURE`.

**Sending the payload correctly from the frontend / curl** (common
"email is missing" causes):
```javascript
fetch(url, {
  method: "POST",
  headers: { "Content-Type": "application/json" },   // required
  body: JSON.stringify({ email: "user@example.com" }) // must stringify; key must be "email"
});
```
```bash
# Linux/Mac
curl -X POST .../start/ -H "Content-Type: application/json" -d '{"email":"user@example.com"}'
# Windows cmd (escape quotes)
curl -X POST .../start/ -H "Content-Type: application/json" -d "{\"email\":\"user@example.com\"}"
```

---

## 8. Retries

```python
@shared_task(bind=True, max_retries=3, default_retry_delay=5)
def build_report(self, email):
    try:
        do_work(email)
    except (ConnectionError, TimeoutError) as exc:   # TRANSIENT → retry
        raise self.retry(exc=exc)
    except Exception:                                 # BUG → fail, don't retry
        logger.exception("Report failed for %s", email)
        raise
```

- `max_retries=3` → up to 3 retries after the first attempt (4 runs total),
  then `MaxRetriesExceededError`.
- `default_retry_delay=5` → wait 5s before each retry (fixed).
- **Only retry TRANSIENT failures.** A bug like `NameError` fails identically
  every time — retrying just wastes resources.
- A retry only happens if YOUR code calls `self.retry()`. `max_retries` alone
  does nothing.

**Backoff (growing delay) — overrides `default_retry_delay`:**
```python
@shared_task(bind=True, max_retries=3,
             retry_backoff=2,        # start 2s: 2 → 4 → 8 ...
             retry_backoff_max=30)   # cap the wait at 30s
```
- `retry_backoff=True` uses base 1s and default cap 600s — this is why a retry
  once said "Retry in 180s". Set `retry_backoff` to a number and
  `retry_backoff_max` to control it.
- Per-retry override: `raise self.retry(exc=exc, countdown=10)` → wait exactly 10s.

---

## 9. Timeouts

```python
from celery.exceptions import SoftTimeLimitExceeded

@shared_task(bind=True, soft_time_limit=30, time_limit=45)
def build_report(self, email):
    try:
        do_slow_work()
    except SoftTimeLimitExceeded:
        cleanup()        # runs — you get to log/clean up
        raise
```

- **`soft_time_limit`** (30s): raises `SoftTimeLimitExceeded` **inside** the
  task → catchable → clean up, log, or retry.
- **`time_limit`** (45s): **hard kill** of the worker child process. No
  exception, no cleanup. Used as a backstop.
- **Rule: `soft_time_limit` MUST be < `time_limit`**, or the process is killed
  before the catchable exception can fire.
- **Timeout ≠ retry.** A timeout stops one run; it only retries if you catch
  `SoftTimeLimitExceeded` and call `self.retry()`.

**Broker enqueue timeout** (how long `.delay()` waits to queue):
```python
CELERY_BROKER_TRANSPORT_OPTIONS = {"socket_timeout": 5}
```

---

## 10. Execution order: how retries + timeouts chain

Two levels:
- **Within one attempt:** `soft_time_limit`, `time_limit` (police a single run).
- **Across attempts:** `max_retries`, `default_retry_delay` (control reruns).

Per attempt:
1. Task starts, clock starts.
2. Finishes before `soft_time_limit` → **SUCCESS**, done.
3. Hits `soft_time_limit` → `SoftTimeLimitExceeded` raised inside task → your
   `except` catches it.
4. Your code calls `self.retry()`:
   - retries remaining → wait `default_retry_delay` (or backoff), rerun (step 1)
   - `max_retries` reached → `MaxRetriesExceededError` → **FAILURE**
5. If a run passes `time_limit` without the soft limit stopping it → worker
   child is **force-killed** (no retry, your code is dead).

```
Attempt 1: run ─30s─► SoftTimeLimitExceeded ─► self.retry() (1/3)
           wait 5s
Attempt 2: run ─30s─► SoftTimeLimitExceeded ─► self.retry() (2/3)
           wait 5s
Attempt 3: run ─30s─► SoftTimeLimitExceeded ─► self.retry() (3/3)
           wait 5s
Attempt 4: run ─30s─► SoftTimeLimitExceeded ─► self.retry()
                      → max_retries hit → MaxRetriesExceededError → FAILURE
```
`time_limit` (45s) only appears if one run blows past 30s without the soft
limit stopping it.

---

## 11. Broker connection failures & limits

Two different failure moments:
- **Broker down when the WORKER starts** → retry loop `(4/100)`. Cap it:
  ```python
  CELERY_BROKER_CONNECTION_MAX_RETRIES = 5   # default 100; 0 = fail immediately
  CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
  CELERY_BROKER_CONNECTION_TIMEOUT = 4       # seconds per attempt
  ```
- **Broker down when the VIEW calls `.delay()`** → `.delay()` raises
  `kombu.exceptions.OperationalError`. Catch it (see §7) and return 503.

Capping retries only changes how gracefully it fails — it does NOT make Celery
work without the broker. You must start Redis.

---

## 12. Monitoring resources with htop

```bash
sudo apt install htop       # or: brew install htop
```

Test task that burns CPU + RAM so there's something to see:
```python
@shared_task
def heavy():
    total = 0
    for i in range(10_000_000):
        total += i * i
    big = [0] * 20_000_000     # ~150MB
    time.sleep(20)             # hold it so you can watch
    return total
```

Workflow:
1. Terminal A: `celery -A config worker --loglevel=info`
2. Terminal B: `htop` → press **F4**, type `celery` to filter.
   You'll see MainProcess + several children (`ForkPoolWorker-N`).
3. Terminal C: `heavy.delay()` (a few times).
4. Watch a **child** process: **CPU%** climbs during the loop; **RES** (real
   RAM) climbs ~150MB when the list is created; both drop when it finishes.

**Columns:**
- **CPU%** — % of one core (can exceed 100% across cores).
- **RES** — real RAM used right now (the number that matters).
- **VIRT** — reserved virtual memory; usually huge, mostly ignore.
- **Command** — confirms it's a celery process.

**Bounding resource use** — limit child processes:
```bash
celery -A config worker --concurrency=2
```
Now only 2 children ever work at once, no matter how many tasks queue. This is
the main lever against "background jobs eating all the CPU/RAM."

Single-process snapshot:
```bash
ps -p <pid> -o %cpu,%mem,rss,cmd
top -p <pid>
```

---

## 13. Troubleshooting — every error & fix

### A. `Error 111 connecting to localhost:6379. Connection refused`
**Meaning:** nothing is listening on Redis's port — Redis isn't running.
**Fix:**
```bash
redis-cli ping            # if not PONG:
docker start my-redis     # or: redis-server
```

### B. Retry loop `Trying again ... (4/100)`
**Meaning:** worker can't reach the broker and keeps retrying.
**Fix:** start Redis (A). To cap the noise:
```python
CELERY_BROKER_CONNECTION_MAX_RETRIES = 5
```

### C. Traceback goes through `amqp` / `pyamqp` + Connection refused
**Meaning:** Celery is trying **RabbitMQ (port 5672)**, not Redis — because
settings weren't loaded, so it used the default broker.
**Cause:** you ran a plain `python` shell and did `import tasks`.
**Fix:** use Django's shell so settings load:
```bash
python manage.py shell
>>> from reports.tasks import heavy
>>> heavy.delay()
# verify: from django.conf import settings; settings.CELERY_BROKER_URL
```

### D. `.delay()` raises `kombu.exceptions.OperationalError`
**Meaning:** broker unreachable at the moment you enqueued.
**Fix:** start Redis; wrap `.delay()` in try/except and return 503 (see §7).

### E. Task stuck in `PENDING` forever
**Meaning:** no worker is running (or it watches a different broker).
**Fix:** start the worker; confirm the task is listed under `[tasks]` in its
banner; confirm broker URL matches.

### F. `Received unregistered task of type '...'`
**Meaning:** worker is running old code / doesn't know this task.
**Fix:** **restart the worker** (Celery doesn't auto-reload). Ensure the task
is in an app's `tasks.py` and `autodiscover_tasks()` is set.

### G. `TypeError: ... got multiple values for argument 'title'`
**Meaning:** `bind=True` is set but the function has no `self` parameter, so
Celery's injected `self` collides with your args.
**Fix:** add `self` as the first parameter:
```python
@shared_task(bind=True, ...)
def build_and_email_workbook(self, recipient_email, title="Report"):
```
Or remove `bind=True` if you don't need `self.retry()`.

### H. `Object of type ... is not JSON serializable`
**Meaning:** you passed a model instance / complex object to `.delay()`.
**Fix:** pass simple values (ids, strings); load objects inside the task.

### I. Task shows `FAILURE` / traceback in worker terminal
**Meaning:** a bug ran inside the task (e.g. `NameError("name 'car' is not
defined")`).
**Fix:** read the traceback in the **worker terminal** (not the browser). Fix
the bug. Don't retry pure bugs — they fail identically every time.

### J. Retry says "Retry in 180s" (or some odd number)
**Meaning:** `retry_backoff=True` is on; 180s is a backoff-computed delay, and
`default_retry_delay` is ignored.
**Fix:** control it:
```python
retry_backoff=2, retry_backoff_max=30      # or
default_retry_delay=5   (remove retry_backoff)   # or
raise self.retry(exc=exc, countdown=10)
```

### K. View returns "email is missing" though you sent it
**Causes & fixes:**
- Missing `Content-Type: application/json` header → add it.
- Forgot `JSON.stringify(...)` → body must be a string.
- Wrong key (`userEmail` vs `email`) → key must be exactly `email`.
- Sent in URL query instead of body → put it in the body.
- Debug: `print(request.body, request.content_type)` at top of the view.

### L. CSRF error on POST from React (different port)
**Fix (local):** `@csrf_exempt` on the view to confirm; long-term send a CSRF
token or use DRF. Also configure CORS:
```bash
pip install django-cors-headers
```
```python
INSTALLED_APPS += ["corsheaders"]
MIDDLEWARE = ["corsheaders.middleware.CorsMiddleware"] + MIDDLEWARE
CORS_ALLOWED_ORIGINS = ["http://localhost:5173"]
```

### M. Windows: worker crashes when a task runs
**Fix:** start with `--pool=solo`:
```bash
celery -A config worker --loglevel=info --pool=solo
```

### N. Task killed by hard `time_limit` → row/state stuck on RUNNING/STARTED
**Meaning:** SIGKILL left no chance to clean up.
**Fix:** add a periodic Celery Beat sweeper that marks jobs running too long as
failed; consider `acks_late=True` so crashed tasks can be retried.

---

## 14. Settings cheat-sheet

```python
# --- Broker / backend ---
CELERY_BROKER_URL = "redis://localhost:6379/0"
CELERY_RESULT_BACKEND = "redis://localhost:6379/0"

# --- Connection retry limits ---
CELERY_BROKER_CONNECTION_MAX_RETRIES = 5        # default 100
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BROKER_CONNECTION_TIMEOUT = 4
CELERY_BROKER_TRANSPORT_OPTIONS = {"socket_timeout": 5}

# --- Serialization ---
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"

# --- Reliability ---
CELERY_TASK_ACKS_LATE = True          # retry tasks if a worker crashes mid-run

# --- Email (learning) ---
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = "reports@myapp.com"
```

Per-task decorator options:
```python
@shared_task(
    bind=True,               # gives self (needed for self.retry)
    max_retries=3,
    default_retry_delay=5,   # fixed retry gap (ignored if retry_backoff set)
    retry_backoff=2,         # growing gap: 2,4,8...
    retry_backoff_max=30,    # cap the gap
    soft_time_limit=30,      # catchable timeout (SoftTimeLimitExceeded)
    time_limit=45,           # hard kill (must be > soft_time_limit)
    acks_late=True,
)
```

Worker CLI flags:
```bash
celery -A config worker \
    --loglevel=info \
    --concurrency=2 \        # number of child processes (caps CPU/RAM)
    --pool=solo              # Windows fix
```

---

## 15. Golden rules

1. **Web server and worker are separate programs** — start both, they talk only
   through the broker.
2. **The `redis` pip package ≠ the Redis server** — you need the server running
   (`redis-cli ping` → `PONG`).
3. **Celery does NOT auto-reload** — restart the worker after every task edit.
4. **`.delay()` = background; direct call = blocking, right now.**
5. **`bind=True` requires a `self` first parameter.**
6. **Pass ids, not objects** to tasks (must be JSON-serializable).
7. **Retry only transient failures**, never real bugs.
8. **`soft_time_limit` < `time_limit`**, always.
9. **Timeout ≠ retry** — wire them together yourself via `self.retry()`.
10. **Fire from `python manage.py shell`**, not plain `python`, or settings
    won't load and Celery defaults to RabbitMQ.
11. **Background bugs show in the WORKER terminal**, not the browser.
12. **`--concurrency` bounds resource use** — your main lever against runaway
    CPU/RAM.
13. **Hard kills can't clean up** — add a sweeper to catch stuck jobs.
