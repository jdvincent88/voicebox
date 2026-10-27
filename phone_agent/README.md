# Phone agent

An AI phone host for a restaurant. It answers calls, answers questions from
your own business info, takes **takeout orders into Square** (texting the
caller a payment link), requests **reservations in Parlor HQ**, records
private-event leads, takes messages, and **transfers** to a person when needed.

```
Caller ──► Twilio number ──► ConversationRelay (speech⇄text, barge-in)
                                   │  WebSocket (text only)
                                   ▼
                         this server (FastAPI)
                         Claude + tools
                ┌──────────────┼────────────────┬──────────────┐
            Square API     Parlor API      Twilio SMS      Voicebox (optional)
         menu + pay links  reservations,   links, staff    greeting + hold clips
                           leads, tasks    alerts          in a cloned voice
```

## What the caller can do

| Request | What happens |
| --- | --- |
| Hours, parking, allergies, events… | Answered from `business.yaml` only, never guessed |
| Takeout | Live Square menu → read-back → payment link texted → order hits POS/KDS **once paid**; staff get a text ticket |
| Reservation | Parlor checks hours, closures, pacing, party limits; booking lands as **Requested** for staff to confirm; guest + staff get a text |
| "Do I have a reservation?" | Looked up in Parlor by caller ID |
| Large party / Magnolia Room / catering | Parlor event lead (Pipeline → Inquiry) + staff text |
| "Can I talk to someone?" / complaint / presses 0 | Transfer to the right number; if nobody answers, staff get a "call back" text |
| Message | Staff text + Parlor task |

## Setup

### 1. Twilio (about 20 minutes, plus SMS registration)

1. Create an account at twilio.com and upgrade it (trial accounts can only call verified numbers).
2. **Phone Numbers → Buy a number** with Voice + SMS, local to your area code.
3. Start **Messaging → Regulatory compliance → A2P 10DLC** registration (brand + campaign
   "Customer care / transactional"). US carriers block texts from unregistered numbers.
   Approval usually takes several days, so do this first. (A toll-free number with toll-free
   verification is the alternative.)
4. Voice → **ConversationRelay**: accept the AI/ML features addendum in the console if prompted.
5. On the number's **Voice configuration**: "A call comes in" → Webhook →
   `https://YOUR_HOST/voice/incoming`, HTTP POST.

### 2. Square

1. developer.squareup.com → create an app → **Production** access token.
2. Copy your **Location ID** (Locations tab).
3. Make sure phone-orderable items have prices, required choices are set as modifier lists
   with "min 1", and items you don't sell by phone are in a category listed under
   `takeout.hidden_categories`.

### 3. Parlor HQ

Parlor → Settings → Developers → create a **read_write** API key → `PARLOR_API_KEY`.
Phone bookings show up as **Requested** with source "zapier/API" and the note
"Booked by phone agent".

### 4. Configure and run

```bash
cd phone_agent
cp .env.example .env              # fill in keys
cp business.example.yaml business.yaml   # fill in every TODO
pip install -r requirements.txt
set -a; . ./.env; set +a
uvicorn app.main:app --host 0.0.0.0 --port 8080 --proxy-headers
```

It needs a public **https** URL. Options, easiest first:

- **Railway / Render / Fly.io**: deploy the `Dockerfile`, set the env vars, mount a volume at `/data`.
- **Local test**: `cloudflared tunnel --url http://localhost:8080` (or ngrok), then put that URL in
  `PUBLIC_BASE_URL` and the Twilio webhook.

Check `GET /health`: it should show your menu item count and `"sms": true`.

### 5. Test before going live

1. Call the Twilio number from your cell. Try: hours question, takeout order, reservation for
   tomorrow, "can I speak to a manager".
2. Pay one small order through the link and confirm it appears on the POS/KDS.
3. Then forward the restaurant line to the Twilio number (all day, after N rings, or only at
   rush times). Most carriers support conditional "forward on no answer".

### Voicebox voice (optional)

Set `VOICEBOX_URL` (where the Voicebox app/server is reachable from this server, e.g. over
Tailscale) and `VOICEBOX_PROFILE` (profile name or id). On startup the greeting and the
"connecting you" line are rendered once in that voice and cached in `data/audio`.

Live replies use the phone TTS voice (`TTS_PROVIDER` / `TTS_VOICE`). Voicebox renders whole
clips before returning them, which is too slow for a live conversation. Pick a TTS voice close
to your clone so the hand-off isn't jarring.

## Running costs (approximate, verify current pricing)

| Item | Approx. cost |
| --- | --- |
| Twilio local number | ~$1.15/month |
| Inbound call minutes | ~$0.0085/min |
| ConversationRelay (speech + TTS) | ~$0.07/min |
| Claude (Opus 5.5, low effort, cached prompt) | roughly a few cents per call |
| SMS | ~$0.008/segment + carrier fees |

A 3-minute call is roughly **$0.25–0.35** all-in.

## Tuning

- `ANTHROPIC_MODEL` / `ANTHROPIC_EFFORT`: the default is Claude Opus 5.5 at low effort. If
  replies feel slow, try `claude-haiku-5-5` and compare on test calls; raise effort if
  order accuracy suffers.
- Everything the agent says about the business comes from `business.yaml`. Keep it current.
- The menu refreshes from Square every 15 minutes; sold-out variations disappear automatically.

## Development

```bash
pip install -r requirements.txt pytest pytest-asyncio
python -m pytest
```

Tests use fakes for Square, Parlor, Twilio and Claude. Parlor has no sandbox, so never point
tests at a real key.

## Known limits

- Square shows API orders on the POS only after payment. "Pay at pickup" orders aren't
  supported; the agent offers a transfer instead.
- One Square location and one Parlor venue per deployment.
- Calls aren't recorded or transcribed to disk; call logs are in the server log.
