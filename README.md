# Real-Time Chat Application

A web chat app built with Python (FastAPI), WebSockets and SQLite. The front end
is plain HTML, CSS and JavaScript.

## Features

- Register and log in
- Private conversations
- Group chats (create, add people, leave)
- Messages appear instantly
- Online/offline status
- Typing indicator
- Message history saved in a database
- Timestamps and read receipts (Sent, Delivered, Read)

## How to run

You need Python 3.10 or newer. These commands are for Windows PowerShell.

```powershell
python -m venv .venv
```

```powershell
.venv\Scripts\python -m pip install -r requirements.txt
```

```powershell
.venv\Scripts\python -m uvicorn main:app
```

Then open http://127.0.0.1:8000. To try it out, open two tabs and register a
different user in each one.

To run the tests:

```powershell
.venv\Scripts\python -m pytest
```

## Files

| File | What it does |
| --- | --- |
| `main.py` | The server: HTTP routes and the WebSocket |
| `database.py` | Database tables and queries |
| `static/` | The web page |
| `test_app.py` | Tests |

## How it works

Things like logging in and loading messages use normal HTTP requests. Each open
tab also keeps a WebSocket connection to the server.

When someone sends a message, it goes over the WebSocket. The server saves it and
tells everyone in the conversation that there is something new, and their
browsers load the latest messages.

A user counts as online while they have at least one WebSocket open.

While you type, the browser sends a "typing" update at most every 2 seconds. The
server passes it to the other members without saving it, and the indicator
disappears after 3 seconds with no new update, or when the message arrives.

For read receipts, there is a `receipts` table with one row per recipient of each
message. It stores when the message was delivered and when it was read.

## Security

- Passwords are salted and hashed with PBKDF2
- SQL queries use `?` parameters to prevent SQL injection
- Messages are displayed with `textContent`, so HTML in a message is not run
- The server checks that you are in a conversation before showing or changing it

## Limitations and future improvements

- Login sessions are stored in memory, so restarting the server logs everyone out
- It only works as a single server process
- The whole history of a chat is loaded at once; pagination would be better
- There is no limit on failed login attempts
- No HTTPS, so it should only be run locally as it is
- Messages are not end-to-end encrypted
- Could add file sharing and message editing
