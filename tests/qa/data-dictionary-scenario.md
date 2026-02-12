# Data Dictionary — QA Test Scenario

This scenario is designed to be executed with showboat and rodney.
Run through each section in order — sessions are grouped by user to minimize login cycles.

All interactive selectors use `data-testid` attributes so the test is resilient to
styling and layout changes. See CLAUDE.md for the testid convention.

## Prerequisites

- App running at `http://localhost:5173` with PostgreSQL backend
- Four user accounts pre-created:
  - **admin@test.com / testpass** — Admin role on the test project
  - **analyst@test.com / testpass** — Analyst role on the test project
  - **viewer@test.com / testpass** — Viewer role on the test project
  - **outsider@test.com / testpass** — No project membership
- A test project with a connected database containing at least 2 tables (e.g. `users`, `orders`)
- Schema has NOT been refreshed yet (fresh state)

## Setup

```bash
rodney start
mkdir -p tests/qa/data-dictionary-images
rodney open http://localhost:5173
rodney waitstable
```

```bash
showboat init tests/qa/data-dictionary-demo.md "Data Dictionary QA"
```

---

## Part 1: Admin Session

Sign in as the admin user who has full permissions.

### 1.1 Sign in as Admin

```bash
showboat note tests/qa/data-dictionary-demo.md "## Part 1: Admin Session

Sign in as admin@test.com who has the Admin role on the test project."
```

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney input '#email' 'admin@test.com' && rodney input '#password' 'testpass' && rodney click 'button[type=\"submit\"]' && rodney waitstable && rodney sleep 2 && echo 'Signed in as admin'"
```

```bash
showboat image tests/qa/data-dictionary-demo.md "rodney screenshot -w 1280 -h 720 tests/qa/data-dictionary-images/01-admin-signed-in.png"
```

### 1.2 Navigate to Data Dictionary — Empty State

Select the test project from the sidebar dropdown, then navigate to the Data Dictionary page. Before any schema refresh has happened, the page should show an empty state.

```bash
showboat note tests/qa/data-dictionary-demo.md "### Empty State

Navigate to Data Dictionary before any schema has been refreshed. The left panel should be empty — no tables to browse."
```

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney click 'a[href=\"/data-dictionary\"]' && rodney waitstable && rodney sleep 1 && echo 'Navigated to data dictionary'"
```

```bash
showboat image tests/qa/data-dictionary-demo.md "rodney screenshot -w 1280 -h 720 tests/qa/data-dictionary-images/02-empty-state.png"
```

**Verify:** Left panel shows "No schemas available". Right panel shows "Select a table" placeholder.

### 1.3 Refresh Schema

Click the refresh button to introspect the connected database and populate the data dictionary.

```bash
showboat note tests/qa/data-dictionary-demo.md "### Schema Refresh

Click the refresh icon to introspect the database. Only admins can do this."
```

```bash
showboat image tests/qa/data-dictionary-demo.md "rodney screenshot -w 1280 -h 720 tests/qa/data-dictionary-images/03-before-refresh.png"
```

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney click '[data-testid=\"refresh-schema-btn\"]' && rodney waitstable && rodney sleep 3 && echo 'Schema refresh triggered'"
```

```bash
showboat image tests/qa/data-dictionary-demo.md "rodney screenshot -w 1280 -h 720 tests/qa/data-dictionary-images/04-after-refresh.png"
```

**Verify:** The left panel now shows schema groups (e.g. `public`) with a table count badge. The refresh icon is no longer spinning.

### 1.4 Browse Schema Tree

Expand the `public` schema group and verify tables are listed.

```bash
showboat note tests/qa/data-dictionary-demo.md "### Browse Schema Tree

Expand the public schema to see tables. Tables are listed alphabetically."
```

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney click '[data-testid=\"schema-group-public\"]' && rodney waitstable && rodney sleep 1 && echo 'Expanded public schema'"
```

