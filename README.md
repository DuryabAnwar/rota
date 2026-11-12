# Rota
Live demo: https://rota-1-xb6n.onrender.com/

(Free Render plan: the first load takes about a minute, and data resets when it sleeps.)
An AI-run rotating savings circle (known as kameti, tanda, susu, chama or paluwagan around the world) built for the PayPal AI Hackathon.

Members pay each round through PayPal Checkout. A ledger updates from the PayPal capture (and from webhooks as a backup). When everyone has paid, the pot goes to that round's recipient through PayPal Payouts. Gemini acts as the group's secretary.

## Try it (for judges)
Open the live demo above and wait for it to load. There is no sign-in. Everything runs in the PayPal **sandbox**, so no real money moves.

Test accounts (PayPal sandbox personal accounts, one per member):

| Member | Email | Password |
|---|---|---|
| Ali | `sb-x47gjj53858767@personal.example.com` | `.$00CGp7` |
| Zoya | `sb-1xvw553858761@personal.example.com` | `]0/f0gOG` |
| Sana | `sb-ezotw53858758@personal.example.com` | `xK3R(W$^` |

Walkthrough:
1. Under "Describe the rules", type a sentence such as: `3 friends, 30 dollars each month, order decided by a draw`.
2. Under "Members", paste these three lines exactly (name, email):
   ```
   Ali, sb-x47gjj53858767@personal.example.com
   Zoya, sb-1xvw553858761@personal.example.com
   Sana, sb-ezotw53858758@personal.example.com
   ```
3. Click **Read the rules**, check what Gemini understood, then create the circle. The payout order is drawn at random and the seed is shown.
4. Click **Pay with PayPal** on a member's row. A PayPal sandbox page opens. Log in with one of the accounts above, approve, and you return to the ledger with that member marked Paid. Use a private window, or log out between members. Any of the three accounts can pay any row. The emails matter for the payout, which goes to the recipient whose turn it is.
5. To see the lateness flow, leave one member unpaid and click **Backdate unpaid dues (demo)**. The ledger shows Overdue and a default-risk rating (hover for the reason). Then try **Draft reminders** and ask the secretary a question, in English, Urdu or Roman Urdu.
6. Once everyone has paid, click the payout button. The pot goes to that round's recipient and the next round opens.

## What PayPal does
- **Orders v2**: each member's contribution is a real sandbox order, approved and captured on PayPal.
- **Webhooks** (`PAYMENT.CAPTURE.COMPLETED`, signature-verified): backup path that marks a payment paid even if the buyer closes the tab.
- **Payouts**: sends the pot to the recipient whose turn it is.

## What the AI model (Google Gemini) does
- Turns a plain-language description (English, Urdu or Roman Urdu) into structured rules, shown for confirmation.
- Answers member questions from the ledger ("When is my turn?", "Who hasn't paid?"), in the member's language.
- Drafts polite reminders for unpaid members, and writes a group update.
- The model never decides amounts or moves money. Amounts, the draw, default-risk rules and payouts are plain code. It sees member names and ledger facts only, not emails. Use fake names in demos: Gemini's free tier may use prompts to improve Google's products.

## Run locally
```
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                     # Windows: copy .env.example .env
# fill in PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, GEMINI_API_KEY
uvicorn main:app --reload
```
Open http://localhost:8000. Check the ledger logic without any keys: `python tests/test_flow.py` (PayPal and Gemini are mocked there).

## Gemini key (free)
Open aistudio.google.com, sign in, click Get API key, then Create API key. Paste it into `.env` as `GEMINI_API_KEY`. The free tier is rate-limited, which is enough for development and a demo.

## PayPal sandbox setup
1. developer.paypal.com > Apps & Credentials > Sandbox > Create app. Copy the Client ID and Secret into `.env`. Check the app's feature list for **Payouts** and enable it if shown.
2. Testing > Sandbox accounts: create 3 **Personal** accounts (the members). Use their emails as the member emails in the app. The app's business account receives the contributions and funds the payouts.
3. To pay as a member, open the PayPal approval link in a private window and log in with that member's sandbox account.
4. If your sandbox rejects Payouts, set `SIMULATE_PAYOUT=1`. The UI then labels each payout as simulated. Say so in your video.

## Deploy on Render
Push to a public GitHub repo, create a Web Service from it (`render.yaml` is included), and set the environment variables. Set `BASE_URL` to your Render URL, for example `https://rota.onrender.com`. Data lives in SQLite on Render's ephemeral disk, so it resets on redeploy. That is fine for a demo.

## Webhook (optional backup path)
In the PayPal developer dashboard, open your app > Webhooks > add `https://YOUR-URL/webhooks/paypal` with the event `PAYMENT.CAPTURE.COMPLETED`. Copy the Webhook ID into `PAYPAL_WEBHOOK_ID`. Without it, payments are still recorded when the buyer returns from PayPal.

## Demo script (under 3 minutes)
1. State the problem and one real quote or sourced number about rotating savings circles.
2. Create a group by typing one sentence, in Urdu if you like. Confirm the parsed rules.
3. Pay as each member with the sandbox accounts. Show the ledger updating.
4. Click "Backdate unpaid dues (demo)" with one member unpaid, then show the default-risk flag, draft reminders, and ask the secretary a question.
5. Everyone pays, click payout, show the PayPal batch ID, and the next round opening.

## Limits
Sandbox only, no real money. No sign-in (anyone with the link can use the demo). The test accounts above are sandbox accounts with no real funds. Holding and distributing pooled money for real would need legal and regulatory work this prototype does not do.

## License
MIT. See LICENSE.
