# Real-time chat server
# Run with: uvicorn main:app

import secrets

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import database

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
database.create_tables()

# Both of these are kept in memory, so everyone has to log in again
# if the server restarts.
sessions = {}  # token -> user id
connections = {}  # user id -> list of open websockets (one per browser tab)


class LoginInfo(BaseModel):
    username: str
    password: str


class NewPrivateChat(BaseModel):
    user_id: int


class NewGroup(BaseModel):
    name: str
    user_ids: list[int]


class NewMember(BaseModel):
    user_id: int


# ---------- helper functions ----------

def get_current_user(authorization):
    """The browser sends its login token in the Authorization header."""
    if authorization not in sessions:
        raise HTTPException(401, "Please log in")
    return sessions[authorization]


def check_member(conversation_id, user_id):
    if not database.is_member(conversation_id, user_id):
        raise HTTPException(403, "You are not in this conversation")


def check_user_exists(user_id):
    if database.get_user(user_id) is None:
        raise HTTPException(404, "User not found")


async def send_to_user(user_id, data):
    for websocket in list(connections.get(user_id, [])):
        try:
            await websocket.send_json(data)
        except Exception:
            pass  # that tab is closing, it gets removed when it disconnects


async def send_to_members(conversation_id, data):
    for member in database.get_members(conversation_id):
        await send_to_user(member["id"], data)


async def send_to_everyone(data):
    for user_id in list(connections):
        await send_to_user(user_id, data)


# ---------- pages and accounts ----------

@app.get("/")
async def home():
    return FileResponse("static/index.html")


