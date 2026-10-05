#!/usr/bin/env python3
"""Offline tests for server.py. A fake Sheets API stands in for Google, so nothing here
touches a real sheet or needs a sign-in.

Run:  python3 tests/test_server.py      (from the sheets-mcp folder)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402

SID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
OTHER = "9ZyXwVuTsRqPoNmLkJiHgFeDcBa9876543210"


class FakeSheets:
    """Tabs as {(row, col): value}, 1-based. Understands the four calls server.py makes."""

    def __init__(self):
        self.title = "Test sheet"
        self.tabs = {"Sales": {}, "It's mine": {}}
        self.size = {"Sales": (20, 6), "It's mine": (10, 3)}
        self.calls = []

    def http(self, method, url, body=None):
        self.calls.append((method, url, body))
        path, _, query = url.partition("?")
        params = urllib.parse.parse_qs(query)
        if "/values/" not in path:
            return {"properties": {"title": self.title}, "sheets": [
                {"properties": {"title": t, "gridProperties": {"rowCount": r, "columnCount": c}}}
                for t, (r, c) in self.size.items()]}
        a1 = urllib.parse.unquote(path.split("/values/", 1)[1])
        tab, r1, c1, r2, c2 = self._range(a1)
        if method == "GET":
            render = params["valueRenderOption"][0]
            rows = []
            for r in range(r1, r2 + 1):
                row = []
                for c in range(c1, c2 + 1):
                    v = self.tabs[tab].get((r, c), "")
                    if render != "FORMULA" and isinstance(v, str) and v.startswith("="):
                        v = "42"
                    row.append(v)
                while row and row[-1] == "":
                    row.pop()
                rows.append(row)
            while rows and not rows[-1]:
                rows.pop()
            return {"range": a1, "values": rows}
        if method == "PUT":
            for i, row in enumerate(body["values"]):
                for j, v in enumerate(row):
                    self.tabs[tab][(r1 + i, c1 + j)] = v
            n = sum(len(r) for r in body["values"])
            return {"updatedRange": a1, "updatedCells": n, "updatedData": {"values": body["values"]}}
        raise AssertionError(method)

    def _range(self, a1):
        m = re.match(r"^(?:'((?:[^']|'')+)'|([^!]+))!([A-Z]+)(\d+):([A-Z]+)(\d+)$", a1)
        tab = (m.group(1) or "").replace("''", "'") or m.group(2)
        return (tab, int(m.group(4)), server.col_to_num(m.group(3)),
                int(m.group(6)), server.col_to_num(m.group(5)))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeSheets()
        server._http = self.fake.http
        server.LOG_DIR = os.path.join(self.tmp.name, "write-log")
        server.ALLOW_LIST_FILE = os.path.join(self.tmp.name, "allow-list.json")
        self.allow([SID])

    def tearDown(self):
        self.tmp.cleanup()

    def allow(self, ids):
        with open(server.ALLOW_LIST_FILE, "w") as h:
            json.dump({"spreadsheets": ids}, h)

    def call(self, name, **args):
        reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": name, "arguments": args}})
        result = reply["result"]
        return result["content"][0]["text"], result["isError"]

    def log_lines(self):
        if not os.path.isdir(server.LOG_DIR):
            return []
        lines = []
        for f in sorted(os.listdir(server.LOG_DIR)):
            with open(os.path.join(server.LOG_DIR, f)) as h:
                lines += [json.loads(x) for x in h]
        return lines

    def puts(self):
        return [c for c in self.fake.calls if c[0] == "PUT"]


class Parsing(unittest.TestCase):
    def test_ids(self):
        url = f"https://docs.google.com/spreadsheets/d/{SID}/edit#gid=0"
        self.assertEqual(server.spreadsheet_id(url), SID)
        self.assertEqual(server.spreadsheet_id(SID), SID)
        with self.assertRaises(server.ToolError):
            server.spreadsheet_id("Sales Tracking for ATP")

    def test_columns_round_trip(self):
        for n in (1, 26, 27, 52, 703, 16384):
            self.assertEqual(server.col_to_num(server.num_to_col(n)), n)

    def test_write_range(self):
        self.assertEqual(server.parse_write_range("Sales!B2:D5"), ("Sales", 2, 2, 4, 3))
        self.assertEqual(server.parse_write_range("'It''s mine'!A1"), ("It's mine", 1, 1, 1, 1))
        self.assertEqual(server.parse_write_range("'October 2026'!c7:c9"), ("October 2026", 7, 3, 3, 1))

    def test_write_range_refusals(self):
        for bad in ("B2:D5", "Sales!A:A", "Sales!D5:B2", "Sales!A0", "Sales", ""):
            with self.assertRaises(server.ToolError, msg=bad):
                server.parse_write_range(bad)


class Writes(Base):
    def test_write_values_logs_before_and_after(self):
        self.fake.tabs["Sales"][(2, 2)] = 10
        text, err = self.call("write_values", spreadsheet=SID, range="Sales!B2:C2", values=[[11, "x"]])
        self.assertFalse(err, text)
        self.assertEqual(self.fake.tabs["Sales"][(2, 2)], 11)
        log = self.log_lines()
        self.assertEqual([e["status"] for e in log], ["about_to_write", "written"])
        self.assertEqual(log[0]["before"], [[10, ""]])
        self.assertEqual(log[0]["after"], [[11, "x"]])
        self.assertIn("RAW", self.puts()[0][1])

    def test_not_on_allow_list(self):
        text, err = self.call("write_values", spreadsheet=OTHER, range="Sales!A1", values=[[1]])
        self.assertTrue(err)
        self.assertIn("allow-list", text)
        self.assertEqual(self.puts(), [])

    def test_star_allows_any(self):
        self.allow(["*"])
        _, err = self.call("write_values", spreadsheet=OTHER, range="Sales!A1", values=[[1]])
        self.assertFalse(err)

    def test_empty_or_missing_allow_list_blocks(self):
        self.allow([])
        _, err = self.call("write_values", spreadsheet=SID, range="Sales!A1", values=[[1]])
        self.assertTrue(err)
        os.remove(server.ALLOW_LIST_FILE)
        _, err = self.call("write_values", spreadsheet=SID, range="Sales!A1", values=[[1]])
        self.assertTrue(err)
        self.assertEqual(self.puts(), [])

    def test_shape_must_match(self):
        text, err = self.call("write_values", spreadsheet=SID, range="Sales!A1:B2", values=[[1, 2]])
        self.assertTrue(err)
        self.assertIn("must match", text)
        self.assertEqual(self.puts(), [])

    def test_cell_cap(self):
        self.fake.size["Sales"] = (1000, 6)
        values = [[1]] * 501
        text, err = self.call("write_values", spreadsheet=SID, range="Sales!A1:A501", values=values)
        self.assertTrue(err)
        self.assertIn("500", text)
        self.assertEqual(self.puts(), [])

    def test_no_clearing(self):
        for blank in ("", None):
            text, err = self.call("write_values", spreadsheet=SID, range="Sales!A1", values=[[blank]])
            self.assertTrue(err)
            self.assertIn("clear", text)
        self.assertEqual(self.puts(), [])

    def test_formula_text_refused_in_write_values(self):
        _, err = self.call("write_values", spreadsheet=SID, range="Sales!A1", values=[["=SUM(1)"]])
        self.assertTrue(err)
        self.assertEqual(self.puts(), [])

    def test_formula_cells_protected(self):
        self.fake.tabs["Sales"][(3, 1)] = "=SUM(A1:A2)"
        text, err = self.call("write_values", spreadsheet=SID, range="Sales!A3", values=[[5]])
        self.assertTrue(err)
        self.assertIn("A3", text)
        self.assertEqual(self.puts(), [])
        self.assertEqual(self.log_lines(), [])
        text, err = self.call("write_values", spreadsheet=SID, range="Sales!A3", values=[[5]],
                              overwrite_formulas=True)
        self.assertFalse(err, text)
        self.assertEqual(self.log_lines()[0]["before"], [["=SUM(A1:A2)"]])

    def test_past_grid_edge(self):
        text, err = self.call("write_values", spreadsheet=SID, range="Sales!A20:A21", values=[[1], [2]])
        self.assertTrue(err)
        self.assertIn("out of scope", text)
        self.assertEqual(self.puts(), [])

    def test_unknown_tab(self):
        text, err = self.call("write_values", spreadsheet=SID, range="Nope!A1", values=[[1]])
        self.assertTrue(err)
        self.assertIn("Sales", text)

    def test_as_typed(self):
        _, err = self.call("write_values", spreadsheet=SID, range="Sales!A1", values=[["4/10/2026"]],
                           input_mode="as_typed")
        self.assertFalse(err)
        self.assertIn("USER_ENTERED", self.puts()[0][1])

    def test_write_formulas(self):
        text, err = self.call("write_formulas", spreadsheet=SID, range="Sales!F2:F3",
                              formulas=[["=B2*2"], ["=B3*2"]])
        self.assertFalse(err, text)
        self.assertIn("USER_ENTERED", self.puts()[0][1])
        _, err = self.call("write_formulas", spreadsheet=SID, range="Sales!F2", formulas=[[12]])
        self.assertTrue(err)

    def test_quoted_tab(self):
        _, err = self.call("write_values", spreadsheet=SID, range="'It''s mine'!B2", values=[["ok"]])
        self.assertFalse(err)
        self.assertEqual(self.fake.tabs["It's mine"][(2, 2)], "ok")

    def test_failed_api_write_is_logged(self):
        def boom(method, url, body=None):
            if method == "PUT":
                raise server.ToolError("Sheets API 403: The caller does not have permission")
            return FakeSheets.http(self.fake, method, url, body)
        server._http = boom
        text, err = self.call("write_values", spreadsheet=SID, range="Sales!A1", values=[[1]])
        self.assertTrue(err)
        self.assertEqual([e["status"] for e in self.log_lines()], ["about_to_write", "failed"])

    def test_log_failure_blocks_write(self):
        server.LOG_DIR = "/dev/null/cannot-exist"
        text, err = self.call("write_values", spreadsheet=SID, range="Sales!A1", values=[[1]])
        self.assertTrue(err)
        self.assertEqual(self.puts(), [])


class Append(Base):
    def test_appends_below_last_used_row(self):
        for r in range(1, 6):
            self.fake.tabs["Sales"][(r, 1)] = f"row{r}"
        self.fake.tabs["Sales"][(3, 1)] = ""  # a gap mid-table must not attract the append
        text, err = self.call("append_rows", spreadsheet=SID, tab="Sales", values=[["new", 1], ["new2"]])
        self.assertFalse(err, text)
        self.assertEqual(self.fake.tabs["Sales"][(6, 1)], "new")
        self.assertEqual(self.fake.tabs["Sales"][(7, 1)], "new2")
        self.assertEqual(self.fake.tabs["Sales"][(3, 1)], "")
        self.assertIn("below row 5", text)

    def test_start_column(self):
        self.fake.tabs["Sales"][(4, 3)] = "x"
        self.fake.tabs["Sales"][(9, 1)] = "column A is further down, but it's not in C:D"
        _, err = self.call("append_rows", spreadsheet=SID, tab="Sales", start_column="C", values=[["a", "b"]])
        self.assertFalse(err)
        self.assertEqual(self.fake.tabs["Sales"][(5, 3)], "a")

    def test_no_room(self):
        self.fake.tabs["Sales"][(20, 1)] = "last row of the grid"
        text, err = self.call("append_rows", spreadsheet=SID, tab="Sales", values=[["x"]])
        self.assertTrue(err)
        self.assertEqual(self.puts(), [])

    def test_append_needs_allow_list(self):
        _, err = self.call("append_rows", spreadsheet=OTHER, tab="Sales", values=[["x"]])
        self.assertTrue(err)
        self.assertEqual(self.puts(), [])


class Reads(Base):
    def test_read_any_sheet(self):
        self.allow([])
        self.fake.tabs["Sales"][(1, 1)] = "=1+1"
        text, err = self.call("read_range", spreadsheet=OTHER, range="Sales!A1:A1")
        self.assertFalse(err)
        self.assertIn('"42"', text)
        text, _ = self.call("read_range", spreadsheet=OTHER, range="Sales!A1:A1", show_formulas=True)
        self.assertIn("=1+1", text)

    def test_list_tabs_says_writable(self):
        text, _ = self.call("list_tabs", spreadsheet=SID)
        self.assertIn("Writable through this tool: yes", text)
        text, _ = self.call("list_tabs", spreadsheet=OTHER)
        self.assertIn("not on allow-list", text)


class Protocol(unittest.TestCase):
    def test_stdio_round_trip(self):
        """Start the real server as Claude Code would and speak MCP to it."""
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "nope"},
        ]
        proc = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(HERE), "server.py")],
            input="\n".join(json.dumps(m) for m in msgs) + "\n",
            capture_output=True, text=True, timeout=20,
        )
        replies = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3])
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")
        names = {t["name"] for t in replies[1]["result"]["tools"]}
        self.assertEqual(names, {"list_tabs", "read_range", "write_values", "write_formulas", "append_rows"})
        self.assertIn("error", replies[2])


if __name__ == "__main__":
    unittest.main(verbosity=1)
