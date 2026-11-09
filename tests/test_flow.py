"""End-to-end test of the ledger logic with PayPal and Gemini mocked.
Run: python tests/test_flow.py
This does NOT test real PayPal or Gemini calls; do that with your sandbox keys (see README)."""
import os
import sys
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["SIMULATE_PAYOUT"] = "0"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

main.pp_create_order = lambda amount, cur, cid, desc: ("O-" + cid, "https://paypal.test/approve")
main.pp_capture = lambda oid: ("COMPLETED", "CAP-" + oid, "COMPLETED")
main.pp_payout = lambda email, amt, cur, ref, note: ("BATCH-" + ref, "PENDING")
main.ask_llm = lambda *a, **k: "mock answer"


def fake_json(system, user):
    if system.startswith("You turn"):
        return {"name": "Friends", "members": 3, "amount": 50, "currency": "usd", "order": "draw", "summary": "ok"}
    return [{"member": "Ali", "message": "Please pay."}, {"member": "Ghost", "message": "x"}]


main.ask_json = fake_json
c = TestClient(main.app)


def pay_all(gid):
    for l in c.get(f"/api/groups/{gid}").json()["ledger"]:
        if l["status"] != "paid":
            assert c.post(f"/api/payments/{l['id']}/order").json()["approve_url"]
            r = c.get(f"/paypal/return?token=O-k{l['id']}", follow_redirects=False)
            assert r.status_code == 307, r.text
            c.get(f"/paypal/return?token=O-k{l['id']}", follow_redirects=False)  # second visit must be harmless


p = c.post("/api/parse", json={"text": "3 friends 50 dollars monthly"}).json()
assert p["amount"] == 50 and p["currency"] == "USD"
members = [{"name": n, "email": f"{n.lower()}@test.com"} for n in ("Ali", "Sana", "Zoya")]
gid = c.post("/api/groups", json={"config": {**p}, "members": members}).json()["id"]
s = c.get(f"/api/groups/{gid}").json()
assert s["pot"] == 150 and len(s["order"]) == 3 and len(s["ledger"]) == 3 and s["group"]["seed"]
assert c.post(f"/api/groups/{gid}/payout").status_code == 409  # cannot pay out before everyone paid

s = c.post(f"/api/groups/{gid}/demo/make-late?days=3").json()
assert all(l["overdue"] and l["risk"]["level"] == "medium" for l in s["ledger"]), s["ledger"]
rem = c.post(f"/api/groups/{gid}/reminders", json={"language": "Urdu"}).json()["reminders"]
assert [r["member"] for r in rem] == ["Ali"]  # unknown names filtered out
assert c.post(f"/api/groups/{gid}/ask", json={"question": "who is late?"}).json()["answer"] == "mock answer"

for rnd in (1, 2, 3):
    pay_all(gid)
    s = c.get(f"/api/groups/{gid}").json()
    assert s["all_paid"] and s["group"]["current_round"] == rnd
    s = c.post(f"/api/groups/{gid}/payout").json()
    assert len(s["payouts"]) == rnd
    assert c.get(f"/api/groups/{gid}").json()["payouts"][-1]["member"] == s["order"][rnd - 1]
assert s["group"]["status"] == "complete"
assert sorted(p["member"] for p in s["payouts"]) == ["Ali", "Sana", "Zoya"]  # everyone got the pot exactly once
assert c.post(f"/api/groups/{gid}/payout").status_code == 409
assert c.get("/").status_code == 200

# Gemini request/response handling (HTTP mocked)
import httpx  # noqa: E402
from fastapi import HTTPException  # noqa: E402
del main.ask_llm  # restore the real function by re-importing it
import importlib  # noqa: E402
real = importlib.reload(main)
real.GEMINI_KEY = "k"
class R:  # minimal fake response
    def __init__(self, code, js): self.status_code, self._j, self.text = code, js, str(js)
    def json(self): return self._j
sent = {}
def fake_post(url, headers=None, json=None, timeout=None):
    sent.update(url=url, headers=headers, body=json)
    return R(200, {"candidates": [{"content": {"parts": [{"text": '```json\n{"amount": 5}\n```'}]}}]})
httpx.post = fake_post
assert real.ask_json("sys", "hi") == {"amount": 5}
assert sent["headers"]["x-goog-api-key"] == "k" and sent["body"]["generationConfig"]["responseMimeType"] == "application/json"
httpx.post = lambda *a, **k: R(404, {})
try:
    real.ask_llm("s", "u"); raise SystemExit("expected 404 error")
except HTTPException as e:
    assert "GEMINI_MODEL" in e.detail
print("All checks passed")
