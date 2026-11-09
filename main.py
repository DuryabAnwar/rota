"""Rota: an AI-run rotating savings circle on PayPal (sandbox) with Gemini.

Design rule: money movement and amounts are decided by plain code and the ledger.
The AI model only reads text, explains, drafts messages and answers from the ledger data.
"""
import json
import os
import random
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

load_dotenv()
PP = "https://api-m.sandbox.paypal.com"  # sandbox only, on purpose
CID = os.getenv("PAYPAL_CLIENT_ID", "")
SEC = os.getenv("PAYPAL_CLIENT_SECRET", "")
BASE = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
DB = os.getenv("DB_PATH", "kameti.db")
SIMULATE_PAYOUT = os.getenv("SIMULATE_PAYOUT", "0") == "1"
WEBHOOK_ID = os.getenv("PAYPAL_WEBHOOK_ID", "")
ROUND_DAYS = 30

app = FastAPI(title="Rota")

# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS circles(id INTEGER PRIMARY KEY, name TEXT, amount REAL, currency TEXT,
  total_rounds INTEGER, current_round INTEGER DEFAULT 1, order_json TEXT, seed TEXT, rules TEXT,
  status TEXT DEFAULT 'active');
CREATE TABLE IF NOT EXISTS members(id INTEGER PRIMARY KEY, group_id INTEGER, name TEXT, email TEXT);
CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY, group_id INTEGER, rnd INTEGER, member_id INTEGER,
  due TEXT, status TEXT DEFAULT 'pending', order_id TEXT, capture_id TEXT, paid_at TEXT);
CREATE TABLE IF NOT EXISTS payouts(id INTEGER PRIMARY KEY, group_id INTEGER, rnd INTEGER, member_id INTEGER,
  amount REAL, batch_id TEXT, status TEXT, simulated INTEGER, at TEXT);
