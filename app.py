import streamlit as st
import pandas as pd
import sqlite3
from datetime import datetime, timedelta

import servicetitan as st_api

# Page Setup
st.set_page_config(layout="wide", page_title="Beola the 3 Day Board")

DB_FILE = "hvac_board.db"

# -------------------------------------------------------------
# DATABASE INITIALIZATION
# -------------------------------------------------------------
def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Booking_Log (
            id TEXT PRIMARY KEY,
            timestamp TEXT,
            date TEXT,
            customer_job TEXT,
            priority_level TEXT,
            timeframe TEXT,
            hours_needed INTEGER,
            job_type TEXT,
            precollection TEXT,
            technician TEXT,
            is_recall INTEGER,
            status TEXT,
            notes TEXT,
            booked_by TEXT,
            is_standby INTEGER DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Tech_Roster (
            date TEXT,
            technician TEXT,
            avail_type TEXT,
            am_hours INTEGER,
            pm_hours INTEGER,
            dispatcher_notes TEXT,
            PRIMARY KEY (date, technician)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Maintenance_Waitlist (
            id TEXT PRIMARY KEY,
            timestamp TEXT,
            customer_name TEXT,
            phone TEXT,
            job_type TEXT,
            notes TEXT,
            status TEXT,
            scheduled_date TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Past_Scheduled_Jobs (
            id TEXT PRIMARY KEY,
            job_number TEXT,
            customer_name TEXT,
            scheduled_date TEXT,
            job_type TEXT,
            technician TEXT,
            status TEXT,
            notes TEXT,
            source TEXT,
            imported_at TEXT,
            resolved INTEGER DEFAULT 0
        )
    """)
    
    # Safe schema migration for older DB files
    try:
        cursor.execute("ALTER TABLE Booking_Log ADD COLUMN is_standby INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE Maintenance_Waitlist ADD COLUMN scheduled_date TEXT")
    except sqlite3.OperationalError:
        pass
        
    conn.commit()
    conn.close()

init_db()

# Global Configurations
capacity_tech_list = [
    "Eddie Glenn", "Derek Moore", "Ken Wilburn", "Austin Baker", "RJ Oyanib", "Tony Brown",
]
WARRANTY_TECH = "Kevin Winland"
tech_list = capacity_tech_list + [WARRANTY_TECH]

# Regular work days by technician (weekday names). Warranty tech is tracked separately.
# RJ: Saturday–Wednesday. Other dispatch techs: Monday–Friday.
# Sat/Sun: RJ is always scheduled; the 2nd weekend seat rotates (not a fixed person).
WEEKEND_DAYS = {"Saturday", "Sunday"}
WEEKEND_ROTATING_CAPACITY = 1  # second weekend tech rotates among the Mon–Fri crew

DEFAULT_TECH_JOB_CAP = 4
DEFAULT_TECH_AM_HOURS = 4
DEFAULT_TECH_PM_HOURS = 4

# Per-tech daily job caps (others default to 4). Tony runs a lighter 3-job day.
TECH_JOB_CAPS = {
    "Tony Brown": 3,
}
# Hour blocks align with job caps (Tony: 3 hrs AM + 3 hrs PM).
TECH_AM_HOURS = {
    "Tony Brown": 3,
}
TECH_PM_HOURS = {
    "Tony Brown": 3,
}

TECH_WORK_DAYS = {
    "Eddie Glenn": {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday"},
    "Derek Moore": {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday"},
    "Ken Wilburn": {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday"},
    "Austin Baker": {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday"},
    "Tony Brown": {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday"},
    "RJ Oyanib": {"Saturday", "Sunday", "Monday", "Tuesday", "Wednesday"},
}

def is_warranty_technician(tech):
    return tech == WARRANTY_TECH

def get_tech_job_cap(tech):
    return TECH_JOB_CAPS.get(tech, DEFAULT_TECH_JOB_CAP)

def get_tech_am_hours(tech):
    return TECH_AM_HOURS.get(tech, DEFAULT_TECH_AM_HOURS)

def get_tech_pm_hours(tech):
    return TECH_PM_HOURS.get(tech, DEFAULT_TECH_PM_HOURS)

def tech_works_on_date(tech, target_date):
    """Whether a capacity tech is on their fixed regular schedule for this date."""
    if is_warranty_technician(tech):
        return True
    work_days = TECH_WORK_DAYS.get(tech)
    if work_days is None:
        return True
    return target_date.strftime("%A") in work_days

def tech_in_weekend_rotate_pool(tech, target_date):
    """Mon–Fri dispatch techs may fill the rotating 2nd weekend seat."""
    day_name = target_date.strftime("%A")
    if day_name not in WEEKEND_DAYS:
        return False
    if is_warranty_technician(tech) or tech not in capacity_tech_list:
        return False
    # RJ is already on the fixed weekend schedule; others are the rotate pool
    return not tech_works_on_date(tech, target_date)

def tech_available_by_schedule(tech, target_date):
    """Assignable by schedule: fixed work day, or weekend rotate pool."""
    if is_warranty_technician(tech):
        return True
    return tech_works_on_date(tech, target_date) or tech_in_weekend_rotate_pool(tech, target_date)

def get_scheduled_capacity_techs(target_date):
    """Named dispatch techs on their fixed schedule for this weekday."""
    return [tech for tech in capacity_tech_list if tech_works_on_date(tech, target_date)]

def get_capacity_headcount(target_date):
    """Named + rotating weekend seats (for priority-tier banding)."""
    count = len(get_scheduled_capacity_techs(target_date))
    if target_date.strftime("%A") in WEEKEND_DAYS:
        count += WEEKEND_ROTATING_CAPACITY
    return count

def get_day_capacity_budget(target_date):
    """Sum AM/PM hours and max jobs from each scheduled tech's personal cap."""
    scheduled = get_scheduled_capacity_techs(target_date)
    total_am = sum(get_tech_am_hours(tech) for tech in scheduled)
    total_pm = sum(get_tech_pm_hours(tech) for tech in scheduled)
    max_jobs = sum(get_tech_job_cap(tech) for tech in scheduled)
    if target_date.strftime("%A") in WEEKEND_DAYS:
        # Floating weekend seat uses the standard 4-job / 4+4 hr profile
        total_am += DEFAULT_TECH_AM_HOURS
        total_pm += DEFAULT_TECH_PM_HOURS
        max_jobs += DEFAULT_TECH_JOB_CAP
    return total_am, total_pm, max_jobs, scheduled

def job_counts_toward_capacity(job):
    """Warranty-route jobs on Kevin do not consume dispatch board capacity."""
    return job["technician"] != WARRANTY_TECH
job_classes = ["Standard", "High Value Opp", "Member", "Maintenance-to-Service", "Recall/Warranty"]
precollect_opts = ["Not Applicable", "Yes", "No", "AR"]
time_slots = ["AM 1", "AM 2", "PM 1", "PM 2"]

PRIORITY_STYLES = {
    "Urgent": {"bg": "#B700FF", "text": "#ffffff", "label": "🚨 URGENT"},
    "High": {"bg": "#FFD100", "text": "#1e293b", "label": "⚡ HIGH"},
    "Normal": {"bg": "#FF8D09", "text": "#ffffff", "label": "🟠 NORMAL"},
    "Low": {"bg": "#ABFA00", "text": "#1e293b", "label": "🟢 LOW"},
}
REPAIR_JOB_COLOR = {"bg": "#2563eb", "text": "#ffffff", "label": "🔧 REPAIR"}
JOB_STATUSES = ["Booked", "Scheduled", "Dispatched", "Completed", "Hold", "Rescheduled", "Cancelled"]
BOARD_HIDDEN_STATUSES = {"Cancelled", "Rescheduled", "Hold"}
# Holds reserve capacity without showing on the live crew board
CAPACITY_EXCLUDED_STATUSES = {"Cancelled", "Rescheduled", "Completed"}

def is_on_board(status):
    return status not in BOARD_HIDDEN_STATUSES

def counts_toward_capacity_status(status):
    return status not in CAPACITY_EXCLUDED_STATUSES

def exclude_standby_jobs(df):
    """Standby entries belong on the Move-Up Queue, not dispatch Unassigned / crew boards."""
    if df.empty or "is_standby" not in df.columns:
        return df
    return df[df["is_standby"].fillna(0).astype(int) != 1]

def filter_board_jobs(df):
    if df.empty:
        return df
    return exclude_standby_jobs(df[df["status"].apply(is_on_board)])

def filter_capacity_jobs(df):
    """Jobs that reserve board capacity (includes Hold; excludes standby waitlist jobs)."""
    if df.empty:
        return df
    return exclude_standby_jobs(df[df["status"].apply(counts_toward_capacity_status)])

def get_future_bookings(df, after_date):
    """Active bookings scheduled beyond the rolling 3-day board window."""
    if df.empty:
        return df
    future = exclude_standby_jobs(
        df[(df["date"] > after_date) & df["status"].apply(is_on_board)]
    ).copy()
    return future.sort_values(["date", "customer_job"])

def get_job_card_colors(job, *, is_unassigned=False):
    p_level = job["priority_level"]
    j_type = job["job_type"]
    recall_tag = " ⚠️ RECALL" if job["is_recall"] else ""

    if is_unassigned:
        return {"bg": "#B45309", "text": "#ffffff", "label": f"📌 NEEDS ASSIGNMENT{recall_tag}"}
    if j_type == "Special Repair" or job["timeframe"] == "All Day":
        repair_label = "🔧 REPAIR" if j_type == "Special Repair" else "🔧 ALL DAY REPAIR"
        return {"bg": REPAIR_JOB_COLOR["bg"], "text": REPAIR_JOB_COLOR["text"], "label": f"{repair_label}{recall_tag}"}

    style = PRIORITY_STYLES.get(p_level, PRIORITY_STYLES["Normal"])
    return {"bg": style["bg"], "text": style["text"], "label": f"{style['label']}{recall_tag}"}

def render_board_color_key():
    t = get_ui_theme()
    legend_items = [
        ("Urgent", PRIORITY_STYLES["Urgent"]["bg"], PRIORITY_STYLES["Urgent"]["text"]),
        ("High", PRIORITY_STYLES["High"]["bg"], PRIORITY_STYLES["High"]["text"]),
        ("Normal", PRIORITY_STYLES["Normal"]["bg"], PRIORITY_STYLES["Normal"]["text"]),
        ("Low", PRIORITY_STYLES["Low"]["bg"], PRIORITY_STYLES["Low"]["text"]),
        ("Repair / All Day", REPAIR_JOB_COLOR["bg"], REPAIR_JOB_COLOR["text"]),
        ("Needs Assignment", "#B45309", "#ffffff"),
    ]
    chips = "".join(
        f"<div style='display:flex;align-items:center;gap:6px;margin-right:14px;margin-bottom:6px;'>"
        f"<span style='width:14px;height:14px;border-radius:3px;background:{bg};"
        f"border:1px solid {t['border']};display:inline-block;flex-shrink:0;'></span>"
        f"<span style='font-size:12px;color:{t['text']};'>{label}</span></div>"
        for label, bg, _ in legend_items
    )
    st.html(f"""
        <div style="background:{t['surface']};border:1px solid {t['border']};border-radius:8px;
            padding:10px 14px;margin-bottom:10px;font-family:sans-serif;">
            <div style="font-size:12px;font-weight:700;color:{t['text']};margin-bottom:8px;">Color Key</div>
            <div style="display:flex;flex-wrap:wrap;align-items:center;">{chips}</div>
        </div>
    """)

def get_ui_theme():
    """Always use dark styling for custom HTML boards — readable on any Streamlit theme."""
    return {
        "wrap_bg": "#0f172a",
        "row_bg": "#111827",
        "border": "#475569",
        "border_subtle": "#334155",
        "border_strong": "#64748b",
        "surface": "#1e293b",
        "surface_header": "#1e293b",
        "text": "#f1f5f9",
        "text_muted": "#94a3b8",
        "text_secondary": "#cbd5e1",
        "table_border": "#334155",
        "table_row": "#1e293b",
        "table_row_urgent": "#3f1d1d",
        "table_row_member": "#172554",
        "table_header": "#0f172a",
        "table_header_text": "#f8fafc",
        "header_unassigned_bg": "linear-gradient(90deg, rgba(127, 29, 29, 0.55), rgba(120, 53, 15, 0.35))",
        "header_unassigned_border": "#f87171",
        "header_assigned_bg": "linear-gradient(90deg, rgba(30, 58, 138, 0.45), rgba(15, 23, 42, 0.6))",
        "header_assigned_border": "#60a5fa",
        "header_warranty_bg": "linear-gradient(90deg, rgba(88, 28, 135, 0.45), rgba(15, 23, 42, 0.6))",
        "header_warranty_border": "#c084fc",
        "grid_header_unassigned_bg": "linear-gradient(90deg, rgba(120, 53, 15, 0.5), rgba(15, 23, 42, 0.5))",
        "grid_header_unassigned_border": "#fbbf24",
        "grid_header_assigned_bg": "linear-gradient(90deg, rgba(30, 64, 175, 0.4), rgba(15, 23, 42, 0.55))",
        "grid_header_assigned_border": "#60a5fa",
        "grid_header_warranty_bg": "linear-gradient(90deg, rgba(88, 28, 135, 0.4), rgba(15, 23, 42, 0.55))",
        "grid_header_warranty_border": "#c084fc",
        "chip_overlay": "rgba(0,0,0,0.35)",
        "chip_overlay_strong": "rgba(0,0,0,0.45)",
    }

def get_schedule_grid_css():
    t = get_ui_theme()
    return f"""
<style>
.tech-schedule-wrap {{
    background: {t['wrap_bg']};
    border: 1px solid {t['border']};
    border-radius: 8px;
    overflow: hidden;
    margin-bottom: 4px;
    font-family: sans-serif;
    color: {t['text']};
}}
.schedule-grid-row {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 8px;
    padding: 8px;
    background: {t['row_bg']};
    border-bottom: 1px solid {t['border_subtle']};
    align-items: stretch;
}}
.schedule-grid-row:last-child {{ border-bottom: none; }}
.schedule-header-row {{
    background: {t['surface_header']};
    border-bottom: 2px solid {t['border_strong']};
    padding-top: 10px;
    padding-bottom: 10px;
}}
.schedule-slot-header {{
    text-align: center;
    font-weight: 700;
    font-size: 0.85rem;
    border-right: 1px solid {t['border']};
    padding: 4px 6px;
    color: {t['text']};
    letter-spacing: 0.03em;
}}
.schedule-slot-header:last-child {{ border-right: none; }}
.schedule-open-cell {{
    background: {t['surface']};
    border: 1px dashed {t['border_strong']};
    border-radius: 6px;
    color: {t['text_muted']};
    font-size: 0.8rem;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 72px;
}}
.schedule-job-span {{ min-width: 0; }}
</style>
"""

def get_crew_header_css():
    t = get_ui_theme()
    return f"""
<style>
.crew-tech-header {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.6rem 0.9rem;
    border-radius: 8px;
    margin: 0.75rem 0 0.5rem 0;
    font-weight: 650;
    color: {t['text']};
}}
.crew-tech-header-unassigned {{
    background: {t['header_unassigned_bg']};
    border-left: 5px solid {t['header_unassigned_border']};
}}
.crew-tech-header-assigned {{
    background: {t['header_assigned_bg']};
    border-left: 4px solid {t['header_assigned_border']};
}}
.crew-tech-header-warranty {{
    background: {t['header_warranty_bg']};
    border-left: 4px solid {t['header_warranty_border']};
}}
</style>
"""

def get_grid_header_css():
    t = get_ui_theme()
    return f"""
<style>
div[data-testid="stTabs"] button[data-baseweb="tab"] {{
    font-weight: 600;
    padding-top: 0.65rem;
    padding-bottom: 0.65rem;
}}
.grid-tech-header {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.55rem 0.85rem;
    border-radius: 8px 8px 0 0;
    margin-bottom: 0.35rem;
    font-weight: 650;
    letter-spacing: 0.01em;
    color: {t['text']};
}}
.grid-tech-header-unassigned {{
    background: {t['grid_header_unassigned_bg']};
    border-left: 4px solid {t['grid_header_unassigned_border']};
}}
.grid-tech-header-assigned {{
    background: {t['grid_header_assigned_bg']};
    border-left: 4px solid {t['grid_header_assigned_border']};
}}
.grid-tech-header-warranty {{
    background: {t['grid_header_warranty_bg']};
    border-left: 4px solid {t['grid_header_warranty_border']};
}}
.grid-tech-count {{
    font-size: 0.78rem;
    font-weight: 500;
    opacity: 0.85;
}}
</style>
"""

def render_beola_title(show_subtitle=False):
    subtitle_html = (
        "<div class='beola-subtitle'>📞 CSR Booking Portal</div>"
        if show_subtitle else ""
    )
    st.html(f"""
        <link href="https://fonts.googleapis.com/css2?family=Fredoka:wght@500;600;700&display=swap" rel="stylesheet">
        <style>
        @keyframes beola-shimmer {{
            0% {{ background-position: 0% center; }}
            100% {{ background-position: 200% center; }}
        }}
        @keyframes beola-sparkle {{
            0%, 100% {{ opacity: 0.15; transform: scale(0.6) rotate(0deg); }}
            50% {{ opacity: 1; transform: scale(1.15) rotate(180deg); }}
        }}
        @keyframes beola-glow {{
            0%, 100% {{ filter: drop-shadow(0 0 8px rgba(183,0,255,0.35)) drop-shadow(0 0 16px rgba(37,99,235,0.2)); }}
            50% {{ filter: drop-shadow(0 0 14px rgba(255,209,0,0.55)) drop-shadow(0 0 22px rgba(183,0,255,0.45)); }}
        }}
        .beola-header-wrap {{
            text-align: center;
            padding: 0.5rem 0 1.25rem 0;
        }}
        .beola-title-wrap {{
            position: relative;
            display: inline-block;
            animation: beola-glow 2.8s ease-in-out infinite;
        }}
        .beola-title {{
            font-family: 'Fredoka', sans-serif;
            font-size: 2.6rem;
            font-weight: 700;
            letter-spacing: 0.02em;
            line-height: 1.1;
            background: linear-gradient(
                90deg,
                #B700FF 0%,
                #FFD100 18%,
                #FF8D09 36%,
                #ABFA00 54%,
                #2563eb 72%,
                #B700FF 100%
            );
            background-size: 200% auto;
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            animation: beola-shimmer 2.5s linear infinite;
        }}
        .beola-sparkle {{
            position: absolute;
            color: #FFD100;
            text-shadow: 0 0 6px rgba(255,209,0,0.9), 0 0 12px rgba(183,0,255,0.6);
            animation: beola-sparkle 2.2s ease-in-out infinite;
            pointer-events: none;
            user-select: none;
        }}
        .beola-subtitle {{
            font-family: 'Fredoka', sans-serif;
            font-size: 0.95rem;
            color: #94a3b8;
            margin-top: 8px;
        }}
        </style>
        <div class="beola-header-wrap">
            <div class="beola-title-wrap">
                <span class="beola-sparkle" style="top:-10px;left:2%;font-size:16px;animation-delay:0s;">✦</span>
                <span class="beola-sparkle" style="top:4px;right:0%;font-size:12px;animation-delay:0.4s;">✨</span>
                <span class="beola-sparkle" style="bottom:-6px;left:18%;font-size:14px;animation-delay:0.9s;">★</span>
                <span class="beola-sparkle" style="top:-4px;right:14%;font-size:11px;animation-delay:1.3s;">✦</span>
                <span class="beola-sparkle" style="bottom:2px;right:22%;font-size:13px;animation-delay:1.8s;">✨</span>
                <span class="beola-sparkle" style="top:12px;left:8%;font-size:10px;animation-delay:2.1s;">★</span>
                <div class="beola-title">Beola the 3 Day Board</div>
            </div>
            {subtitle_html}
        </div>
    """)

def render_beola_header():
    render_beola_title(show_subtitle=True)

# -------------------------------------------------------------
# AUTOMATED CALENDAR LOGIC (CONTINUOUS 3-DAYS)
# -------------------------------------------------------------
def get_3_continuous_days(start_date):
    """Returns a continuous 3-day window from today, including weekends."""
    return [start_date, start_date + timedelta(days=1), start_date + timedelta(days=2)]

today = datetime.today().date()
day1, day2, day3 = get_3_continuous_days(today)

def select_board_day(key, *, style="radio", label="Board day"):
    """Day picker that keeps the selected day after save/rerun (st.tabs always reset to Day 1)."""
    days = [day1, day2, day3]
    radio_labels = [
        f"📅 Day 1 · {day1.strftime('%a %b %d')}",
        f"📆 Day 2 · {day2.strftime('%a %b %d')}",
        f"🗓️ Day 3 · {day3.strftime('%a %b %d')}",
    ]
    if style == "selectbox":
        idx = st.selectbox(
            label,
            options=[0, 1, 2],
            format_func=lambda i: days[i].strftime("%A, %b %d"),
            key=key,
        )
    else:
        idx = st.radio(
            label,
            options=[0, 1, 2],
            format_func=lambda i: radio_labels[i],
            horizontal=True,
            key=key,
            label_visibility="collapsed",
        )
    return days[idx], idx + 1

render_beola_title()

st.markdown(get_schedule_grid_css() + get_crew_header_css() + get_grid_header_css(), unsafe_allow_html=True)

# --- Session State Flash Messages Router ---
if "flash_success" in st.session_state:
    st.success(st.session_state.flash_success)
    del st.session_state.flash_success
if "flash_error" in st.session_state:
    st.error(st.session_state.flash_error)
    del st.session_state.flash_error

st.sidebar.markdown(f"📆 **Today's System Date:** {today.strftime('%A, %b %d')}")

# -------------------------------------------------------------
# DATABASE READ HELPERS
# -------------------------------------------------------------
def load_bookings():
    conn = get_db_connection()
    df = pd.read_sql_query("SELECT * FROM Booking_Log", conn)
    conn.close()
    if not df.empty:
        df['date'] = pd.to_datetime(df['date']).dt.date
        df['is_recall'] = df['is_recall'].astype(bool)
        df['hours_needed'] = df['hours_needed'].astype(int)
        df['is_standby'] = df['is_standby'].fillna(0).astype(int)
    else:
        df = pd.DataFrame(columns=[
            "id", "timestamp", "date", "customer_job", "priority_level", "timeframe", 
            "hours_needed", "job_type", "precollection", "technician", "is_recall", "status", "notes", "booked_by", "is_standby"
        ])
    return df

def load_roster():
    conn = get_db_connection()
    df = pd.read_sql_query("SELECT * FROM Tech_Roster", conn)
    conn.close()
    if not df.empty:
        df['date'] = pd.to_datetime(df['date']).dt.date
    else:
        df = pd.DataFrame(columns=["date", "technician", "avail_type", "am_hours", "pm_hours", "dispatcher_notes"])
    return df

def load_waitlist():
    conn = get_db_connection()
    df = pd.read_sql_query("SELECT * FROM Maintenance_Waitlist WHERE status = 'Active'", conn)
    conn.close()
    if not df.empty and "scheduled_date" in df.columns:
        df["scheduled_date"] = pd.to_datetime(df["scheduled_date"], errors="coerce").dt.date
    return df

def load_past_scheduled_jobs(*, include_resolved=False):
    conn = get_db_connection()
    if include_resolved:
        df = pd.read_sql_query("SELECT * FROM Past_Scheduled_Jobs ORDER BY scheduled_date DESC", conn)
    else:
        df = pd.read_sql_query(
            "SELECT * FROM Past_Scheduled_Jobs WHERE resolved = 0 ORDER BY scheduled_date DESC",
            conn,
        )
    conn.close()
    if not df.empty and "scheduled_date" in df.columns:
        df["scheduled_date"] = pd.to_datetime(df["scheduled_date"], errors="coerce").dt.date
    return df

def get_board_past_incomplete_jobs(bookings):
    """Local board jobs scheduled before today that were never completed/cancelled."""
    if bookings.empty:
        return bookings
    open_statuses = {"Booked", "Scheduled", "Dispatched"}
    past = exclude_standby_jobs(bookings)
    past = past[
        (past["date"] < today) &
        (past["status"].isin(open_statuses))
    ].copy()
    return past.sort_values(["date", "customer_job"])

def find_bookings(query, bookings):
    """Case-insensitive search across customer/job, notes, tech, status, date."""
    q = (query or "").strip().lower()
    if not q or bookings.empty:
        return bookings.iloc[0:0]
    searchable = bookings.copy()
    searchable["_blob"] = (
        searchable["customer_job"].astype(str) + " " +
        searchable["notes"].fillna("").astype(str) + " " +
        searchable["technician"].astype(str) + " " +
        searchable["status"].astype(str) + " " +
        searchable["job_type"].astype(str) + " " +
        searchable["date"].astype(str) + " " +
        searchable["id"].astype(str)
    ).str.lower()
    return searchable[searchable["_blob"].str.contains(q, regex=False)].drop(columns=["_blob"])

def _match_csv_column(columns, *candidates):
    lowered = {str(c).strip().lower(): c for c in columns}
    for cand in candidates:
        if cand in lowered:
            return lowered[cand]
    for col_lower, col in lowered.items():
        for cand in candidates:
            if cand in col_lower:
                return col
    return None

def normalize_past_jobs_csv(uploaded_df):
    """Map common ServiceTitan export column names into Past_Scheduled_Jobs fields."""
    import hashlib

    job_col = _match_csv_column(uploaded_df.columns, "job #", "job number", "job#", "job id", "job")
    cust_col = _match_csv_column(uploaded_df.columns, "customer name", "customer", "location name", "location")
    date_col = _match_csv_column(
        uploaded_df.columns,
        "appointment date", "scheduled date", "job start date", "first appt date",
        "last appt date", "start date", "appt date", "scheduled on",
    )
    type_col = _match_csv_column(uploaded_df.columns, "job type", "type")
    tech_col = _match_csv_column(uploaded_df.columns, "technician", "tech", "assigned technician")
    status_col = _match_csv_column(uploaded_df.columns, "job status", "status", "appointment status")
    notes_col = _match_csv_column(uploaded_df.columns, "notes", "summary", "description", "address")

    rows = []
    for i, row in uploaded_df.iterrows():
        job_number = str(row[job_col]).strip() if job_col else ""
        customer = str(row[cust_col]).strip() if cust_col else ""
        if (not job_number or job_number.lower() == "nan") and (not customer or customer.lower() == "nan"):
            continue
        raw_date = row[date_col] if date_col else None
        parsed_date = pd.to_datetime(raw_date, errors="coerce")
        scheduled = parsed_date.date().isoformat() if pd.notna(parsed_date) else ""
        job_number = job_number if job_number.lower() != "nan" else ""
        customer = customer if customer.lower() != "nan" else ""
        stable_key = f"{job_number}|{customer}|{scheduled}|{i}"
        row_id = "st-" + hashlib.md5(stable_key.encode("utf-8")).hexdigest()[:16]
        rows.append({
            "id": row_id,
            "job_number": job_number,
            "customer_name": customer,
            "scheduled_date": scheduled,
            "job_type": str(row[type_col]).strip() if type_col and pd.notna(row[type_col]) else "",
            "technician": str(row[tech_col]).strip() if tech_col and pd.notna(row[tech_col]) else "",
            "status": str(row[status_col]).strip() if status_col and pd.notna(row[status_col]) else "Scheduled",
            "notes": str(row[notes_col]).strip() if notes_col and pd.notna(row[notes_col]) else "",
        })
    return pd.DataFrame(rows)

bookings_df = load_bookings()
roster_df = load_roster()

def get_tech_roster_info(target_date, tech):
    """Return availability info for a tech on a given date."""
    day_name = target_date.strftime("%A")
    in_rotate_pool = tech_in_weekend_rotate_pool(tech, target_date)

    # Fixed off-day (e.g. RJ Thu/Fri, or weekday tech with no weekend rotate role)
    if not is_warranty_technician(tech) and not tech_available_by_schedule(tech, target_date):
        return {
            "avail_type": "Off Schedule",
            "am_hours": 0,
            "pm_hours": 0,
            "notes": "Not on regular schedule this day",
            "is_modified": True,
            "is_full_day_out": True,
            "is_assignable": False,
            "badge": "📅 OFF SCHEDULE",
            "is_off_schedule": True,
        }

    if not roster_df.empty:
        match = roster_df[(roster_df["date"] == target_date) & (roster_df["technician"] == tech)]
        if not match.empty:
            row = match.iloc[0]
            avail = row["avail_type"]
            am_h = int(row["am_hours"])
            pm_h = int(row["pm_hours"])
            notes = row["dispatcher_notes"] or ""
            is_full_out = avail in ["PTO Full", "Call Out"] or (am_h == 0 and pm_h == 0)
            if avail == "PTO Full":
                badge = "🚫 PTO FULL"
            elif avail == "Call Out":
                badge = "🚫 CALL OUT"
            elif "AM off" in avail:
                badge = "🌅 AM OFF"
            elif "PM off" in avail:
                badge = "🌇 PM OFF"
            elif avail == "Training/Meeting":
                badge = "📚 TRAINING"
            else:
                badge = avail
            return {
                "avail_type": avail,
                "am_hours": am_h,
                "pm_hours": pm_h,
                "notes": notes,
                "is_modified": True,
                "is_full_day_out": is_full_out,
                "is_assignable": not is_full_out,
                "badge": badge,
                "is_off_schedule": False,
            }

    if in_rotate_pool:
        return {
            "avail_type": "Weekend Rotate Pool",
            "am_hours": 4,
            "pm_hours": 4,
            "notes": "Eligible for rotating 2nd weekend seat (not a fixed assignment)",
            "is_modified": True,
            "is_full_day_out": False,
            "is_assignable": True,
            "badge": "🔄 WEEKEND ROTATE",
            "is_off_schedule": False,
        }

    return {
        "avail_type": "Active",
        "am_hours": 4,
        "pm_hours": 4,
        "notes": "",
        "is_modified": False,
        "is_full_day_out": False,
        "is_assignable": True,
        "badge": "🟢 ACTIVE",
        "is_off_schedule": False,
    }

def get_assignable_tech_options(target_date, keep_techs=None):
    """Techs available for new assignment on a date (+ already-assigned techs on jobs)."""
    keep_techs = keep_techs or []
    options = ["Unassigned"]
    for tech in tech_list:
        info = get_tech_roster_info(target_date, tech)
        if info["is_assignable"] or tech in keep_techs:
            options.append(tech)
    return options

def render_tech_out_banner(tech_name, roster_info):
    t = get_ui_theme()
    notes_line = (
        f"<div style='font-size:11px;color:{t['text_muted']};margin-top:4px;'>"
        f"📝 {roster_info['notes']}</div>"
    ) if roster_info.get("notes") else ""
    st.html(f"""
        <div style="background:{t['surface']};border:2px dashed #ef4444;border-radius:8px;
            padding:14px 16px;margin-bottom:8px;font-family:sans-serif;">
            <div style="display:flex;justify-content:space-between;align-items:center;">
                <span style="font-weight:700;color:{t['text']};">🚫 {tech_name}</span>
                <span style="font-size:12px;font-weight:700;color:#f87171;
                    background:rgba(239,68,68,0.15);padding:4px 10px;border-radius:6px;">
                    {roster_info['badge']}
                </span>
            </div>
            <div style="font-size:12px;color:{t['text_secondary']};margin-top:6px;">
                AM: {roster_info['am_hours']} hrs · PM: {roster_info['pm_hours']} hrs — not available for routing
            </div>
            {notes_line}
        </div>
    """)

def render_tech_schedule_section(tech_name, target_date, tech_jobs, *, is_unassigned=False, is_warranty=False):
    roster_info = get_tech_roster_info(target_date, tech_name) if tech_name != "Unassigned" else None
    header_icon = "🚨" if is_unassigned else ("🛡️" if is_warranty else "👤")
    job_count = len(tech_jobs)

    if is_unassigned:
        header_note = f"{job_count} awaiting assignment"
        header_class = "crew-tech-header-unassigned"
    elif is_warranty:
        header_note = f"{job_count} warranty job(s) · separate from dispatch capacity"
        header_class = "crew-tech-header-warranty"
    else:
        partial_note = f" · {roster_info['badge']}" if roster_info and roster_info["is_modified"] and not roster_info["is_full_day_out"] else ""
        header_note = f"{job_count} job(s) scheduled{partial_note}"
        header_class = "crew-tech-header-assigned"
        if roster_info and roster_info["is_full_day_out"]:
            header_class = "crew-tech-header-unassigned"
            header_icon = "🚫"

    st.markdown(
        f"<div class='crew-tech-header {header_class}'>"
        f"<span>{header_icon} {tech_name}</span>"
        f"<span style='font-size:0.8rem; opacity:0.9;'>{header_note}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )

    if not is_unassigned and roster_info and roster_info["is_full_day_out"] and tech_jobs.empty:
        render_tech_out_banner(tech_name, roster_info)
    elif not is_unassigned and roster_info and roster_info["is_full_day_out"] and not tech_jobs.empty:
        st.warning(f"⚠️ {tech_name} is marked OUT but still has {job_count} job(s) assigned — reassign in the grid.")
        render_tech_slot_grid(tech_jobs, is_unassigned=is_unassigned)
    else:
        render_tech_slot_grid(tech_jobs, is_unassigned=is_unassigned)

def render_warranty_board_section(target_date, day_jobs):
    """Always-visible warranty lane — separate from dispatch crew capacity."""
    st.markdown(
        "<div style='font-size:0.78rem;font-weight:700;letter-spacing:0.06em;"
        "text-transform:uppercase;color:#c084fc;margin:1rem 0 0.35rem 0;'>"
        "🛡️ Warranty Technician (not counted in dispatch capacity)</div>",
        unsafe_allow_html=True,
    )
    warranty_jobs = day_jobs[day_jobs["technician"] == WARRANTY_TECH] if not day_jobs.empty else pd.DataFrame()
    render_tech_schedule_section(
        WARRANTY_TECH, target_date, warranty_jobs, is_warranty=True,
    )

def render_dispatch_crew_sections(target_date, day_jobs, *, include_unassigned=True):
    """Render unassigned + dispatch crew rows (excludes warranty lane)."""
    unassigned_jobs = day_jobs[day_jobs["technician"] == "Unassigned"] if not day_jobs.empty else day_jobs
    visible_crew_techs = []
    if include_unassigned and not unassigned_jobs.empty:
        visible_crew_techs.append("Unassigned")
    for tech in capacity_tech_list:
        tech_jobs_for_day = day_jobs[day_jobs["technician"] == tech] if not day_jobs.empty else pd.DataFrame()
        roster_info = get_tech_roster_info(target_date, tech)
        # Always show techs on their fixed schedule (even with an open day), plus anyone out or with jobs
        show_tech = (
            (not tech_jobs_for_day.empty)
            or roster_info["is_full_day_out"]
            or tech_works_on_date(tech, target_date)
        )
        if show_tech:
            visible_crew_techs.append(tech)

    for tech_idx, current_tech in enumerate(visible_crew_techs):
        tech_jobs = day_jobs[day_jobs["technician"] == current_tech] if not day_jobs.empty else pd.DataFrame()
        render_tech_schedule_section(
            current_tech, target_date, tech_jobs,
            is_unassigned=(current_tech == "Unassigned"),
        )
        if tech_idx < len(visible_crew_techs) - 1:
            st.divider()

    return visible_crew_techs

def render_callout_board(target_days):
    t = get_ui_theme()
    day_labels = [f"{d.strftime('%a %b %d')}" for d in target_days]
    header = "".join(f"<th style='padding:10px;text-align:left;'>{lbl}</th>" for lbl in day_labels)

    def build_rows(techs, section_label=None):
        rows_html = ""
        if section_label:
            colspan = len(target_days) + 1
            rows_html += f"""
            <tr>
                <td colspan="{colspan}" style="padding:8px 10px;font-size:11px;font-weight:700;
                    letter-spacing:0.06em;text-transform:uppercase;color:{t['text_muted']};
                    background:{t['surface_header']};border-bottom:1px solid {t['border']};">
                    {section_label}
                </td>
            </tr>"""
        for tech in techs:
            cells = ""
            for d in target_days:
                info = get_tech_roster_info(d, tech)
                if info["is_modified"]:
                    cell_bg = "#3f1d1d" if info["is_full_day_out"] else "#1a2744"
                    note = f"<div style='font-size:10px;color:{t['text_muted']};margin-top:4px;'>{info['notes']}</div>" if info["notes"] else ""
                    cells += f"""
                    <td style="padding:10px;background:{cell_bg};border-bottom:1px solid {t['border']};vertical-align:top;">
                        <div style="font-weight:700;color:{t['text']};">{info['badge']}</div>
                        <div style="font-size:11px;color:{t['text_secondary']};">AM {info['am_hours']}h · PM {info['pm_hours']}h</div>
                        {note}
                    </td>"""
                else:
                    cells += f"""
                    <td style="padding:10px;background:{t['row_bg']};border-bottom:1px solid {t['border']};
                        color:{t['text_muted']};font-size:12px;vertical-align:top;">🟢 Active</td>"""
            warranty_tag = " <span style='font-size:10px;color:#c084fc;'>(Warranty)</span>" if is_warranty_technician(tech) else ""
            rows_html += f"""
            <tr>
                <td style="padding:10px;font-weight:700;color:{t['text']};background:{t['surface_header']};
                    border-bottom:1px solid {t['border']};white-space:nowrap;">{tech}{warranty_tag}</td>
                {cells}
            </tr>"""
        return rows_html

    rows_html = build_rows(capacity_tech_list, "Dispatch Crew") + build_rows([WARRANTY_TECH], "Warranty Technician")

    st.html(f"""
        <div style="border:1px solid {t['border']};border-radius:8px;overflow:hidden;font-family:sans-serif;margin-bottom:12px;">
            <table style="width:100%;border-collapse:collapse;font-size:13px;color:{t['text']};">
                <thead>
                    <tr style="background:{t['table_header']};color:{t['table_header_text']};">
                        <th style="padding:10px;text-align:left;">Technician</th>
                        {header}
                    </tr>
                </thead>
                <tbody>{rows_html}</tbody>
            </table>
        </div>
    """)

# -------------------------------------------------------------
# DYNAMIC METRIC, HOUR, & CAP CALCULATOR
# -------------------------------------------------------------
def get_detailed_metrics(target_date):
    scheduled_techs = get_scheduled_capacity_techs(target_date)
    base_tech_count = get_capacity_headcount(target_date)
    day_name = target_date.strftime("%A")
    is_weekend = day_name in WEEKEND_DAYS

    # Priority tier allotments scale with how many dispatch seats are open
    if base_tech_count >= 5:
        day_caps = {"Urgent": 4, "High": 9, "Normal": 5, "Low": 3}
    elif base_tech_count >= 4:
        day_caps = {"Urgent": 3, "High": 7, "Normal": 4, "Low": 2}
    elif base_tech_count >= 2:
        day_caps = {"Urgent": 1, "High": 2, "Normal": 2, "Low": 0}
    elif base_tech_count == 1:
        day_caps = {"Urgent": 1, "High": 1, "Normal": 1, "Low": 0}
    else:
        day_caps = {"Urgent": 0, "High": 0, "Normal": 0, "Low": 0}

    total_am_hours, total_pm_hours, max_jobs_allowed, _ = get_day_capacity_budget(target_date)
    base_max_jobs = max_jobs_allowed

    # Weekday: only fixed-schedule techs can reduce capacity via PTO.
    # Weekend: RJ (fixed) + any rotate-pool tech marked out can reduce capacity.
    roster_eligible = set(scheduled_techs)
    if is_weekend:
        roster_eligible = set(capacity_tech_list)
    
    if not roster_df.empty:
        day_roster = roster_df[roster_df['date'] == target_date]
        for _, change in day_roster.iterrows():
            tech_name = change['technician']
            if tech_name not in roster_eligible:
                continue
            status = change['avail_type']
            tech_am = get_tech_am_hours(tech_name)
            tech_pm = get_tech_pm_hours(tech_name)
            tech_jobs = get_tech_job_cap(tech_name)
            
            if status in ["PTO Full", "Call Out"]:
                total_am_hours -= tech_am
                total_pm_hours -= tech_pm
                max_jobs_allowed -= tech_jobs
            elif "AM off" in status:
                total_am_hours -= tech_am
                max_jobs_allowed -= max(1, tech_jobs // 2)
            elif "PM off" in status:
                total_pm_hours -= tech_pm
                max_jobs_allowed -= max(1, tech_jobs // 2)
            elif status == "Training/Meeting":
                total_am_hours -= max(1, tech_am // 2)
                total_pm_hours -= max(1, tech_pm // 2)

    max_jobs_allowed = max(0, max_jobs_allowed)
    total_am_hours = max(0, total_am_hours)
    total_pm_hours = max(0, total_pm_hours)
    if base_max_jobs > 0 and max_jobs_allowed < base_max_jobs:
        capacity_ratio = max_jobs_allowed / base_max_jobs
        day_caps = {tier: max(0, round(cap * capacity_ratio)) for tier, cap in day_caps.items()}

    sold_am, sold_pm = 0, 0
    total_jobs_booked = 0
    priority_counts = {"Urgent": 0, "High": 0, "Normal": 0, "Low": 0}
    
    if not bookings_df.empty:
        active_jobs = filter_capacity_jobs(bookings_df[bookings_df['date'] == target_date])
        
        for _, job in active_jobs.iterrows():
            if not job_counts_toward_capacity(job):
                continue
            total_jobs_booked += 1
            # Holds reserve hours/slots but do not fill priority tier allotments yet
            if job["status"] != "Hold" and job['priority_level'] in priority_counts and not job['is_recall']:
                priority_counts[job['priority_level']] += 1
                
            if not job['is_recall']:
                hours = int(job['hours_needed'])
                if hours > 4 or job['timeframe'] == "All Day":
                    sold_am += 4
                    sold_pm += max(0, hours - 4)
                else:
                    if job['timeframe'] in ["AM", "AM 1"]:
                        sold_am += hours
                    elif job['timeframe'] in ["PM", "PM 1", "PM 2"]:
                        sold_pm += hours
                    elif job['timeframe'] in ["Open", "AM 2"]:
                        if sold_am + hours <= total_am_hours:
                            sold_am += hours
                        else:
                            remainder = (sold_am + hours) - total_am_hours
                            sold_am = total_am_hours
                            sold_pm += remainder
                        
    return {
        "am_hours_left": max(0, total_am_hours - sold_am),
        "pm_hours_left": max(0, total_pm_hours - sold_pm),
        "priority_counts": priority_counts,
        "day_caps": day_caps,
        "total_jobs_booked": total_jobs_booked,
        "max_jobs_allowed": max_jobs_allowed
    }

# -------------------------------------------------------------
# HELPER: Extract requested time constraint from notes string
# -------------------------------------------------------------
def extract_requested_time(notes_str):
    """Pulls the [TIME REQUEST: ...] tag out of a notes string for display."""
    if not notes_str:
        return None
    import re
    # Match both the new [TIME REQUEST: ...] format and old [Requested Time: ...] format
    match = re.search(r'\[TIME REQUEST:\s*([^\]]+)\]', str(notes_str), re.IGNORECASE)
    if not match:
        match = re.search(r'\[Requested Time:\s*([^\]]+)\]', str(notes_str), re.IGNORECASE)
    if not match:
        return None
    val = match.group(1).strip()
    return val if val else None

TIME_REQUEST_TAG = "[TIME REQUEST: ]"

def prepend_time_request_tag(notes_str):
    """Prepend the time-request tag to notes without overwriting existing content."""
    import re
    notes = str(notes_str).strip() if notes_str else ""
    if re.search(r'\[TIME REQUEST:', notes, re.IGNORECASE) or re.search(r'\[Requested Time:', notes, re.IGNORECASE):
        return notes
    return f"{TIME_REQUEST_TAG.strip()} {notes}".strip() if notes else TIME_REQUEST_TAG.strip()

LACE_BOOK_TAG = "[LACE BOOKED]"

def append_lace_book_tag(notes_str):
    """Append Lace Booked tag to notes without overwriting existing content."""
    import re
    notes = str(notes_str).strip() if notes_str else ""
    if re.search(r'\[LACE BOOKED\]', notes, re.IGNORECASE):
        return notes
    return f"{notes} {LACE_BOOK_TAG}".strip() if notes else LACE_BOOK_TAG

def map_job_to_visual_slot(tf):
    if tf == "AM" or tf == "All Day":
        return "AM 1"
    if tf == "PM":
        return "PM 1"
    if tf == "Open":
        return "AM 2"
    return tf

SLOT_TO_INDEX = {"AM 1": 0, "AM": 0, "All Day": 0, "AM 2": 1, "Open": 1, "PM 1": 2, "PM": 2, "PM 2": 3}

def get_job_start_slot_index(timeframe):
    return SLOT_TO_INDEX.get(str(timeframe), 0)

def get_job_slot_span(hours_needed):
    return max(1, min(4, int(hours_needed) // 2))

def layout_tech_jobs(tech_jobs):
    """Pack jobs into grid rows; each job spans consecutive slots from its start."""
    if tech_jobs.empty:
        return [], 0

    jobs_sorted = tech_jobs.copy()
    jobs_sorted["_start"] = jobs_sorted["timeframe"].apply(get_job_start_slot_index)
    jobs_sorted = jobs_sorted.sort_values(["_start", "hours_needed"], ascending=[True, False])

    layers = []
    placements = []

    for _, job in jobs_sorted.iterrows():
        start = int(job["_start"])
        span = min(get_job_slot_span(job["hours_needed"]), 4 - start)
        placed = False

        for row_idx, row in enumerate(layers):
            if all(row[i] is None for i in range(start, start + span)):
                for i in range(start, start + span):
                    row[i] = job["id"]
                placements.append((row_idx, start, span, job))
                placed = True
                break

        if not placed:
            new_row = [None] * 4
            for i in range(start, start + span):
                new_row[i] = job["id"]
            layers.append(new_row)
            placements.append((len(layers) - 1, start, span, job))

    return placements, max(1, len(layers))

def build_job_card_html(job, *, is_unassigned=False):
    import re

    colors = get_job_card_colors(job, is_unassigned=is_unassigned)
    bg_color = colors["bg"]
    text_color = colors["text"]
    title_label = colors["label"]
    chip_on_light = text_color != "#ffffff"

    is_standby_flag = int(job['is_standby']) if 'is_standby' in job and pd.notna(job['is_standby']) else 0
    standby_badge = (
        "<span style='background-color: rgba(0,0,0,0.2); "
        "padding: 2px 6px; border-radius: 4px; font-size: 10px; "
        "margin-left: 6px; font-weight: bold;'>"
        "📋 STANDBY</span>"
    ) if is_standby_flag == 1 and chip_on_light else (
        "<span style='background-color: rgba(255,255,255,0.25); "
        "padding: 2px 6px; border-radius: 4px; font-size: 10px; "
        "margin-left: 6px; font-weight: bold;'>"
        "📋 STANDBY</span>"
    ) if is_standby_flag == 1 else ""

    req_time = extract_requested_time(job['notes'])
    t = get_ui_theme()
    req_time_html = (
        f"<div style='margin-top:5px; font-size:11px; "
        f"background-color: {t['chip_overlay']}; border-radius:4px; "
        f"padding: 3px 6px; display:inline-block;'>"
        f"⏰ <strong>Time Req:</strong> {req_time}</div>"
    ) if req_time else ""

    safe_customer_job = str(job['customer_job']).replace('"', '&quot;').replace("'", "&#39;")
    safe_notes_raw = str(job['notes']).replace('"', '&quot;').replace("'", "&#39;") if job['notes'] else ""
    safe_notes_display = re.sub(r'\[TIME REQUEST:[^\]]+\]\s*', '', safe_notes_raw, flags=re.IGNORECASE)
    safe_notes_display = re.sub(r'\[Requested Time:[^\]]+\]\s*', '', safe_notes_display).strip()

    unassigned_badge = (
        f"<div style='margin-top:4px; font-size:10px; font-weight:700; "
        f"background-color: {t['chip_overlay_strong']}; border-radius:4px; "
        f"padding: 2px 6px; display:inline-block; letter-spacing:0.4px;'>"
        f"⚠️ ROUTE TO TECH</div>"
    ) if is_unassigned else ""

    notes_str = (
        f"<div style='margin-top:4px; font-size:11px; opacity:0.95;'>"
        f"✏️ {safe_notes_display}</div>"
    ) if safe_notes_display else ""

    border_style = "3px solid #fcd34d" if is_unassigned else (
        "1px solid rgba(0,0,0,0.2)" if chip_on_light else "1px solid rgba(255,255,255,0.15)"
    )
    box_shadow = "0 0 0 2px rgba(245,158,11,0.35), 0 2px 6px rgba(0,0,0,0.25)" if is_unassigned else "0 1px 3px rgba(0,0,0,0.2)"

    return f"""
        <div class="schedule-job-card" style="
            background-color: {bg_color};
            color: {text_color};
            padding: 10px;
            border-radius: 6px;
            height: 100%;
            min-height: 88px;
            box-sizing: border-box;
            border: {border_style};
            box-shadow: {box_shadow};
            font-family: sans-serif;
        ">
            <div style="font-weight: bold; font-size: 14px; margin-bottom: 2px;">{safe_customer_job}{standby_badge}</div>
            <div style="font-size: 11px; font-weight: 600; opacity: 0.9;">{title_label} | {int(job['hours_needed']/2)} slot(s)</div>
            <div style="font-size: 11px; opacity: 0.85;">Status: {job['status']}</div>
            {unassigned_badge}
            {req_time_html}
            {notes_str}
        </div>
    """

def render_schedule_job_card(job, *, is_unassigned=False):
    st.html(build_job_card_html(job, is_unassigned=is_unassigned))

def render_tech_slot_grid(tech_jobs, *, is_unassigned=False):
    placements, row_count = layout_tech_jobs(tech_jobs)
    start_map = {(row_idx, start): (span, job) for row_idx, start, span, job in placements}

    header_cells = "".join(
        f"<div class='schedule-slot-header'>{slot}</div>" for slot in time_slots
    )

    if row_count == 0:
        body = "<div class='schedule-grid-row'><div class='schedule-open-cell' style='grid-column: span 4;'>— Open —</div></div>"
    else:
        row_html_parts = []
        for row_idx in range(row_count):
            cells = []
            col = 0
            while col < 4:
                if (row_idx, col) in start_map:
                    span, job = start_map[(row_idx, col)]
                    cells.append(
                        f"<div class='schedule-job-span' style='grid-column: span {span};'>"
                        f"{build_job_card_html(job, is_unassigned=is_unassigned)}"
                        f"</div>"
                    )
                    col += span
                else:
                    is_covered = any(
                        r == row_idx and start < col < start + span
                        for r, start, span, _ in placements
                    )
                    if not is_covered:
                        cells.append("<div class='schedule-open-cell'>— Open —</div>")
                    col += 1
            row_html_parts.append(f"<div class='schedule-grid-row'>{''.join(cells)}</div>")
        body = "".join(row_html_parts)

    st.html(f"""
        {get_schedule_grid_css()}
        <div class="tech-schedule-wrap">
            <div class="schedule-grid-row schedule-header-row">{header_cells}</div>
            {body}
        </div>
    """)

# -------------------------------------------------------------
# NAVIGATION SIDEBAR HUB
# -------------------------------------------------------------
view = st.sidebar.radio(
    "Navigate Department Hub:",
    [
        "CSR Booking & Standby Hub",
        "Parts & Repairs Hub",
        "Dispatch Operational Desk",
        "Live Summary Board",
        "Past Scheduled Jobs",
    ],
)
st.sidebar.markdown("---")
search_query = st.sidebar.text_input(
    "🔍 Search jobs / customers",
    placeholder="Name, job #, tech, notes…",
    key="global_job_search",
)
if search_query.strip():
    search_hits = find_bookings(search_query, bookings_df)
    st.sidebar.caption(f"{len(search_hits)} match(es) on this board")
else:
    search_hits = bookings_df.iloc[0:0]

if search_query.strip():
    with st.expander(f"🔍 Search results for “{search_query.strip()}” ({len(search_hits)})", expanded=True):
        if search_hits.empty:
            st.info("No matching jobs found on this board.")
        else:
            st.dataframe(
                search_hits[
                    ["date", "customer_job", "technician", "status", "priority_level", "timeframe", "job_type", "notes"]
                ],
                column_config={
                    "date": st.column_config.DateColumn("Date", format="MM/DD/YYYY"),
                    "customer_job": st.column_config.TextColumn("Customer / Job"),
                    "technician": st.column_config.TextColumn("Technician"),
                    "status": st.column_config.TextColumn("Status"),
                    "priority_level": st.column_config.TextColumn("Priority"),
                    "timeframe": st.column_config.TextColumn("Slot"),
                    "job_type": st.column_config.TextColumn("Job Type"),
                    "notes": st.column_config.TextColumn("Notes", width="large"),
                },
                use_container_width=True,
                hide_index=True,
            )

# -------------------------------------------------------------
# 1. CSR BOOKING & STANDBY HUB
# -------------------------------------------------------------
if view == "CSR Booking & Standby Hub":
    st.markdown(
        "<div style='text-align:center;font-family:Fredoka,sans-serif;color:#94a3b8;"
        "margin:-0.5rem 0 1rem 0;font-size:0.95rem;'>📞 CSR Booking Portal</div>",
        unsafe_allow_html=True,
    )
    
    tab_booking, tab_standby, tab_future = st.tabs([
        "📋 Customer Service Booking Portal",
        "⚡ Move-Up Standby Queue",
        "📅 Future Bookings",
    ])
    
    with tab_booking:
        st.info(
            "📌 **Maintenance that needs to be moved up?** "
            "Open the **⚡ Move-Up Standby Queue** tab (or check **Add to Move-Up Standby Queue only**). "
            "Those requests stay on the standby list and will **not** show as Unassigned on dispatch."
        )
        cols = st.columns(3)
        for i, d in enumerate([day1, day2, day3]):
            metrics = get_detailed_metrics(d)
            with cols[i]:
                st.markdown(f"### Day {i+1}: {d.strftime('%a, %b %d')}")
                mc1, mc2 = st.columns(2)
                mc1.metric("AM Slots Left", f"{int(metrics['am_hours_left'] / 2)} slots")
                mc2.metric("PM Slots Left", f"{int(metrics['pm_hours_left'] / 2)} slots")
                
                jobs_booked = metrics["total_jobs_booked"]
                max_jobs = metrics["max_jobs_allowed"]
                job_status = "🔴 BOARD FULL" if jobs_booked >= max_jobs else f"{jobs_booked} / {max_jobs} calls"
                st.markdown(f"**Total Board Run Count:** `{job_status}`")
                
                with st.expander("View Priority Limits Filled"):
                    scheduled = get_scheduled_capacity_techs(d)
                    headcount = get_capacity_headcount(d)
                    weekend_note = (
                        f" · +{WEEKEND_ROTATING_CAPACITY} rotating weekend seat"
                        if d.strftime("%A") in WEEKEND_DAYS else ""
                    )
                    st.caption(
                        f"Capacity seats: **{headcount}** "
                        f"(fixed: {', '.join(scheduled) if scheduled else 'none'}{weekend_note}). "
                        "RJ Sat–Wed · Tony Mon–Fri (3-job cap) · Sat/Sun 2nd seat rotates · Kevin excluded."
                    )
                    for p_tier, cap in metrics["day_caps"].items():
                        current_count = metrics["priority_counts"][p_tier]
                        status_text = "🔴 SOLD OUT" if current_count >= cap else f"{current_count} / {cap} booked"
                        st.write(f"**{p_tier}:** {status_text}")

        st.markdown("---")
        
        with st.expander("ℹ️ Priority Level Descriptions Legend"):
            st.markdown("""
            * **🟣 Urgent:** Vulnerable Home / No Heat or Cool
            * **🟡 High:** High Value Opportunity / System Down (Comfort Compromised)
            * **🟠 Normal:** Partial No Heat or Cool / Routine Service
            * **🟢 Low:** Minor Issues / Non-Urgent Maintenance
            """)
            
        st.subheader("New Booking Entry Form")

        # Disable Enter-to-submit via JS
        st.html("""
            <script>
            (function() {
                function blockEnter(e) {
                    if (e.key === "Enter" && e.target.tagName === "INPUT") {
                        e.preventDefault();
                        e.stopPropagation();
                    }
                }
                document.addEventListener("keydown", blockEnter, true);
            })();
            </script>
        """)

        with st.form("csr_booking_form", clear_on_submit=True):
            col_left, col_right = st.columns(2)
            with col_left:
                cust_info = st.text_input("Customer Name / Job #")
                target_timeline = st.selectbox("Assign Target Day", ["Day 1 (Today)", "Day 2 (Tomorrow)", "Day 3 (Next Day)", "Future Booking"])
                chosen_future_date = st.date_input("If Future Booking:", value=today + timedelta(days=3))
                
                st.markdown("**Priority Hierarchy Configuration**")
                p_col1, p_col2 = st.columns(2)
                with p_col1:
                    job_class = st.selectbox("Priority L1 Classification", job_classes)
                with p_col2:
                    p_level = st.selectbox("Priority L2 Category", ["Normal", "Urgent", "High", "Low"])
                
            with col_right:
                time_frame = st.selectbox("Timeframe Preference", ["AM", "PM", "Open"])

                req_time_check = st.checkbox("⏰ Special Time Constraint? (adds tag to notes)")
                lace_book_check = st.checkbox("Did Lace Book? (adds tag to notes)")

                slots_est = st.selectbox("Required Slots (1 Slot = 2 Hours)", [1, 2, 3, 4])
                hours_est = slots_est * 2
                
                pre_coll = st.selectbox("Precollection Flag", precollect_opts)
                is_rec = st.checkbox("Is this a Recall? (Bypasses hour capacities & priority limits ⚠️)")
                m_override = st.checkbox("🚨 Apply Manager Cap Override? (Bypasses urgency tier limits 🛠️)")
                add_to_standby = st.checkbox(
                    "Add to Move-Up Standby Queue only? 📋",
                    help="Puts this request on the Standby list only — it will NOT appear as Unassigned on the dispatch board.",
                )
                if add_to_standby:
                    st.caption("Standby = waitlist only. Dispatch will pull it forward from the Move-Up Queue when a slot opens.")

                if req_time_check:
                    st.caption("⏰ [TIME REQUEST] will be added to the front of your notes on submit. Add the time detail after the colon.")
                if lace_book_check:
                    st.caption("📌 [LACE BOOKED] will be added to the end of your notes on submit.")
                notes_field = st.text_area("Internal Booking Instructions")
                
            if st.form_submit_button("Commit to Dispatch Board"):
                final_date = day1 if "Day 1" in target_timeline else day2 if "Day 2" in target_timeline else day3 if "Day 3" in target_timeline else chosen_future_date
                day_metrics = get_detailed_metrics(final_date)
                active_caps = day_metrics["day_caps"]
                
                is_board_maxed_out = day_metrics["total_jobs_booked"] >= day_metrics["max_jobs_allowed"]
                is_priority_sold_out = day_metrics["priority_counts"][p_level] >= active_caps[p_level]
                
                is_timeframe_full = (
                    (time_frame == "AM" and day_metrics["am_hours_left"] < hours_est) or 
                    (time_frame == "PM" and day_metrics["pm_hours_left"] < hours_est) or
                    (time_frame == "Open" and (day_metrics["am_hours_left"] + day_metrics["pm_hours_left"]) < hours_est)
                )
                
                bypass_urgency_rules = is_rec or m_override
                
                if not cust_info:
                    st.error("You must enter a Customer Name or Job Number.")
                elif add_to_standby:
                    # Standby = Move-Up Queue only — never lands on dispatch Unassigned
                    processed_notes = prepend_time_request_tag(notes_field) if req_time_check else notes_field
                    if lace_book_check:
                        processed_notes = append_lace_book_tag(processed_notes)
                    if m_override:
                        processed_notes = f"[MANAGER OVERRIDE APPLIED] {processed_notes}"

                    standby_id = str(int(datetime.now().timestamp()))
                    standby_tags = []
                    if job_class == "Member":
                        standby_tags.append("[MEMBER]")
                    if p_level == "Urgent":
                        standby_tags.append("[URGENT]")
                    tag_str = " ".join(standby_tags)
                    compiled_job_type = f"{job_class} Call {tag_str}".strip()
                    wait_notes = processed_notes or ""
                    if time_frame:
                        wait_notes = f"Preferred window: {time_frame}. {wait_notes}".strip()

                    conn = get_db_connection()
                    conn.execute("""
                        INSERT INTO Maintenance_Waitlist (id, timestamp, customer_name, phone, job_type, notes, status, scheduled_date)
                        VALUES (?, ?, ?, ?, ?, ?, 'Active', ?)
                    """, (
                        standby_id,
                        datetime.now().strftime("%Y-%m-%d %H:%M"),
                        cust_info,
                        "",
                        compiled_job_type,
                        wait_notes,
                        str(final_date),
                    ))
                    conn.commit()
                    conn.close()
                    st.session_state.flash_success = (
                        f"'{cust_info}' added to Move-Up Standby Queue only "
                        f"(not placed on the dispatch board)."
                    )
                    st.rerun()
                elif is_board_maxed_out:
                    st.session_state.flash_error = "🛑 BOARD SOLD OUT! Technicians have reached their safe daily run capacity."
                    st.rerun()
                elif is_priority_sold_out and not bypass_urgency_rules:
                    st.session_state.flash_error = f"🛑 TIER SOLD OUT! Daily allotment for '{p_level}' calls has been reached. Use Manager Override if approved."
                    st.rerun()
                elif is_timeframe_full and not is_rec:
                    st.session_state.flash_error = f"🛑 SLOT CAPACITY FULL! Not enough remaining space in the selected time window block."
                    st.rerun()
                else:
                    unique_id = str(int(datetime.now().timestamp()))
                    
                    processed_notes = prepend_time_request_tag(notes_field) if req_time_check else notes_field
                    if lace_book_check:
                        processed_notes = append_lace_book_tag(processed_notes)
                    if m_override:
                        processed_notes = f"[MANAGER OVERRIDE APPLIED] {processed_notes}"
                        
                    conn = get_db_connection()
                    conn.execute("""
                        INSERT INTO Booking_Log (id, timestamp, date, customer_job, priority_level, timeframe, hours_needed, job_type, precollection, technician, is_recall, status, notes, booked_by, is_standby)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (unique_id, datetime.now().strftime("%Y-%m-%d %H:%M"), str(final_date), cust_info, p_level, time_frame, hours_est, job_class, pre_coll, "Unassigned", 1 if is_rec else 0, "Booked", processed_notes, "CSR Desk", 0))
                    
                    conn.commit()
                    conn.close()
                    
                    st.session_state.flash_success = f"Call for '{cust_info}' successfully committed to the board!"
                    st.rerun()

    with tab_standby:
        st.subheader("📋 Catchall Move-Up Standby Queue")
        st.caption(
            "Waitlist only — these do **not** appear as Unassigned on the dispatch board. "
            "When the day is slow, pull a job forward from here into a real board booking."
        )

        st.markdown("#### Add Standalone Request to Standby Queue")
        with st.form("waitlist_form", clear_on_submit=True):
            wc1, wc2 = st.columns(2)
            with wc1:
                w_name = st.text_input("Customer Name / Location")
                w_scheduled = st.date_input("Current Scheduled Date", value=today + timedelta(days=7))
                w_type = st.selectbox("Job Type Base", ["Service Call", "Maintenance Clean & Check", "Filter Run", "System Performance Inspection"])
            with wc2:
                w_member = st.checkbox("Is Member? ⭐")
                w_urgent = st.checkbox("Is Urgent? 🚨")
                w_notes = st.text_area("Customer Availability Notes")

            if st.form_submit_button("Log on Standby Board"):
                if not w_name:
                    st.error("Customer name is required.")
                else:
                    w_id = str(int(datetime.now().timestamp()))
                    tags = []
                    if w_member: tags.append("[MEMBER]")
                    if w_urgent: tags.append("[URGENT]")
                    tag_str = " ".join(tags)
                    final_w_type = f"{w_type} {tag_str}".strip()

                    conn = get_db_connection()
                    conn.execute("""
                        INSERT INTO Maintenance_Waitlist (id, timestamp, customer_name, phone, job_type, notes, status, scheduled_date)
                        VALUES (?, ?, ?, ?, ?, ?, 'Active', ?)
                    """, (w_id, datetime.now().strftime("%Y-%m-%d %H:%M"), w_name, "", final_w_type, w_notes, str(w_scheduled)))
                    conn.commit()
                    conn.close()

                    st.session_state.flash_success = f"{w_name} successfully added to the Move-Up Standby Queue."
                    st.rerun()

        st.markdown("---")
        waitlist_df = load_waitlist()
        if waitlist_df.empty:
            st.info("The standby move-up queue is currently clear.")
        else:
            def standby_sort_key(row):
                urgent = 0 if "[URGENT]" in str(row.get("job_type", "")) else 1
                member = 0 if "[MEMBER]" in str(row.get("job_type", "")) else 1
                return (urgent, member, row.get("timestamp", ""))
            waitlist_sorted = waitlist_df.copy()
            waitlist_sorted["_sort"] = waitlist_sorted.apply(standby_sort_key, axis=1)
            waitlist_sorted = waitlist_sorted.sort_values("_sort").drop(columns=["_sort"])

            rows_html = ""
            theme = get_ui_theme()
            for i, row in waitlist_sorted.iterrows():
                jtype = str(row.get("job_type", ""))
                is_urgent = "[URGENT]" in jtype
                is_member = "[MEMBER]" in jtype
                badge = ""
                if is_urgent:
                    badge += "<span style='background:#CE2029;color:white;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:bold;margin-right:4px;'>🚨 URGENT</span>"
                if is_member:
                    badge += "<span style='background:#1d4ed8;color:white;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:bold;margin-right:4px;'>⭐ MEMBER</span>"
                clean_type = jtype.replace("[URGENT]", "").replace("[MEMBER]", "").strip()
                ts = str(row.get("timestamp", ""))[:16]
                sched = row.get("scheduled_date")
                if pd.isna(sched) or sched is None or str(sched).strip() in ("", "NaT", "None"):
                    sched_display = "—"
                elif hasattr(sched, "strftime"):
                    sched_display = sched.strftime("%a %m/%d/%Y")
                else:
                    sched_display = str(sched)
                notes = str(row.get("notes", "")) or "—"
                if is_urgent:
                    row_bg = theme["table_row_urgent"]
                elif is_member:
                    row_bg = theme["table_row_member"]
                else:
                    row_bg = theme["table_row"]
                rows_html += f"""
                <tr style="background:{row_bg}; border-bottom:1px solid {theme['table_border']}; color:{theme['text']};">
                    <td style="padding:10px 12px; font-weight:600; white-space:nowrap;">{row['customer_name']}</td>
                    <td style="padding:10px 12px; white-space:nowrap;">{sched_display}</td>
                    <td style="padding:10px 12px;">{clean_type}<br>{badge}</td>
                    <td style="padding:10px 12px; font-size:12px; color:{theme['text_secondary']}; max-width:220px;">{notes}</td>
                    <td style="padding:10px 12px; font-size:12px; color:{theme['text_muted']}; white-space:nowrap;">{ts}</td>
                </tr>"""

            st.html(f"""
                <div style="border:1px solid {theme['table_border']}; border-radius:8px; overflow:hidden; font-family:sans-serif; margin-bottom:12px;">
                    <table style="width:100%; border-collapse:collapse; font-size:13px; color:{theme['text']};">
                        <thead>
                            <tr style="background:{theme['table_header']}; color:{theme['table_header_text']};">
                                <th style="padding:10px 12px; text-align:left; font-weight:600;">Customer</th>
                                <th style="padding:10px 12px; text-align:left; font-weight:600;">Current Scheduled Date</th>
                                <th style="padding:10px 12px; text-align:left; font-weight:600;">Job Type</th>
                                <th style="padding:10px 12px; text-align:left; font-weight:600;">Notes</th>
                                <th style="padding:10px 12px; text-align:left; font-weight:600;">Logged At</th>
                            </tr>
                        </thead>
                        <tbody>{rows_html}</tbody>
                    </table>
                </div>
            """)

            st.markdown("#### ✅ Mark Entry as Completed")
            with st.form("complete_standby_form"):
                options_map = {f"{row['customer_name']} — {str(row['job_type']).replace('[URGENT]','').replace('[MEMBER]','').strip()}": row['id'] for _, row in waitlist_sorted.iterrows()}
                selected_option = st.selectbox("Select entry to complete / remove:", options=list(options_map.keys()))

                if st.form_submit_button("✅ Mark as Completed"):
                    target_id = options_map[selected_option]
                    conn = get_db_connection()
                    conn.execute("UPDATE Maintenance_Waitlist SET status = 'Completed' WHERE id = ?", (target_id,))
                    conn.commit()
                    conn.close()
                    st.session_state.flash_success = f"Standby entry '{selected_option}' marked as completed."
                    st.rerun()

    with tab_future:
        st.subheader("📅 Future Bookings")
        st.caption(
            f"Jobs scheduled after the live 3-day window (after **{day3.strftime('%A, %b %d')}**). "
            "These appear here when booked with the **Future Booking** option."
        )

        future_jobs = get_future_bookings(bookings_df, day3)
        if future_jobs.empty:
            st.info("No future bookings on file yet.")
        else:
            st.metric("Upcoming Future Jobs", len(future_jobs))
            future_display = future_jobs[
                [
                    "date", "customer_job", "priority_level", "timeframe", "hours_needed",
                    "job_type", "technician", "status", "booked_by", "timestamp", "notes",
                ]
            ].copy()
            st.dataframe(
                future_display,
                column_config={
                    "date": st.column_config.DateColumn("Scheduled Date", format="ddd MM/DD/YYYY"),
                    "customer_job": st.column_config.TextColumn("Customer / Job"),
                    "priority_level": st.column_config.TextColumn("Priority"),
                    "timeframe": st.column_config.TextColumn("Timeframe"),
                    "hours_needed": st.column_config.NumberColumn("Hours"),
                    "job_type": st.column_config.TextColumn("Job Type"),
                    "technician": st.column_config.TextColumn("Technician"),
                    "status": st.column_config.TextColumn("Status"),
                    "booked_by": st.column_config.TextColumn("Booked By"),
                    "timestamp": st.column_config.TextColumn("Logged At"),
                    "notes": st.column_config.TextColumn("Notes", width="large"),
                },
                use_container_width=True,
                hide_index=True,
            )

# -------------------------------------------------------------
# 2. PARTS & REPAIRS HUB
# -------------------------------------------------------------
elif view == "Parts & Repairs Hub":
    st.header("📦 Parts Coordination & Special Repair Scheduler")
    st.caption(
        "Use **Place on Hold** to reserve capacity while waiting on customer confirmation. "
        "Holds stay off the live dispatch board until you set status to **Booked** or **Scheduled**."
    )

    with st.form("parts_booking_form", clear_on_submit=True):
        col_p1, col_p2 = st.columns(2)
        with col_p1:
            p_cust = st.text_input("Customer Name / Job #")
            p_timeline = st.selectbox("Target Date", ["Day 1 (Today)", "Day 2 (Tomorrow)", "Day 3 (Next Day)", "Future Booking"])
            p_future_date = st.date_input("If Future, choose here:", value=today + timedelta(days=3))
            p_timeframe = st.selectbox("Timeframe Window Allocation", ["All Day", "AM 1", "AM 2", "PM 1", "PM 2", "AM", "PM", "Open"])
        with col_p2:
            p_slots = st.selectbox("Required Slots (1 Slot = 2 Hours)", [1, 2, 3, 4], index=3, help="For All Day jobs, consider locking to 4 slots (8 hours).")
            p_hours = p_slots * 2

            parts_form_tech_options = ["Unassigned"]
            for d in [day1, day2, day3]:
                parts_form_tech_options.extend(get_assignable_tech_options(d))
            parts_form_tech_options = list(dict.fromkeys(parts_form_tech_options))

            p_assigned_tech = st.selectbox(
                "Pre-Assign Repair Technician",
                parts_form_tech_options,
                help="Techs marked OUT are not listed. Availability follows the Call-Out Board.",
            )
            p_booking_mode = st.radio(
                "Booking mode",
                ["Lock in now (Scheduled)", "Place on Hold (reserve space)"],
                horizontal=True,
                help="Hold reserves AM/PM capacity for that day but stays off the live crew board until confirmed.",
            )
            p_notes = st.text_area("Parts / PO Details & Requirements")

        if st.form_submit_button("Save Repair Entry"):
            if not p_cust:
                st.error("Customer field is required.")
            else:
                final_date = day1 if "Day 1" in p_timeline else day2 if "Day 2" in p_timeline else day3 if "Day 3" in p_timeline else p_future_date
                assignable = get_assignable_tech_options(final_date, [p_assigned_tech])
                assigned_tech = p_assigned_tech if p_assigned_tech in assignable else "Unassigned"
                unique_id = str(int(datetime.now().timestamp()))
                is_hold = "Hold" in p_booking_mode
                save_status = "Hold" if is_hold else "Scheduled"
                note_prefix = "⏸️ PARTS HOLD:" if is_hold else "🔧 PARTS REPAIR:"

                conn = get_db_connection()
                conn.execute("""
                    INSERT INTO Booking_Log (id, timestamp, date, customer_job, priority_level, timeframe, hours_needed, job_type, precollection, technician, is_recall, status, notes, booked_by, is_standby)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (unique_id, datetime.now().strftime("%Y-%m-%d %H:%M"), str(final_date), p_cust, "Normal", p_timeframe, p_hours, "Special Repair", "Not Applicable", assigned_tech, 0, save_status, f"{note_prefix} {p_notes}", "Parts Coordination", 0))
                conn.commit()
                conn.close()

                if is_hold:
                    st.session_state.flash_success = (
                        f"Repair hold placed for '{p_cust}' on {final_date.strftime('%a %m/%d')} — "
                        f"capacity reserved. Confirm from On Hold below when ready."
                    )
                else:
                    st.session_state.flash_success = f"Special Repair booked and assigned to {assigned_tech}."
                st.rerun()

    st.markdown("---")
    st.subheader("⏸️ On Hold — awaiting customer confirmation")
    st.caption(
        "Change **Held Date** if that day does not work for the customer. "
        "Set **Status** to **Booked** or **Scheduled** to commit onto the live dispatch board."
    )

    if not bookings_df.empty:
        hold_jobs = bookings_df[
            (bookings_df["job_type"] == "Special Repair") &
            (bookings_df["status"] == "Hold")
        ].copy()
    else:
        hold_jobs = pd.DataFrame()

    if hold_jobs.empty:
        st.info("No repairs currently on hold.")
    else:
        hold_jobs = hold_jobs.sort_values(["date", "customer_job"])
        hold_tech_options = ["Unassigned"]
        for _, row in hold_jobs.iterrows():
            hold_tech_options.extend(get_assignable_tech_options(row["date"], [row["technician"]]))
        hold_tech_options = list(dict.fromkeys(hold_tech_options))
        hold_display_cols = ["id", "date", "customer_job", "timeframe", "hours_needed", "technician", "status", "notes"]
        hold_editor = st.data_editor(
            hold_jobs[hold_display_cols],
            column_config={
                "id": None,
                "date": st.column_config.DateColumn(
                    "Held Date",
                    format="MM/DD/YYYY",
                    help="Change this if the original hold date does not work for the customer.",
                ),
                "customer_job": st.column_config.TextColumn("Customer / Job"),
                "timeframe": st.column_config.SelectboxColumn(
                    "Slot",
                    options=["All Day", "AM 1", "AM 2", "PM 1", "PM 2", "AM", "PM", "Open"],
                ),
                "hours_needed": st.column_config.SelectboxColumn(
                    "Duration (hrs)",
                    options=[2, 4, 6, 8],
                ),
                "technician": st.column_config.SelectboxColumn("Technician", options=hold_tech_options),
                "status": st.column_config.SelectboxColumn(
                    "Status",
                    options=["Hold", "Booked", "Scheduled", "Cancelled"],
                    help="Booked or Scheduled commits this hold onto the live board.",
                ),
                "notes": st.column_config.TextColumn("Parts / PO Notes", width="large"),
            },
            use_container_width=True,
            hide_index=True,
            key="parts_hold_editor",
        )

        if st.button("💾 Save Hold Updates", type="primary", key="save_parts_holds"):
            conn = get_db_connection()
            confirmed = 0
            for _, row in hold_editor.iterrows():
                target_date = row["date"]
                if hasattr(target_date, "date"):
                    target_date = target_date.date()
                assignable = get_assignable_tech_options(target_date, [row["technician"]])
                tech = row["technician"] if row["technician"] in assignable else "Unassigned"
                notes = row["notes"]
                if row["status"] in ("Booked", "Scheduled") and isinstance(notes, str):
                    notes = notes.replace("⏸️ PARTS HOLD:", "🔧 PARTS REPAIR:", 1)
                if row["status"] in ("Booked", "Scheduled"):
                    confirmed += 1
                conn.execute(
                    """UPDATE Booking_Log SET date = ?, customer_job = ?, timeframe = ?, hours_needed = ?,
                       technician = ?, status = ?, notes = ? WHERE id = ?""",
                    (
                        str(target_date), row["customer_job"], row["timeframe"], int(row["hours_needed"]),
                        tech, row["status"], notes, str(row["id"]),
                    ),
                )
            conn.commit()
            conn.close()
            msg = "Hold updates saved."
            if confirmed:
                msg += f" {confirmed} job(s) committed to the live board."
            st.session_state.flash_success = msg
            st.rerun()

    st.markdown("---")
    st.subheader("📋 Active Repair Bookings (on the board)")
    st.caption("Confirmed Special Repair jobs currently on the dispatch board.")

    if not bookings_df.empty:
        parts_jobs = bookings_df[
            (bookings_df["job_type"] == "Special Repair") &
            (bookings_df["status"].apply(is_on_board))
        ].copy()
    else:
        parts_jobs = pd.DataFrame()

    if parts_jobs.empty:
        st.info("No active repair bookings on the board yet.")
    else:
        parts_jobs = parts_jobs.sort_values(["date", "customer_job"])
        parts_tech_options = ["Unassigned"]
        for _, row in parts_jobs.iterrows():
            parts_tech_options.extend(get_assignable_tech_options(row["date"], [row["technician"]]))
        parts_tech_options = list(dict.fromkeys(parts_tech_options))
        parts_display_cols = ["id", "date", "customer_job", "timeframe", "hours_needed", "technician", "status", "notes"]
        parts_editor = st.data_editor(
            parts_jobs[parts_display_cols],
            column_config={
                "id": None,
                "date": st.column_config.DateColumn("Date", format="MM/DD/YYYY"),
                "customer_job": st.column_config.TextColumn("Customer / Job"),
                "timeframe": st.column_config.SelectboxColumn(
                    "Slot",
                    options=["All Day", "AM 1", "AM 2", "PM 1", "PM 2", "AM", "PM", "Open"],
                ),
                "hours_needed": st.column_config.SelectboxColumn(
                    "Duration (hrs)",
                    options=[2, 4, 6, 8],
                    help="2 hrs = 1 slot, 4 hrs = 2 slots, etc.",
                ),
                "technician": st.column_config.SelectboxColumn("Technician", options=parts_tech_options),
                "status": st.column_config.SelectboxColumn(
                    "Status",
                    options=JOB_STATUSES,
                ),
                "notes": st.column_config.TextColumn("Parts / PO Notes", width="large"),
            },
            use_container_width=True,
            hide_index=True,
            key="parts_repair_editor",
        )

        if st.button("💾 Save Repair Booking Changes", type="primary"):
            conn = get_db_connection()
            for _, row in parts_editor.iterrows():
                target_date = row["date"]
                if hasattr(target_date, "date"):
                    target_date = target_date.date()
                assignable = get_assignable_tech_options(target_date, [row["technician"]])
                tech = row["technician"] if row["technician"] in assignable else "Unassigned"
                conn.execute(
                    """UPDATE Booking_Log SET date = ?, customer_job = ?, timeframe = ?, hours_needed = ?,
                       technician = ?, status = ?, notes = ? WHERE id = ?""",
                    (
                        str(target_date), row["customer_job"], row["timeframe"], int(row["hours_needed"]),
                        tech, row["status"], row["notes"], str(row["id"]),
                    ),
                )
            conn.commit()
            conn.close()
            st.session_state.flash_success = "Repair booking updates saved."
            st.rerun()

# -------------------------------------------------------------
# 4. DISPATCH OPERATIONAL DESK
# -------------------------------------------------------------
elif view == "Dispatch Operational Desk":
    st.header("🎛️ Dispatcher Management Control")
    
    day1_metrics = get_detailed_metrics(day1)
    if day1_metrics["am_hours_left"] > 0 or day1_metrics["pm_hours_left"] > 0:
        if not bookings_df.empty:
            day2_urgents = bookings_df[(bookings_df["date"] == day2) & (bookings_df["priority_level"] == "Urgent") & (bookings_df["status"].apply(is_on_board))]
            if not day2_urgents.empty:
                st.warning(f"⚠️ **PULL-FORWARD ALERT:** You have `{len(day2_urgents)}` Urgent call(s) sitting on Tomorrow's board ({day2.strftime('%m/%d')}), while Today ({day1.strftime('%m/%d')}) has remaining capacity!")
                with st.expander("View Tomorrow's Pull-Forward Candidates"):
                    for _, u_job in day2_urgents.iterrows():
                        st.markdown(f"* **{u_job['customer_job']}** ({u_job['timeframe']} window) | Notes: *{u_job['notes']}*")
                        
    DISPATCH_DESK_SECTIONS = [
        "Active 3-Day Routing Matrix",
        "📅 Reschedule Queue",
        "🔮 Future Jobs Board",
        "🚫 Call-Out Board",
        "Tech Attendance & Availability",
    ]
    desk_tab = st.radio(
        "Dispatch desk section",
        DISPATCH_DESK_SECTIONS,
        horizontal=True,
        key="dispatch_desk_tab",
        label_visibility="collapsed",
    )
    st.caption("Selected section stays put when you change dates or save.")

    if desk_tab == "🚫 Call-Out Board":
        st.subheader("🚫 Crew Call-Out & Availability Board")
        st.caption("Live view of dispatcher attendance changes across the 3-day window. Update entries in **Tech Attendance & Availability**.")
        render_callout_board([day1, day2, day3])

    if desk_tab == "📅 Reschedule Queue":
        st.subheader("📅 Reschedule Queue")
        st.caption("Jobs marked **Rescheduled** are held here until you assign a new day and set status back to **Booked**.")

        if bookings_df.empty:
            st.info("No bookings in the system.")
        else:
            rescheduled_jobs = bookings_df[bookings_df["status"] == "Rescheduled"].copy()
            if rescheduled_jobs.empty:
                st.success("✅ No jobs waiting to be rescheduled.")
            else:
                rescheduled_jobs = rescheduled_jobs.sort_values(["date", "customer_job"])
                reschedule_tech_options = ["Unassigned"]
                for _, row in rescheduled_jobs.iterrows():
                    reschedule_tech_options.extend(
                        get_assignable_tech_options(row["date"], [row["technician"]])
                    )
                reschedule_tech_options = list(dict.fromkeys(reschedule_tech_options))
                reschedule_cols = ["id", "date", "customer_job", "priority_level", "timeframe", "hours_needed", "technician", "status", "notes"]
                reschedule_editor = st.data_editor(
                    rescheduled_jobs[reschedule_cols],
                    column_config={
                        "id": None,
                        "date": st.column_config.DateColumn("New Date", format="MM/DD/YYYY"),
                        "customer_job": st.column_config.TextColumn("Customer / Job", disabled=True),
                        "priority_level": st.column_config.SelectboxColumn(
                            "Priority", options=["Urgent", "High", "Normal", "Low"]
                        ),
                        "timeframe": st.column_config.SelectboxColumn(
                            "Slot", options=time_slots + ["AM", "PM", "Open", "All Day"]
                        ),
                        "hours_needed": st.column_config.SelectboxColumn("Hrs", options=[2, 4, 6, 8]),
                        "technician": st.column_config.SelectboxColumn(
                            "Technician",
                            options=reschedule_tech_options,
                            help="Off techs are hidden — pick an available tech or Unassigned.",
                        ),
                        "status": st.column_config.SelectboxColumn(
                            "Status",
                            options=["Rescheduled", "Booked", "Scheduled", "Cancelled"],
                            help="Set to Booked or Scheduled to return the job to the live board.",
                        ),
                        "notes": st.column_config.TextColumn("Notes", width="large"),
                    },
                    use_container_width=True,
                    hide_index=True,
                    key="reschedule_queue_editor",
                )

                if st.button("💾 Save Reschedule Updates", type="primary"):
                    conn = get_db_connection()
                    for _, row in reschedule_editor.iterrows():
                        target_date = row["date"]
                        if hasattr(target_date, "date"):
                            target_date = target_date.date()
                        assignable = get_assignable_tech_options(target_date, [row["technician"]])
                        tech = row["technician"] if row["technician"] in assignable else "Unassigned"
                        conn.execute(
                            """UPDATE Booking_Log SET date = ?, timeframe = ?, hours_needed = ?,
                               technician = ?, status = ?, priority_level = ?, notes = ? WHERE id = ?""",
                            (
                                str(target_date), row["timeframe"], int(row["hours_needed"]),
                                tech, row["status"], row["priority_level"], row["notes"], str(row["id"]),
                            ),
                        )
                    conn.commit()
                    conn.close()
                    st.session_state.flash_success = "Reschedule queue updated."
                    st.rerun()

    if desk_tab == "🔮 Future Jobs Board":
        st.subheader("🔮 Future Jobs Board")
        st.caption(
            f"Edit bookings scheduled after **{day3.strftime('%A, %b %d')}**. "
            "Move a job onto the live board by changing its date to today or the next two days."
        )

        future_jobs = get_future_bookings(bookings_df, day3)
        if future_jobs.empty:
            st.info("No future bookings on file.")
        else:
            future_jobs = future_jobs.sort_values(["date", "customer_job"])
            future_tech_options = ["Unassigned"]
            for _, row in future_jobs.iterrows():
                future_tech_options.extend(get_assignable_tech_options(row["date"], [row["technician"]]))
            future_tech_options = list(dict.fromkeys(future_tech_options))
            future_cols = [
                "id", "date", "customer_job", "priority_level", "timeframe", "hours_needed",
                "job_type", "technician", "status", "booked_by", "notes",
            ]
            future_editor = st.data_editor(
                future_jobs[future_cols],
                column_config={
                    "id": None,
                    "date": st.column_config.DateColumn("Scheduled Date", format="MM/DD/YYYY"),
                    "customer_job": st.column_config.TextColumn("Customer / Job"),
                    "priority_level": st.column_config.SelectboxColumn(
                        "Priority", options=["Urgent", "High", "Normal", "Low"]
                    ),
                    "timeframe": st.column_config.SelectboxColumn(
                        "Slot", options=time_slots + ["AM", "PM", "Open", "All Day"]
                    ),
                    "hours_needed": st.column_config.SelectboxColumn("Hrs", options=[2, 4, 6, 8]),
                    "job_type": st.column_config.TextColumn("Job Type", disabled=True),
                    "technician": st.column_config.SelectboxColumn(
                        "Technician",
                        options=future_tech_options,
                        help="Off techs are hidden — pick an available tech or Unassigned.",
                    ),
                    "status": st.column_config.SelectboxColumn("Status", options=JOB_STATUSES),
                    "booked_by": st.column_config.TextColumn("Booked By", disabled=True),
                    "notes": st.column_config.TextColumn("Notes", width="large"),
                },
                use_container_width=True,
                hide_index=True,
                key="future_jobs_editor",
            )

            if st.button("💾 Save Future Job Updates", type="primary"):
                conn = get_db_connection()
                for _, row in future_editor.iterrows():
                    target_date = row["date"]
                    if hasattr(target_date, "date"):
                        target_date = target_date.date()
                    assignable = get_assignable_tech_options(target_date, [row["technician"]])
                    tech = row["technician"] if row["technician"] in assignable else "Unassigned"
                    conn.execute(
                        """UPDATE Booking_Log SET date = ?, timeframe = ?, hours_needed = ?,
                           technician = ?, status = ?, priority_level = ?, notes = ? WHERE id = ?""",
                        (
                            str(target_date), row["timeframe"], int(row["hours_needed"]),
                            tech, row["status"], row["priority_level"], row["notes"], str(row["id"]),
                        ),
                    )
                conn.commit()
                conn.close()
                st.session_state.flash_success = "Future job board updated."
                st.rerun()
    
    if desk_tab == "Tech Attendance & Availability":
        st.subheader("🗓️ Crew Attendance & Capacity Adjustments")
        st.caption(
            "RJ Oyanib: **Sat–Wed**. Tony Brown: **Mon–Fri**, **3-job daily cap**. "
            "Other dispatch techs: **Mon–Fri**, 4-job cap. "
            "Sat/Sun = RJ + **1 rotating seat**. Thu/Fri RJ is off schedule."
        )
        b_date = st.date_input("Select Target Roster Date", value=today, key="attendance_date_picker")
        day_exceptions = roster_df[roster_df['date'] == b_date] if not roster_df.empty else pd.DataFrame(columns=["date", "technician", "avail_type", "dispatcher_notes"])
        
        roster_status_list = []
        for tech in tech_list:
            warranty_tech = is_warranty_technician(tech)
            on_schedule = tech_works_on_date(tech, b_date) if not warranty_tech else True
            if not day_exceptions.empty and 'technician' in day_exceptions.columns:
                tech_exc = day_exceptions[day_exceptions['technician'] == tech]
            else:
                tech_exc = pd.DataFrame()
            
            if not on_schedule and not tech_in_weekend_rotate_pool(tech, b_date):
                current_status = "📅 Off Schedule"
                am_disp, pm_disp = "0 hrs", "0 hrs"
                notes = "Regular schedule does not include this day"
                is_modified = False
            elif tech_in_weekend_rotate_pool(tech, b_date) and tech_exc.empty:
                current_status = "🔄 Weekend Rotate Pool"
                am_disp, pm_disp = f"{DEFAULT_TECH_AM_HOURS} hrs", f"{DEFAULT_TECH_PM_HOURS} hrs"
                notes = "Eligible for rotating 2nd weekend seat"
                is_modified = False
            elif not tech_exc.empty:
                current_status = tech_exc.iloc[0]['avail_type']
                notes = tech_exc.iloc[0]['dispatcher_notes']
                is_modified = True
                if current_status in ["PTO Full", "Call Out"]: am_disp, pm_disp = "0 hrs", "0 hrs"
                elif "AM off" in current_status: am_disp, pm_disp = "0 hrs", f"{get_tech_pm_hours(tech)} hrs"
                elif "PM off" in current_status: am_disp, pm_disp = f"{get_tech_am_hours(tech)} hrs", "0 hrs"
                elif current_status == "Training/Meeting":
                    am_disp = f"{max(1, get_tech_am_hours(tech) // 2)} hrs"
                    pm_disp = f"{max(1, get_tech_pm_hours(tech) // 2)} hrs"
                else: am_disp, pm_disp = f"{get_tech_am_hours(tech)} hrs", f"{get_tech_pm_hours(tech)} hrs"
            else:
                current_status = "🟢 Active / Working"
                am_disp, pm_disp = f"{get_tech_am_hours(tech)} hrs", f"{get_tech_pm_hours(tech)} hrs"
                job_cap = get_tech_job_cap(tech)
                notes = f"Standard Schedule · {job_cap}-job daily cap"
                is_modified = False

            if warranty_tech:
                am_disp, pm_disp = "—", "—"
                notes = "Warranty route — tracked for visibility only; does not reduce dispatch capacity."
                
            roster_status_list.append({
                "Technician": f"{tech} (Warranty)" if warranty_tech else tech,
                "Current Status": current_status,
                "AM Capacity": am_disp,
                "PM Capacity": pm_disp,
                "Notes / Reasons": notes,
                "Remove Exception": False if is_modified else None
            })
            
        status_table = pd.DataFrame(roster_status_list)
        edited_attendance = st.data_editor(
            status_table,
            column_config={"Remove Exception": st.column_config.CheckboxColumn("Cancel Time Off?", default=False)},
            disabled=["Technician", "Current Status", "AM Capacity", "PM Capacity", "Notes / Reasons"],
            use_container_width=True, hide_index=True, key="attendance_live_editor"
        )
        
        if "Remove Exception" in edited_attendance.columns:
            techs_to_restore = edited_attendance[edited_attendance["Remove Exception"] == True]["Technician"].tolist()
            techs_to_restore = [t.replace(" (Warranty)", "") for t in techs_to_restore]
            if techs_to_restore and st.button("Save Attendance Removals"):
                conn = get_db_connection()
                for tech in techs_to_restore:
                    conn.execute("DELETE FROM Tech_Roster WHERE date = ? AND technician = ?", (str(b_date), tech))
                conn.commit()
                conn.close()
                
                st.session_state.flash_success = "Attendance roster exceptions cleared successfully."
                st.rerun()
                    
        st.markdown("---")
        with st.form("blackout_form", clear_on_submit=True):
            col_b1, col_b2 = st.columns(2)
            with col_b1:
                b_tech = st.selectbox("Select Technician", tech_list)
                b_status = st.selectbox("Assign Status Shift", ["PTO Partial (AM off)", "PTO Partial (PM off)", "PTO Full", "Training/Meeting", "Call Out"])
            with col_b2:
                b_notes = st.text_input("Reason / Operational Note")
            
            if st.form_submit_button("Commit Status Shift"):
                am_h, pm_h = 4, 4
                if "AM off" in b_status: am_h, pm_h = 0, 4
                elif "PM off" in b_status: am_h, pm_h = 4, 0
                elif b_status in ["PTO Full", "Call Out"]: am_h, pm_h = 0, 0
                elif b_status == "Training/Meeting": am_h, pm_h = 2, 2
                
                conn = get_db_connection()
                conn.execute("INSERT OR REPLACE INTO Tech_Roster (date, technician, avail_type, am_hours, pm_hours, dispatcher_notes) VALUES (?, ?, ?, ?, ?, ?)", (str(b_date), b_tech, b_status, am_h, pm_h, b_notes))
                conn.commit()
                conn.close()
                
                st.session_state.flash_success = f"Status shift recorded for {b_tech}."
                st.rerun()

    if desk_tab == "Active 3-Day Routing Matrix":
        st.subheader("Visualized Crew Slot Schedule")
        target_view_day, day_idx = select_board_day("dispatcher_routing_day_idx")
        st.caption("Selected day stays after you save.")
        render_board_color_key()
        
        if bookings_df.empty:
            day_jobs = pd.DataFrame(columns=[
                "id", "customer_job", "priority_level", "timeframe", "hours_needed",
                "technician", "status", "notes", "job_type", "is_recall", "is_standby",
            ])
        else:
            day_jobs = filter_board_jobs(bookings_df[bookings_df['date'] == target_view_day]).copy()

        unassigned_jobs = day_jobs[day_jobs['technician'] == "Unassigned"] if not day_jobs.empty else day_jobs
        day_metrics = get_detailed_metrics(target_view_day)

        sc1, sc2, sc3, sc4 = st.columns(4)
        sc1.metric("Total Jobs", len(day_jobs))
        sc2.metric("Needs Assignment", len(unassigned_jobs))
        sc3.metric("Urgent", len(day_jobs[day_jobs['priority_level'] == "Urgent"]) if not day_jobs.empty else 0)
        sc4.metric("Open Capacity", f"{int(day_metrics['am_hours_left'] / 2)} AM / {int(day_metrics['pm_hours_left'] / 2)} PM")

        if not unassigned_jobs.empty:
            st.error(f"🚨 **{len(unassigned_jobs)} job(s) still need a technician** — assign these in the Quick Modification Grid below.")
        elif not day_jobs.empty:
            st.success("✅ All jobs are assigned to a technician for this day.")

        st.markdown("---")

        visible_crew_techs = render_dispatch_crew_sections(target_view_day, day_jobs)

        if visible_crew_techs:
            st.divider()
        render_warranty_board_section(target_view_day, day_jobs)

        if not visible_crew_techs:
            st.info("No dispatch crew jobs or call-outs for this day.")

        st.markdown("---")
        st.subheader("Quick Modification Grid")
        st.caption("Use **Rescheduled** or **Cancelled** to remove a job from the board (no-shows, reschedules).")

        with st.container(border=True):
            st.markdown("**How to edit Slot, Technician, Status, and Hours**")
            st.markdown(
                "1. **Click the cell** in the **Slot**, **Technician**, **Status**, or **Hrs** column for the job you want to change.\n"
                "2. **Select a value from the dropdown** (or enter hours: 2, 4, 6, or 8 — each slot is 2 hours).\n"
                "3. When finished editing all jobs, click **💾 Save Grid Adjustments** at the bottom of the page."
            )
            ig1, ig2, ig3, ig4 = st.columns(4)
            with ig1:
                st.markdown("**Slot** — click cell → pick time window")
            with ig2:
                st.markdown("**Technician** — off techs hidden per day (see Call-Out Board)")
            with ig3:
                st.markdown("**Status** — **Rescheduled** sends job to Reschedule Queue")
            with ig4:
                st.markdown("**Hrs** — click cell → set duration (4 hrs spans 2 slots on the board)")

        render_board_color_key()

        with st.container(border=True):
            gf_col1, gf_col2 = st.columns(2)
            with gf_col1:
                grid_filter_tech = st.selectbox(
                    "Filter by Technician",
                    ["All Technicians", "Unassigned"] + tech_list,
                    key="grid_filter_tech"
                )
            with gf_col2:
                grid_filter_status = st.selectbox(
                    "Filter by Status",
                    ["Active Only", "All Statuses"],
                    key="grid_filter_status"
                )

        if bookings_df.empty:
            st.info("No jobs to display in the grid.")
        else:
            viewable_jobs = bookings_df[bookings_df['date'].isin([day1, day2, day3])].copy()
            viewable_jobs = exclude_standby_jobs(viewable_jobs)

            if grid_filter_tech != "All Technicians":
                viewable_jobs = viewable_jobs[viewable_jobs['technician'] == grid_filter_tech]

            if grid_filter_status == "Active Only":
                viewable_jobs = filter_board_jobs(viewable_jobs)

            slot_order = {"AM 1": 0, "AM": 0, "AM 2": 1, "Open": 1, "PM 1": 2, "PM": 2, "PM 2": 3, "All Day": 0}
            priority_order = {"Urgent": 0, "High": 1, "Normal": 2, "Low": 3}

            display_cols = ["id", "timeframe", "customer_job", "priority_level", "status", "hours_needed", "technician", "notes"]
            base_grid_column_config = {
                "id": None,
                "timeframe": st.column_config.SelectboxColumn(
                    "Slot",
                    options=time_slots + ["AM", "PM", "Open", "All Day"],
                    width="small",
                    help="Click the cell, then choose a time slot from the dropdown.",
                ),
                "customer_job": st.column_config.TextColumn("Customer / Job", width="medium"),
                "priority_level": st.column_config.SelectboxColumn(
                    "Priority",
                    options=["Urgent", "High", "Normal", "Low"],
                    width="small",
                    help="Click the cell to change priority level.",
                ),
                "status": st.column_config.SelectboxColumn(
                    "Status",
                    options=JOB_STATUSES,
                    width="small",
                    help="Rescheduled or Cancelled removes the job from the live board.",
                ),
                "hours_needed": st.column_config.SelectboxColumn(
                    "Hrs",
                    options=[2, 4, 6, 8],
                    width="small",
                    help="Duration in hours. 4 hrs = 2 slots (bubble spans AM 1 + AM 2 on the board).",
                ),
                "notes": st.column_config.TextColumn("Notes", width="large", help="Click the cell to edit internal booking notes."),
            }

            edited_by_date = []
            any_jobs = False
            tech_sections = ["Unassigned"] + capacity_tech_list
            target_day = target_view_day

            grid_day_jobs = viewable_jobs[viewable_jobs['date'] == target_day].copy()

            if grid_day_jobs.empty:
                st.info("No jobs for this day.")
            else:
                day_metrics = get_detailed_metrics(target_day)
                sm1, sm2, sm3, sm4 = st.columns(4)
                sm1.metric("Total Jobs", len(grid_day_jobs))
                sm2.metric("Unassigned", len(grid_day_jobs[grid_day_jobs['technician'] == "Unassigned"]))
                sm3.metric("Urgent", len(grid_day_jobs[grid_day_jobs['priority_level'] == "Urgent"]))
                sm4.metric("Open Slots", f"{int(day_metrics['am_hours_left'] / 2)} AM / {int(day_metrics['pm_hours_left'] / 2)} PM")
                st.markdown("")

                day_assigned_techs = grid_day_jobs[grid_day_jobs["technician"] != "Unassigned"]["technician"].tolist()
                day_tech_options = get_assignable_tech_options(target_day, day_assigned_techs)
                day_grid_column_config = {
                    **base_grid_column_config,
                    "technician": st.column_config.SelectboxColumn(
                        "Technician",
                        options=day_tech_options,
                        width="medium",
                        help="Techs marked OUT for this day are not available to assign.",
                    ),
                }

                day_has_jobs = False
                for tech_idx, tech_name in enumerate(tech_sections):
                    tech_jobs = grid_day_jobs[grid_day_jobs['technician'] == tech_name].copy()
                    if tech_jobs.empty:
                        continue

                    day_has_jobs = True
                    any_jobs = True
                    tech_jobs["_slot_sort"] = tech_jobs["timeframe"].map(slot_order).fillna(9)
                    tech_jobs["_priority_sort"] = tech_jobs["priority_level"].map(priority_order).fillna(9)
                    tech_jobs = tech_jobs.sort_values(["_priority_sort", "_slot_sort"]).drop(columns=["_slot_sort", "_priority_sort"])

                    urgent_count = len(tech_jobs[tech_jobs['priority_level'] == "Urgent"])
                    slot_summary = ", ".join(
                        f"{slot}: {count}"
                        for slot, count in tech_jobs['timeframe'].value_counts().items()
                    )
                    header_class = "grid-tech-header-unassigned" if tech_name == "Unassigned" else "grid-tech-header-assigned"
                    header_icon = "⚠️" if tech_name == "Unassigned" else "👤"
                    urgent_note = f" · {urgent_count} urgent" if urgent_count else ""

                    st.markdown(
                        f"<div class='grid-tech-header {header_class}'>"
                        f"<span>{header_icon} {tech_name}</span>"
                        f"<span class='grid-tech-count'>{len(tech_jobs)} job(s){urgent_note} · {slot_summary}</span>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )

                    with st.container(border=True):
                        edited_tech = st.data_editor(
                            tech_jobs[display_cols],
                            column_config=day_grid_column_config,
                            disabled=["customer_job"],
                            use_container_width=True,
                            hide_index=True,
                            key=f"dispatcher_live_grid_d{day_idx}_t{tech_idx}"
                        )
                        edited_by_date.append((target_day, edited_tech))

                warranty_jobs = grid_day_jobs[grid_day_jobs["technician"] == WARRANTY_TECH].copy()
                st.markdown(
                    "<div style='font-size:0.78rem;font-weight:700;letter-spacing:0.06em;"
                    "text-transform:uppercase;color:#c084fc;margin:1rem 0 0.35rem 0;'>"
                    "🛡️ Warranty Technician (not counted in dispatch capacity)</div>",
                    unsafe_allow_html=True,
                )
                if warranty_jobs.empty:
                    st.caption(f"{WARRANTY_TECH} — no warranty jobs on this day.")
                else:
                    day_has_jobs = True
                    any_jobs = True
                    warranty_jobs["_slot_sort"] = warranty_jobs["timeframe"].map(slot_order).fillna(9)
                    warranty_jobs["_priority_sort"] = warranty_jobs["priority_level"].map(priority_order).fillna(9)
                    warranty_jobs = warranty_jobs.sort_values(["_priority_sort", "_slot_sort"]).drop(columns=["_slot_sort", "_priority_sort"])
                    slot_summary = ", ".join(
                        f"{slot}: {count}"
                        for slot, count in warranty_jobs['timeframe'].value_counts().items()
                    )
                    st.markdown(
                        f"<div class='grid-tech-header grid-tech-header-warranty'>"
                        f"<span>🛡️ {WARRANTY_TECH}</span>"
                        f"<span class='grid-tech-count'>{len(warranty_jobs)} warranty job(s) · {slot_summary}</span>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
                    with st.container(border=True):
                        edited_warranty = st.data_editor(
                            warranty_jobs[display_cols],
                            column_config=day_grid_column_config,
                            disabled=["customer_job"],
                            use_container_width=True,
                            hide_index=True,
                            key=f"dispatcher_live_grid_d{day_idx}_warranty",
                        )
                        edited_by_date.append((target_day, edited_warranty))

                if not day_has_jobs and warranty_jobs.empty:
                    st.info("No jobs for this day.")

            if not any_jobs:
                st.info("No jobs match the current filters.")

            if any_jobs:
                st.markdown("")
                save_col1, save_col2 = st.columns([1, 3])
                with save_col1:
                    save_grid = st.button("💾 Save Grid Adjustments", type="primary", use_container_width=True)
                with save_col2:
                    st.caption("Saves edits for the day currently selected above.")

            if any_jobs and save_grid:
                conn = get_db_connection()
                for target_day, edited_grid in edited_by_date:
                    assigned = edited_grid[edited_grid["technician"] != "Unassigned"]["technician"].tolist()
                    assignable = get_assignable_tech_options(target_day, assigned)
                    for _, row in edited_grid.iterrows():
                        tech = row["technician"] if row["technician"] in assignable else "Unassigned"
                        conn.execute(
                            "UPDATE Booking_Log SET technician = ?, status = ?, notes = ?, date = ?, timeframe = ?, priority_level = ?, hours_needed = ? WHERE id = ?",
                            (tech, row['status'], row['notes'], str(target_day), row['timeframe'], row['priority_level'], int(row['hours_needed']), str(row['id']))
                        )
                conn.commit()
                conn.close()
                st.session_state.flash_success = "Routing grid updates synced to database."
                st.rerun()

# -------------------------------------------------------------
# 5. LIVE SUMMARY BOARD (HUDDLE SNAPSHOT) - FULL BACKGROUND COLORS
# -------------------------------------------------------------
elif view == "Live Summary Board":
    st.subheader("🦅 Master 3-Day Huddle Snapshot Board")
    st.caption("Job bubbles span across the slots they occupy (e.g. 4 hrs starting AM 1 covers AM 1 + AM 2).")
    render_board_color_key()

    target_date, _ = select_board_day("live_summary_day_idx")
    metrics = get_detailed_metrics(target_date)

    m_col1, m_col2, m_col3 = st.columns(3)
    m_col1.markdown(f"🟢 **AM Slots Remaining:** `{int(metrics['am_hours_left'] / 2)} Slots`")
    m_col2.markdown(f"🔵 **PM Slots Remaining:** `{int(metrics['pm_hours_left'] / 2)} Slots`")
    m_col3.markdown(f"📊 **Total Schedule Load:** `{metrics['total_jobs_booked']} / {metrics['max_jobs_allowed']} Calls`")
    st.markdown("---")

    if not bookings_df.empty:
        current_day_jobs = filter_board_jobs(bookings_df[bookings_df['date'] == target_date]).copy()
    else:
        current_day_jobs = pd.DataFrame()

    visible_techs = render_dispatch_crew_sections(target_date, current_day_jobs)

    if visible_techs:
        st.divider()
    render_warranty_board_section(target_date, current_day_jobs)

# -------------------------------------------------------------
# 6. PAST SCHEDULED JOBS (ST REPORT + LOCAL BOARD)
# -------------------------------------------------------------
elif view == "Past Scheduled Jobs":
    st.header("🗂️ Jobs Scheduled in the Past")
    st.caption(
        "Same idea as the ServiceTitan **Jobs scheduled in past** report — jobs that were scheduled "
        "but never completed. Prefer **Pull from ServiceTitan API**; CSV import is still available as a backup."
    )

    board_past = get_board_past_incomplete_jobs(bookings_df)
    imported_past = load_past_scheduled_jobs()

    m1, m2, m3 = st.columns(3)
    m1.metric("Open on this board (past dates)", len(board_past))
    m2.metric("From ServiceTitan (open)", len(imported_past))
    m3.metric("ST API", "Ready" if st_api.is_configured() else "Not configured")

    st.markdown("---")
    st.subheader("🔌 Pull from ServiceTitan API")
    if not st_api.is_configured():
        st.warning(
            "Add your ServiceTitan credentials to **`.streamlit/secrets.toml`** "
            "(copy from `.streamlit/secrets.toml.example`). "
            "You need: `client_id`, `client_secret`, `app_key`, and `tenant_id`."
        )
        with st.expander("How to get API credentials"):
            st.markdown(
                """
1. Confirm your ServiceTitan plan includes **API access** (ask your ST admin / account rep if unsure).
2. In the [ServiceTitan Developer Portal](https://developer.servicetitan.io/), create or open your app → copy the **App Key**.
3. In ServiceTitan → **Settings → Integrations / API Application Access**, connect that app and copy the **Client ID**, **Client Secret**, and **Tenant ID**.
4. Paste them into `.streamlit/secrets.toml`, then restart Streamlit.
                """
            )
    else:
        lookback = st.slider("Look back how many days?", min_value=14, max_value=365, value=120, step=7)
        c_test, c_pull = st.columns(2)
        with c_test:
            if st.button("Test ST connection", use_container_width=True):
                try:
                    msg = st_api.test_connection()
                    st.success(msg)
                except st_api.ServiceTitanError as exc:
                    st.error(str(exc))
        with c_pull:
            pull_clicked = st.button("⬇️ Pull past scheduled jobs from ST", type="primary", use_container_width=True)
        if pull_clicked:
            try:
                with st.spinner("Talking to ServiceTitan…"):
                    rows = st_api.fetch_past_scheduled_jobs(lookback_days=lookback)
                if not rows:
                    st.warning("API returned no open past-scheduled jobs for that lookback window.")
                else:
                    conn = get_db_connection()
                    imported_at = datetime.now().strftime("%Y-%m-%d %H:%M")
                    for row in rows:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO Past_Scheduled_Jobs
                            (id, job_number, customer_name, scheduled_date, job_type, technician, status, notes, source, imported_at, resolved)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'st_api', ?, 0)
                            """,
                            (
                                row["id"], row["job_number"], row["customer_name"], row["scheduled_date"],
                                row["job_type"], row["technician"], row["status"], row["notes"], imported_at,
                            ),
                        )
                    conn.commit()
                    conn.close()
                    st.session_state.flash_success = f"Pulled {len(rows)} past-scheduled job(s) from ServiceTitan."
                    st.rerun()
            except st_api.ServiceTitanError as exc:
                st.error(str(exc))

    st.markdown("---")
    st.subheader("📥 Backup: Import ServiceTitan Report (CSV)")
    st.markdown(
        "If API access is not ready yet, export **Jobs scheduled in past** from ST as CSV and upload below. "
        "Common columns like Job #, Customer, Appointment/Scheduled Date, Job Type, Technician, and Status are auto-mapped."
    )
    uploaded = st.file_uploader("Upload ST report CSV", type=["csv"], key="past_jobs_csv_upload")
    if uploaded is not None:
        try:
            raw_csv = pd.read_csv(uploaded)
            preview = normalize_past_jobs_csv(raw_csv)
            st.write(f"Detected **{len(preview)}** row(s) from upload.")
            if preview.empty:
                st.warning("Could not map rows — check that the CSV has customer and/or job number columns.")
                with st.expander("Raw CSV columns found"):
                    st.write(list(raw_csv.columns))
            else:
                st.dataframe(preview.head(20), use_container_width=True, hide_index=True)
                if st.button("💾 Import these jobs into Past Scheduled list", type="primary"):
                    conn = get_db_connection()
                    imported_at = datetime.now().strftime("%Y-%m-%d %H:%M")
                    for _, row in preview.iterrows():
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO Past_Scheduled_Jobs
                            (id, job_number, customer_name, scheduled_date, job_type, technician, status, notes, source, imported_at, resolved)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'st_csv', ?, 0)
                            """,
                            (
                                row["id"], row["job_number"], row["customer_name"], row["scheduled_date"],
                                row["job_type"], row["technician"], row["status"], row["notes"], imported_at,
                            ),
                        )
                    conn.commit()
                    conn.close()
                    st.session_state.flash_success = f"Imported {len(preview)} job(s) from ServiceTitan CSV."
                    st.rerun()
        except Exception as exc:
            st.error(f"Could not read that CSV: {exc}")

    st.markdown("---")
    st.subheader("📋 From ServiceTitan (API or CSV)")
    if imported_past.empty:
        st.info("No ServiceTitan past-scheduled jobs loaded yet. Use **Pull from ServiceTitan API** or upload a CSV.")
    else:
        show_cols = ["job_number", "customer_name", "scheduled_date", "job_type", "technician", "status", "notes", "source", "imported_at"]
        available = [c for c in show_cols if c in imported_past.columns]
        st.dataframe(
            imported_past[available],
            column_config={
                "job_number": st.column_config.TextColumn("Job #"),
                "customer_name": st.column_config.TextColumn("Customer"),
                "scheduled_date": st.column_config.DateColumn("Scheduled Date", format="MM/DD/YYYY"),
                "job_type": st.column_config.TextColumn("Job Type"),
                "technician": st.column_config.TextColumn("Technician"),
                "status": st.column_config.TextColumn("Status"),
                "notes": st.column_config.TextColumn("Notes", width="large"),
                "source": st.column_config.TextColumn("Source"),
                "imported_at": st.column_config.TextColumn("Pulled / Imported At"),
            },
            use_container_width=True,
            hide_index=True,
        )
        resolve_options = {
            f"{row['customer_name']} · {row['job_number']} · {row['scheduled_date']}": row["id"]
            for _, row in imported_past.iterrows()
        }
        with st.form("resolve_past_import_form"):
            pick = st.selectbox("Mark imported job as resolved / handled:", options=list(resolve_options.keys()))
            if st.form_submit_button("✅ Mark Resolved"):
                conn = get_db_connection()
                conn.execute("UPDATE Past_Scheduled_Jobs SET resolved = 1 WHERE id = ?", (resolve_options[pick],))
                conn.commit()
                conn.close()
                st.session_state.flash_success = f"Marked resolved: {pick}"
                st.rerun()

    st.markdown("---")
    st.subheader("📌 From this board (past date, still open)")
    st.caption("Jobs on Beola scheduled before today with status Booked, Scheduled, or Dispatched.")
    if board_past.empty:
        st.success("No past incomplete jobs on this board.")
    else:
        board_editor = st.data_editor(
            board_past[[
                "id", "date", "customer_job", "technician", "status", "priority_level",
                "timeframe", "job_type", "notes",
            ]],
            column_config={
                "id": None,
                "date": st.column_config.DateColumn("Scheduled Date", format="MM/DD/YYYY"),
                "customer_job": st.column_config.TextColumn("Customer / Job"),
                "technician": st.column_config.SelectboxColumn(
                    "Technician", options=["Unassigned"] + tech_list
                ),
                "status": st.column_config.SelectboxColumn("Status", options=JOB_STATUSES),
                "priority_level": st.column_config.TextColumn("Priority", disabled=True),
                "timeframe": st.column_config.TextColumn("Slot", disabled=True),
                "job_type": st.column_config.TextColumn("Job Type", disabled=True),
                "notes": st.column_config.TextColumn("Notes", width="large"),
            },
            disabled=["customer_job", "priority_level", "timeframe", "job_type"],
            use_container_width=True,
            hide_index=True,
            key="board_past_incomplete_editor",
        )
        if st.button("💾 Save Board Past-Job Updates", type="primary"):
            conn = get_db_connection()
            for _, row in board_editor.iterrows():
                conn.execute(
                    "UPDATE Booking_Log SET technician = ?, status = ?, notes = ? WHERE id = ?",
                    (row["technician"], row["status"], row["notes"], str(row["id"])),
                )
            conn.commit()
            conn.close()
            st.session_state.flash_success = "Past board jobs updated."
            st.rerun()