```bash
showboat image tests/qa/data-dictionary-demo.md "rodney screenshot -w 1280 -h 720 tests/qa/data-dictionary-images/05-schema-expanded.png"
```

**Verify:** Tables appear under the `public` group. Each table has a table icon and name.

### 1.5 Select a Table — View Columns

Click on a table to load its detail in the right panel.

```bash
showboat note tests/qa/data-dictionary-demo.md "### Select Table

Click a table to view its columns and annotation fields in the right panel."
```

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney click '[data-testid=\"table-item-users\"]' && rodney waitstable && rodney sleep 1 && echo 'Selected users table'"
```

```bash
showboat image tests/qa/data-dictionary-demo.md "rodney screenshot -w 1280 -h 720 tests/qa/data-dictionary-images/06-table-detail.png"
```

**Verify:** Right panel header shows schema badge (`public`) and table name (`users`). Column count is displayed. Columns table shows Name, Type, Nullable, and Description. Annotation fields below are empty.

### 1.6 Add Annotations

Fill in the annotation fields. The form auto-saves after a 1-second debounce — there is no save button.

```bash
showboat note tests/qa/data-dictionary-demo.md "### Add Annotations

Fill in table annotations. Changes auto-save after 1 second."
```

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney input '#description' 'Core user accounts table containing all registered users' && rodney input '#use_cases' 'User retention analysis, revenue attribution' && rodney input '#data_quality_notes' 'email may contain test accounts with @example.com' && rodney input '#refresh_frequency' 'Real-time' && rodney input '#owner' 'Platform Team' && rodney sleep 2 && echo 'Filled in annotations'"
```

```bash
showboat image tests/qa/data-dictionary-demo.md "rodney screenshot -w 1280 -h 720 tests/qa/data-dictionary-images/07-annotations-filled.png"
```

**Verify:** All five annotation fields are populated. "Changes are saved automatically" text is visible at the bottom.

### 1.7 Add Column-Level Note

Add a description for the email column.

```bash
showboat note tests/qa/data-dictionary-demo.md "### Column-Level Notes

Add a description for the email column."
```

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney input '[data-testid=\"column-note-email\"]' 'Always lowercase, validated on write' && rodney sleep 2 && echo 'Added column note for email'"
```

```bash
showboat image tests/qa/data-dictionary-demo.md "rodney screenshot -w 1280 -h 720 tests/qa/data-dictionary-images/08-column-note.png"
```

### 1.8 Verify Persistence — Reload Page

Reload the page and re-select the table to confirm annotations were saved to the backend.

```bash
showboat note tests/qa/data-dictionary-demo.md "### Verify Persistence

Reload the page and re-select the table. All annotations should still be there."
```

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney open http://localhost:5173/data-dictionary && rodney waitstable && rodney sleep 2 && echo 'Page reloaded'"
```

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney click '[data-testid=\"schema-group-public\"]' && rodney sleep 1 && rodney click '[data-testid=\"table-item-users\"]' && rodney waitstable && rodney sleep 1 && echo 'Re-selected users table'"
```

```bash
showboat image tests/qa/data-dictionary-demo.md "rodney screenshot -w 1280 -h 720 tests/qa/data-dictionary-images/09-persisted.png"
```

**Verify:** Description shows "Core user accounts table containing all registered users". Owner shows "Platform Team". Column note for email shows "Always lowercase, validated on write". The `users` table has a blue annotation dot in the schema tree.

### 1.9 Verify Annotation Indicator

Check that the annotated table (`users`) has a blue dot in the schema tree, and unannotated tables do not.

```bash
showboat note tests/qa/data-dictionary-demo.md "### Annotation Indicator

Annotated tables show a blue dot in the schema tree. Unannotated tables do not."
```

```bash
showboat image tests/qa/data-dictionary-demo.md "rodney screenshot -w 1280 -h 720 tests/qa/data-dictionary-images/10-annotation-dot.png" "[data-testid=\"schema-panel\"]"
```

**Verify:** `users` row has a blue dot (`[data-testid="annotation-indicator-users"]` is present). Other tables do not have an annotation indicator.

### 1.10 Edit an Existing Annotation

Change the owner field and verify the update persists.

```bash
showboat note tests/qa/data-dictionary-demo.md "### Edit Existing Annotation

