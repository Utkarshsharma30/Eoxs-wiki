"""Employee directory: the one write-capable corner of this MCP server.

Deliberately NOT part of the wiki/tiered-content system -- employees has no
access_tier column, isn't cited by wiki pages, and is never touched by
wiki_ingestion. See schema/025_employees.sql for the full rationale.

Every mutating function below takes a `changed_by` kwarg, bound per-server-
instance by build_server() exactly like `clearance` is for the tier-filtered
tools (see server.py) -- never part of a tool's inputSchema, so nothing a
caller sends can spoof who made a change. Only the `full` (Raj) and `hr`
(Isha) identities get these tools registered at all; see server.py's
EMPLOYEE_TOOLS / enable_employee_tools.

Audit logging follows ingest_log.py's documented philosophy ("a logging
failure must never mask an otherwise-successful run"): the employee write
itself is the operation that has to succeed; the employee_change_log insert
is best-effort, wrapped so a logging failure never raises past the caller.
"""
import json
import sys

from mcp.types import Tool

from mcp_server.db import query, query_one, execute

STATUS_FILTERS = {"active": "employees.status = 'active'", "inactive": "employees.status = 'inactive'", "all": "TRUE"}

EMPLOYEE_COLUMNS = "id, full_name, department, role_title, employment_type, official_email, manager, date_of_joining, date_of_leaving, status, notes, created_at, updated_at"


