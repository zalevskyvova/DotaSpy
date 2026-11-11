from fastapi.testclient import TestClient

from app import ROOMS, app

client = TestClient(app)


def test_deal_random_item_to_all_except_one() -> None:
    ROOMS.clear()

    res = client.post("/api/rooms/create", json={"name": "Host", "player_id": "p1"})
    assert res.status_code == 200
    room_code = res.json()["room_code"]

    for idx in range(2, 6):
        join_res = client.post(
            "/api/rooms/join",
            json={"room_code": room_code, "name": f"Player{idx}", "player_id": f"p{idx}"},
        )
        assert join_res.status_code == 200

    with (
        client.websocket_connect(f"/ws/{room_code}/p1") as ws1,
        client.websocket_connect(f"/ws/{room_code}/p2"),
        client.websocket_connect(f"/ws/{room_code}/p3"),
        client.websocket_connect(f"/ws/{room_code}/p4"),
        client.websocket_connect(f"/ws/{room_code}/p5"),
    ):
        room = ROOMS[room_code]
        assert len(room.players) == 5

        ws1.send_json({"action": "deal_round"})
        ws1.receive_json()

        assert room.started is True
        assert room.round_number == 1
        assert room.spy_id in room.players
        assert room.current_item is not None

        spies = []
        citizens = []
        for pid in room.players:
            state = room.state_for_player(pid)
            if state["role"] == "spy":
                assert state["item"] is None
                spies.append(pid)
            else:
                assert state["role"] == "citizen"
                assert state["item"] is not None
                assert state["item"]["id"] == room.current_item["id"]
                citizens.append(pid)

        assert len(spies) == 1
        assert len(citizens) == 4
