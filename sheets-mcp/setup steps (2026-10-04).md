# Sheets tool — setup steps for Htet

last updated: 2026-10-04

About 25 minutes, nothing costs money. Same shape as `hermes-drive` on 1 Oct. Do all of it signed in as **innyarr@gmail.com**. Check the avatar, top right, before each part. **Never the Sando Workspace account.**

PRD: `Projects/Claude Code/PRD - Sheets write tool (2026-10-04).md`.

---

## Part 1 — Put two pages on your GitHub site (5 min)

Google won't publish the app without a homepage and a privacy page. The Hermes pages can't be reused: they say read-only and "not shared with any external service", and neither is true here. The new pages say the tool edits sheets and that what it reads goes to Claude.

1. Open https://github.com/innyarr-cpu/innyarr-cpu.github.io
2. **Add file → Upload files.**
3. Open Finder at `CoWork playground/Projects/Claude Code/sheets-mcp/site/` and drag the whole **`claude-sheets`** folder onto the GitHub page. It holds `index.html` and `privacy.html`.
4. **Commit changes.**
5. Wait a minute, then check both load without signing in:
   - https://innyarr-cpu.github.io/claude-sheets/
   - https://innyarr-cpu.github.io/claude-sheets/privacy.html

Don't touch the existing Hermes pages at the root of the repo.

---

## Part 2 — New Cloud project (2 min)

1. Open https://console.cloud.google.com/projectselector2/home/dashboard
2. Top bar, project dropdown → **New Project**.
3. Name: `claude-sheets`. Location: **No organization**. **Create.**
4. Confirm `claude-sheets` shows in the top bar. **Everything below happens inside this project.** Not `hermes-drive`, not `htet-gws-cli`.

---

## Part 3 — Turn on the Sheets API (1 min)

1. Open https://console.cloud.google.com/flows/enableapi?apiid=sheets.googleapis.com
2. Check the project says `claude-sheets`. **Enable.**

Only this one. Not the Drive API, not anything with "MCP" in the name.

---

## Part 4 — Consent screen (5 min)

1. Open https://console.cloud.google.com/auth/branding → **Get Started**.
   - **App name:** `Claude Sheets`. **Support email:** innyarr@gmail.com. **Next.**
   - **Audience:** **External** (personal Gmail doesn't offer Internal). **Next.**
   - **Contact:** innyarr@gmail.com. **Next.**
   - Tick the User Data Policy box → **Continue** → **Create**.
2. Back on **Branding**, fill in and **Save**:
   - **Authorized domains:** add `innyarr-cpu.github.io` first.
   - **Application home page:** `https://innyarr-cpu.github.io/claude-sheets/`
   - **Application privacy policy link:** `https://innyarr-cpu.github.io/claude-sheets/privacy.html`

   The domain was already verified for `hermes-drive` on 1 Oct, under the same Google account, so it should go straight through. If Google says it isn't verified, stop and tell me.

---

## Part 5 — One permission only (2 min)

1. Open https://console.cloud.google.com/auth/scopes → **Add or Remove Scopes**.
2. Under **Manually add scopes**, paste exactly:
   ```
   https://www.googleapis.com/auth/spreadsheets
   ```
3. **Add to table → Update → Save.**

Nothing else. No Drive scopes. This list is Google's hard stop: anything not on it gets refused, whatever the tool asks for later.

---

## Part 6 — Test user, then publish (2 min)

1. Open https://console.cloud.google.com/auth/audience
2. **Test users → Add users →** innyarr@gmail.com → **Save.**
3. **Publishing status → Publish app →** confirm.

It shows **In production**, unverified. That's right. It stops the 7-day logout. Ignore any prompt to submit for verification.

---

## Part 7 — The OAuth client (3 min)

1. Open https://console.cloud.google.com/auth/clients/create
2. **Application type: Desktop app.** Name: `Claude Sheets`. **Create.**
3. **Download JSON** in the dialog.
4. In Finder, move the file from `Downloads` into `CoWork playground/tools/`, next to the existing `client_secret_963095…` file. Then delete the copy left in `Downloads`.
5. Tell me the filename. **Don't paste what's inside it into the chat.**

---

## Part 8 — Sign in once (after I've built the tool)

I'll give you one command to paste into Terminal. It opens Google's sign-in page in your browser.

1. Sign in as **innyarr@gmail.com**.
2. **"Google hasn't verified this app"** → **Advanced** → **Go to Claude Sheets (unsafe)**. Expected: it's your own app.
3. Read the permission line before you click **Allow**. It should say **"See, edit, create, and delete all your Google Sheets spreadsheets"**. That's the `spreadsheets` permission and it's correct. If you see anything about **Google Drive files**, stop and tell me.
4. The browser then says you can close the tab. Terminal prints "Signed in".

---

## Things that look wrong but aren't

| what you see | what it means |
|---|---|
| "Google hasn't verified this app" | Expected. Advanced → Go to Claude Sheets |
| No **Internal** option | Normal on personal Gmail. Pick External |
| Scope list empty on Data Access | Part 3 not finished yet. Wait a minute, reload |
| **400 invalid_scope** at sign-in | Something asked for more than `spreadsheets`. Tell me; don't add scopes |
| **403 access_denied** | innyarr@gmail.com isn't a test user, or the app isn't published. Redo Part 6 |
