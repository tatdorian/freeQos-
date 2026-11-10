# Reference: access management in the "Liptinfor Client Hub" demo

Source: <https://liptinfor-client-hub-demo.crossconnect.chatgpt.site/> (read on 2026-11-10).
It was reached with `curl` (HTTP 200) and rendered with Playwright/Chromium.

**How this was gathered.** The site is a static, client-side-only prototype: `index.html`, `domain.js`, `fixtures.js`, `app.js`, `styles.css`, plus a downloadable `resources/HANDOFF.md` ("SYIT integration brief"). Everything below comes from reading that source and from clicking through the rendered app. The quotes are the app's exact UI strings.

## TL;DR

- **The demo has no real access management.** It has no login, no user list, no invitations, no user creation and no disabling of users. There is one hard-coded sample operator, *"Amina Mahamadou"*. The app says so itself: *"Roles here demonstrate the intended permissions. They are not authentication or production access controls."*
- **There are 4 fixed roles:** `Viewer`, `Operations`, `Supervisor`, `Administrator`. You switch between them with a single drop-down on the Administration page called *"Sample operator role"* (default: `Supervisor`). Nothing is configurable per user. There are no custom roles, no toggles per module, and no scoping to particular customers or sites.
- **The code only enforces two checks:**
  - `canEdit = role !== 'Viewer'`: makes any change.
  - `canBulk = role ∈ {Supervisor, Administrator}`: makes a group (multi-client) change.
  - In this demo, `Supervisor` and `Administrator` behave **identically**.
- **The "modes"** are **bandwidth modes** applied to a client connection: Automatic, Day, Weekend, Evening, Night, Backup and Extreme backup, plus a separate "Boost +20 Mbps". "Managing the modes" means changing a client's bandwidth mode or boost. It is the main write action gated by the roles. See §5.
- **Audit:** an "Activity" page records every change: action, detail, actor as `"Amina · <role>"`, time, and state (Applied / Queued / Failed). It can be searched, exported to CSV, and has a "Restore" (undo) button.
- **Production intent** (from HANDOFF.md):
  - Offer Viewer / Operations / Supervisor / Administrator permission sets, *enforced by the service*.
  - *"Restrict bulk actions, credential changes and integration administration separately."*
  - Use *"route-level permissions and customer/site scope checks"*.
  - *"Record every mutation with actor, timestamp, scope and outcome; redact secrets from audit data."*

## 1. Roles and permission matrix

The role names are exact. The helper text under the selector reads: *"Operations: individual changes. Supervisor / Administrator: group changes too."*

| Role | Meaning in the demo |
|---|---|
| **Viewer** | Read-only. A banner is shown on every page: *"Viewing as a Viewer. Changes are disabled in this demo role."* Edit buttons are rendered `disabled`. |
| **Operations** | Can make changes to **one client at a time**. Group actions are refused. |
| **Supervisor** | Operations, plus **group (bulk) changes**. This is the default role. |
| **Administrator** | Same as Supervisor in the code. The HANDOFF says it should additionally hold integration administration and credential rights in production. |

Matrix. ✅ = allowed. ❌ = button disabled or a "This action needs permission" dialog opens. "—" = no gate at all in the demo (anyone, even a Viewer).

