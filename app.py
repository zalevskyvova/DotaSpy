import random
import string
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from items import DOTA_ITEMS, DotaItem

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Dota 2 Spy")


@dataclass
class Player:
    id: str
    name: str
    is_host: bool = False
    connected: bool = True
    last_seen: float = field(default_factory=time.time)


@dataclass
class Room:
    code: str
    host_id: str
    players: dict[str, Player] = field(default_factory=dict)
    connections: dict[str, list[WebSocket]] = field(default_factory=dict)
    started: bool = False
    round_number: int = 0
    spy_id: str | None = None
    current_item: DotaItem | None = None
    used_item_ids: set[str] = field(default_factory=set)

    def refresh_presence(self) -> None:
        now = time.time()
        for pid, p in self.players.items():
            has_ws = bool(self.connections.get(pid))
            p.connected = has_ws or (now - p.last_seen < 18.0)

        if self.host_id not in self.players or not self.players[self.host_id].connected:
            active_ids = [p.id for p in self.players.values() if p.connected]
            if active_ids:
                for p in self.players.values():
                    p.is_host = False
                self.host_id = active_ids[0]
                self.players[self.host_id].is_host = True

    def deal_new_round(self) -> tuple[bool, str]:
        self.refresh_presence()
        connected_ids = [p.id for p in self.players.values() if p.connected]
        pool_ids = connected_ids if len(connected_ids) >= 2 else list(self.players.keys())
        if len(pool_ids) < 2:
            return False, "Для роздачі потрібно щонайменше 2 гравці в кімнаті."

        self.spy_id = random.choice(pool_ids)

        available = [item for item in DOTA_ITEMS if item["id"] not in self.used_item_ids]
        if not available:
            self.used_item_ids.clear()
            available = list(DOTA_ITEMS)

        self.current_item = random.choice(available)
        self.used_item_ids.add(self.current_item["id"])
        self.started = True
        self.round_number += 1
        return True, ""

    def state_for_player(self, viewer_id: str) -> dict[str, Any]:
        self.refresh_presence()
        is_spy = self.started and (viewer_id == self.spy_id)
        item_payload = None
        role = None

        if self.started:
            role = "spy" if is_spy else "citizen"
            if not is_spy:
                item_payload = self.current_item

        players_payload = [
            {
                "id": p.id,
                "name": p.name,
                "is_host": p.is_host,
                "connected": p.connected,
            }
            for p in self.players.values()
        ]

        return {
            "type": "state",
            "room_code": self.code,
            "host_id": self.host_id,
            "you_id": viewer_id,
            "started": self.started,
            "round_number": self.round_number,
            "role": role,
            "item": item_payload,
            "players": players_payload,
        }


ROOMS: dict[str, Room] = {}


def normalize_room_code(raw: str) -> str:
    cleaned = raw.strip()
    if "room=" in cleaned:
        cleaned = cleaned.split("room=")[-1].split("&")[0]
    cleaned = "".join(ch for ch in cleaned.upper() if ch.isalnum())
    return cleaned[:8]


def generate_room_code(length: int = 5) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        code = "".join(random.choices(alphabet, k=length))
        if code not in ROOMS:
            return code


def generate_player_id() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=12))


async def broadcast_room(room: Room) -> None:
    for pid, sockets in list(room.connections.items()):
        state = room.state_for_player(pid)
        dead: list[WebSocket] = []
        for ws in sockets:
            try:
                await ws.send_json(state)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in sockets:
                sockets.remove(ws)


async def send_error(ws: WebSocket, message: str) -> None:
    try:
        await ws.send_json({"type": "error", "message": message})
    except Exception:
        pass


class CreateRoomRequest(BaseModel):
    name: str
    player_id: str | None = None


class JoinRoomRequest(BaseModel):
    room_code: str
    name: str
    player_id: str | None = None


class RoomActionRequest(BaseModel):
    player_id: str
    action: str
    target_id: str | None = None


def ensure_player_in_room(room: Room, player_id: str, name: str | None = None) -> Player:
    now = time.time()
    if player_id in room.players:
        p = room.players[player_id]
        if name:
            p.name = name[:24]
        p.connected = True
        p.last_seen = now
        return p

    display_name = (name or "").strip()[:24] or f"Гравець {len(room.players) + 1}"
    is_first = len(room.players) == 0
    p = Player(id=player_id, name=display_name, is_host=is_first, connected=True, last_seen=now)
    room.players[player_id] = p
    if is_first:
        room.host_id = player_id
    return p


