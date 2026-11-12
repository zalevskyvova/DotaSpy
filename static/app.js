const STORAGE_PID_KEY = "dota_spy_player_id";
const STORAGE_NAME_KEY = "dota_spy_player_name";
const STORAGE_ROOM_KEY = "dota_spy_room_code";

let socket = null;
let currentState = null;
let secretHidden = false;
let activeRoomCode = null;
let activePlayerId = null;
let activePlayerName = "";
let pingTimer = null;
let pollTimer = null;
let reconnectTimer = null;
let lastRenderedRound = 0;

const el = (id) => document.getElementById(id);

function showError(msg) {
    const banner = el("error-banner");
    banner.textContent = msg;
    banner.classList.remove("hidden");
    setTimeout(() => {
        banner.classList.add("hidden");
    }, 4500);
}

function cleanRoomInput(raw) {
    let text = (raw || "").trim();
    if (text.includes("room=")) {
        text = text.split("room=").pop().split("&")[0];
    }
    return text.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 8);
}

function getOrCreatePlayerId() {
    let pid = localStorage.getItem(STORAGE_PID_KEY);
    if (!pid) {
        pid = Math.random().toString(36).substring(2, 10) + Math.random().toString(36).substring(2, 6);
        localStorage.setItem(STORAGE_PID_KEY, pid);
    }
    return pid;
}

function getOrCreateName() {
    let name = el("player-name-input").value.trim();
    if (!name) {
        name = localStorage.getItem(STORAGE_NAME_KEY) || "";
    }
    if (!name) {
        name = "Гравець " + Math.floor(10 + Math.random() * 90);
        el("player-name-input").value = name;
    }
    localStorage.setItem(STORAGE_NAME_KEY, name);
    return name;
}

async function createRoom() {
    const name = getOrCreateName();
    const pid = getOrCreatePlayerId();

    try {
        const res = await fetch("/api/rooms/create", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name, player_id: pid }),
        });
        const data = await res.json();
        if (!res.ok) {
            showError(data.error || "Помилка створення кімнати");
            return;
        }
        enterRoom(data.room_code, data.player_id, name);
    } catch {
        showError("Не вдалося з'єднатися з сервером.");
    }
}

async function joinRoom(codeOverride) {
    const roomCode = cleanRoomInput(codeOverride || el("room-code-input").value);
    if (!roomCode) {
        showError("Введіть код кімнати або вставте посилання.");
        return;
    }
    const name = getOrCreateName();
    const pid = getOrCreatePlayerId();

    try {
        const res = await fetch("/api/rooms/join", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ room_code: roomCode, name, player_id: pid }),
        });
        const data = await res.json();
        if (!res.ok) {
            showError(data.error || "Не вдалося увійти в кімнату");
            return;
        }
        enterRoom(data.room_code, data.player_id, name);
    } catch {
        showError("Не вдалося підключитися до кімнати.");
    }
}

function enterRoom(roomCode, playerId, playerName) {
    activeRoomCode = roomCode;
    activePlayerId = playerId;
    activePlayerName = playerName;

    localStorage.setItem(STORAGE_ROOM_KEY, roomCode);
    localStorage.setItem(STORAGE_PID_KEY, playerId);

    const url = new URL(window.location.href);
    url.searchParams.set("room", roomCode);
    window.history.replaceState({}, "", url.toString());

    el("view-auth").classList.add("hidden");
    el("view-room").classList.remove("hidden");
    el("room-header-info").classList.remove("hidden");
    el("header-room-code").textContent = roomCode;

    connectWebSocket();
    startPollingFallback();
}

function leaveRoom() {
    activeRoomCode = null;
    stopTimers();
    if (socket) {
        socket.onclose = null;
        socket.close();
        socket = null;
    }
    localStorage.removeItem(STORAGE_ROOM_KEY);
    const url = new URL(window.location.href);
    url.searchParams.delete("room");
    window.history.replaceState({}, "", url.toString());

    el("view-room").classList.add("hidden");
    el("room-header-info").classList.add("hidden");
    el("view-auth").classList.remove("hidden");
}

