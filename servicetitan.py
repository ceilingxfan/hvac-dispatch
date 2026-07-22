"""ServiceTitan API client for Beola the 3 Day Board.

Credentials (any of these sources work):
  1. .streamlit/secrets.toml under [servicetitan]
  2. Environment variables: ST_CLIENT_ID, ST_CLIENT_SECRET, ST_APP_KEY, ST_TENANT_ID

Optional: ST_ENVIRONMENT = production | integration
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests

try:
    import streamlit as st
except ImportError:  # pragma: no cover
    st = None

AUTH_URLS = {
    "production": "https://auth.servicetitan.io/connect/token",
    "integration": "https://auth-integration.servicetitan.io/connect/token",
}
API_BASES = {
    "production": "https://api.servicetitan.io",
    "integration": "https://api-integration.servicetitan.io",
}

# Appointment statuses that mean the visit is still outstanding
OPEN_APPOINTMENT_STATUSES = ("Scheduled", "Dispatched", "Working", "Hold")
# Job statuses that mean the job was never finished
OPEN_JOB_STATUSES = ("Scheduled", "InProgress", "Hold", "Dispatched")


class ServiceTitanError(Exception):
    """Raised when ST credentials are missing or an API call fails."""


def _secrets_get(key: str, default: str = "") -> str:
    if st is None:
        return default
    try:
        block = st.secrets.get("servicetitan", {})
        if key in block:
            return str(block[key]).strip()
    except Exception:
        pass
    return default


def get_config() -> dict[str, str]:
    env = (
        _secrets_get("environment")
        or os.getenv("ST_ENVIRONMENT", "production")
    ).strip().lower()
    if env not in ("production", "integration"):
        env = "production"

    cfg = {
        "client_id": _secrets_get("client_id") or os.getenv("ST_CLIENT_ID", "").strip(),
        "client_secret": _secrets_get("client_secret") or os.getenv("ST_CLIENT_SECRET", "").strip(),
        "app_key": _secrets_get("app_key") or os.getenv("ST_APP_KEY", "").strip(),
        "tenant_id": _secrets_get("tenant_id") or os.getenv("ST_TENANT_ID", "").strip(),
        "environment": env,
    }
    return cfg


def is_configured() -> bool:
    cfg = get_config()
    return all(cfg.get(k) for k in ("client_id", "client_secret", "app_key", "tenant_id"))


def _require_config() -> dict[str, str]:
    cfg = get_config()
    missing = [k for k in ("client_id", "client_secret", "app_key", "tenant_id") if not cfg.get(k)]
    if missing:
        raise ServiceTitanError(
            "ServiceTitan is not configured. Add these under [servicetitan] in "
            f".streamlit/secrets.toml (or env vars): {', '.join(missing)}"
        )
    return cfg


def get_access_token(force_refresh: bool = False) -> str:
    """OAuth client-credentials token (cached in session_state when available)."""
    cfg = _require_config()
    cache_key = "st_access_token"
    exp_key = "st_access_token_expires"

    if st is not None and not force_refresh:
        token = st.session_state.get(cache_key)
        expires = st.session_state.get(exp_key)
        if token and expires and datetime.now(timezone.utc).timestamp() < expires:
            return token

    resp = requests.post(
        AUTH_URLS[cfg["environment"]],
        data={
            "grant_type": "client_credentials",
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise ServiceTitanError(f"Auth failed ({resp.status_code}): {resp.text[:400]}")

    payload = resp.json()
    token = payload.get("access_token")
    if not token:
        raise ServiceTitanError("Auth response did not include access_token.")

    # Refresh a minute early
    expires_in = int(payload.get("expires_in", 3600))
    if st is not None:
        st.session_state[cache_key] = token
        st.session_state[exp_key] = datetime.now(timezone.utc).timestamp() + max(60, expires_in - 60)
    return token


def api_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET a tenant-scoped ST API path like /jpm/v2/tenant/{tenant}/jobs."""
    cfg = _require_config()
    url = f"{API_BASES[cfg['environment']]}{path}"
    headers = {
        "Authorization": f"Bearer {get_access_token()}",
        "ST-App-Key": cfg["app_key"],
    }
    resp = requests.get(url, headers=headers, params=params or {}, timeout=60)
    if resp.status_code == 401:
        headers["Authorization"] = f"Bearer {get_access_token(force_refresh=True)}"
        resp = requests.get(url, headers=headers, params=params or {}, timeout=60)
    if resp.status_code >= 400:
        raise ServiceTitanError(f"API {path} failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


def _paginate(path: str, params: dict[str, Any], *, max_pages: int = 50) -> list[dict]:
    page = 1
    rows: list[dict] = []
    while page <= max_pages:
        page_params = {**params, "page": page, "pageSize": params.get("pageSize", 100)}
        data = api_get(path, page_params)
        chunk = data.get("data") or []
        rows.extend(chunk)
        if not data.get("hasMore"):
            break
        page += 1
    return rows


def test_connection() -> str:
    """Lightweight connectivity check — returns a short success message or raises."""
    cfg = _require_config()
    get_access_token(force_refresh=True)
    tenant = cfg["tenant_id"]
    env = cfg["environment"]
    # Prefer technicians directory; fall back to a tiny jobs page if that path differs by account
    try:
        data = api_get(f"/settings/v2/tenant/{tenant}/technicians", {"page": 1, "pageSize": 1})
        total = data.get("totalCount")
        if total is None:
            return f"Connected to ServiceTitan ({env})."
        return f"Connected to ServiceTitan ({env}). Technician directory reachable ({total} techs)."
    except ServiceTitanError:
        api_get(f"/jpm/v2/tenant/{tenant}/jobs", {"page": 1, "pageSize": 1})
        return f"Connected to ServiceTitan ({env}). Jobs API reachable."


def _parse_iso_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _job_customer_name(job: dict) -> str:
    for key in ("customerName", "customer_name"):
        if job.get(key):
            return str(job[key])
    cust = job.get("customer") or {}
    if isinstance(cust, dict):
        name = cust.get("name") or cust.get("customerName")
        if name:
            return str(name)
    return ""


def _job_type_name(job: dict) -> str:
    for key in ("jobTypeName", "typeName", "summary"):
        if job.get(key):
            return str(job[key])
    jt = job.get("jobType") or {}
    if isinstance(jt, dict) and jt.get("name"):
        return str(jt["name"])
    return ""


def _appointment_start(job: dict) -> date | None:
    """Best-effort scheduled date from job payload / nested appointments."""
    for key in ("firstAppointmentStart", "appointmentStart", "scheduledOn", "start"):
        parsed = _parse_iso_date(job.get(key))
        if parsed:
            return parsed

    appointments = job.get("appointments") or []
    if isinstance(appointments, list):
        starts = []
        for appt in appointments:
            if not isinstance(appt, dict):
                continue
            for key in ("start", "arrivalWindowStart", "scheduledStart"):
                parsed = _parse_iso_date(appt.get(key))
                if parsed:
                    starts.append(parsed)
                    break
        if starts:
            return min(starts)
    return None


def fetch_past_scheduled_jobs(*, lookback_days: int = 120, as_of: date | None = None) -> list[dict]:
    """
    Pull jobs that look like the ST "Jobs scheduled in past" report:
    open/incomplete jobs whose appointment start is before today.
    """
    cfg = _require_config()
    as_of = as_of or date.today()
    starts_before = datetime.combine(as_of, datetime.min.time()).replace(tzinfo=timezone.utc)
    starts_after = starts_before - timedelta(days=max(1, lookback_days))

    tenant = cfg["tenant_id"]
    jobs_path = f"/jpm/v2/tenant/{tenant}/jobs"

    # Prefer jobs filtered by appointment window + open statuses
    jobs = _paginate(
        jobs_path,
        {
            "jobStatus": ",".join(OPEN_JOB_STATUSES),
            "appointmentStartsBefore": starts_before.isoformat().replace("+00:00", "Z"),
            "appointmentStartsOnOrAfter": starts_after.isoformat().replace("+00:00", "Z"),
            "pageSize": 100,
            "includeTotal": "true",
        },
    )

    # Fallback: appointments still open that started in the past, then hydrate jobs
    if not jobs:
        appt_path = f"/jpm/v2/tenant/{tenant}/appointments"
        appointments = []
        for status in OPEN_APPOINTMENT_STATUSES:
            appointments.extend(
                _paginate(
                    appt_path,
                    {
                        "status": status,
                        "startsBefore": starts_before.isoformat().replace("+00:00", "Z"),
                        "startsOnOrAfter": starts_after.isoformat().replace("+00:00", "Z"),
                        "pageSize": 100,
                    },
                    max_pages=20,
                )
            )
        job_ids = sorted({a.get("jobId") for a in appointments if a.get("jobId")})
        for job_id in job_ids:
            try:
                job = api_get(f"/jpm/v2/tenant/{tenant}/jobs/{job_id}")
                if isinstance(job, dict):
                    # Some responses wrap in "data"
                    jobs.append(job.get("data") if isinstance(job.get("data"), dict) else job)
            except ServiceTitanError:
                continue

        # Attach appointment start onto job for date display
        start_by_job: dict[Any, date] = {}
        for appt in appointments:
            jid = appt.get("jobId")
            start = _parse_iso_date(appt.get("start"))
            if jid and start:
                prev = start_by_job.get(jid)
                if prev is None or start < prev:
                    start_by_job[jid] = start
        for job in jobs:
            jid = job.get("id")
            if jid in start_by_job and not _appointment_start(job):
                job["_appointment_start"] = start_by_job[jid].isoformat()

    # Enrich missing customer names (batch of unique IDs, capped)
    customer_ids = []
    for job in jobs:
        if not _job_customer_name(job) and job.get("customerId"):
            customer_ids.append(job["customerId"])
    customer_names: dict[Any, str] = {}
    for cid in list(dict.fromkeys(customer_ids))[:80]:
        try:
            cust = api_get(f"/crm/v2/tenant/{tenant}/customers/{cid}")
            if isinstance(cust.get("data"), dict):
                cust = cust["data"]
            name = cust.get("name") or cust.get("customerName") or ""
            if name:
                customer_names[cid] = str(name)
        except ServiceTitanError:
            continue

    rows: list[dict] = []
    for job in jobs:
        status = str(job.get("jobStatus") or job.get("status") or "")
        if status and status not in OPEN_JOB_STATUSES:
            # Skip completed/canceled if they slipped through
            if status.lower() in {"completed", "canceled", "cancelled"}:
                continue

        scheduled = _appointment_start(job)
        if job.get("_appointment_start") and not scheduled:
            scheduled = _parse_iso_date(job["_appointment_start"])
        if scheduled is None or scheduled >= as_of:
            # Without a past appointment date, keep if ST already filtered — else skip
            if scheduled is not None and scheduled >= as_of:
                continue

        job_id = job.get("id")
        job_number = str(job.get("jobNumber") or job.get("number") or job_id or "")
        customer = _job_customer_name(job) or customer_names.get(job.get("customerId"), "")
        if not customer and job.get("customerId"):
            customer = f"Customer #{job['customerId']}"

        tech = ""
        for key in ("technicianName", "primaryTechnician", "assignedTechnician"):
            val = job.get(key)
            if isinstance(val, dict):
                tech = str(val.get("name") or "")
            elif val:
                tech = str(val)
            if tech:
                break

        notes = str(job.get("summary") or job.get("description") or "")
        row_id = f"st-api-{job_id or job_number}"
        rows.append({
            "id": row_id,
            "job_number": job_number,
            "customer_name": customer,
            "scheduled_date": scheduled.isoformat() if scheduled else "",
            "job_type": _job_type_name(job),
            "technician": tech,
            "status": status or "Scheduled",
            "notes": notes,
            "source": "st_api",
        })

    # De-dupe by id
    deduped = {r["id"]: r for r in rows}
    return list(deduped.values())
