# Run with: pytest

import pytest
from fastapi.testclient import TestClient

import database
from main import app, connections, sessions

PASSWORD = "password123"


@pytest.fixture
def client(tmp_path):
    # every test gets its own empty database, so real data isn't touched
    database.DB_FILE = str(tmp_path / "test.db")
    database.create_tables()
    sessions.clear()
    connections.clear()
    with TestClient(app) as test_client:
        yield test_client


def make_user(client, username):
    """Register and log in. Returns (headers, token, user id)."""
    info = {"username": username, "password": PASSWORD}
    client.post("/register", json=info)
    data = client.post("/login", json=info).json()
    return {"Authorization": data["token"]}, data["token"], data["user"]["id"]


def wait_for(websocket, event_type):
    """Read from the websocket until we get the kind of event we want."""
    while True:
        data = websocket.receive_json()
        if data["type"] == event_type:
            return data


def test_register_and_login(client):
    info = {"username": "alice", "password": PASSWORD}
    assert client.post("/register", json=info).status_code == 200
    assert client.post("/register", json=info).status_code == 400  # name taken
    assert client.post("/login", json=info).status_code == 200

    info["password"] = "wrong password"
    assert client.post("/login", json=info).status_code == 401


def test_bad_registration(client):
    short_name = {"username": "al", "password": PASSWORD}
    short_password = {"username": "alice", "password": "123"}
    assert client.post("/register", json=short_name).status_code == 400
    assert client.post("/register", json=short_password).status_code == 400


def test_must_be_logged_in(client):
    assert client.get("/conversations").status_code == 401


def test_private_chat_is_only_created_once(client):
    alice, _, alice_id = make_user(client, "alice")
    bob, _, bob_id = make_user(client, "bob")

    first = client.post("/conversations/private", json={"user_id": bob_id}, headers=alice)
    second = client.post("/conversations/private", json={"user_id": alice_id}, headers=bob)
    assert first.json()["id"] == second.json()["id"]


def test_other_people_cannot_read_a_chat(client):
    alice, _, _ = make_user(client, "alice")
    _, _, bob_id = make_user(client, "bob")
    eve, _, _ = make_user(client, "eve")

    chat = client.post("/conversations/private", json={"user_id": bob_id}, headers=alice).json()
    response = client.get(f"/conversations/{chat['id']}/messages", headers=eve)
    assert response.status_code == 403


def test_message_and_read_receipt(client):
    alice, alice_token, _ = make_user(client, "alice")
    _, bob_token, bob_id = make_user(client, "bob")
    chat = client.post("/conversations/private", json={"user_id": bob_id}, headers=alice).json()
    url = f"/conversations/{chat['id']}/messages"

    with client.websocket_connect("/ws") as alice_ws, client.websocket_connect("/ws") as bob_ws:
        alice_ws.send_text(alice_token)
        wait_for(alice_ws, "status")
        bob_ws.send_text(bob_token)
        wait_for(bob_ws, "status")

        alice_ws.send_json({"type": "message", "conversation_id": chat["id"], "text": "hi bob"})
        wait_for(bob_ws, "message")

        message = client.get(url, headers=alice).json()[0]
        assert message["text"] == "hi bob"
        assert message["sent_at"]
        assert message["receipts"][0]["delivered_at"]
        assert message["receipts"][0]["read_at"] is None

        bob_ws.send_json({"type": "read", "conversation_id": chat["id"]})
        wait_for(alice_ws, "receipts")
        assert client.get(url, headers=alice).json()[0]["receipts"][0]["read_at"]


def test_offline_user_gets_message_later(client):
    alice, alice_token, _ = make_user(client, "alice")
    bob, bob_token, bob_id = make_user(client, "bob")
    chat = client.post("/conversations/private", json={"user_id": bob_id}, headers=alice).json()
    url = f"/conversations/{chat['id']}/messages"

    with client.websocket_connect("/ws") as alice_ws:
        alice_ws.send_text(alice_token)
        alice_ws.send_json({"type": "message", "conversation_id": chat["id"], "text": "you there?"})
        wait_for(alice_ws, "message")

        assert client.get(url, headers=alice).json()[0]["receipts"][0]["delivered_at"] is None
        assert client.get("/conversations", headers=bob).json()[0]["unread"] == 1

        with client.websocket_connect("/ws") as bob_ws:
            bob_ws.send_text(bob_token)
            wait_for(alice_ws, "receipts")
            assert client.get(url, headers=alice).json()[0]["receipts"][0]["delivered_at"]


def test_typing_indicator(client):
    alice, alice_token, alice_id = make_user(client, "alice")
    _, bob_token, bob_id = make_user(client, "bob")
    chat = client.post("/conversations/private", json={"user_id": bob_id}, headers=alice).json()

    with client.websocket_connect("/ws") as alice_ws, client.websocket_connect("/ws") as bob_ws:
        alice_ws.send_text(alice_token)
        wait_for(alice_ws, "status")
        bob_ws.send_text(bob_token)
        wait_for(bob_ws, "status")

        alice_ws.send_json({"type": "typing", "conversation_id": chat["id"]})
        event = wait_for(bob_ws, "typing")
        assert event["user_id"] == alice_id
        assert event["conversation_id"] == chat["id"]

    # typing doesn't save anything
    assert client.get(f"/conversations/{chat['id']}/messages", headers=alice).json() == []


def test_online_status(client):
    alice, _, _ = make_user(client, "alice")
    _, bob_token, _ = make_user(client, "bob")
    assert client.get("/users", headers=alice).json()[0]["online"] is False

    with client.websocket_connect("/ws") as bob_ws:
        bob_ws.send_text(bob_token)
        wait_for(bob_ws, "status")
        assert client.get("/users", headers=alice).json()[0]["online"] is True

    assert client.get("/users", headers=alice).json()[0]["online"] is False


def test_group_chat(client):
    alice, _, _ = make_user(client, "alice")
    bob, _, bob_id = make_user(client, "bob")
    carol, _, carol_id = make_user(client, "carol")

    group = {"name": "Study group", "user_ids": [bob_id]}
    group_id = client.post("/conversations/group", json=group, headers=alice).json()["id"]
    assert client.get("/conversations", headers=carol).json() == []

    client.post(f"/conversations/{group_id}/members", json={"user_id": carol_id}, headers=alice)
    assert len(client.get("/conversations", headers=carol).json()[0]["members"]) == 3

    client.post(f"/conversations/{group_id}/leave", headers=bob)
    assert client.get("/conversations", headers=bob).json() == []