Change the Owner from Platform Team to Identity Team."
```

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney js \"const input = document.querySelector('#owner'); input.focus(); input.select();\" && rodney type 'Identity Team' && rodney sleep 2 && echo 'Updated owner'"
```

Navigate to another table and back to confirm the edit stuck.

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney click '[data-testid=\"table-item-orders\"]' && rodney waitstable && rodney sleep 1 && rodney click '[data-testid=\"table-item-users\"]' && rodney waitstable && rodney sleep 1 && echo 'Navigated away and back'"
```

```bash
showboat image tests/qa/data-dictionary-demo.md "rodney screenshot -w 1280 -h 720 tests/qa/data-dictionary-images/11-edited-annotation.png"
```

**Verify:** Owner field shows "Identity Team", not "Platform Team".

### 1.11 Search Tables

Test the search filter in the schema tree.

```bash
showboat note tests/qa/data-dictionary-demo.md "### Search Tables

Filter the schema tree by typing a partial table name."
```

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney input '[data-testid=\"table-search\"]' 'ord' && rodney sleep 1 && echo 'Searched for ord'"
```

```bash
showboat image tests/qa/data-dictionary-demo.md "rodney screenshot -w 1280 -h 720 tests/qa/data-dictionary-images/12-search-filter.png"
```

**Verify:** Only tables matching "ord" are shown (e.g. `orders`). Schema group auto-expanded.

Clear the search to restore the full list.

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney js \"const input = document.querySelector('[data-testid=\\\"table-search\\\"]'); input.focus(); input.select();\" && rodney type '' && rodney sleep 1 && echo 'Cleared search'"
```

Search for something that doesn't exist.

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney input '[data-testid=\"table-search\"]' 'zzz_nonexistent' && rodney sleep 1 && echo 'Searched for nonexistent table'"
```

```bash
showboat image tests/qa/data-dictionary-demo.md "rodney screenshot -w 1280 -h 720 tests/qa/data-dictionary-images/13-search-no-results.png"
```

**Verify:** "No tables found" message is displayed.

### 1.12 Admin Logout

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney js \"const input = document.querySelector('[data-testid=\\\"table-search\\\"]'); if (input) { input.focus(); input.select(); }\" && rodney type '' && rodney sleep 0.5 && echo 'Cleared search'"
```

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney click '[data-testid=\"logout-btn\"]' && rodney waitstable && rodney sleep 1 && echo 'Logged out admin'"
```

---

## Part 2: Viewer Session

Sign in as a viewer to confirm read-only access — can browse but cannot edit annotations or refresh schema.

### 2.1 Sign in as Viewer

```bash
showboat note tests/qa/data-dictionary-demo.md "## Part 2: Viewer Session

Sign in as viewer@test.com. Viewers can browse the dictionary but cannot edit annotations or refresh schema."
```

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney input '#email' 'viewer@test.com' && rodney input '#password' 'testpass' && rodney click 'button[type=\"submit\"]' && rodney waitstable && rodney sleep 2 && echo 'Signed in as viewer'"
```

### 2.2 Navigate to Data Dictionary

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney click 'a[href=\"/data-dictionary\"]' && rodney waitstable && rodney sleep 1 && echo 'Navigated to data dictionary'"
```

```bash
showboat image tests/qa/data-dictionary-demo.md "rodney screenshot -w 1280 -h 720 tests/qa/data-dictionary-images/14-viewer-dictionary.png"
```

