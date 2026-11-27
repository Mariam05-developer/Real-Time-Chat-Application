// The server only tells us that something changed ("there's a new message"),
// then we fetch the latest data again. This keeps the browser code simple.

let token = sessionStorage.getItem("token");
let me = JSON.parse(sessionStorage.getItem("user"));
let socket = null;
let users = [];
let conversations = [];
let currentId = null;              // id of the open conversation
const selectedUsers = new Set();   // people ticked for a new group
const typingUsers = new Map();     // user id -> timer that removes them
let lastTypingSent = 0;

const byId = (id) => document.getElementById(id);

// make an element and set its text. textContent never runs HTML, so a message
// containing <script> is just shown as text
function makeElement(tag, text, className) {
  const element = document.createElement(tag);
  element.textContent = text || "";
  if (className) element.className = className;
  return element;
}

// send a request to the server and return the JSON reply
async function request(url, body) {
  const options = { headers: { Authorization: token || "" } };
  if (body) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401 && token) logout();
    throw new Error(typeof data.detail === "string" ? data.detail : "Something went wrong");
  }
  return data;
}

// ---------- login ----------

async function login() {
  const username = byId("username").value;
  const password = byId("password").value;
  try {
    const data = await request("/login", { username, password });
    token = data.token;
    me = data.user;
    // sessionStorage is separate for each tab, so you can test with two users
    sessionStorage.setItem("token", token);
    sessionStorage.setItem("user", JSON.stringify(me));
    showChatPage();
  } catch (error) {
    byId("login-error").textContent = error.message;
  }
}

byId("login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  login();
});

byId("register-button").addEventListener("click", async () => {
  const username = byId("username").value;
  const password = byId("password").value;
  try {
    await request("/register", { username, password });
    await login();
  } catch (error) {
    byId("login-error").textContent = error.message;
  }
});

function logout() {
  fetch("/logout", { method: "POST", headers: { Authorization: token } })
    .finally(() => {
      sessionStorage.clear();
      location.reload();
    });
  token = null;
}

byId("logout-button").addEventListener("click", logout);

function showChatPage() {
  byId("login-page").classList.add("hidden");
  byId("chat-page").classList.remove("hidden");
  byId("my-name").textContent = me.username;
  connect();
}

// ---------- websocket ----------

function connect() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  socket = new WebSocket(`${protocol}//${location.host}/ws`);

  socket.onopen = async () => {
    socket.send(token);
    byId("sidebar-error").textContent = "";
    // load everything (again), in case we missed something while disconnected
    await loadUsers();
    await loadConversations();
    if (currentId) await openChat(currentId);
  };

  socket.onmessage = async (event) => {
    const data = JSON.parse(event.data);

    if (data.type === "status" || data.type === "users_changed") {
      await loadUsers();
    } else if (data.type === "conversations_changed") {
      await loadConversations();
    } else if (data.type === "message") {
      if (data.conversation_id === currentId) {
        stopTyping(data.user_id); // they've sent it, so they're done typing
        await loadMessages();
        markAsRead();
      }
      await loadConversations();
    } else if (data.type === "typing") {
      if (data.conversation_id === currentId) startTyping(data.user_id);
    } else if (data.type === "receipts") {
      if (data.conversation_id === currentId) await loadMessages();
      await loadConversations();
    }
  };

  socket.onclose = (event) => {
    if (!token) return;                      // we logged out on purpose
    if (event.code === 4001) return logout(); // the server doesn't know our token
    byId("sidebar-error").textContent = "Connection lost. Reconnecting...";
    setTimeout(connect, 2000);
  };
}

function markAsRead() {
  // only counts as read if the tab is actually visible
  if (currentId && !document.hidden && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: "read", conversation_id: currentId }));
  }
}

document.addEventListener("visibilitychange", markAsRead);

// ---------- sidebar ----------

async function loadUsers() {
  users = await request("/users");
  showUsers();
  showChatHeader();
}

async function loadConversations() {
  conversations = await request("/conversations");
  // the open chat disappears if we left the group
  if (currentId && !conversations.some((c) => c.id === currentId)) {
    currentId = null;
  }
  showConversations();
  showChatHeader();
}

function isOnline(userId) {
  return users.some((user) => user.id === userId && user.online);
}

// private chats are named after the other person
function getTitle(conversation) {
  if (conversation.is_group) return conversation.name;
  return conversation.members.find((m) => m.id !== me.id).username;
}

function showUsers() {
  const list = byId("user-list");
  list.replaceChildren();

  for (const user of users) {
    const item = document.createElement("li");

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = selectedUsers.has(user.id);
    checkbox.onchange = () => {
      if (checkbox.checked) selectedUsers.add(user.id);
      else selectedUsers.delete(user.id);
    };

    const button = makeElement("button", user.username, "link");
    button.onclick = async () => {
      const chat = await request("/conversations/private", { user_id: user.id });
      await loadConversations();
      openChat(chat.id);
    };

    const status = makeElement("span", user.online ? "online" : "offline",
                               user.online ? "status online" : "status");
    item.append(checkbox, button, status);
    list.append(item);
  }
}

function showConversations() {
  const list = byId("conversation-list");
  list.replaceChildren();

  for (const conversation of conversations) {
    const item = document.createElement("li");
    const label = getTitle(conversation) + (conversation.is_group ? " (group)" : "");
    const button = makeElement("button", label, "link");
    button.onclick = () => openChat(conversation.id);
    item.append(button);

    if (conversation.unread > 0) {
      item.append(makeElement("span", conversation.unread, "unread"));
    }
    if (conversation.id === currentId) item.classList.add("selected");
    list.append(item);
  }
}

