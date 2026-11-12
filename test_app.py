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
        assert room.first_speaker_id in room.players
        assert room.current_item is not None

        spies = []
        citizens = []
        for pid in room.players:
            state = room.state_for_player(pid)
            assert state["first_speaker_id"] == room.first_speaker_id
            assert state["first_speaker_name"] == room.players[room.first_speaker_id].name
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


def test_change_name_in_lobby() -> None:
    ROOMS.clear()
    create_res = client.post("/api/rooms/create", json={"name": "OldName", "player_id": "p1"})
    assert create_res.status_code == 200
    room_code = create_res.json()["room_code"]

    with client.websocket_connect(f"/ws/{room_code}/p1") as ws:
        init_state = ws.receive_json()
        assert init_state["players"][0]["name"] == "OldName"

        # Change name via WebSocket
        ws.send_json({"action": "change_name", "name": "NewName"})
        updated_state = ws.receive_json()
        assert updated_state["players"][0]["name"] == "NewName"

    # Change name via HTTP action
    action_res = client.post(
        f"/api/rooms/{room_code}/action",
        json={"player_id": "p1", "action": "change_name", "name": "HttpName"},
    )
    assert action_res.status_code == 200
    assert action_res.json()["players"][0]["name"] == "HttpName"
    assert ROOMS[room_code].players["p1"].name == "HttpName"
