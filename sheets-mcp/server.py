#!/usr/bin/env python3
"""Claude Sheets — a local MCP server that lets Claude Code read and edit cells in
Htet's Google Sheets.

Why this exists: the Google Drive connector only works on whole files, and Google's own
Sheets MCP server is gated behind the Workspace Developer Preview, which a personal Gmail
can't join (Hermes hit the same wall on the Drive MCP server, 2026-10-01). The ordinary
Sheets API works fine on personal Gmail, so this server speaks MCP to Claude Code over
stdio and calls the Sheets API v4 underneath.

What it can do, and nothing else:
  list_tabs, read_range           read any sheet innyarr@gmail.com can open
  write_values, write_formulas,   write to sheets on allow-list.json only, max 500 cells,
  append_rows                     never clears a cell, logs before/after for every write

No delete, clear, format, share, insert or new-tab code exists here. The OAuth token only
carries https://www.googleapis.com/auth/spreadsheets.

Standard library only, Python 3.9+, so it runs on the Mac's built-in python3.
PRD: Projects/Claude Code/PRD - Sheets write tool (2026-10-04).md
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.0.0"
HERE = os.path.dirname(os.path.abspath(__file__))
ALLOW_LIST_FILE = os.environ.get("CLAUDE_SHEETS_ALLOW_LIST", os.path.join(HERE, "allow-list.json"))
LOG_DIR = os.environ.get("CLAUDE_SHEETS_LOG_DIR", os.path.join(HERE, "write-log"))
TOKEN_FILE = os.environ.get(
    "CLAUDE_SHEETS_TOKEN", os.path.expanduser("~/.config/claude-sheets/token.json")
)

API = "https://sheets.googleapis.com/v4/spreadsheets"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/spreadsheets"

MAX_CELLS_PER_WRITE = 500
MAX_READ_CHARS = 40_000
SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


class ToolError(Exception):
    """A refusal or failure the model should read as plain text, not a traceback."""


# --------------------------------------------------------------------------- auth + HTTP

_access_token = None
_access_expiry = 0.0


def _load_token_file() -> dict:
    try:
        with open(TOKEN_FILE, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise ToolError(
            f"Not signed in: {TOKEN_FILE} doesn't exist. Htet runs login.py once (setup steps, part 8)."
        ) from None


def _access() -> str:
    global _access_token, _access_expiry
    if _access_token and time.time() < _access_expiry - 60:
        return _access_token
    stored = _load_token_file()
    body = urllib.parse.urlencode({
        "client_id": stored["client_id"],
        "client_secret": stored["client_secret"],
        "refresh_token": stored["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    request = urllib.request.Request(
        TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        if "invalid_grant" in detail:
            raise ToolError(
                "Google refused the saved sign-in (expired or revoked). Htet runs login.py again."
            ) from None
        raise ToolError(f"Token refresh failed: HTTP {exc.code} {detail}") from None
    _access_token = payload["access_token"]
    _access_expiry = time.time() + float(payload.get("expires_in", 3600))
    return _access_token


def _http(method: str, url: str, body: dict | None = None) -> dict:
    """One Sheets API call. Tests replace this function with a fake."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": "Bearer " + _access()}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        try:
            detail = json.loads(detail)["error"]["message"]
        except Exception:
            pass
        raise ToolError(f"Sheets API {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise ToolError(f"Couldn't reach Google: {exc.reason}") from None


def _values_url(spreadsheet_id: str, a1: str, params: dict) -> str:
    return (
        f"{API}/{spreadsheet_id}/values/{urllib.parse.quote(a1, safe='')}?"
        + urllib.parse.urlencode(params)
    )


# --------------------------------------------------------------------------- parsing

_ID_IN_URL = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
_BARE_ID = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_A1 = re.compile(
    r"^(?:(?P<sheet>'(?:[^']|'')+'|[^'!]+)!)"
    r"(?P<c1>[A-Za-z]{1,3})(?P<r1>[1-9][0-9]*)"
    r"(?::(?P<c2>[A-Za-z]{1,3})(?P<r2>[1-9][0-9]*))?$"
)
_COL = re.compile(r"^[A-Za-z]{1,3}$")


def spreadsheet_id(value: str) -> str:
    value = (value or "").strip()
    match = _ID_IN_URL.search(value)
    if match:
        return match.group(1)
    if _BARE_ID.match(value):
        return value
    raise ToolError(f"'{value}' isn't a spreadsheet ID or a Google Sheets URL.")


def col_to_num(letters: str) -> int:
    number = 0
    for char in letters.upper():
        number = number * 26 + (ord(char) - 64)
    return number


def num_to_col(number: int) -> str:
    letters = ""
    while number:
        number, rem = divmod(number - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def quote_tab(tab: str) -> str:
    return "'" + tab.replace("'", "''") + "'"


def parse_write_range(a1: str) -> tuple[str, int, int, int, int]:
    """'Tab'!B2:D5 -> (tab, first_row, first_col, rows, cols). Writes must name the tab and
    give exact cell corners, so nothing lands on an unintended tab or an open-ended range."""
    match = _A1.match((a1 or "").strip())
    if not match:
        raise ToolError(
            f"Write range '{a1}' must name the tab and exact cells, like 'October'!B2:D5 or Sales!C7."
        )
    sheet = match.group("sheet")
    tab = sheet[1:-1].replace("''", "'") if sheet.startswith("'") else sheet
    c1, r1 = col_to_num(match.group("c1")), int(match.group("r1"))
    c2 = col_to_num(match.group("c2")) if match.group("c2") else c1
    r2 = int(match.group("r2")) if match.group("r2") else r1
    if c2 < c1 or r2 < r1:
        raise ToolError(f"Write range '{a1}' runs backwards. Give the top-left cell first.")
    return tab, r1, c1, r2 - r1 + 1, c2 - c1 + 1


def check_grid(values, rows: int, cols: int, what: str) -> None:
    if not isinstance(values, list) or not values or not all(isinstance(r, list) for r in values):
        raise ToolError(f"{what} must be a list of rows, each row a list of cells.")
    if len(values) != rows or any(len(r) != cols for r in values):
        shape = f"{len(values)} rows x {sorted({len(r) for r in values})} cols"
        raise ToolError(f"{what} is {shape} but the range is {rows} rows x {cols} cols. They must match exactly.")
    if rows * cols > MAX_CELLS_PER_WRITE:
        raise ToolError(
            f"{rows * cols} cells is over the {MAX_CELLS_PER_WRITE}-cell limit per write. "
            "Treat it as a mass operation: show Htet the plan, get 'proceed', then write in batches."
        )
    for row in values:
        for cell in row:
            if not (cell is None or isinstance(cell, (str, int, float, bool))):
                raise ToolError(f"Cell value {cell!r} isn't text, a number or true/false.")


def pad(grid: list, rows: int, cols: int) -> list:
    """The API trims trailing empty rows and cells. Square it back up so before/after line up."""
    out = []
    for i in range(rows):
        row = list(grid[i]) if i < len(grid) else []
        out.append(row + [""] * (cols - len(row)))
    return out


# --------------------------------------------------------------------------- guards + log


def allowed(sid: str) -> bool:
    try:
        with open(ALLOW_LIST_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return False
    except json.JSONDecodeError as exc:
        raise ToolError(f"allow-list.json is broken ({exc}). Nothing gets written until it's fixed.") from None
    for entry in data.get("spreadsheets", []):
        entry_id = entry.get("id") if isinstance(entry, dict) else entry
        if entry_id == "*" or entry_id == sid:
            return True
    return False


def require_allowed(sid: str) -> None:
    if not allowed(sid):
        raise ToolError(
            f"Spreadsheet {sid} isn't on allow-list.json, so the tool won't write to it. "
            "Only Htet adds sheets to that file."
        )


def log(entry: dict) -> None:
    """Append one line to write-log/YYYY-MM.jsonl. A failed log write stops the write."""
    os.makedirs(LOG_DIR, exist_ok=True)
    now = datetime.datetime.now().astimezone()
    entry = {"time": now.isoformat(timespec="seconds"), **entry}
    path = os.path.join(LOG_DIR, now.strftime("%Y-%m") + ".jsonl")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- Sheets helpers


def metadata(sid: str) -> dict:
    fields = "properties.title,sheets.properties(sheetId,title,index,hidden,gridProperties(rowCount,columnCount))"
    return _http("GET", f"{API}/{sid}?" + urllib.parse.urlencode({"fields": fields}))


def tab_grid(meta: dict, tab: str) -> dict:
    for sheet in meta.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") == tab:
            return props.get("gridProperties", {})
    tabs = ", ".join(s["properties"]["title"] for s in meta.get("sheets", []))
    raise ToolError(f"No tab called '{tab}'. Tabs are: {tabs}")


def get_values(sid: str, a1: str, render: str) -> list:
    url = _values_url(sid, a1, {"valueRenderOption": render, "majorDimension": "ROWS"})
    return _http("GET", url).get("values", [])


def put_values(sid: str, a1: str, values: list, input_option: str) -> dict:
    url = _values_url(sid, a1, {
        "valueInputOption": input_option,
        "includeValuesInResponse": "true",
        "responseValueRenderOption": "FORMULA",
    })
    return _http("PUT", url, {"range": a1, "majorDimension": "ROWS", "values": values})


def _write(tool: str, sid: str, tab: str, r1: int, c1: int, rows: int, cols: int,
           values: list, input_option: str, overwrite_formulas: bool) -> str:
    """Shared path for every write: allow-list, tab check, before-read, guards, log, write, log."""
    require_allowed(sid)
    meta = metadata(sid)
    grid = tab_grid(meta, tab)
    last_row, last_col = r1 + rows - 1, c1 + cols - 1
    if last_row > grid.get("rowCount", 0) or last_col > grid.get("columnCount", 0):
        raise ToolError(
            f"The write reaches row {last_row}, column {num_to_col(last_col)}, past the edge of tab "
            f"'{tab}' ({grid.get('rowCount')} rows x {num_to_col(grid.get('columnCount', 0))}). "
            "Adding rows or columns is out of scope."
        )
    a1 = f"{quote_tab(tab)}!{num_to_col(c1)}{r1}:{num_to_col(last_col)}{last_row}"
    before = pad(get_values(sid, a1, "FORMULA"), rows, cols)

    formula_cells = [
        f"{num_to_col(c1 + j)}{r1 + i}"
        for i, row in enumerate(before) for j, cell in enumerate(row)
        if isinstance(cell, str) and cell.startswith("=")
    ]
    if formula_cells and not overwrite_formulas:
        raise ToolError(
            f"{len(formula_cells)} cell(s) in {a1} hold formulas: {', '.join(formula_cells[:20])}. "
            "Nothing written. Show Htet, and only pass overwrite_formulas=true if Htet says to replace them."
        )

    title = meta.get("properties", {}).get("title", "")
    base = {"tool": tool, "spreadsheet_id": sid, "spreadsheet": title, "range": a1, "input": input_option}
    log({**base, "status": "about_to_write", "before": before, "after": values})
    try:
        result = put_values(sid, a1, values, input_option)
    except ToolError as exc:
        log({**base, "status": "failed", "error": str(exc)})
        raise
    log({
        **base, "status": "written",
        "updated_range": result.get("updatedRange"), "updated_cells": result.get("updatedCells"),
        "now": result.get("updatedData", {}).get("values"),
    })

    changed = sum(
        1 for i in range(rows) for j in range(cols)
        if str(before[i][j]) != str(values[i][j] if values[i][j] is not None else "")
    )
    lines = [
        f"Wrote {result.get('updatedCells', rows * cols)} cell(s) to {title} {result.get('updatedRange', a1)}.",
        f"{changed} cell(s) changed value. Before and after are in write-log/.",
    ]
    if formula_cells:
        lines.append(f"Replaced {len(formula_cells)} formula(s), as instructed: {', '.join(formula_cells[:20])}.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- tools


def tool_list_tabs(args: dict) -> str:
    sid = spreadsheet_id(args.get("spreadsheet", ""))
    meta = metadata(sid)
    lines = [f"{meta.get('properties', {}).get('title', '')} ({sid})",
             f"Writable through this tool: {'yes' if allowed(sid) else 'no, not on allow-list.json'}"]
    for sheet in meta.get("sheets", []):
        p = sheet["properties"]
        g = p.get("gridProperties", {})
        hidden = " (hidden)" if p.get("hidden") else ""
        lines.append(f"- {p['title']}{hidden}: {g.get('rowCount')} rows x {num_to_col(g.get('columnCount', 0))}")
    return "\n".join(lines)


def tool_read_range(args: dict) -> str:
    sid = spreadsheet_id(args.get("spreadsheet", ""))
    a1 = (args.get("range") or "").strip()
    if not a1:
        raise ToolError("Give a range, like 'October'!A1:J40 or Sales!A:D.")
    render = "FORMULA" if args.get("show_formulas") else "FORMATTED_VALUE"
    url = _values_url(sid, a1, {"valueRenderOption": render, "majorDimension": "ROWS"})
    data = _http("GET", url)
    text = json.dumps({"range": data.get("range", a1), "shown_as": render.lower(),
                       "values": data.get("values", [])}, ensure_ascii=False)
    if len(text) > MAX_READ_CHARS:
        return text[:MAX_READ_CHARS] + f"\n[CUT at {MAX_READ_CHARS} characters. Read a smaller range.]"
    return text


def tool_write_values(args: dict) -> str:
    sid = spreadsheet_id(args.get("spreadsheet", ""))
    tab, r1, c1, rows, cols = parse_write_range(args.get("range", ""))
    values = args.get("values")
    check_grid(values, rows, cols, "values")
    for row in values:
        for cell in row:
            if cell is None or cell == "":
                raise ToolError("Empty cells in 'values' would clear cells, and clearing is out of scope. "
                                "Narrow the range to the cells that change.")
            if isinstance(cell, str) and cell.startswith("="):
                raise ToolError(f"'{cell}' looks like a formula. Use write_formulas for formulas.")
    mode = args.get("input_mode", "exact")
    if mode not in ("exact", "as_typed"):
        raise ToolError("input_mode is 'exact' (default) or 'as_typed'.")
    option = "RAW" if mode == "exact" else "USER_ENTERED"
    return _write("write_values", sid, tab, r1, c1, rows, cols, values, option,
                  bool(args.get("overwrite_formulas")))


def tool_write_formulas(args: dict) -> str:
    sid = spreadsheet_id(args.get("spreadsheet", ""))
    tab, r1, c1, rows, cols = parse_write_range(args.get("range", ""))
    formulas = args.get("formulas")
    check_grid(formulas, rows, cols, "formulas")
    for row in formulas:
        for cell in row:
            if not (isinstance(cell, str) and cell.startswith("=")):
                raise ToolError(f"{cell!r} isn't a formula. Every cell must start with '='. "
                                "Use write_values for plain values.")
    return _write("write_formulas", sid, tab, r1, c1, rows, cols, formulas, "USER_ENTERED", True)


def tool_append_rows(args: dict) -> str:
    sid = spreadsheet_id(args.get("spreadsheet", ""))
    tab = (args.get("tab") or "").strip()
    first_col = (args.get("start_column") or "A").strip()
    if not tab:
        raise ToolError("Give the tab name.")
    if not _COL.match(first_col):
        raise ToolError(f"start_column '{first_col}' should be column letters, like A or AB.")
    values = args.get("values")
    if not isinstance(values, list) or not values or not all(isinstance(r, list) and r for r in values):
        raise ToolError("values must be a list of rows, each row a list of cells.")
    cols = max(len(r) for r in values)
    values = [[("" if c is None else c) for c in r] + [""] * (cols - len(r)) for r in values]
    rows = len(values)
    check_grid(values, rows, cols, "values")
    for row in values:
        for cell in row:
            if isinstance(cell, str) and cell.startswith("="):
                raise ToolError(f"'{cell}' looks like a formula. Append the values, then use write_formulas.")

    require_allowed(sid)
    c1 = col_to_num(first_col)
    c2 = c1 + cols - 1
    grid = tab_grid(metadata(sid), tab)
    column_span = f"{quote_tab(tab)}!{num_to_col(c1)}1:{num_to_col(c2)}{grid.get('rowCount', 1)}"
    last_used = len(get_values(sid, column_span, "FORMULA"))
    r1 = last_used + 1
    mode = args.get("input_mode", "exact")
    if mode not in ("exact", "as_typed"):
        raise ToolError("input_mode is 'exact' (default) or 'as_typed'.")
    option = "RAW" if mode == "exact" else "USER_ENTERED"
    result = _write("append_rows", sid, tab, r1, c1, rows, cols, values, option, False)
    return f"Appended below row {last_used} (the last row with anything in columns " \
           f"{num_to_col(c1)} to {num_to_col(c2)}).\n" + result


_SHEET = {"type": "string", "description": "Spreadsheet ID, or its full Google Sheets URL."}
_GRID = {"type": "array", "items": {"type": "array", "items": {"type": ["string", "number", "boolean", "null"]}}}
_MODE = {
    "type": "string", "enum": ["exact", "as_typed"],
    "description": "exact (default): stored exactly as sent. Send numbers as JSON numbers, not strings, or "
                   "they land as text. as_typed: Sheets reads each value as if typed in (dates, %, currency).",
}

TOOLS = {
    "list_tabs": {
        "fn": tool_list_tabs,
        "description": "List a spreadsheet's tabs and sizes, and say whether it's writable through this tool.",
        "schema": {"type": "object", "properties": {"spreadsheet": _SHEET}, "required": ["spreadsheet"]},
        "annotations": {"title": "List tabs", "readOnlyHint": True, "openWorldHint": True},
    },
    "read_range": {
        "fn": tool_read_range,
        "description": "Read cells from any spreadsheet innyarr@gmail.com can open. show_formulas=true "
                       "returns formulas instead of their results, which tells a typed cell from a calculated one.",
        "schema": {"type": "object", "properties": {
            "spreadsheet": _SHEET,
            "range": {"type": "string", "description": "A1 range, e.g. 'October 2026'!A1:J40 or Sales!A:D."},
            "show_formulas": {"type": "boolean", "default": False},
        }, "required": ["spreadsheet", "range"]},
        "annotations": {"title": "Read range", "readOnlyHint": True, "openWorldHint": True},
    },
    "write_values": {
        "fn": tool_write_values,
        "description": "Overwrite cells with plain values on an allow-listed sheet. The range must name the "
                       "tab and exact corners, and values must match its shape. Refuses empty cells (no "
                       "clearing), formulas (use write_formulas), more than 500 cells, and cells that "
                       "currently hold formulas unless overwrite_formulas=true. Logs before/after. "
                       "Read the range first and show Htet what will change.",
        "schema": {"type": "object", "properties": {
            "spreadsheet": _SHEET,
            "range": {"type": "string", "description": "Tab and exact cells, e.g. 'October 2026'!D2:D15."},
            "values": {**_GRID, "description": "Rows of cells, matching the range's shape exactly."},
            "input_mode": _MODE,
            "overwrite_formulas": {"type": "boolean", "default": False,
                                   "description": "Only when Htet has said to replace existing formulas."},
        }, "required": ["spreadsheet", "range", "values"]},
        "annotations": {"title": "Write values", "readOnlyHint": False, "destructiveHint": True,
                        "idempotentHint": True, "openWorldHint": True},
    },
    "write_formulas": {
        "fn": tool_write_formulas,
        "description": "Write formulas (each starting with '=') to an allow-listed sheet. Same range rules, "
                       "500-cell limit and before/after log as write_values. May replace existing formulas.",
        "schema": {"type": "object", "properties": {
            "spreadsheet": _SHEET,
            "range": {"type": "string", "description": "Tab and exact cells, e.g. Sales!F2:F40."},
            "formulas": {**_GRID, "description": "Rows of formulas, matching the range's shape exactly."},
        }, "required": ["spreadsheet", "range", "formulas"]},
        "annotations": {"title": "Write formulas", "readOnlyHint": False, "destructiveHint": True,
                        "idempotentHint": True, "openWorldHint": True},
    },
    "append_rows": {
        "fn": tool_append_rows,
        "description": "Add rows below the last row that has anything in the target columns, on an "
                       "allow-listed sheet. Writes only into empty cells, never inserts rows, refuses if the "
                       "tab has no room left. Logs before/after.",
        "schema": {"type": "object", "properties": {
            "spreadsheet": _SHEET,
            "tab": {"type": "string"},
            "start_column": {"type": "string", "default": "A", "description": "Column of the first cell in each row."},
            "values": {**_GRID, "description": "Rows to add. Empty cells allowed."},
            "input_mode": _MODE,
        }, "required": ["spreadsheet", "tab", "values"]},
        "annotations": {"title": "Append rows", "readOnlyHint": False, "destructiveHint": False,
                        "idempotentHint": False, "openWorldHint": True},
    },
}

INSTRUCTIONS = (
    "Read and edit cells in Htet's Google Sheets (innyarr@gmail.com). Reads work on any sheet; writes "
    "only on sheets in allow-list.json. Before any write: read the range, show Htet what will change, "
    "and check the sheet's Apps Script for onEdit triggers, because API writes don't fire them. "
    "Every write is logged with before-values in write-log/. Nothing here deletes, clears, shares or "
    "inserts rows."
)


# --------------------------------------------------------------------------- MCP over stdio


def handle(message: dict) -> dict | None:
    method = message.get("method")
    msg_id = message.get("id")
    if msg_id is None:
        return None  # notifications (initialized, cancelled) need no reply

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    if method == "initialize":
        asked = (message.get("params") or {}).get("protocolVersion")
        version = asked if asked in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[1]
        return ok({
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "claude-sheets", "version": VERSION},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [
            {"name": name, "description": t["description"], "inputSchema": t["schema"],
             "annotations": t["annotations"]}
            for name, t in TOOLS.items()
        ]})
    if method == "tools/call":
        params = message.get("params") or {}
        tool = TOOLS.get(params.get("name"))
        if not tool:
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32602, "message": f"Unknown tool {params.get('name')!r}"}}
        try:
            text, is_error = tool["fn"](params.get("arguments") or {}), False
        except ToolError as exc:
            text, is_error = str(exc), True
        except Exception as exc:  # a bug here should read as a refusal, never crash the server
            text, is_error = f"Unexpected error, nothing assumed written: {type(exc).__name__}: {exc}", True
        return ok({"content": [{"type": "text", "text": text}], "isError": is_error})
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Unknown method {method!r}"}}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            reply = handle(message)
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