@app.post("/api/rooms/create")
async def create_room(req: CreateRoomRequest) -> JSONResponse:
    name = req.name.strip() or "Хост"
    code = generate_room_code()
    pid = (req.player_id or "").strip() or generate_player_id()
    room = Room(code=code, host_id=pid)
    ensure_player_in_room(room, pid, name)
    ROOMS[code] = room
    return JSONResponse({"room_code": code, "player_id": pid})


@app.post("/api/rooms/join")
async def join_room(req: JoinRoomRequest) -> JSONResponse:
    code = normalize_room_code(req.room_code)
    if not code:
        return JSONResponse({"error": "Введіть код кімнати."}, status_code=400)

    pid = (req.player_id or "").strip() or generate_player_id()
    name = req.name.strip() or f"Гравець {random.randint(10, 99)}"

    if code not in ROOMS:
        # Автоматично відновлюємо або створюємо кімнату за цим кодом, щоб не було 404 після перезапуску
        ROOMS[code] = Room(code=code, host_id=pid)

    room = ROOMS[code]
    ensure_player_in_room(room, pid, name)
    await broadcast_room(room)
    return JSONResponse({"room_code": code, "player_id": pid})


@app.get("/api/rooms/{room_code}/state")
async def get_room_state(room_code: str, player_id: str, name: str = "") -> JSONResponse:
    code = normalize_room_code(room_code)
    if not code:
        return JSONResponse({"error": "Невірний код кімнати."}, status_code=400)

    if code not in ROOMS:
        ROOMS[code] = Room(code=code, host_id=player_id)

    room = ROOMS[code]
    ensure_player_in_room(room, player_id, name or None)
    return JSONResponse(room.state_for_player(player_id))


@app.post("/api/rooms/{room_code}/action")
async def room_http_action(room_code: str, req: RoomActionRequest) -> JSONResponse:
    code = normalize_room_code(room_code)
    room = ROOMS.get(code)
    if not room:
        return JSONResponse({"error": "Кімнату не знайдено."}, status_code=404)

    ensure_player_in_room(room, req.player_id)

    if req.action == "deal_round":
        if req.player_id != room.host_id:
            return JSONResponse(
                {"error": "Тільки хост кімнати може роздати новий предмет."},
                status_code=403,
            )
        ok, err = room.deal_new_round()
        if not ok:
            return JSONResponse({"error": err}, status_code=400)
        await broadcast_room(room)
        return JSONResponse(room.state_for_player(req.player_id))

    if req.action == "kick_player":
        if req.player_id == room.host_id and req.target_id and req.target_id in room.players:
            if req.target_id != room.host_id:
                for ws in room.connections.pop(req.target_id, []):
                    try:
                        await ws.close(code=4001)
                    except Exception:
                        pass
                del room.players[req.target_id]
                await broadcast_room(room)
        return JSONResponse(room.state_for_player(req.player_id))

    return JSONResponse(room.state_for_player(req.player_id))


@app.websocket("/ws/{room_code}/{player_id}")
async def room_websocket(websocket: WebSocket, room_code: str, player_id: str) -> None:
    # Завжди спочатку робимо accept(), щоб клієнт ніколи не отримував 403 Forbidden при WebSocket handshake
    await websocket.accept()

    code = normalize_room_code(room_code)
    name_param = websocket.query_params.get("name", "").strip()

    if code not in ROOMS:
        ROOMS[code] = Room(code=code, host_id=player_id)

    room = ROOMS[code]
    ensure_player_in_room(room, player_id, name_param or None)
    room.connections.setdefault(player_id, []).append(websocket)
    await broadcast_room(room)

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")
            if player_id in room.players:
                room.players[player_id].last_seen = time.time()
                room.players[player_id].connected = True

            if action == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            if action == "deal_round":
                if player_id != room.host_id:
                    await send_error(websocket, "Тільки хост кімнати може роздати новий предмет.")
                    continue
                ok, err = room.deal_new_round()
                if not ok:
                    await send_error(websocket, err)
                else:
                    await broadcast_room(room)

            elif action == "kick_player":
                if player_id != room.host_id:
                    continue
                target_id = data.get("target_id")
                if target_id and target_id in room.players and target_id != room.host_id:
                    for ws in room.connections.pop(target_id, []):
                        try:
                            await ws.close(code=4001)
                        except Exception:
                            pass
                    del room.players[target_id]
                    await broadcast_room(room)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        sockets = room.connections.get(player_id, [])
        if websocket in sockets:
            sockets.remove(websocket)
        room.refresh_presence()
        await broadcast_room(room)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
