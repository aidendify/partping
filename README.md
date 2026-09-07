# PartPing

Free, self-hosted parts-status pages for local service owners. Create a job, share a magic link, flip milestones as the part moves, and let customers check the timeline instead of calling the shop.

No signup. No license. One Docker Compose service and a SQLite file. About 15 minutes on a 1GB VPS.

## What it does

- Create a parts job (customer, part name, optional email/phone/job ref/distributor/notes/ETA)
- Share an unguessable public link `{PUBLIC_BASE_URL}/p/{token}` - Copy link, Copy SMS/text blurb, Copy email, or optional Notify customer now
- Flip fixed milestones: ordered -> shipped -> arrived -> ready -> closed | cancelled
- Customer sees a clean timeline + ETA on a mobile-friendly page (no login)
- Optional BYO SMTP email and/or Twilio SMS on milestone change
- Paste-from-distributor textarea stores text only (does not auto-parse ETA)
- `GET /health` -> HTTP 200 `{"status":"ok","smtp_configured":false,"sms_configured":false}` even when SMTP/Twilio unset

Without SMTP or Twilio you still get in-app status and can copy the link / blurbs. Product is fully usable via Copy link alone.

## Privacy

Self-hosted. You run the box; the owner is the data controller for customer names and contact fields. No Stripe, no bundled SMS numbers, no third-party analytics SaaS. Data lives in your SQLite file on the Compose volume.

## 15-minute Ubuntu VPS install

Documented on **Ubuntu 22.04 / 24.04**. About 15 minutes.

**Debian 13:** do **not** run the Ubuntu `docker-ce` recipe below on Debian. Use the distro packages instead:

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose
sudo usermod -aG docker "$USER"
```

Log out and back in (or `newgrp docker`). On Debian, start the stack with `docker-compose` (hyphen) if `docker compose` is not available.

**Amazon Linux:** not documented yet. Use Ubuntu or Debian.

### 1. Install Docker Engine and the Compose plugin (Ubuntu only)

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo ${UBUNTU_CODENAME:-$VERSION_CODENAME}) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Log out and back in (or run `newgrp docker`) so `docker` works without `sudo`.

### 2. Clone, configure, start

```bash
git clone https://github.com/aidendify/partping.git
cd partping
cp .env.example .env
```

Edit `.env` and set at least `BUSINESS_NAME`, `PUBLIC_BASE_URL`, `SECRET_KEY`, and `OWNER_PASSWORD`. Leave `SMTP_*`, Twilio, and `MARKETING_URL` empty unless configured. Set `OWNER_PASSWORD` on any VPS reachable from the internet (empty means the admin UI is open).

```bash
docker compose up --build -d
```

(On Debian, `docker-compose up --build -d` if the Compose plugin is not installed.)

The app binds `0.0.0.0:8080` in the container. Compose maps host `8080:8080`. SQLite lives on the `partping-data` volume at `/data/partping.db`.

### 3. Smoke test

Use this `.env` for a first pass (Verifier values). Production should use a real `SECRET_KEY` and `OWNER_PASSWORD`. Do not bake these test passwords as production defaults.

```
OWNER_PASSWORD=testpass
PUBLIC_BASE_URL=http://localhost:8080
BUSINESS_NAME=Harbor HVAC
MARKETING_URL=
SECRET_KEY=change-me
```

Leave all `SMTP_*` and Twilio vars unset.

1. Healthcheck:

   ```bash
   curl -sf http://localhost:8080/health
   ```

   Expected: JSON containing `"status":"ok"`, `"smtp_configured":false`, `"sms_configured":false`, HTTP 200.

2. Open http://localhost:8080, log in with `testpass`, create a job with part **TXV valve** and customer **Priya Sharma**. Copy the `/p/{token}` link from the detail page.

3. Open the public link (second browser or phone). Confirm business name, part name, milestone **Part ordered**, and a created timeline entry.

4. On the owner detail page, set milestone to **shipped**, ETA **2030-01-15**, note **Left supply house**. Refresh the public page - shipped, ETA, and a new timeline entry appear.

5. Setting **shipped** again with no changes is a no-op (no duplicate milestone event). Set **closed** - job leaves the open dashboard; public page stays readable.

See `sample-job.md` for example field values.

## Configuration

Copy `.env.example` to `.env` before `docker compose up`. Variables:

| Variable | Purpose |
| --- | --- |
| `PORT` | Documented as 8080. The container always binds gunicorn to `0.0.0.0:8080`. |
| `DATABASE_PATH` | SQLite file. Compose overrides this to `/data/partping.db`. |
| `SECRET_KEY` | Flask session key. Change it on a public VPS. |
| `OWNER_PASSWORD` | Admin login. Empty = open admin (local/dev). Set this on any internet-reachable VPS. |
| `BUSINESS_NAME` | Public page. |
| `BUSINESS_PHONE` | Optional phone shown under "Questions? Call us." |
| `PUBLIC_BASE_URL` | No trailing slash. Used in magic links, e.g. `http://localhost:8080`. |
| `FROM_NAME`, `FROM_EMAIL` | SMTP From / email blurb sign-off. |
| `OWNER_NOTIFY_EMAIL` | Optional copy of milestone changes when SMTP is set. Empty -> in-app only. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_TLS` | Optional email notify. If `SMTP_HOST` is unset, email notify is unused. |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` | Optional SMS notify. If unset, SMS UI is hidden. |
| `MARKETING_URL` | If set, footer link **Powered by PartPing** points here. If unset, there is no footer. |

Do not commit `.env`. SMTP / Twilio secrets and `OWNER_PASSWORD` are never written to application logs.

## Healthcheck

`GET /health` -> HTTP 200:

```json
{"status":"ok","smtp_configured":false,"sms_configured":false}
```

`smtp_configured` is `true` only when `SMTP_HOST` is set. `sms_configured` is `true` only when all three Twilio vars are set. Health succeeds even when both are unset. This route never requires login.

## Local development (optional)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_PATH=./partping.db
export OWNER_PASSWORD=testpass
export PUBLIC_BASE_URL=http://localhost:8080
export BUSINESS_NAME="Harbor HVAC"
python app.py
```

Then open http://localhost:8080. This path is for hacking on the code; the supported install is Docker Compose.

```bash
python -m unittest test_app.py -v
```

## What this is not

PartPing is **not** a Nudge-style multi-day drip (no Day 0/3/7 sequence). It is **not** AfterJob (no CSAT / Google review ask). It is **not** FormFirst (no contact-form webhook). It is **not** OpenPing (no quote open-tracking). It is **not** ChangeSlip (no priced Accept).

No distributor APIs, no live tracking maps, no Stripe / payments, no Redis, no Celery, no LLM, no second Compose service.