function stopTimers() {
    if (pingTimer) {
        clearInterval(pingTimer);
        pingTimer = null;
    }
    if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
    }
    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }
}

function startPollingFallback() {
    if (pollTimer) clearInterval(pollTimer);
    fetchRoomState();
    pollTimer = setInterval(fetchRoomState, 2500);
}

async function fetchRoomState() {
    if (!activeRoomCode || !activePlayerId) return;
    try {
        const q = new URLSearchParams({
            player_id: activePlayerId,
            name: activePlayerName || "",
        });
        const res = await fetch(`/api/rooms/${activeRoomCode}/state?${q.toString()}`);
        if (res.ok) {
            const state = await res.json();
            if (state && state.type === "state") {
                currentState = state;
                renderState(state);
            }
        }
    } catch {
        // Ignore transient network errors during background poll
    }
}

function connectWebSocket() {
    if (!activeRoomCode || !activePlayerId) return;
    if (socket) {
        socket.onclose = null;
        socket.close();
    }
    if (pingTimer) clearInterval(pingTimer);

    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const q = new URLSearchParams({ name: activePlayerName || "" });
    const wsUrl = `${proto}//${window.location.host}/ws/${activeRoomCode}/${activePlayerId}?${q.toString()}`;
    socket = new WebSocket(wsUrl);

    socket.onopen = () => {
        pingTimer = setInterval(() => {
            if (socket && socket.readyState === WebSocket.OPEN) {
                socket.send(JSON.stringify({ action: "ping" }));
            }
        }, 20000);
    };

    socket.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        if (msg.type === "error") {
            showError(msg.message);
            return;
        }
        if (msg.type === "state") {
            currentState = msg;
            renderState(msg);
        }
    };

    socket.onclose = (ev) => {
        if (ev.code === 4001) {
            showError("Хост видалив вас із кімнати.");
            leaveRoom();
            return;
        }
        if (activeRoomCode) {
            reconnectTimer = setTimeout(connectWebSocket, 2000);
        }
    };
}

async function sendAction(payload) {
    if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify(payload));
        return;
    }
    if (!activeRoomCode || !activePlayerId) return;
    try {
        const res = await fetch(`/api/rooms/${activeRoomCode}/action`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                player_id: activePlayerId,
                action: payload.action,
                target_id: payload.target_id || null,
            }),
        });
        const data = await res.json();
        if (!res.ok) {
            showError(data.error || "Помилка виконання дії");
        } else if (data.type === "state") {
            currentState = data;
            renderState(data);
        }
    } catch {
        showError("Помилка зв'язку із сервером.");
    }
}

function renderState(state) {
    if (!state) return;

    // Запобігаємо зникненню картки, якщо запит випадково потрапив на несинхронізований процес
    if (!state.started && currentState && currentState.started && currentState.round_number > 0) {
        renderPlayersList(state);
        return;
    }

    const isHost = state.you_id === state.host_id;
    el("host-controls").classList.toggle("hidden", !isHost);

    if (!state.started) {
        el("round-badge").textContent = "Очікування роздачі";
        el("waiting-state").classList.remove("hidden");
        el("dealt-state").classList.add("hidden");
        el("toggle-secret-btn").classList.add("hidden");
        el("deal-btn").textContent = "Роздати предмет";
    } else {
        if (state.round_number !== lastRenderedRound) {
            lastRenderedRound = state.round_number;
            secretHidden = false;
        }

        el("round-badge").textContent = `Раунд #${state.round_number}`;
        el("waiting-state").classList.add("hidden");
        el("toggle-secret-btn").classList.remove("hidden");
        el("deal-btn").textContent = "Наступна шмотка (Новий раунд)";

        el("dealt-state").classList.toggle("hidden", secretHidden);
        el("toggle-secret-btn").textContent = secretHidden ? "Показати картку" : "Приховати картку";

        if (state.first_speaker_name) {
            const isFirst = state.first_speaker_id === state.you_id;
            el("first-speaker-name").textContent = isFirst ? "Ви" : state.first_speaker_name;
            el("first-speaker-box").classList.remove("hidden");
        } else {
            el("first-speaker-box").classList.add("hidden");
        }

        if (state.role === "spy") {
            el("item-view").classList.add("hidden");
            el("spy-view").classList.remove("hidden");
        } else if (state.item) {
            el("spy-view").classList.add("hidden");
            el("item-view").classList.remove("hidden");

            const imgEl = el("item-icon");
            if (imgEl.getAttribute("src") !== state.item.icon) {
                imgEl.src = state.item.icon;
            }
            if (el("item-name").textContent !== state.item.name) {
                el("item-name").textContent = state.item.name;
            }
        }
    }

    renderPlayersList(state);
}

