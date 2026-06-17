# HVAC 3-Day Capacity Board

A Streamlit dispatch board for managing HVAC service capacity across a rolling 3-day window.

## Features

- **CSR Booking & Standby Hub** — Book customer calls with priority tiers, capacity checks, and move-up standby queue
- **Parts & Repairs Hub** — Schedule special repair jobs with technician pre-assignment
- **Dispatch Operational Desk** — Manage tech attendance, route jobs, and edit the live grid
- **Live Summary Board** — Color-coded huddle snapshot for today, tomorrow, and day 3

## Setup

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
streamlit run app.py
```

The app creates `hvac_board.db` automatically on first launch.

## Tech Stack

- Python
- Streamlit
- SQLite
- Pandas