"""


def _c():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def qa(sql, a=()):
    c = _c()
    try:
        return [dict(r) for r in c.execute(sql, a).fetchall()]
    finally:
        c.close()


def qo(sql, a=()):
    r = qa(sql, a)
    return r[0] if r else None


def ex(sql, a=()):
    c = _c()
    try:
        cur = c.execute(sql, a)
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


_c0 = _c()
_c0.executescript(SCHEMA)
_c0.commit()
_c0.close()


def now():
    return datetime.now(timezone.utc)


def iso(d):
    return d.isoformat(timespec="seconds")


def parse_dt(s):
    return datetime.fromisoformat(s)


# ---------------------------------------------------------------- PayPal
_tok = {"v": None, "exp": 0.0}


def pp_token():
    if _tok["v"] and time.time() < _tok["exp"] - 60:
        return _tok["v"]
    if not (CID and SEC):
        raise HTTPException(500, "PayPal credentials are missing. Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET.")
    r = httpx.post(f"{PP}/v1/oauth2/token", data={"grant_type": "client_credentials"}, auth=(CID, SEC), timeout=30)
    if r.status_code != 200:
        raise HTTPException(502, f"PayPal sign-in failed: {r.text[:200]}")
    j = r.json()
    _tok.update(v=j["access_token"], exp=time.time() + j["expires_in"])
    return _tok["v"]


def pp(method, path, headers=None, **kw):
    h = {"Authorization": f"Bearer {pp_token()}"}
    h.update(headers or {})
    return httpx.request(method, PP + path, headers=h, timeout=30, **kw)


def pp_create_order(amount, cur, custom_id, desc):
    r = pp("POST", "/v2/checkout/orders", json={
        "intent": "CAPTURE",
        "purchase_units": [{"custom_id": custom_id, "description": desc[:120],
                            "amount": {"currency_code": cur, "value": f"{amount:.2f}"}}],
        "payment_source": {"paypal": {"experience_context": {
            "return_url": f"{BASE}/paypal/return", "cancel_url": f"{BASE}/?cancelled=1",
            "user_action": "PAY_NOW", "brand_name": "Rota"}}},
    })
    if r.status_code not in (200, 201):
        raise HTTPException(502, f"PayPal could not create the order: {r.text[:300]}")
    j = r.json()
    url = next((l["href"] for l in j.get("links", []) if l["rel"] in ("payer-action", "approve")), None)
    if not url:
        raise HTTPException(502, "PayPal returned no approval link.")
    return j["id"], url


def pp_capture(order_id):
    r = pp("POST", f"/v2/checkout/orders/{order_id}/capture", headers={"Prefer": "return=representation"}, json={})
    if r.status_code == 422 and "ORDER_ALREADY_CAPTURED" in r.text:
        r = pp("GET", f"/v2/checkout/orders/{order_id}")
    if r.status_code not in (200, 201):
        raise HTTPException(502, f"PayPal capture failed: {r.text[:300]}")
    j = r.json()
    cap = j["purchase_units"][0]["payments"]["captures"][0]
    return j["status"], cap["id"], cap["status"]


def pp_payout(email, amount, cur, ref, note):
    r = pp("POST", "/v1/payments/payouts", json={
        "sender_batch_header": {"sender_batch_id": ref, "email_subject": "Your Rota payout", "email_message": note},
        "items": [{"recipient_type": "EMAIL", "amount": {"value": f"{amount:.2f}", "currency": cur},
                   "receiver": email, "note": note, "sender_item_id": ref}]})
    if r.status_code not in (200, 201):
        raise HTTPException(502, f"PayPal payout failed: {r.text[:300]}. "
                                 "If Payouts is not enabled for your sandbox app, set SIMULATE_PAYOUT=1.")
    h = r.json()["batch_header"]
    return h["payout_batch_id"], h.get("batch_status", "PENDING")


def pp_verify_webhook(headers, event):
    r = pp("POST", "/v1/notifications/verify-webhook-signature", json={
        "auth_algo": headers.get("paypal-auth-algo"), "cert_url": headers.get("paypal-cert-url"),
        "transmission_id": headers.get("paypal-transmission-id"),
        "transmission_sig": headers.get("paypal-transmission-sig"),
        "transmission_time": headers.get("paypal-transmission-time"),
        "webhook_id": WEBHOOK_ID, "webhook_event": event})
    return r.status_code == 200 and r.json().get("verification_status") == "SUCCESS"


# ---------------------------------------------------------------- AI model (Gemini, REST)
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"


def ask_llm(system, user, as_json=False):
    if not GEMINI_KEY:
        raise HTTPException(500, "Gemini key is missing. Set GEMINI_API_KEY in .env.")
    gen = {"temperature": 0.3}
    if as_json:
        gen["responseMimeType"] = "application/json"
    body = {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}], "generationConfig": gen}
    for attempt in range(3):  # free tier can briefly rate-limit; retry with a short pause
        r = httpx.post(GEMINI_URL.format(GEMINI_MODEL), headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=60)
        if r.status_code in (429, 503) and attempt < 2:
            time.sleep(3 * (attempt + 1))
            continue
        break
    if r.status_code == 404:
        raise HTTPException(502, f"Gemini model '{GEMINI_MODEL}' was not found. Set GEMINI_MODEL in .env to a "
                                 "Flash model listed in Google AI Studio.")
    if r.status_code == 429:
        raise HTTPException(502, "Gemini free-tier limit reached. Wait a minute and try again.")
    if r.status_code != 200:
        raise HTTPException(502, f"Gemini request failed: {r.text[:200]}")
    try:
        parts = r.json()["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError):
        raise HTTPException(502, "Gemini returned no answer (it may have been blocked). Try rephrasing.")
    return "".join(p.get("text", "") for p in parts).strip()


def ask_json(system, user):
    t = ask_llm(system + "\nReturn only valid JSON. No markdown fences, no commentary.", user, as_json=True)
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t.strip())
    try:
        return json.loads(t)
    except ValueError:
        raise HTTPException(502, "The AI returned something unreadable. Try again.")


PARSE_SYS = """You turn a kameti organizer's description into settings. A kameti is a rotating savings \
committee: every member pays the same amount each round and one member receives the whole pot each round. \
The description may be in English, Urdu or Roman Urdu. Return a JSON object with keys: \
name (short group name), members (integer, or null if not stated), amount (number each member pays per round, \
or null if not stated), currency (3-letter code, default USD), order ("draw" for a random draw, "manual" if the \
organizer sets the order), summary (one or two sentences restating the rules in the language the organizer \
wrote in, so they can confirm them). Never invent an amount."""

