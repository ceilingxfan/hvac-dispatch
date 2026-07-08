# Beola the 3 Day Board

A Streamlit dispatch board for managing HVAC service capacity across a rolling 3-day window.

## Features

- **CSR Booking & Standby Hub** — Book customer calls with priority tiers, capacity checks, and move-up standby queue
- **Parts & Repairs Hub** — Schedule special repair jobs with technician pre-assignment
- **Dispatch Operational Desk** — Call-out board, reschedule queue, tech attendance, routing grid
- **Live Summary Board** — Color-coded huddle snapshot for today, tomorrow, and day 3

## Run locally

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

The app creates `hvac_board.db` automatically on first launch.

## Deploy on Streamlit Community Cloud (free)

1. Push this repo to GitHub (`ceilingxfan/hvac-dispatch`)
2. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with GitHub
3. Click **Create app**
4. Set **Repository** to `ceilingxfan/hvac-dispatch`, **Branch** to `main`, **Main file path** to `app.py`
5. Click **Deploy**

Share the app URL with your team. The database starts fresh in the cloud (`hvac_board.db` is not in git).

## Tech Stack

- Python · Streamlit · SQLite · Pandas
