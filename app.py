import random
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
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

    def deal_new_round(self) -> tuple[bool, str]:
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


@app.post("/api/rooms/create")
async def create_room(req: CreateRoomRequest) -> JSONResponse:
    name = req.name.strip()
    if not name:
        return JSONResponse({"error": "Введіть нікнейм."}, status_code=400)

    code = generate_room_code()
    pid = req.player_id or generate_player_id()
    room = Room(code=code, host_id=pid)
    room.players[pid] = Player(id=pid, name=name[:24], is_host=True)
    ROOMS[code] = room
    return JSONResponse({"room_code": code, "player_id": pid})


@app.post("/api/rooms/join")
async def join_room(req: JoinRoomRequest) -> JSONResponse:
    name = req.name.strip()
    code = req.room_code.strip().upper()
    if not name:
        return JSONResponse({"error": "Введіть нікнейм."}, status_code=400)
    if code not in ROOMS:
        return JSONResponse({"error": "Кімнату з таким кодом не знайдено."}, status_code=404)

    room = ROOMS[code]
    pid = req.player_id or generate_player_id()

    if pid in room.players:
        room.players[pid].name = name[:24]
        room.players[pid].connected = True
    else:
        room.players[pid] = Player(id=pid, name=name[:24], is_host=False)

    return JSONResponse({"room_code": code, "player_id": pid})


@app.websocket("/ws/{room_code}/{player_id}")
async def room_websocket(websocket: WebSocket, room_code: str, player_id: str) -> None:
    code = room_code.upper()
    room = ROOMS.get(code)
    if not room or player_id not in room.players:
        await websocket.close(code=4004)
        return

    await websocket.accept()
    room.players[player_id].connected = True
    room.connections.setdefault(player_id, []).append(websocket)
    await broadcast_room(room)

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")

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
    finally:
        sockets = room.connections.get(player_id, [])
        if websocket in sockets:
            sockets.remove(websocket)
        if not sockets and player_id in room.players:
            room.players[player_id].connected = False
            if room.host_id == player_id:
                connected_others = [
                    p.id for p in room.players.values() if p.connected and p.id != player_id
                ]
                if connected_others:
                    room.players[player_id].is_host = False
                    new_host_id = connected_others[0]
                    room.host_id = new_host_id
                    room.players[new_host_id].is_host = True
        await broadcast_room(room)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