ASK_SYS = """You are the secretary of a kameti (rotating savings committee). Answer only from the JSON data in the \
message. If the data does not say, say you don't know. Reply in the language and script the member used \
(English, Urdu or Roman Urdu), in one to four short sentences. You cannot move money: payments and payouts happen \
only through the app's buttons. Risk levels come from simple rules shown in the data; explain them plainly and \
never invent reasons."""

REM_SYS = """You are the secretary of a savings circle (a kameti). Write one short, polite payment reminder for each member who has \
not paid this round, in LANGUAGE. Mention the amount, currency and due date from the data; if the payment is \
overdue, say so gently. No threats, no emojis. Return a JSON list of objects {"member": name, "message": text}, \
one per unpaid member, using the names exactly as given."""

SUM_SYS = """You are the secretary of a savings circle (a kameti). Write a short update for the group in LANGUAGE, three to five \
sentences, from the JSON data only: who has paid, who hasn't, who receives the pot this round, and anything \
overdue. Neutral, factual tone. Do not shame anyone."""


# ---------------------------------------------------------------- ledger logic
def risk_for(member_payments, t):
    late = sum(1 for p in member_payments if p["status"] == "paid" and parse_dt(p["paid_at"]) > parse_dt(p["due"]))
    over = [p for p in member_payments if p["status"] != "paid" and parse_dt(p["due"]) < t]
    days = max(((t - parse_dt(p["due"])).days for p in over), default=0)
    score = late * 2 + ((3 + min(days, 10) // 5) if over else 0)
    reasons = []
    if late:
        reasons.append(f"paid late {late} time(s) in earlier rounds")
    if over:
        reasons.append(f"this round's payment is overdue by {days} day(s)")
    return {"score": score, "level": "high" if score >= 5 else "medium" if score >= 2 else "low", "reasons": reasons}


def open_round(gid, rnd, member_ids):
    due = iso(now() + timedelta(days=ROUND_DAYS))
    for m in member_ids:
        ex("INSERT INTO payments(group_id,rnd,member_id,due) VALUES(?,?,?,?)", (gid, rnd, m, due))


def state(gid):
    g = qo("SELECT * FROM circles WHERE id=?", (gid,))
    if not g:
        raise HTTPException(404, "Circle not found")
    members = {m["id"]: m for m in qa("SELECT * FROM members WHERE group_id=?", (gid,))}
    pays = qa("SELECT * FROM payments WHERE group_id=? ORDER BY rnd, member_id", (gid,))
    payouts = qa("SELECT * FROM payouts WHERE group_id=? ORDER BY rnd", (gid,))
    order = json.loads(g["order_json"])
    t = now()
    cur = [p for p in pays if p["rnd"] == g["current_round"]]
    ledger = [{"id": p["id"], "member": members[p["member_id"]]["name"], "due": p["due"], "status": p["status"],
               "paid_at": p["paid_at"], "overdue": p["status"] != "paid" and parse_dt(p["due"]) < t,
               "risk": risk_for([x for x in pays if x["member_id"] == p["member_id"]], t)} for p in cur]
    active = g["status"] == "active"
    return {
        "group": {k: g[k] for k in ("id", "name", "amount", "currency", "total_rounds", "current_round", "seed",
                                    "rules", "status")},
        "order": [members[i]["name"] for i in order],
        "recipient": members[order[g["current_round"] - 1]]["name"] if active else None,
        "pot": round(g["amount"] * len(members), 2),
        "ledger": ledger,
        "all_paid": bool(cur) and all(p["status"] == "paid" for p in cur),
        "payouts": [{"round": po["rnd"], "member": members[po["member_id"]]["name"], "amount": po["amount"],
                     "batch_id": po["batch_id"], "status": po["status"], "simulated": bool(po["simulated"]),
                     "at": po["at"]} for po in payouts],
    }


def ai_context(gid):
    """What the AI model sees: names and ledger facts only. No emails, no payment IDs."""
    s = state(gid)
    return json.dumps({
        "today": now().date().isoformat(), "group": {k: s["group"][k] for k in ("name", "amount", "currency",
                                                                                 "total_rounds", "current_round", "status", "rules")},
        "payout_order": s["order"], "pot": s["pot"], "this_round_recipient": s["recipient"],
        "ledger_this_round": [{"member": l["member"], "status": l["status"], "due": l["due"][:10],
                               "overdue": l["overdue"], "risk": l["risk"]} for l in s["ledger"]],
        "payouts_done": [{"round": p["round"], "to": p["member"], "amount": p["amount"],
                          "simulated": p["simulated"]} for p in s["payouts"]],
    }, ensure_ascii=False)


# ---------------------------------------------------------------- API models
class ParseIn(BaseModel):
    text: str = Field(min_length=5, max_length=1000)


class Cfg(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    amount: float = Field(gt=0, le=100000)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    order: Literal["draw", "manual"] = "draw"
    summary: str = Field(default="", max_length=600)


class Mem(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    email: str = Field(min_length=3, max_length=120, pattern=r"^[^@\s]+@[^@\s]+$")


class NewGroup(BaseModel):
    config: Cfg
    members: list[Mem] = Field(min_length=2, max_length=24)


class AskIn(BaseModel):
    question: str = Field(min_length=2, max_length=400)


class LangIn(BaseModel):
    language: Literal["English", "Urdu", "Roman Urdu"] = "English"


# ---------------------------------------------------------------- routes
@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.post("/api/parse")
def parse_rules(b: ParseIn):
    j = ask_json(PARSE_SYS, b.text)
    try:
        amt = float(j["amount"])
        assert amt > 0
    except (KeyError, TypeError, ValueError, AssertionError):
        raise HTTPException(422, "I couldn't find how much each member pays. Add the amount to the description.")
    return {"name": str(j.get("name") or "Our circle")[:80], "members": j.get("members"),
            "amount": round(amt, 2), "currency": str(j.get("currency") or "USD").upper()[:3],
            "order": "manual" if j.get("order") == "manual" else "draw", "summary": str(j.get("summary") or "")}


@app.get("/api/groups")
def list_groups():
    return qa("SELECT id, name, status, current_round, total_rounds FROM circles ORDER BY id DESC")


@app.post("/api/groups")
def create_group(b: NewGroup):
    c = b.config
    gid = ex("INSERT INTO circles(name,amount,currency,total_rounds,order_json,seed,rules) VALUES(?,?,?,?,?,?,?)",
             (c.name.strip(), round(c.amount, 2), c.currency.upper(), len(b.members), "[]", "", c.summary))
    mids = [ex("INSERT INTO members(group_id,name,email) VALUES(?,?,?)", (gid, m.name.strip(), m.email.strip()))
            for m in b.members]
    order, seed = mids[:], ""
    if c.order == "draw":  # seed is stored, so anyone can re-run the shuffle and verify the order
        seed = secrets.token_hex(6)
        random.Random(seed).shuffle(order)
    ex("UPDATE circles SET order_json=?, seed=? WHERE id=?", (json.dumps(order), seed, gid))
    open_round(gid, 1, mids)
    return {"id": gid}


@app.get("/api/groups/{gid}")
def get_group(gid: int):
    return state(gid)


@app.post("/api/payments/{pid}/order")
def pay_order(pid: int):
    p = qo("SELECT * FROM payments WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404, "Payment not found")
    if p["status"] == "paid":
        raise HTTPException(409, "This payment is already paid")
    g = qo("SELECT * FROM circles WHERE id=?", (p["group_id"],))
    m = qo("SELECT * FROM members WHERE id=?", (p["member_id"],))
    oid, url = pp_create_order(g["amount"], g["currency"], f"k{pid}", f"{g['name']}, round {p['rnd']}, {m['name']}")
    ex("UPDATE payments SET order_id=? WHERE id=?", (oid, pid))
    return {"approve_url": url}


def mark_paid(pid, capture_id):
    ex("UPDATE payments SET status='paid', capture_id=?, paid_at=? WHERE id=? AND status!='paid'",
       (capture_id, iso(now()), pid))


@app.get("/paypal/return")
def paypal_return(token: str):
    p = qo("SELECT * FROM payments WHERE order_id=?", (token,))
    if not p:
        raise HTTPException(404, "Unknown PayPal order")
    _, cap_id, cap_status = pp_capture(token)
    if cap_status == "COMPLETED":
        mark_paid(p["id"], cap_id)
    return RedirectResponse(f"/?g={p['group_id']}&paid={'1' if cap_status == 'COMPLETED' else '0'}")


@app.post("/webhooks/paypal")
async def paypal_webhook(req: Request):
    """Backup path: marks a contribution paid when PayPal confirms the capture, even if the buyer closed the tab."""
    if not WEBHOOK_ID:
        raise HTTPException(503, "Set PAYPAL_WEBHOOK_ID to enable webhooks")
    event = await req.json()
    if not await run_in_threadpool(pp_verify_webhook, dict(req.headers), event):
        raise HTTPException(400, "Invalid webhook signature")
    if event.get("event_type") == "PAYMENT.CAPTURE.COMPLETED":
        res = event.get("resource", {})
        cid = res.get("custom_id", "")
        if cid.startswith("k") and cid[1:].isdigit():
            mark_paid(int(cid[1:]), res.get("id"))
    return {"ok": True}


@app.post("/api/groups/{gid}/payout")
def payout(gid: int):
    s = state(gid)
    g = s["group"]
    if g["status"] != "active":
        raise HTTPException(409, "This circle is complete")
    if not s["all_paid"]:
        raise HTTPException(409, "Not everyone has paid this round yet")
    if qo("SELECT id FROM payouts WHERE group_id=? AND rnd=?", (gid, g["current_round"])):
        raise HTTPException(409, "This round has already been paid out")
    order = json.loads(qo("SELECT order_json FROM circles WHERE id=?", (gid,))["order_json"])
    m = qo("SELECT * FROM members WHERE id=?", (order[g["current_round"] - 1],))
    ref = f"kameti-{gid}-{g['current_round']}-{secrets.token_hex(3)}"
    if SIMULATE_PAYOUT:
        batch, status = "SIMULATED-" + ref, "SIMULATED"
    else:
        batch, status = pp_payout(m["email"], s["pot"], g["currency"], ref,
                                  f"Your {g['name']} payout for round {g['current_round']}")
    ex("INSERT INTO payouts(group_id,rnd,member_id,amount,batch_id,status,simulated,at) VALUES(?,?,?,?,?,?,?,?)",
       (gid, g["current_round"], m["id"], s["pot"], batch, status, int(SIMULATE_PAYOUT), iso(now())))
    if g["current_round"] < g["total_rounds"]:
        ex("UPDATE circles SET current_round=current_round+1 WHERE id=?", (gid,))
        open_round(gid, g["current_round"] + 1, order)
    else:
        ex("UPDATE circles SET status='complete' WHERE id=?", (gid,))
    return state(gid)


@app.post("/api/groups/{gid}/demo/make-late")
def make_late(gid: int, days: int = 3):
    """Demo helper: backdates this round's unpaid dues so the lateness flow can be shown on camera."""
    s = state(gid)
    ex("UPDATE payments SET due=? WHERE group_id=? AND rnd=? AND status!='paid'",
       (iso(now() - timedelta(days=max(1, min(days, 30)))), gid, s["group"]["current_round"]))
    return state(gid)


@app.post("/api/groups/{gid}/ask")
def ask(gid: int, b: AskIn):
    return {"answer": ask_llm(ASK_SYS, f"DATA:\n{ai_context(gid)}\n\nQUESTION: {b.question}")}


@app.post("/api/groups/{gid}/reminders")
def reminders(gid: int, b: LangIn):
    s = state(gid)
    if not any(l["status"] != "paid" for l in s["ledger"]):
        return {"reminders": []}
    out = ask_json(REM_SYS.replace("LANGUAGE", b.language), f"DATA:\n{ai_context(gid)}")
    names = {l["member"] for l in s["ledger"] if l["status"] != "paid"}
    if not isinstance(out, list):
        raise HTTPException(502, "The AI returned something unreadable. Try again.")
    return {"reminders": [{"member": str(o.get("member")), "message": str(o.get("message", ""))}
                          for o in out if isinstance(o, dict) and o.get("member") in names]}


@app.post("/api/groups/{gid}/summary")
def summary(gid: int, b: LangIn):
    return {"summary": ask_llm(SUM_SYS.replace("LANGUAGE", b.language), f"DATA:\n{ai_context(gid)}")}
