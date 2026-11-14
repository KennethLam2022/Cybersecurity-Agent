# Admin Navigation Contract

Status: decision for review
Scope: the `/admin` grouped navigation only
Evidence baseline: `origin/main` at `ad2463c2cd6c37a181671a2b97b8b0589d91d45c`

## Observed behavior

- The admin template contains 27 secondary `role="tab"` controls and one
  secondary knowledge-base link across five primary groups: Operations (7),
  Knowledge (5 destinations, including the link), Configuration (10),
  Capabilities (3), and Quality (3).
- Selecting a secondary tab writes its key to the `tab` query parameter with
  `history.replaceState`. Loading `/admin?tab=<key>` restores the tab after
  role visibility is applied. An unknown or role-restricted key falls back to
  a visible tab and replaces the invalid key.
- The URL contract is query-string based, not hash based. It preserves other
  query parameters when changing `tab`.
- `platform_admin` can see platform-only tabs. `org_admin` can see the
  organization workspaces, notifications, monitoring, documents, graph,
  knowledge-base governance, external API, webhooks, Skill, MCP, and workflow
  areas as currently rendered. Server-side API authorization remains the
  authority; navigation visibility does not grant access.
- Desktop displays five primary groups in one row and secondary tabs in a
  horizontally scrollable strip. At widths up to 768px, primary groups use a
  two-column grid and the secondary strip remains horizontally scrollable.
- Existing Python and Node tests cover query persistence, refresh restoration,
  role filtering, and fallback. They do not cover which tab a primary group
  selects for each role.

## Decision

Keep the existing five-group hierarchy and `?tab=<key>` URL contract. Do not
introduce hash routing, a new route format, or a full navigation redesign in
this slice.

When a user selects a primary group, select the first tab in that group that
is visible to the authenticated role. If the group has no visible tabs, keep
the current group and current tab unchanged. This avoids routing an
`org_admin` through platform-only defaults such as `detail` or
`modelconfig`. Direct links to restricted tabs continue to fall back to an
allowed tab, without weakening server authorization.

## Role visibility matrix

| Area | `platform_admin` | `org_admin` |
| --- | --- | --- |
| Operations | All seven tabs | Workspaces, notifications, monitoring |
| Knowledge | All five tabs | Documents, graph, knowledge-base governance |
| Configuration | All ten tabs | External API, webhooks |
| Capabilities | All three tabs | Skill, MCP, workflows |
| Quality | Retrieval, end-to-end, Agent evaluation | No tabs; existing access notice remains |

This matrix describes current UI visibility, not a replacement for endpoint
authorization. Any future role change requires a separately reviewed
authorization change and corresponding backend tests.

## Responsive behavior

- Desktop: retain the five-column primary group row and horizontal secondary
  tab strip.
- Narrow screens (768px and below): retain the current two-column group grid
  and horizontal secondary tab scrolling; do not wrap tabs into multiple rows
  or change the URL contract.
- Keyboard focus remains on the activated primary group; the selected
  secondary tab is exposed through the existing tab semantics and
  `aria-selected`.

## First implementation slice

Change only `switchPrimaryGroup()` to find the first visible tab belonging to
the requested group before calling `switchTab()`. If none is visible, leave
the current selection unchanged.

Acceptance: for `org_admin`, selecting Operations chooses Workspaces and
selecting Configuration chooses External API; groups with no visible tabs do
not change selection. For `platform_admin`, each group still selects its
current first tab. Existing URL, role fallback, and backend authorization
behavior remain unchanged.

Expected scope: one template and one focused JavaScript test file; no more
than four hours. This estimate is a planning assumption, not measured effort.

## Evidence and assumptions

- Fact: 27 secondary tab controls, one secondary link, and five primary groups
  are present in the template.
- Fact: `loadAdminPrincipal()` applies role filtering before restoring the
  requested tab.
- Fact: `switchTab()` updates the `tab` query parameter using
  `replaceState`.
- Fact: current group switching uses each group's first configured tab,
  without checking role visibility first.
- Assumption: choosing the first role-visible tab is less surprising than
  showing a transient permission warning when switching groups. Validate with
  a short task walkthrough by one `org_admin` and one `platform_admin`.
- Assumption: retaining horizontal scrolling on narrow screens is acceptable.
  Revisit only if the walkthrough shows users cannot locate a tab.

## Verification

1. Run `node --test packages/agent/src/tests/test_admin_tab_restore.js` and
   `python -m pytest packages/agent/src/tests/test_admin_tab_restore.py -q`.
2. Walk through group switching and direct `?tab=` links with both roles.
3. Check a desktop viewport and a viewport at or below 768px; verify no
   horizontal page overflow, keyboard focus remains visible, and the selected
   tab stays identifiable.