function renderPlayersList(state) {
    const list = el("players-list");
    list.innerHTML = "";
    el("players-count-badge").textContent = state.players.length;
    const isHost = state.you_id === state.host_id;

    for (const p of state.players) {
        const li = document.createElement("li");
        li.className = "player-row";

        const tags = [];
        if (p.is_host) tags.push('<span class="tag tag-host">HOST</span>');
        if (p.id === state.you_id) tags.push('<span class="tag tag-you">ВИ</span>');
        if (state.first_speaker_id && p.id === state.first_speaker_id) {
            tags.push('<span class="tag tag-speaker">1-Й ГОВОРИТЬ</span>');
        }
        if (!p.connected) tags.push('<span class="tag tag-offline">OFFLINE</span>');

        li.innerHTML = `
            <div class="player-name-line">
                <span>${p.name}</span>
                ${tags.join(" ")}
            </div>
            <div class="player-right"></div>
        `;

        if (isHost && p.id !== state.you_id) {
            const kickBtn = document.createElement("button");
            kickBtn.type = "button";
            kickBtn.className = "btn btn-small btn-danger-outline";
            kickBtn.textContent = "X";
            kickBtn.title = "Видалити гравця";
            kickBtn.addEventListener("click", () => {
                sendAction({ action: "kick_player", target_id: p.id });
            });
            li.querySelector(".player-right").appendChild(kickBtn);
        }

        list.appendChild(li);
    }
}

document.addEventListener("DOMContentLoaded", () => {
    const savedName = localStorage.getItem(STORAGE_NAME_KEY);
    if (savedName) {
        el("player-name-input").value = savedName;
    }

    const params = new URLSearchParams(window.location.search);
    const roomParam = params.get("room");
    if (roomParam) {
        const cleaned = cleanRoomInput(roomParam);
        el("room-code-input").value = cleaned;
        if (cleaned) {
            joinRoom(cleaned);
        }
    }

    el("create-room-btn").addEventListener("click", createRoom);
    el("join-room-btn").addEventListener("click", () => joinRoom());
    el("leave-room-btn").addEventListener("click", leaveRoom);

    el("copy-link-btn").addEventListener("click", async () => {
        if (!activeRoomCode) return;
        const link = `${window.location.origin}/?room=${activeRoomCode}`;
        try {
            await navigator.clipboard.writeText(link);
            el("copy-link-btn").textContent = "Скопійовано!";
            setTimeout(() => {
                el("copy-link-btn").textContent = "Копіювати посилання";
            }, 2000);
        } catch {
            showError(`Посилання: ${link}`);
        }
    });

    el("deal-btn").addEventListener("click", () => {
        secretHidden = false;
        sendAction({ action: "deal_round" });
    });

    el("toggle-secret-btn").addEventListener("click", () => {
        secretHidden = !secretHidden;
        if (currentState) renderState(currentState);
    });
});