| Page / action | Viewer | Operations | Supervisor | Administrator |
|---|:-:|:-:|:-:|:-:|
| **Clients** page: list, search, filter, open client record (all tabs) | ✅ | ✅ | ✅ | ✅ |
| Export clients (CSV) | — ✅ | ✅ | ✅ | ✅ |
| **Alerts** page: view, "View clients" | ✅ | ✅ | ✅ | ✅ |
| Alerts: "Mark maintenance" / "End maintenance" | ❌ | ✅ | ✅ | ✅ |
| **Client → Change mode** (one client) | ❌ | ✅ | ✅ | ✅ |
| **Client → Boost +20 Mbps / Stop boost** (one client) | ❌ | ✅ | ✅ | ✅ |
| Client → Account & plans: edit name, category, contact, email, phone, the six bandwidth profile rates → "Review changes" / "Save account changes" | ❌ (fields disabled) | ✅ | ✅ | ✅ |
| Client → Local network & Wi-Fi: "Edit" SSID/password, "Retry Wi-Fi change" | ❌ | ✅ | ✅ | ✅ |
| Retry a failed/pending change, one client | ❌ | ✅ | ✅ | ✅ |
| **Actions** page: view exception tabs (Backup, Extreme backup, Boosts active, Pending, Failed, No recent data) | ✅ | ✅ | ✅ | ✅ |
| "Manage a group" (Clients page) / "Choose clients" (Actions page) — choose by category | ❌ | ❌ | ✅ | ✅ |
| Change mode / Enable boost / Stop boost on **>1 selected clients** | ❌ | ❌ | ✅ | ✅ |
| Retry on >1 clients | ❌ | ❌ | ✅ | ✅ |
| **Activity** page: view, search | ✅ | ✅ | ✅ | ✅ |
| Activity → Export activity (CSV) | — ✅ | ✅ | ✅ | ✅ |
| Activity / client history → "Restore" (undo a change) | ❌ | ✅ | ✅ | ✅ |
| **Administration** page: view integrations and the schedule | ✅ | ✅ | ✅ | ✅ |
| Administration → change "Sample operator role" | — ✅ (demo switch) | ✅ | ✅ | ✅ |
| Administration → "Schedule preview" clock, "Quick tour", "Reset demo" | — ✅ | ✅ | ✅ | ✅ |
| Edit integrations / time zone / schedule | not implemented (read-only for all) | | | |

Note: the bulk check counts the targets. Selecting exactly **one** client through the checkbox bar is still allowed for Operations.

How a refusal looks: a modal titled **"This action needs permission"**. Its subtitle is *"Group actions are available to Supervisors and Administrators."* for bulk actions, or *"Your current sample role is read-only."* for a Viewer. Its body is *"Change the sample operator role in Administration to explore this action. In production, the server must enforce these permissions."* See `06-operations-bulk-denied.png`.

## 2. Fixed or configurable?

- **Fixed per role.** The role list is hard-coded (`['Viewer','Operations','Supervisor','Administrator']`), and so are the two predicates `canEdit` and `canBulk`.
- There are **no custom roles, no per-user overrides and no per-module toggles.** There is no read/write/admin split per module. The role is a single linear ladder: read → individual write → bulk write.
- There is **no customer or site scoping.** Every role sees all 24 clients and all 3 relays. Scoping only appears as a production requirement in HANDOFF.md (*"customer/site scope checks"*).
- HANDOFF.md asks for three permission dimensions to be kept **separate** in production: **bulk actions**, **credential changes** (Wi-Fi passwords) and **integration administration**. The demo only implements the first one.

## 3. Users: creation, invitation, assignment, disabling, audit

- **Not implemented.** The app has no users page, no invite flow, no role assignment per user, and no disabling.
- The "current user" is the hard-coded card *"Amina Mahamadou · `<role>` · Sample operator"* at the bottom of the sidebar. Clicking it opens Administration.
- The role is kept in `localStorage` together with the rest of the demo state. Changing it shows a toast: *"Sample role changed to `<role>`."*
- **Audit / activity log:**
  - Every mutation appends an entry `{time, actor:'Amina · <role>', action, detail, clientId, state, before, reverted}`. The `before` field holds the previous values, which makes undo possible.
  - Entries are shown on the **Activity** page under *"Team activity — Every change, clearly recorded."* (*"See what was requested, who requested it and whether it reached the device."*). The page has search and **Export activity** (CSV columns: Time, Operator, Action, Detail, State), and each row shows a state of *Applied / Queued / Failed* and *Reverted*.
  - Each client page also has a per-client "Recent activity" panel (last 3 entries).
  - "Restore" creates a *new* tracked command rather than erasing history.
- Production requirements in HANDOFF.md:
  - Persist command state and audit **before** acknowledging a command.
  - Use idempotency keys and revision checks (`If-Match`).
  - Redact secrets from audit data.
  - Never return existing Wi-Fi passwords in reads.

## 4. UI notes

