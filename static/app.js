const STORAGE_PID_KEY = "dota_spy_player_id";
const STORAGE_NAME_KEY = "dota_spy_player_name";
const STORAGE_ROOM_KEY = "dota_spy_room_code";

let socket = null;
let currentState = null;
let secretHidden = false;

const el = (id) => document.getElementById(id);

function showError(msg) {
    const banner = el("error-banner");
    banner.textContent = msg;
    banner.classList.remove("hidden");
    setTimeout(() => {
        banner.classList.add("hidden");
    }, 4500);
}

function getOrCreatePlayerId() {
    let pid = localStorage.getItem(STORAGE_PID_KEY);
    if (!pid) {
        pid = Math.random().toString(36).substring(2, 10) + Math.random().toString(36).substring(2, 6);
        localStorage.setItem(STORAGE_PID_KEY, pid);
    }
    return pid;
}

async function createRoom() {
    const nameInput = el("player-name-input").value.trim();
    if (!nameInput) {
        showError("Введіть свій нікнейм.");
        return;
    }
    localStorage.setItem(STORAGE_NAME_KEY, nameInput);
    const pid = getOrCreatePlayerId();

    const res = await fetch("/api/rooms/create", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: nameInput, player_id: pid }),
    });
    const data = await res.json();
    if (!res.ok) {
        showError(data.error || "Помилка створення кімнати");
        return;
    }
    enterRoom(data.room_code, data.player_id);
}

async function joinRoom(codeOverride) {
    const nameInput = el("player-name-input").value.trim();
    const roomCode = (codeOverride || el("room-code-input").value).trim().toUpperCase();
    if (!nameInput) {
        showError("Введіть свій нікнейм для входу в кімнату.");
        return;
    }
    if (!roomCode) {
        showError("Введіть код кімнати.");
        return;
    }
    localStorage.setItem(STORAGE_NAME_KEY, nameInput);
    const pid = getOrCreatePlayerId();

    const res = await fetch("/api/rooms/join", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ room_code: roomCode, name: nameInput, player_id: pid }),
    });
    const data = await res.json();
    if (!res.ok) {
        showError(data.error || "Не вдалося увійти в кімнату");
        return;
    }
    enterRoom(data.room_code, data.player_id);
}

function enterRoom(roomCode, playerId) {
    localStorage.setItem(STORAGE_ROOM_KEY, roomCode);
    localStorage.setItem(STORAGE_PID_KEY, playerId);

    const url = new URL(window.location.href);
    url.searchParams.set("room", roomCode);
    window.history.replaceState({}, "", url.toString());

    el("view-auth").classList.add("hidden");
    el("view-room").classList.remove("hidden");
    el("room-header-info").classList.remove("hidden");
    el("header-room-code").textContent = roomCode;

    connectWebSocket(roomCode, playerId);
}

function leaveRoom() {
    if (socket) {
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

function connectWebSocket(roomCode, playerId) {
    if (socket) {
        socket.close();
    }
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${proto}//${window.location.host}/ws/${roomCode}/${playerId}`;
    socket = new WebSocket(wsUrl);

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
        } else if (ev.code === 4004) {
            leaveRoom();
        }
    };
}

function sendAction(payload) {
    if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify(payload));
    }
}

function renderState(state) {
    const isHost = state.you_id === state.host_id;
    el("host-controls").classList.toggle("hidden", !isHost);

    if (!state.started) {
        el("round-badge").textContent = "Очікування роздачі";
        el("waiting-state").classList.remove("hidden");
        el("dealt-state").classList.add("hidden");
        el("toggle-secret-btn").classList.add("hidden");
        el("deal-btn").textContent = "Роздати предмет";
    } else {
        el("round-badge").textContent = `Раунд #${state.round_number}`;
        el("waiting-state").classList.add("hidden");
        el("toggle-secret-btn").classList.remove("hidden");
        el("deal-btn").textContent = "Наступна шмотка (Новий раунд)";

        el("dealt-state").classList.toggle("hidden", secretHidden);
        el("toggle-secret-btn").textContent = secretHidden ? "Показати картку" : "Приховати картку";

        if (state.role === "spy") {
            el("item-view").classList.add("hidden");
            el("spy-view").classList.remove("hidden");
        } else if (state.item) {
            el("spy-view").classList.add("hidden");
            el("item-view").classList.remove("hidden");
            el("item-icon").src = state.item.icon;
            el("item-name").textContent = state.item.name;
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
        el("room-code-input").value = roomParam.toUpperCase();
        if (savedName) {
            joinRoom(roomParam);
        }
    }

    el("create-room-btn").addEventListener("click", createRoom);
    el("join-room-btn").addEventListener("click", () => joinRoom());
    el("leave-room-btn").addEventListener("click", leaveRoom);

    el("copy-link-btn").addEventListener("click", async () => {
        if (!currentState) return;
        const link = `${window.location.origin}/?room=${currentState.room_code}`;
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