byId("group-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  byId("sidebar-error").textContent = "";
  try {
    const group = await request("/conversations/group", {
      name: byId("group-name").value,
      user_ids: Array.from(selectedUsers),
    });
    byId("group-name").value = "";
    selectedUsers.clear();
    showUsers();
    await loadConversations();
    openChat(group.id);
  } catch (error) {
    byId("sidebar-error").textContent = error.message;
  }
});

// ---------- the open chat ----------

async function openChat(conversationId) {
  currentId = conversationId;
  clearTyping();
  lastTypingSent = 0;
  byId("messages").replaceChildren();
  showConversations();
  showChatHeader();
  await loadMessages();
  markAsRead();
  byId("message-input").focus();
}

function showChatHeader() {
  const conversation = conversations.find((c) => c.id === currentId);
  byId("no-chat").classList.toggle("hidden", Boolean(conversation));
  byId("chat").classList.toggle("hidden", !conversation);
  if (!conversation) return;

  byId("chat-title").textContent = getTitle(conversation);
  byId("group-controls").classList.toggle("hidden", !conversation.is_group);

  const others = conversation.members.filter((m) => m.id !== me.id);
  if (conversation.is_group) {
    const names = others.map((m) => m.username + (isOnline(m.id) ? " (online)" : " (offline)"));
    byId("chat-status").textContent = ["you", ...names].join(", ");

    // dropdown of people who aren't in the group yet
    const select = byId("add-select");
    const previous = select.value;
    select.replaceChildren();
    for (const user of users) {
      if (!conversation.members.some((m) => m.id === user.id)) {
        select.append(new Option(user.username, user.id));
      }
    }
    if (Array.from(select.options).some((o) => o.value === previous)) select.value = previous;

    // nothing to add if everyone is already in the group
    const nobodyLeft = select.options.length === 0;
    select.classList.toggle("hidden", nobodyLeft);
    byId("add-button").classList.toggle("hidden", nobodyLeft);
  } else {
    byId("chat-status").textContent = isOnline(others[0].id) ? "Online" : "Offline";
  }
}

async function loadMessages() {
  const id = currentId;
  const messages = await request(`/conversations/${id}/messages`);
  if (id !== currentId) return; // the user switched chats while we were waiting

  const box = byId("messages");
  box.replaceChildren();

  for (const message of messages) {
    const mine = message.sender_id === me.id;
    const div = makeElement("div", "", mine ? "message mine" : "message");

    if (!mine) div.append(makeElement("strong", message.sender));
    div.append(makeElement("div", message.text));

    let details = new Date(message.sent_at).toLocaleString([], {
      dateStyle: "short",
      timeStyle: "short",
    });
    if (mine) details += " - " + receiptText(message.receipts);
    div.append(makeElement("div", details, "small"));

    box.append(div);
  }
  box.scrollTop = box.scrollHeight; // jump to the newest message
}

// works out what to show under a message we sent
function receiptText(receipts) {
  const readers = receipts.filter((r) => r.read_at);

  if (receipts.length > 0 && readers.length === receipts.length) {
    if (receipts.length === 1) {
      const time = new Date(readers[0].read_at).toLocaleTimeString([], { timeStyle: "short" });
      return `Read at ${time}`;
    }
    return "Read by everyone";
  }
  if (readers.length > 0) {
    return "Read by " + readers.map((r) => r.username).join(", ");
  }
  if (receipts.length > 0 && receipts.every((r) => r.delivered_at)) {
    return "Delivered";
  }
  return "Sent";
}

byId("message-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const input = byId("message-input");
  const text = input.value.trim();
  // if we're disconnected, keep the text in the box so it isn't lost
  if (!text || socket.readyState !== WebSocket.OPEN) return;
  socket.send(JSON.stringify({ type: "message", conversation_id: currentId, text }));
  input.value = "";
  lastTypingSent = 0;
});

// ---------- typing indicator ----------

byId("message-input").addEventListener("input", () => {
  // send at most one update every 2 seconds, not one for every key press
  const time = Date.now();
  if (byId("message-input").value.trim() && time - lastTypingSent > 2000 &&
      socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: "typing", conversation_id: currentId }));
    lastTypingSent = time;
  }
});

function startTyping(userId) {
  // restart the timer. if no new update arrives in 3 seconds, they've stopped
  clearTimeout(typingUsers.get(userId));
  typingUsers.set(userId, setTimeout(() => stopTyping(userId), 3000));
  showTyping();
}

function stopTyping(userId) {
  clearTimeout(typingUsers.get(userId));
  typingUsers.delete(userId);
  showTyping();
}

function clearTyping() {
  for (const timer of typingUsers.values()) clearTimeout(timer);
  typingUsers.clear();
  showTyping();
}

function showTyping() {
  const names = users.filter((u) => typingUsers.has(u.id)).map((u) => u.username);
  let text = "";
  if (names.length === 1) text = `${names[0]} is typing...`;
  else if (names.length > 1) text = `${names.join(", ")} are typing...`;
  byId("typing").textContent = text;
}

byId("add-button").addEventListener("click", async () => {
  const userId = Number(byId("add-select").value);
  if (userId) {
    await request(`/conversations/${currentId}/members`, { user_id: userId });
  }
});

byId("leave-button").addEventListener("click", async () => {
  if (confirm("Leave this group?")) {
    await request(`/conversations/${currentId}/leave`, {});
    await loadConversations();
  }
});

// stay logged in if the page is refreshed
if (token) showChatPage();