def _log_change(employee_id, changed_by, change_type, changes):
    try:
        execute(
            """INSERT INTO employee_change_log (employee_id, changed_by, change_type, changes)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (employee_id, changed_by, change_type, json.dumps(changes, default=str)),
        )
    except Exception as e:
        print(f"employee_change_log write failed (main operation already committed): {e}", file=sys.stderr)


def _status_clause(status):
    if status not in STATUS_FILTERS:
        status = "active"
    return STATUS_FILTERS[status]


def list_employees(status="active", department=""):
    """status: 'active' (default -- current headcount only) | 'inactive' (people
    who've left) | 'all'. department: exact match, or empty for all."""
    sql = f"SELECT {EMPLOYEE_COLUMNS} FROM employees WHERE {_status_clause(status)}"
    params = []
    if department:
        sql += " AND department ILIKE %s"
        params.append(department)
    sql += " ORDER BY full_name"
    return query(sql, params)


def search_employees(query_text, status="active"):
    """Fuzzy match against name, department, role, manager, or email."""
    sql = f"""
        SELECT {EMPLOYEE_COLUMNS} FROM employees
        WHERE {_status_clause(status)}
          AND (full_name ILIKE %s OR department ILIKE %s OR role_title ILIKE %s
               OR manager ILIKE %s OR official_email ILIKE %s)
        ORDER BY full_name LIMIT 50
    """
    like = f"%{query_text}%"
    return query(sql, (like, like, like, like, like))


def get_employee(identifier):
    """identifier: numeric id, or a name/email substring (first match wins --
    use search_employees first if you're not sure which record you want)."""
    if str(identifier).isdigit():
        row = query_one(f"SELECT {EMPLOYEE_COLUMNS} FROM employees WHERE id = %s", (int(identifier),))
    else:
        row = query_one(
            f"SELECT {EMPLOYEE_COLUMNS} FROM employees WHERE full_name ILIKE %s OR official_email ILIKE %s ORDER BY status, full_name LIMIT 1",
            (f"%{identifier}%", f"%{identifier}%"),
        )
    if not row:
        return {"error": f"no employee matching '{identifier}'"}
    row["change_history"] = query(
        "SELECT changed_by, change_type, changes, occurred_at FROM employee_change_log "
        "WHERE employee_id = %s ORDER BY occurred_at DESC",
        (row["id"],),
    )
    return row


def create_employee(full_name, department="", role_title="", employment_type="",
                     official_email="", manager="", date_of_joining="", notes="", changed_by=""):
    row = execute(
        f"""INSERT INTO employees (full_name, department, role_title, employment_type,
                                    official_email, manager, date_of_joining, notes)
            VALUES (%s, %s, %s, %s, %s, %s, NULLIF(%s, '')::date, %s)
            RETURNING {EMPLOYEE_COLUMNS}""",
        (full_name, department or None, role_title or None, employment_type or None,
         official_email or None, manager or None, date_of_joining, notes or None),
    )
    _log_change(row["id"], changed_by, "created", {
        "full_name": full_name, "department": department, "role_title": role_title,
        "employment_type": employment_type, "official_email": official_email,
        "manager": manager, "date_of_joining": date_of_joining, "notes": notes,
    })
    return row


def update_employee(employee_id, full_name=None, department=None, role_title=None,
                     employment_type=None, official_email=None, manager=None,
                     date_of_joining=None, notes=None, changed_by=""):
    """Only fields explicitly passed (non-None) are changed. To clear a field,
    pass an empty string, not omit it."""
    before = query_one(f"SELECT {EMPLOYEE_COLUMNS} FROM employees WHERE id = %s", (employee_id,))
    if not before:
        return {"error": f"no employee with id={employee_id}"}

    updates = {}
    for field, value in (
        ("full_name", full_name), ("department", department), ("role_title", role_title),
        ("employment_type", employment_type), ("official_email", official_email),
        ("manager", manager), ("date_of_joining", date_of_joining), ("notes", notes),
    ):
        if value is None:
            continue
        normalized = (value or None) if field == "date_of_joining" else value
        if str(before.get(field) or "") != (normalized or ""):
            updates[field] = normalized
    if not updates:
        return {"note": "no changes -- every provided value already matched", **before}

    set_clause = ", ".join(
        f"{f} = %s::date" if f == "date_of_joining" else f"{f} = %s" for f in updates
    ) + ", updated_at = now()"
    params = list(updates.values()) + [employee_id]
    row = execute(
        f"UPDATE employees SET {set_clause} WHERE id = %s RETURNING {EMPLOYEE_COLUMNS}", params,
    )
    _log_change(employee_id, changed_by, "updated", {
        f: {"old": before.get(f), "new": v} for f, v in updates.items()
    })
    return row


def deactivate_employee(employee_id, date_of_leaving="", changed_by=""):
    """Soft delete -- sets status='inactive'. The row and its full history
    stay queryable forever via list_employees(status='inactive'/'all')."""
    before = query_one("SELECT status, date_of_leaving FROM employees WHERE id = %s", (employee_id,))
    if not before:
        return {"error": f"no employee with id={employee_id}"}
    row = execute(
        f"""UPDATE employees SET status = 'inactive', date_of_leaving = COALESCE(NULLIF(%s, '')::date, date_of_leaving, CURRENT_DATE),
               updated_at = now() WHERE id = %s RETURNING {EMPLOYEE_COLUMNS}""",
        (date_of_leaving, employee_id),
    )
    _log_change(employee_id, changed_by, "deactivated", {
        "status": {"old": before["status"], "new": "inactive"},
        "date_of_leaving": {"old": str(before["date_of_leaving"]), "new": str(row["date_of_leaving"])},
    })
    return row


def reactivate_employee(employee_id, changed_by=""):
    """For someone who left and rejoined -- sets status='active' again, clears
    date_of_leaving. The old departure is still visible in change_history."""
    before = query_one("SELECT status FROM employees WHERE id = %s", (employee_id,))
    if not before:
        return {"error": f"no employee with id={employee_id}"}
    row = execute(
        f"UPDATE employees SET status = 'active', date_of_leaving = NULL, updated_at = now() "
        f"WHERE id = %s RETURNING {EMPLOYEE_COLUMNS}",
        (employee_id,),
    )
    _log_change(employee_id, changed_by, "reactivated", {"status": {"old": before["status"], "new": "active"}})
    return row


EMPLOYEE_TOOLS = {
    "list_employees": list_employees,
    "search_employees": search_employees,
    "get_employee": get_employee,
    "create_employee": create_employee,
    "update_employee": update_employee,
    "deactivate_employee": deactivate_employee,
    "reactivate_employee": reactivate_employee,
}

# Tools whose changed_by kwarg must be bound at server-construction time --
# same treatment as TIER_FILTERED_TOOLS' `clearance` kwarg in server.py.
EMPLOYEE_WRITE_TOOLS = {"create_employee", "update_employee", "deactivate_employee", "reactivate_employee"}


def tool_defs():
    return [
        Tool(
            name="list_employees",
            description="List employees. status: 'active' (default -- current headcount only) | "
                        "'inactive' (people who've left) | 'all'. department: exact match, or empty for all.",
            inputSchema={"type": "object", "properties": {
                "status": {"type": "string", "default": "active"}, "department": {"type": "string", "default": ""}}},
        ),
        Tool(
            name="search_employees",
            description="Fuzzy-search employees by name, department, role, manager, or email. "
                        "status: 'active' (default) | 'inactive' | 'all'.",
            inputSchema={"type": "object", "properties": {
                "query_text": {"type": "string"}, "status": {"type": "string", "default": "active"}}, "required": ["query_text"]},
        ),
        Tool(
            name="get_employee",
            description="Return one employee record (all fields) plus its full change_history, "
                        "by numeric id or a name/email substring.",
            inputSchema={"type": "object", "properties": {"identifier": {"type": "string"}}, "required": ["identifier"]},
        ),
        Tool(
            name="create_employee",
            description="Add a new employee. Only full_name is required -- everything else can be filled "
                        "in later via update_employee. date_of_joining: 'YYYY-MM-DD' or empty.",
            inputSchema={"type": "object", "properties": {
                "full_name": {"type": "string"}, "department": {"type": "string", "default": ""},
                "role_title": {"type": "string", "default": ""}, "employment_type": {"type": "string", "default": ""},
                "official_email": {"type": "string", "default": ""}, "manager": {"type": "string", "default": ""},
                "date_of_joining": {"type": "string", "default": ""}, "notes": {"type": "string", "default": ""},
            }, "required": ["full_name"]},
        ),
        Tool(
            name="update_employee",
            description="Update one or more fields on an existing employee, by id. Only pass the fields "
                        "you want changed -- omitted fields are left alone. Does not change status (see "
                        "deactivate_employee/reactivate_employee for that).",
            inputSchema={"type": "object", "properties": {
                "employee_id": {"type": "integer"}, "full_name": {"type": "string"}, "department": {"type": "string"},
                "role_title": {"type": "string"}, "employment_type": {"type": "string"}, "official_email": {"type": "string"},
                "manager": {"type": "string"}, "date_of_joining": {"type": "string"}, "notes": {"type": "string"},
            }, "required": ["employee_id"]},
        ),
        Tool(
            name="deactivate_employee",
            description="Mark an employee inactive (soft delete -- they left the company). The record and its "
                        "full history are preserved and stay reachable via status='inactive'/'all'. "
                        "date_of_leaving: 'YYYY-MM-DD', or omit to default to today.",
            inputSchema={"type": "object", "properties": {
                "employee_id": {"type": "integer"}, "date_of_leaving": {"type": "string", "default": ""}}, "required": ["employee_id"]},
        ),
        Tool(
            name="reactivate_employee",
            description="Mark a previously-inactive employee active again (they rejoined). Clears "
                        "date_of_leaving; the prior departure stays visible in change_history.",
            inputSchema={"type": "object", "properties": {"employee_id": {"type": "integer"}}, "required": ["employee_id"]},
        ),
    ]
