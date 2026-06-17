import streamlit as st
import pandas as pd
import sqlite3
from datetime import datetime, timedelta

# Page Setup
st.set_page_config(layout="wide", page_title="HVAC 3-Day Capacity Board")

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
            status TEXT
        )
    """)
    
    # Safe schema migration for older DB files to add the new is_standby column
    try:
        cursor.execute("ALTER TABLE Booking_Log ADD COLUMN is_standby INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
        
    conn.commit()
    conn.close()

init_db()

# Global Configurations
tech_list = ["Eddie Glenn", "Derek Moore", "Ken Wilburn", "Austin Baker"]
job_classes = ["Standard", "High Value Opp", "Member", "Maintenance-to-Service", "Recall/Warranty"]
precollect_opts = ["Not Applicable", "Yes", "No", "AR"]
time_slots = ["AM 1", "AM 2", "PM 1", "PM 2"]

# -------------------------------------------------------------
# AUTOMATED CALENDAR LOGIC (CONTINUOUS 3-DAYS)
# -------------------------------------------------------------
def get_3_continuous_days(start_date):
    """Returns a continuous 3-day window from today, including weekends."""
    return [start_date, start_date + timedelta(days=1), start_date + timedelta(days=2)]

today = datetime.today().date()
day1, day2, day3 = get_3_continuous_days(today)

st.title("🛠️ HVAC 3-Day Live Capacity Dispatch")

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
    return df

bookings_df = load_bookings()
roster_df = load_roster()

# -------------------------------------------------------------
# DYNAMIC METRIC, HOUR, & CAP CALCULATOR
# -------------------------------------------------------------
def get_detailed_metrics(target_date):
    day_name = target_date.strftime("%A")

    if day_name in ["Monday", "Tuesday"]:
        base_tech_count = 5
        day_caps = {"Urgent": 4, "High": 9, "Normal": 5, "Low": 3}
    elif day_name in ["Wednesday", "Thursday", "Friday"]:
        base_tech_count = 4
        day_caps = {"Urgent": 3, "High": 7, "Normal": 4, "Low": 2}
    else:  # Saturday and Sunday
        base_tech_count = 2
        day_caps = {"Urgent": 1, "High": 2, "Normal": 2, "Low": 0}

    total_am_hours = base_tech_count * 4
    total_pm_hours = base_tech_count * 4
    max_jobs_allowed = base_tech_count * 4
    
    if not roster_df.empty:
        day_roster = roster_df[roster_df['date'] == target_date]
        for _, change in day_roster.iterrows():
            status = change['avail_type']
            
            if status in ["PTO Full", "Call Out"]:
                total_am_hours -= 4
                total_pm_hours -= 4
                max_jobs_allowed -= 4
            elif "AM off" in status:
                total_am_hours -= 4
                max_jobs_allowed -= 2
            elif "PM off" in status:
                total_pm_hours -= 4
                max_jobs_allowed -= 2
            elif status == "Training/Meeting":
                total_am_hours -= 2
                total_pm_hours -= 2

    sold_am, sold_pm = 0, 0
    total_jobs_booked = 0
    priority_counts = {"Urgent": 0, "High": 0, "Normal": 0, "Low": 0}
    
    if not bookings_df.empty:
        active_jobs = bookings_df[(bookings_df['date'] == target_date) & (bookings_df['status'] != "Cancelled")]
        
        for _, job in active_jobs.iterrows():
            total_jobs_booked += 1
            if job['priority_level'] in priority_counts and not job['is_recall']:
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
        "max_jobs_allowed": max(0, max_jobs_allowed)
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

# -------------------------------------------------------------
# NAVIGATION SIDEBAR HUB
# -------------------------------------------------------------
view = st.sidebar.radio("Navigate Department Hub:", ["CSR Booking & Standby Hub", "Parts & Repairs Hub", "Dispatch Operational Desk", "Live Summary Board"])
st.sidebar.markdown("---")

# -------------------------------------------------------------
# 1. CSR BOOKING & STANDBY HUB
# -------------------------------------------------------------
if view == "CSR Booking & Standby Hub":
    st.header("📞 CSR Booking & Move-Up Standby Hub")
    
    tab_booking, tab_standby = st.tabs(["📋 Customer Service Booking Portal", "⚡ Move-Up Standby Queue"])
    
    with tab_booking:
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
                    for p_tier, cap in metrics["day_caps"].items():
                        current_count = metrics["priority_counts"][p_tier]
                        status_text = "🔴 SOLD OUT" if current_count >= cap else f"{current_count} / {cap} booked"
                        st.write(f"**{p_tier}:** {status_text}")

        st.markdown("---")
        
        with st.expander("ℹ️ Priority Level Descriptions Legend"):
            st.markdown("""
            * **🚨 Urgent:** Vulnerable Home / No Heat or Cool
            * **⚡ High:** High Value Opportunity / System Down (Comfort Compromised)
            * **🟢 Normal:** Partial No Heat or Cool / Routine Service
            * **🔵 Low:** Minor Issues / Non-Urgent Maintenance
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

                # Checkbox is a simple inside-the-form widget — no state tricks needed.
                # When checked it pre-fills [TIME REQUEST] into the notes field so the
                # CSR knows to complete the detail there. Everything resets on clear_on_submit.
                req_time_check = st.checkbox("⏰ Special Time Constraint? (adds tag to notes)")

                slots_est = st.selectbox("Required Slots (1 Slot = 2 Hours)", [1, 2, 3, 4])
                hours_est = slots_est * 2
                
                pre_coll = st.selectbox("Precollection Flag", precollect_opts)
                is_rec = st.checkbox("Is this a Recall? (Bypasses hour capacities & priority limits ⚠️)")
                m_override = st.checkbox("🚨 Apply Manager Cap Override? (Bypasses urgency tier limits 🛠️)")
                add_to_standby = st.checkbox("Add to Move-Up Standby Queue? 📋")

                # Pre-populate [TIME REQUEST] tag at the front of notes when checkbox is on
                notes_default = "[TIME REQUEST: ] " if req_time_check else ""
                if req_time_check:
                    st.caption("⏰ Fill in the time detail after the colon in the notes box below.")
                notes_field = st.text_area("Internal Booking Instructions", value=notes_default)
                
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
                    
                    processed_notes = notes_field
                    # [TIME REQUEST: ] tag was already pre-filled into notes_field by the checkbox.
                    # Just prepend the manager override flag if needed.
                    if m_override:
                        processed_notes = f"[MANAGER OVERRIDE APPLIED] {processed_notes}"
                        
                    conn = get_db_connection()
                    conn.execute("""
                        INSERT INTO Booking_Log (id, timestamp, date, customer_job, priority_level, timeframe, hours_needed, job_type, precollection, technician, is_recall, status, notes, booked_by, is_standby)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (unique_id, datetime.now().strftime("%Y-%m-%d %H:%M"), str(final_date), cust_info, p_level, time_frame, hours_est, job_class, pre_coll, "Unassigned", 1 if is_rec else 0, "Booked", processed_notes, "CSR Desk", 1 if add_to_standby else 0))
                    
                    if add_to_standby:
                        standby_id = str(int(datetime.now().timestamp()) + 1)
                        standby_tags = []
                        if job_class == "Member": standby_tags.append("[MEMBER]")
                        if p_level == "Urgent": standby_tags.append("[URGENT]")
                        standby_tag_str = " ".join(standby_tags)
                        compiled_job_type = f"{job_class} Call {standby_tag_str}".strip()
                        
                        conn.execute("""
                            INSERT INTO Maintenance_Waitlist (id, timestamp, customer_name, phone, job_type, notes, status)
                            VALUES (?, ?, ?, ?, ?, ?, 'Active')
                        """, (standby_id, datetime.now().strftime("%Y-%m-%d %H:%M"), cust_info, "See Schedule", compiled_job_type, f"Auto-linked from booking entry. Notes: {processed_notes}"))
                    
                    conn.commit()
                    conn.close()
                    
                    success_msg = f"Call for '{cust_info}' successfully committed to the board!"
                    if add_to_standby:
                        success_msg += " (Also populated on Move-Up Standby Queue)"
                    st.session_state.flash_success = success_msg
                    st.rerun()

    with tab_standby:
        st.subheader("📋 Catchall Move-Up Standby Queue")
        
        waitlist_df = load_waitlist()
        if waitlist_df.empty:
            st.info("The standby move-up queue is currently clear.")
        else:
            # Sort: URGENT first, then by timestamp
            def standby_sort_key(row):
                urgent = 0 if "[URGENT]" in str(row.get("job_type", "")) else 1
                member = 0 if "[MEMBER]" in str(row.get("job_type", "")) else 1
                return (urgent, member, row.get("timestamp", ""))
            waitlist_sorted = waitlist_df.copy()
            waitlist_sorted["_sort"] = waitlist_sorted.apply(standby_sort_key, axis=1)
            waitlist_sorted = waitlist_sorted.sort_values("_sort").drop(columns=["_sort"])

            rows_html = ""
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
                phone = str(row.get("phone", "")) or "—"
                notes = str(row.get("notes", "")) or "—"
                row_bg = "#fff8f8" if is_urgent else "#f8faff" if is_member else "#ffffff"
                rows_html += f"""
                <tr style="background:{row_bg}; border-bottom:1px solid #e5e7eb;">
                    <td style="padding:10px 12px; font-weight:600; white-space:nowrap;">{row['customer_name']}</td>
                    <td style="padding:10px 12px;">{phone}</td>
                    <td style="padding:10px 12px;">{clean_type}<br>{badge}</td>
                    <td style="padding:10px 12px; font-size:12px; color:#555; max-width:220px;">{notes}</td>
                    <td style="padding:10px 12px; font-size:12px; color:#888; white-space:nowrap;">{ts}</td>
                </tr>"""

            st.html(f"""
                <div style="border:1px solid #e5e7eb; border-radius:8px; overflow:hidden; font-family:sans-serif; margin-bottom:12px;">
                    <table style="width:100%; border-collapse:collapse; font-size:13px;">
                        <thead>
                            <tr style="background:#1e3a5f; color:white;">
                                <th style="padding:10px 12px; text-align:left; font-weight:600;">Customer</th>
                                <th style="padding:10px 12px; text-align:left; font-weight:600;">Phone</th>
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
                    
        st.markdown("---")
        st.subheader("Add Standalone Request to Standby Queue")
        with st.form("waitlist_form", clear_on_submit=True):
            wc1, wc2 = st.columns(2)
            with wc1:
                w_name = st.text_input("Customer Name / Location")
                w_phone = st.text_input("Contact Phone Number")
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
                        INSERT INTO Maintenance_Waitlist (id, timestamp, customer_name, phone, job_type, notes, status)
                        VALUES (?, ?, ?, ?, ?, ?, 'Active')
                    """, (w_id, datetime.now().strftime("%Y-%m-%d %H:%M"), w_name, w_phone, final_w_type, w_notes))
                    conn.commit()
                    conn.close()
                    
                    st.session_state.flash_success = f"{w_name} successfully added to the Move-Up Standby Queue."
                    st.rerun()

# -------------------------------------------------------------
# 2. PARTS & REPAIRS HUB
# -------------------------------------------------------------
elif view == "Parts & Repairs Hub":
    st.header("📦 Parts Coordination & Special Repair Scheduler")
    
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
            
            p_assigned_tech = st.selectbox("Pre-Assign Repair Technician", ["Unassigned"] + tech_list)
            p_notes = st.text_area("Parts / PO Details & Requirements")
            
        if st.form_submit_button("Lock In Repair Booking"):
            if not p_cust:
                st.error("Customer field is required.")
            else:
                final_date = day1 if "Day 1" in p_timeline else day2 if "Day 2" in p_timeline else day3 if "Day 3" in p_timeline else p_future_date
                unique_id = str(int(datetime.now().timestamp()))
                
                conn = get_db_connection()
                conn.execute("""
                    INSERT INTO Booking_Log (id, timestamp, date, customer_job, priority_level, timeframe, hours_needed, job_type, precollection, technician, is_recall, status, notes, booked_by, is_standby)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (unique_id, datetime.now().strftime("%Y-%m-%d %H:%M"), str(final_date), p_cust, "Normal", p_timeframe, p_hours, "Special Repair", "Not Applicable", p_assigned_tech, 0, "Scheduled", f"🔧 PARTS REPAIR: {p_notes}", "Parts Coordination", 0))
                conn.commit()
                conn.close()
                
                st.session_state.flash_success = f"Special Repair booked and assigned to {p_assigned_tech}."
                st.rerun()

