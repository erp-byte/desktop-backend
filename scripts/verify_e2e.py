"""End-to-end verification: webhook + WS event delivery."""
import asyncio, json, os, sys, time
import httpx, websockets, asyncpg
from dotenv import load_dotenv
load_dotenv()

SESSION = "gsd-verify2-5364267572c34d72bf325863825c4721"
BASE = "http://127.0.0.1:8000"
INTERNAL_TOK = os.environ["INTERNAL_WEBHOOK_TOKEN"]
DB = os.environ["DATABASE_URL"]


async def main():
    async with httpx.AsyncClient() as http:
        ep = await http.get(f"{BASE}/api/v1/webhooks/endpoints", headers={"Authorization": f"Bearer {SESSION}"})
        endpoints = ep.json()
        print(f"[setup] existing endpoints: {len(endpoints)}")
        endpoint_id = endpoints[0]["id"]

        sub = await http.post(
            f"{BASE}/api/v1/webhooks/subscriptions",
            headers={"Authorization": f"Bearer {SESSION}", "Content-Type": "application/json"},
            json={"endpoint_id": endpoint_id, "event_type": "ping", "filter_jsonb": {}},
        )
        print(f"[setup] subscribe -> {sub.status_code}: {sub.text[:120]}")

        ws_tok = await http.post(f"{BASE}/api/v1/ws/token", headers={"Authorization": f"Bearer {SESSION}"})
        token = ws_tok.json()["token"]

        received = []

        async def ws_listener():
            async with websockets.connect(f"ws://127.0.0.1:8000/ws?token={token}") as ws:
                print("[ws] connected")
                try:
                    for _ in range(5):
                        msg = await asyncio.wait_for(ws.recv(), timeout=10)
                        received.append(json.loads(msg))
                        print(f"[ws] received: {msg[:200]}")
                        if any(m.get("event_type") == "ping" for m in received):
                            return
                except asyncio.TimeoutError:
                    print("[ws] timeout waiting for events")

        listener_task = asyncio.create_task(ws_listener())
        await asyncio.sleep(0.5)

        event_id = f"gsd-e2e-{int(time.time())}"
        inj = await http.post(
            f"{BASE}/internal/events",
            headers={"Authorization": f"Bearer {INTERNAL_TOK}", "Content-Type": "application/json"},
            json={"event_type": "ping", "entity": "cfpl", "event_id": event_id,
                  "payload": {"msg": "end-to-end verify", "ts": int(time.time())}},
        )
        print(f"[inject] {inj.status_code}: {inj.text}")

        try:
            await asyncio.wait_for(listener_task, timeout=15)
        except asyncio.TimeoutError:
            listener_task.cancel()

        await asyncio.sleep(2)

        conn = await asyncpg.connect(DB)
        deliveries = await conn.fetch(
            "SELECT id, endpoint_id, event_type, event_id, status, response_code, attempts, response_body FROM webhook_delivery ORDER BY id DESC LIMIT 5"
        )
        print("\n[db] recent webhook_delivery rows:")
        for d in deliveries:
            print(f"  {dict(d)}")
        await conn.close()

        ws_ping = any(m.get("event_type") == "ping" for m in received)
        webhook_delivered = any(d["event_type"] == "ping" and d["status"] in ("delivered", "success") for d in deliveries)
        print(f"\n[verdict] ws received ping: {ws_ping}")
        print(f"[verdict] webhook delivered: {webhook_delivered}")
        sys.exit(0 if (ws_ping and webhook_delivered) else 1)


if __name__ == "__main__":
    asyncio.run(main())
