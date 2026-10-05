# sheets-mcp — Claude Sheets

last updated: 2026-10-04

A small local server that lets Claude Code read and edit cells in Htet's Google Sheets (innyarr@gmail.com) through the ordinary Sheets API. Why it exists and what it may do: `../PRD - Sheets write tool (2026-10-04).md`. Setup: `setup steps (2026-10-04).md`.

## Files

| file | owner | what it is |
|---|---|---|
| `server.py` | machine (Claude builds it) | the server. Python standard library only, runs on the Mac's `/usr/bin/python3` (3.9) |
| `login.py` | machine | one-time Google sign-in. Htet runs it in Terminal |
| `allow-list.json` | **Htet** | spreadsheets the tool may write to. The server only reads it |
| `write-log/` | machine | `YYYY-MM.jsonl`, two lines per write: `about_to_write` (with before-values) then `written` or `failed` |
| `tests/test_server.py` | machine | offline tests against a fake Google. `python3 tests/test_server.py` |
| `site/claude-sheets/` | machine | the homepage and privacy page, uploaded by Htet to `innyarr-cpu.github.io/claude-sheets/` |

Outside this folder:
- Sign-in token: `~/.config/claude-sheets/token.json`, owner-only.
- Registered in Claude Code at user scope as `claude-sheets` (`~/.claude.json`).
- `~/.claude/settings.json` has "ask" rules for `write_values`, `write_formulas` and `append_rows`, so each write needs approval, auto mode included. Backup from before that change: `~/.claude/settings.json.bak-2026-10-04`.

## Undoing a write

Open the month's file in `write-log/`, find the `about_to_write` line, and write its `before` grid back to its `range`. Or use the sheet's File → Version history.

## Known limits

- Writes through the API don't fire simple `onEdit` triggers (Google's trigger guide). Installable triggers: not confirmed. Check a sheet's Apps Script before its first write.
- `append_rows` finds the last row with anything in the target columns only. A totals row in those columns means the new rows go below it.
- Finding a sheet by name goes through the Drive connector. This tool needs the ID or URL.