@app.post("/register")
async def register(info: LoginInfo):
    username = info.username.strip().lower()
    if not username.isalnum() or not 3 <= len(username) <= 20:
        raise HTTPException(400, "Username must be 3-20 letters or numbers")
    if len(info.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")

    user_id = database.create_user(username, info.password)
    if user_id is None:
        raise HTTPException(400, "That username is taken")

    # so the new user shows up in everyone's list straight away
    await send_to_everyone({"type": "users_changed"})
    return {"message": "Account created"}


@app.post("/login")
async def login(info: LoginInfo):
    user_id = database.check_login(info.username.strip().lower(), info.password)
    if user_id is None:
        raise HTTPException(401, "Wrong username or password")

    token = secrets.token_hex(32)
    sessions[token] = user_id
    return {"token": token, "user": database.get_user(user_id)}


@app.post("/logout")
async def logout(authorization: str = Header("")):
    sessions.pop(authorization, None)
    return {"message": "Logged out"}


@app.get("/users")
async def users(authorization: str = Header("")):
    my_id = get_current_user(authorization)
    result = []
    for user in database.get_all_users():
        if user["id"] != my_id:
            user["online"] = user["id"] in connections
            result.append(user)
    return result


# ---------- conversations ----------

@app.get("/conversations")
async def conversations(authorization: str = Header("")):
    my_id = get_current_user(authorization)
    return database.get_conversations(my_id)


@app.post("/conversations/private")
async def start_private_chat(chat: NewPrivateChat, authorization: str = Header("")):
    my_id = get_current_user(authorization)
    check_user_exists(chat.user_id)
    if chat.user_id == my_id:
        raise HTTPException(400, "You can't chat with yourself")

    # reuse the old chat if these two already have one
    conversation_id = database.find_private_chat(my_id, chat.user_id)
    if conversation_id is None:
        conversation_id = database.create_conversation(None, 0, [my_id, chat.user_id])
        await send_to_members(conversation_id, {"type": "conversations_changed"})
    return {"id": conversation_id}


@app.post("/conversations/group")
async def create_group(group: NewGroup, authorization: str = Header("")):
    my_id = get_current_user(authorization)
    name = group.name.strip()
    if not name or len(name) > 50:
        raise HTTPException(400, "Group name must be 1-50 characters")

    member_ids = set(group.user_ids)
    member_ids.discard(my_id)
    if not member_ids:
        raise HTTPException(400, "Pick at least one other person")
    for user_id in member_ids:
        check_user_exists(user_id)

    member_ids.add(my_id)
    conversation_id = database.create_conversation(name, 1, member_ids)
    await send_to_members(conversation_id, {"type": "conversations_changed"})
    return {"id": conversation_id}


@app.post("/conversations/{conversation_id}/members")
async def add_to_group(conversation_id: int, member: NewMember, authorization: str = Header("")):
    my_id = get_current_user(authorization)
    check_member(conversation_id, my_id)
    check_user_exists(member.user_id)
    if not database.get_conversation(conversation_id)["is_group"]:
        raise HTTPException(400, "You can only add people to groups")

    database.add_member(conversation_id, member.user_id)
    await send_to_members(conversation_id, {"type": "conversations_changed"})
    return {"message": "Added"}


@app.post("/conversations/{conversation_id}/leave")
async def leave_group(conversation_id: int, authorization: str = Header("")):
    my_id = get_current_user(authorization)
    check_member(conversation_id, my_id)
    if not database.get_conversation(conversation_id)["is_group"]:
        raise HTTPException(400, "You can only leave groups")

    database.remove_member(conversation_id, my_id)
    await send_to_user(my_id, {"type": "conversations_changed"})
    await send_to_members(conversation_id, {"type": "conversations_changed"})
    # read receipts may have changed too
    await send_to_members(conversation_id, {"type": "receipts", "conversation_id": conversation_id})
    return {"message": "Left group"}


@app.get("/conversations/{conversation_id}/messages")
async def message_history(conversation_id: int, authorization: str = Header("")):
    my_id = get_current_user(authorization)
    check_member(conversation_id, my_id)
    return database.get_messages(conversation_id)


# ---------- websocket (real-time part) ----------

async def handle_new_message(user_id, conversation_id, text):
    message_id = database.save_message(conversation_id, user_id, text)
    # anyone who is online right now has received it
    for member in database.get_members(conversation_id):
        if member["id"] != user_id and member["id"] in connections:
            database.mark_delivered(message_id, member["id"])
    await send_to_members(
        conversation_id,
        {"type": "message", "conversation_id": conversation_id, "user_id": user_id},
    )


async def handle_typing(user_id, conversation_id):
    # just pass it on to the other people, nothing needs to be saved
    for member in database.get_members(conversation_id):
        if member["id"] != user_id:
            await send_to_user(
                member["id"],
                {"type": "typing", "conversation_id": conversation_id, "user_id": user_id},
            )


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    # The first thing the browser sends is its token. We don't put it in the
    # URL because URLs get written to the server logs.
    try:
        token = await websocket.receive_text()
    except WebSocketDisconnect:
        return
    if token not in sessions:
        await websocket.close(code=4001)
        return
    user_id = sessions[token]

    connections.setdefault(user_id, []).append(websocket)
    try:
        await send_to_everyone({"type": "status"})  # someone came online

        # messages sent while this user was offline have now been delivered
        for conversation_id in database.mark_all_delivered(user_id):
            await send_to_members(
                conversation_id, {"type": "receipts", "conversation_id": conversation_id}
            )

        while True:
            data = await websocket.receive_json()
            if token not in sessions:  # logged out
                break

            conversation_id = data.get("conversation_id")
            if not database.is_member(conversation_id, user_id):
                continue

            if data.get("type") == "message":
                text = str(data.get("text", "")).strip()
                if text and len(text) <= 2000:
                    await handle_new_message(user_id, conversation_id, text)

            elif data.get("type") == "typing":
                await handle_typing(user_id, conversation_id)

            elif data.get("type") == "read":
                if database.mark_read(conversation_id, user_id):
                    await send_to_members(
                        conversation_id, {"type": "receipts", "conversation_id": conversation_id}
                    )
    except WebSocketDisconnect:
        pass
    finally:
        # runs however the connection ended
        connections[user_id].remove(websocket)
        if not connections[user_id]:
            del connections[user_id]
            await send_to_everyone({"type": "status"})  # someone went offline