**Verify:** Tables are visible in the schema tree (populated from admin's earlier refresh).

### 2.3 Viewer Can Browse Tables

```bash
showboat note tests/qa/data-dictionary-demo.md "### Viewer Browses Tables

Viewers can see table schema and existing annotations."
```

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney click '[data-testid=\"schema-group-public\"]' && rodney sleep 1 && rodney click '[data-testid=\"table-item-users\"]' && rodney waitstable && rodney sleep 1 && echo 'Viewer selected users table'"
```

```bash
showboat image tests/qa/data-dictionary-demo.md "rodney screenshot -w 1280 -h 720 tests/qa/data-dictionary-images/15-viewer-table-detail.png"
```

**Verify:** Right panel shows the users table with all columns. Admin's annotations are visible (Description, Owner, etc.). Column note for email is visible.

### 2.4 Viewer Cannot Save Annotations

Type into the description field. The UI may allow typing, but the backend PUT should return 403 and the change should not persist after reload.

```bash
showboat note tests/qa/data-dictionary-demo.md "### Viewer Cannot Edit

The viewer types into the description field. The auto-save fires but the backend rejects the PUT with 403. After page reload, the original value is restored."
```

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney js \"const ta = document.querySelector('#description'); ta.focus(); ta.select();\" && rodney type 'Viewer tried to edit this' && rodney sleep 2 && echo 'Viewer typed in description'"
```

Reload and re-select to confirm the edit did not persist.

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney open http://localhost:5173/data-dictionary && rodney waitstable && rodney sleep 2 && echo 'Reloaded page'"
```

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney click '[data-testid=\"schema-group-public\"]' && rodney sleep 1 && rodney click '[data-testid=\"table-item-users\"]' && rodney waitstable && rodney sleep 1 && echo 'Re-selected users table'"
```

```bash
showboat image tests/qa/data-dictionary-demo.md "rodney screenshot -w 1280 -h 720 tests/qa/data-dictionary-images/16-viewer-edit-rejected.png"
```

**Verify:** Description still shows the admin's original text ("Core user accounts table..."), NOT "Viewer tried to edit this".

### 2.5 Viewer Logout

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney click '[data-testid=\"logout-btn\"]' && rodney waitstable && rodney sleep 1 && echo 'Logged out viewer'"
```

---

## Part 3: Analyst Session

Quick check that analysts also cannot edit annotations (admin-only operation).

### 3.1 Sign in as Analyst

```bash
showboat note tests/qa/data-dictionary-demo.md "## Part 3: Analyst Session

Sign in as analyst@test.com. Analysts can browse but cannot edit annotations — same restrictions as viewer for this feature."
```

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney input '#email' 'analyst@test.com' && rodney input '#password' 'testpass' && rodney click 'button[type=\"submit\"]' && rodney waitstable && rodney sleep 2 && echo 'Signed in as analyst'"
```

### 3.2 Analyst Cannot Save Annotations

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney click 'a[href=\"/data-dictionary\"]' && rodney waitstable && rodney sleep 1 && echo 'Navigated to data dictionary'"
```

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney click '[data-testid=\"schema-group-public\"]' && rodney sleep 1 && rodney click '[data-testid=\"table-item-users\"]' && rodney waitstable && rodney sleep 1 && echo 'Analyst selected users table'"
```

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney js \"const ta = document.querySelector('#description'); ta.focus(); ta.select();\" && rodney type 'Analyst tried to edit this' && rodney sleep 2 && echo 'Analyst typed in description'"
```

Reload and confirm the edit did not persist.

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney open http://localhost:5173/data-dictionary && rodney waitstable && rodney sleep 2 && rodney click '[data-testid=\"schema-group-public\"]' && rodney sleep 1 && rodney click '[data-testid=\"table-item-users\"]' && rodney waitstable && rodney sleep 1 && echo 'Re-selected users table'"
```

```bash
showboat image tests/qa/data-dictionary-demo.md "rodney screenshot -w 1280 -h 720 tests/qa/data-dictionary-images/17-analyst-edit-rejected.png"
```

**Verify:** Description still shows admin's original text.

### 3.3 Analyst Logout

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney click '[data-testid=\"logout-btn\"]' && rodney waitstable && rodney sleep 1 && echo 'Logged out analyst'"
```

---

## Part 4: Non-Member Session

Sign in as a user with no project membership. They should not be able to access the data dictionary at all.

### 4.1 Sign in as Outsider

```bash
showboat note tests/qa/data-dictionary-demo.md "## Part 4: Non-Member Session

Sign in as outsider@test.com who has no membership in the test project. The data dictionary should be inaccessible."
```

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney input '#email' 'outsider@test.com' && rodney input '#password' 'testpass' && rodney click 'button[type=\"submit\"]' && rodney waitstable && rodney sleep 2 && echo 'Signed in as outsider'"
```

### 4.2 Non-Member Blocked from Data Dictionary

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney click 'a[href=\"/data-dictionary\"]' && rodney waitstable && rodney sleep 1 && echo 'Attempted to navigate to data dictionary'"
```

```bash
showboat image tests/qa/data-dictionary-demo.md "rodney screenshot -w 1280 -h 720 tests/qa/data-dictionary-images/18-outsider-blocked.png"
```

**Verify:** The page shows an error state or empty state. The API returned 403 so no table data is loaded.

### 4.3 Outsider Logout

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney click '[data-testid=\"logout-btn\"]' && rodney waitstable && rodney sleep 1 && echo 'Logged out outsider'"
```

---

## Part 5: Unauthenticated Access

Confirm the data dictionary is not accessible without being signed in.

### 5.1 Access Without Auth

```bash
showboat note tests/qa/data-dictionary-demo.md "## Part 5: Unauthenticated Access

Navigate directly to the data dictionary URL without being signed in. Should be redirected to login."
```

```bash
showboat exec tests/qa/data-dictionary-demo.md bash "rodney open http://localhost:5173/data-dictionary && rodney waitstable && rodney sleep 2 && echo 'Navigated to data-dictionary while logged out'"
```

```bash
showboat image tests/qa/data-dictionary-demo.md "rodney screenshot -w 1280 -h 720 tests/qa/data-dictionary-images/19-unauthenticated.png"
```

**Verify:** The login form is shown, or an error/redirect occurs. The data dictionary content is not visible.

---

## Teardown

```bash
showboat note tests/qa/data-dictionary-demo.md "## Results

All scenarios completed. Review screenshots in \`tests/qa/data-dictionary-images/\` for visual verification."
```

```bash
rodney stop
```

## Verification Checklist

After running, review the generated `tests/qa/data-dictionary-demo.md` and confirm each screenshot shows the expected state:

| # | Screenshot | What to Check |
|---|-----------|---------------|
| 01 | admin-signed-in | Dashboard loaded for admin |
| 02 | empty-state | No tables, "No schemas available" or "Select a table" |
| 03 | before-refresh | Empty schema tree with refresh icon |
| 04 | after-refresh | Tables populated in schema tree with count badge |
| 05 | schema-expanded | Tables listed under `public` group |
| 06 | table-detail | Column table with name/type/nullable, empty annotation fields |
| 07 | annotations-filled | All 5 annotation fields populated |
| 08 | column-note | Email column has description text |
| 09 | persisted | After reload: annotations still present |
| 10 | annotation-dot | Blue dot on `users`, no dot on other tables |
| 11 | edited-annotation | Owner changed to "Identity Team" |
| 12 | search-filter | Only matching tables shown |
| 13 | search-no-results | "No tables found" message |
| 14 | viewer-dictionary | Viewer sees tables and refresh icon |
| 15 | viewer-table-detail | Viewer sees columns and admin's annotations |
| 16 | viewer-edit-rejected | After reload: admin's original text preserved |
| 17 | analyst-edit-rejected | After reload: admin's original text preserved |
| 18 | outsider-blocked | Error state, no table data loaded |
| 19 | unauthenticated | Login form shown, no dictionary content |