# -------------------------------------------------------------
# 4. DISPATCH OPERATIONAL DESK
# -------------------------------------------------------------
elif view == "Dispatch Operational Desk":
    st.header("🎛️ Dispatcher Management Control")
    
    day1_metrics = get_detailed_metrics(day1)
    if day1_metrics["am_hours_left"] > 0 or day1_metrics["pm_hours_left"] > 0:
        if not bookings_df.empty:
            day2_urgents = bookings_df[(bookings_df["date"] == day2) & (bookings_df["priority_level"] == "Urgent") & (bookings_df["status"] != "Cancelled")]
            if not day2_urgents.empty:
                st.warning(f"⚠️ **PULL-FORWARD ALERT:** You have `{len(day2_urgents)}` Urgent call(s) sitting on Tomorrow's board ({day2.strftime('%m/%d')}), while Today ({day1.strftime('%m/%d')}) has remaining capacity!")
                with st.expander("View Tomorrow's Pull-Forward Candidates"):
                    for _, u_job in day2_urgents.iterrows():
                        st.markdown(f"* **{u_job['customer_job']}** ({u_job['timeframe']} window) | Notes: *{u_job['notes']}*")
                        
    t_blackout, t_routing = st.tabs(["Tech Attendance & Availability", "Active 3-Day Routing Matrix"])
    
    with t_blackout:
        st.subheader("🗓️ Crew Attendance & Capacity Adjustments")
        b_date = st.date_input("Select Target Roster Date", value=today, key="attendance_date_picker")
        day_exceptions = roster_df[roster_df['date'] == b_date] if not roster_df.empty else pd.DataFrame(columns=["date", "technician", "avail_type", "dispatcher_notes"])
        
        roster_status_list = []
        for tech in tech_list:
            if not day_exceptions.empty and 'technician' in day_exceptions.columns:
                tech_exc = day_exceptions[day_exceptions['technician'] == tech]
            else:
                tech_exc = pd.DataFrame()
            
            if not tech_exc.empty:
                current_status = tech_exc.iloc[0]['avail_type']
                notes = tech_exc.iloc[0]['dispatcher_notes']
                is_modified = True
                if current_status in ["PTO Full", "Call Out"]: am_disp, pm_disp = "0 hrs", "0 hrs"
                elif "AM off" in current_status: am_disp, pm_disp = "0 hrs", "4 hrs"
                elif "PM off" in current_status: am_disp, pm_disp = "4 hrs", "0 hrs"
                elif current_status == "Training/Meeting": am_disp, pm_disp = "2 hrs", "2 hrs"
                else: am_disp, pm_disp = "4 hrs", "4 hrs"
            else:
                current_status = "🟢 Active / Working"
                am_disp, pm_disp = "4 hrs", "4 hrs"
                notes = "Standard Schedule"
                is_modified = False
                
            roster_status_list.append({
                "Technician": tech,
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

    with t_routing:
        st.subheader("Visualized Crew Slot Schedule")
        target_view_day = st.selectbox("Select View Day", [day1, day2, day3], format_func=lambda x: x.strftime("%A, %b %d"))
        
        if not bookings_df.empty:
            day_jobs = bookings_df[(bookings_df['date'] == target_view_day) & (bookings_df['status'] != "Cancelled")].copy()
            
            def map_to_slot(tf):
                if tf == "AM" or tf == "All Day": return "AM 1"
                if tf == "PM": return "PM 1"
                if tf == "Open": return "AM 2"
                return tf

            day_jobs['visual_slot'] = day_jobs['timeframe'].apply(map_to_slot)
            
            for current_tech in ["Unassigned"] + tech_list:
                st.markdown(f"#### 👤 {current_tech}")
                tech_jobs = day_jobs[day_jobs['technician'] == current_tech]
                
                slot_cols = st.columns(4)
                for s_idx, slot in enumerate(time_slots):
                    with slot_cols[s_idx]:
                        st.markdown(f"**⏱️ {slot}**")
                        slot_specific_jobs = tech_jobs[tech_jobs['visual_slot'] == slot]
                        
                        if slot_specific_jobs.empty:
                            st.caption("— *Open* —")
                        else:
                            for _, job in slot_specific_jobs.iterrows():
                                # Show standby badge on dispatch board too
                                is_standby_flag = int(job['is_standby']) if 'is_standby' in job and pd.notna(job['is_standby']) else 0
                                standby_marker = " 📋 **STANDBY**" if is_standby_flag == 1 else ""

                                # Extract and display time constraint if present
                                req_time = extract_requested_time(job['notes'])

                                with st.container(border=True):
                                    label_prefix = "⚠️ [ALL DAY] " if job['timeframe'] == "All Day" else ""
                                    st.markdown(f"**{label_prefix}{job['customer_job']}**{standby_marker}")
                                    st.caption(f"Pri: {job['priority_level']} | Slots: {int(job['hours_needed']/2)} | ({job['status']})")
                                    if req_time:
                                        st.markdown(f"⏰ **Time Req:** `{req_time}`")
                st.markdown("")
                
            st.markdown("---")
            st.subheader("Quick Modification Grid")

            # --- Filter controls ---
            gf_col1, gf_col2, gf_col3 = st.columns(3)
            with gf_col1:
                grid_filter_date = st.selectbox(
                    "Filter by Date",
                    ["All 3 Days", day1.strftime("%a %b %d"), day2.strftime("%a %b %d"), day3.strftime("%a %b %d")],
                    key="grid_filter_date"
                )
            with gf_col2:
                grid_filter_tech = st.selectbox(
                    "Filter by Technician",
                    ["All Technicians", "Unassigned"] + tech_list,
                    key="grid_filter_tech"
                )
            with gf_col3:
                grid_filter_status = st.selectbox(
                    "Filter by Status",
                    ["Active Only", "All Statuses"],
                    key="grid_filter_status"
                )

            viewable_jobs = bookings_df[bookings_df['date'].isin([day1, day2, day3])].copy()

            # Apply filters
            if grid_filter_date != "All 3 Days":
                date_map = {
                    day1.strftime("%a %b %d"): day1,
                    day2.strftime("%a %b %d"): day2,
                    day3.strftime("%a %b %d"): day3
                }
                viewable_jobs = viewable_jobs[viewable_jobs['date'] == date_map[grid_filter_date]]

            if grid_filter_tech != "All Technicians":
                viewable_jobs = viewable_jobs[viewable_jobs['technician'] == grid_filter_tech]

            if grid_filter_status == "Active Only":
                viewable_jobs = viewable_jobs[viewable_jobs['status'] != "Cancelled"]

            # Sort: date → technician → timeframe slot order
            slot_order = {"AM 1": 0, "AM": 0, "AM 2": 1, "Open": 1, "PM 1": 2, "PM": 2, "PM 2": 3, "All Day": 0}
            viewable_jobs["_slot_sort"] = viewable_jobs["timeframe"].map(slot_order).fillna(9)
            tech_order = {t: i for i, t in enumerate(["Unassigned"] + tech_list)}
            viewable_jobs["_tech_sort"] = viewable_jobs["technician"].map(tech_order).fillna(99)
            viewable_jobs = viewable_jobs.sort_values(["date", "_tech_sort", "_slot_sort"]).drop(columns=["_slot_sort", "_tech_sort"])

            if viewable_jobs.empty:
                st.info("No jobs match the current filters.")
            else:
                display_cols = ["id", "date", "customer_job", "priority_level", "timeframe", "hours_needed", "technician", "status", "notes"]
                edited_grid = st.data_editor(
                    viewable_jobs[display_cols],
                    column_config={
                        "id": None,
                        "date": st.column_config.DateColumn("Date", format="MM/DD ddd"),
                        "customer_job": st.column_config.TextColumn("Customer / Job"),
                        "priority_level": st.column_config.SelectboxColumn("Priority", options=["Urgent", "High", "Normal", "Low"]),
                        "timeframe": st.column_config.SelectboxColumn("Time Slot", options=time_slots + ["AM", "PM", "Open", "All Day"]),
                        "hours_needed": st.column_config.NumberColumn("Hrs", min_value=1, max_value=8, step=1),
                        "technician": st.column_config.SelectboxColumn("Technician", options=["Unassigned"] + tech_list),
                        "status": st.column_config.SelectboxColumn("Status", options=["Booked", "Scheduled", "Dispatched", "Completed", "Cancelled"]),
                        "notes": st.column_config.TextColumn("Notes", width="large"),
                    },
                    disabled=["customer_job", "hours_needed"],
                    use_container_width=True,
                    hide_index=True,
                    key="dispatcher_live_grid"
                )
                
                if st.button("💾 Save Grid Adjustments"):
                    conn = get_db_connection()
                    for _, row in edited_grid.iterrows():
                        conn.execute(
                            "UPDATE Booking_Log SET technician = ?, status = ?, notes = ?, date = ?, timeframe = ?, priority_level = ? WHERE id = ?",
                            (row['technician'], row['status'], row['notes'], str(row['date']), row['timeframe'], row['priority_level'], str(row['id']))
                        )
                    conn.commit()
                    conn.close()
                    st.session_state.flash_success = "Routing grid updates synced to database."
                    st.rerun()
        else:
            st.info("No jobs to display.")

# -------------------------------------------------------------
# 5. LIVE SUMMARY BOARD (HUDDLE SNAPSHOT) - FULL BACKGROUND COLORS
# -------------------------------------------------------------
elif view == "Live Summary Board":
    st.subheader("🦅 Master 3-Day Huddle Snapshot Board")
    
    t1, t2, t3 = st.tabs([
        f"📅 TODAY ({day1.strftime('%A, %b %d')})",
        f"📆 TOMORROW ({day2.strftime('%A, %b %d')})",
        f"🗓️ {day3.strftime('%A, %b %d')}"
    ])
    
    def map_to_slot(tf):
        if tf == "AM" or tf == "All Day": return "AM 1"
        if tf == "PM": return "PM 1"
        if tf == "Open": return "AM 2"
        return tf

    target_days = [
        {"tab": t1, "date": day1},
        {"tab": t2, "date": day2},
        {"tab": t3, "date": day3}
    ]
    
    for day_obj in target_days:
        with day_obj["tab"]:
            metrics = get_detailed_metrics(day_obj["date"])
            
            m_col1, m_col2, m_col3 = st.columns(3)
            m_col1.markdown(f"🟢 **AM Slots Remaining:** `{int(metrics['am_hours_left'] / 2)} Slots`")
            m_col2.markdown(f"🔵 **PM Slots Remaining:** `{int(metrics['pm_hours_left'] / 2)} Slots`")
            m_col3.markdown(f"📊 **Total Schedule Load:** `{metrics['total_jobs_booked']} / {metrics['max_jobs_allowed']} Calls`")
            st.markdown("---")
            
            if not bookings_df.empty:
                current_day_jobs = bookings_df[(bookings_df['date'] == day_obj["date"]) & (bookings_df['status'] != "Cancelled")].copy()
            else:
                current_day_jobs = pd.DataFrame()
                
            if current_day_jobs.empty:
                st.info("No jobs scheduled for this date.")
            else:
                current_day_jobs['visual_slot'] = current_day_jobs['timeframe'].apply(map_to_slot)
                
                all_active_techs = ["Unassigned"] + tech_list
                for current_tech in all_active_techs:
                    tech_jobs = current_day_jobs[current_day_jobs['technician'] == current_tech]
                    
                    if tech_jobs.empty:
                        continue
                        
                    st.markdown(f"#### 👤 {current_tech}")
                    
                    grid_cols = st.columns(4)
                    for idx, slot in enumerate(time_slots):
                        with grid_cols[idx]:
                            st.markdown(f"**⏱️ {slot}**")
                            slot_jobs = tech_jobs[tech_jobs['visual_slot'] == slot]
                            
                            if slot_jobs.empty:
                                st.caption("—")
                            else:
                                for _, job in slot_jobs.iterrows():
                                    p_level = job['priority_level']
                                    j_type = job['job_type']
                                    recall_tag = " ⚠️ RECALL" if job['is_recall'] else ""
                                    
                                    # Hardened standby flag cast from SQLite integer
                                    is_standby_flag = int(job['is_standby']) if 'is_standby' in job and pd.notna(job['is_standby']) else 0
                                    standby_badge = (
                                        "<span style='background-color: rgba(255,255,255,0.25); "
                                        "padding: 2px 6px; border-radius: 4px; font-size: 10px; "
                                        "margin-left: 6px; font-weight: bold; letter-spacing: 0.3px;'>"
                                        "📋 STANDBY</span>"
                                    ) if is_standby_flag == 1 else ""

                                    # Extract requested time constraint from notes for display
                                    req_time = extract_requested_time(job['notes'])
                                    req_time_html = (
                                        f"<div style='margin-top:5px; font-size:11px; "
                                        f"background-color: rgba(0,0,0,0.2); border-radius:4px; "
                                        f"padding: 3px 6px; display:inline-block;'>"
                                        f"⏰ <strong>Time Req:</strong> {req_time}</div>"
                                    ) if req_time else ""
                                    
                                    # Anti-Crash String Sanitization for raw internal HTML containers
                                    safe_customer_job = str(job['customer_job']).replace('"', '&quot;').replace("'", "&#39;")
                                    safe_notes_raw = str(job['notes']).replace('"', '&quot;').replace("'", "&#39;") if job['notes'] else ""
                                    # Strip the [Requested Time: ...] tag from the notes display since we show it separately
                                    import re
                                    safe_notes_display = re.sub(r'\[Requested Time:[^\]]+\]\s*', '', safe_notes_raw).strip()
                                    
                                    if p_level == "Urgent":
                                        bg_color = "#CE2029"  # Crimson Red
                                        title_label = f"🚨 URGENT{recall_tag}"
                                    elif p_level == "High":
                                        bg_color = "#D97706"  # Dark Amber
                                        title_label = f"⚡ HIGH VALUE{recall_tag}"
                                    elif j_type == "Special Repair" or job['timeframe'] == "All Day":
                                        bg_color = "#6B21A8"  # Deep Purple
                                        title_label = f"🔧 ALL DAY REPAIR{recall_tag}"
                                    else:
                                        bg_color = "#15803D"  # Field Green
                                        title_label = f"🟢 STANDARD{recall_tag}"
                                        
                                    notes_str = (
                                        f"<div style='margin-top:4px; font-size:11px; opacity:0.95;'>"
                                        f"✏️ {safe_notes_display}</div>"
                                    ) if safe_notes_display else ""
                                    
                                    st.html(f"""
                                        <div style="
                                            background-color: {bg_color}; 
                                            color: white; 
                                            padding: 10px; 
                                            border-radius: 6px; 
                                            margin-bottom: 8px;
                                            box-shadow: 0 1px 3px rgba(0,0,0,0.2);
                                            font-family: sans-serif;
                                        ">
                                            <div style="font-weight: bold; font-size: 14px; margin-bottom: 2px;">{safe_customer_job}{standby_badge}</div>
                                            <div style="font-size: 11px; font-weight: 600; opacity: 0.9;">{title_label} | {int(job['hours_needed']/2)} slot(s)</div>
                                            <div style="font-size: 11px; opacity: 0.85;">Status: {job['status']}</div>
                                            {req_time_html}
                                            {notes_str}
                                        </div>
                                    """)
                    st.markdown("")