- **Left sidebar:** Clients (count), Alerts (count), Actions, Activity, Administration. Below that are client categories, a help card and the operator card. See `01-clients-supervisor.png`.
- **Role management** is a single `<select>` labelled **"Sample operator role"**. It sits in the panel **"Review this prototype"** on the **Administration** page ("Workspace setup — Ready for your integrations."), next to "Schedule preview", "Quick tour" and "Reset demo". See `02-administration.png` and `03-role-selector.png`. The demo has no permission matrix UI and no toggles.
- **Read-only mode is shown three ways:**
  - a grey banner at the top of every page for a Viewer;
  - disabled buttons and form fields (`04`, `05`);
  - a permission modal when a gated action is attempted (`06`).
- **Every write goes through review first:** "Review change →", then "Apply to N clients". Bulk changes report a result per client (*"a partial failure never looks like a complete success"*).
- The Administration page also lists 4 integration adapters: *Billing & client accounts*, **freeQoS** (*"Internet service and bandwidth control: apply profile changes and boosts; read usage 1h/24h/7d; track requested vs applied state"*), *Ubiquiti airOS / UISP*, and *UniFi Network*. Each shows *"Demo adapter · Not connected to live systems"*.

## 5. "Modes" = bandwidth modes

These are the "modes" a user can "manage". They are gated by `canEdit` for one client and `canBulk` for a group. The *"Change bandwidth mode"* dialog (`07-change-mode-dialog.png`) offers:

| Mode (label) | Behaviour (dialog subtitle) |
|---|---|
| **Automatic** | *"Resume the profile for the current local time"*: the schedule picks Day / Weekend / Evening / Night |
| Day | *"Manual override · Schedule paused"* |
| Weekend | *"Manual override · Schedule paused"* |
| Evening | *"Manual override · Schedule paused"* |
| Night | *"Manual override · Schedule paused"* |
| **Backup** | *"Schedule paused until cancelled"* (preselected in the dialog) |
| **Extreme backup** | *"Schedule paused · Boost disabled"* |

The dialog adds this notice: *"Backup stays active until cancelled. Extreme backup also stops any existing boost. All rules apply to download and upload."*

- **Automatic schedule** (time zone `Africa/Niamey`; HANDOFF says an administrator must be able to configure it):

  | Profile | Days | Local time |
  |---|---|---|
  | Day | Monday–Friday | 08:00–18:00 |
  | Weekend | Saturday–Sunday | 08:00–18:00 |
  | Evening | Every day | 18:00–00:00 |
  | Night | Every day | 00:00–08:00 |

- **Per-client rates:** each client has six symmetric rate profiles (day, weekend, evening, night, backup, extreme), each between 0 and 10,000 Mbps. They are edited in *Account & plans*, which also requires `canEdit`.
- **Boost:**
  - Adds +20 Mbps in each direction.
  - Allowed in Automatic and Backup.
  - Has no expiry.
  - Is blocked in Extreme backup (*"Boost is unavailable in Extreme backup. Change the mode first."*).
- **Group mode changes** come from the Actions page or the client-list checkbox bar ("Change mode / Enable boost / Stop boost"). They need Supervisor or Administrator.

## Takeaways for freeQoS

1. The minimum viable model has three tiers:
   - **read-only**;
   - **operate one target** (change mode/boost, edit account, Wi-Fi);
   - **operate many targets** (bulk).

   Admin rights sit on top: integrations, time zone/schedule, credentials, user management.
2. Enforce permissions on the **server**. The UI only mirrors them: a banner, disabled controls and an explanatory permission dialog.
3. Add the parts the demo leaves out: a users list, invites, role assignment, disabling, separate rights for credentials and integrations, and **customer/site scoping**.
4. Log every mutation with actor and role, before/after values and outcome. Make the log searchable, exportable and undoable, and keep secrets out of it.

## Screenshots (`docs/reference-access-shots/`)

| File | Content |
|---|---|
| `01-clients-supervisor.png` | Clients page as Supervisor (default) |
| `02-administration.png` | Administration page (full page) |
| `03-role-selector.png` | "Review this prototype" panel with the role drop-down |
| `04-clients-viewer-banner.png` | Viewer banner on the Clients page |
| `05-client-detail-viewer.png` | Client record as Viewer (actions disabled) |
| `06-operations-bulk-denied.png` | Operations trying a group action → permission dialog |
| `07-change-mode-dialog.png` | "Change bandwidth mode" dialog (the "modes") |
| `08-activity-log.png` | Activity / audit page |
