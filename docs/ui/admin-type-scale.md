# Admin Type Scale Baseline

Status: decision for review
Scope: typography on the `/admin?tab=notifications` pilot only
Evidence baseline: `origin/main` at `e2ea0392833e85522a675fbf8e0d2ea8e576a89d`

## Decision

Use these minimum sizes for readable admin content. A minimum is a floor, not
a requirement to enlarge every existing label; preserve compact layouts when
text remains legible and the role of each item stays clear.

| Use | Minimum | Guidance |
| --- | --- | --- |
| Primary prose and notification body | 13px | Use 15px as the default body size where the layout permits. |
| Table header and cell content | 12px | Keep titles, target, state, and time readable without relying on color alone. |
| Supporting and helper text | 12px | Prefer 13px for multi-line explanations. |
| Severity and state labels | 12px | Keep the text label visible; color is supplemental. |
| Page and section headings | 18px / 15px | Use 18px for the page title and 15px for section headings. |

Do not introduce new readable text at 11px or below. Existing technical
identifiers may remain compact in this pilot only when they are supplementary
and not the sole way to identify an action, state, or result. These rules
refine the provisional typography values in the shared-token decision from
Issue #70; they do not require that token implementation in this slice.

## First Pilot

Pilot only the notification center at `/admin?tab=notifications`, rendered by
`packages/agent/src/templates/admin.html`. It contains notification body copy,
a table header and cells, severity and status labels, supporting status text,
and row actions. This makes the type hierarchy reviewable without migrating
the whole admin shell.

The follow-up implementation should change only typography in this tab and
its notification rows. Preserve labels, data density, filtering, actions,
authorization, and other admin tabs. Expected implementation scope is one
template and focused tests, under four hours; this is an estimate, not
measured effort. Other pages need separate Issues.

## Verification

Use the local running application with synthetic test data; do not change
notification state during the visual review.

1. Sign in as `platform_admin` and `org_admin`, then open
   `/admin?tab=notifications` in each session.
2. At 1440x900, verify the title, controls, table headings, notification body,
   severity, state, time, and actions follow the hierarchy above. Capture a
   screenshot for each role.
3. At 768x1024, verify the controls wrap without overlap, the page itself does
   not gain horizontal scrolling, and any necessary table scrolling remains
   inside the notification list. Capture a screenshot for each role.
4. Confirm state and severity remain understandable from text when color is
   unavailable, and that no new readable label is 11px or smaller.

## Evidence and Assumptions

- Fact: Issue #71 cites UI/UE report §2.2, which recorded 186 `12px` and 72
  `13px` declarations in `admin.html`. The report file is ignored and is not
  present in this checkout, so its original counting method cannot be checked.
- Fact: a regex count of exact `font-size: Npx` declarations in
  `admin.html` at the evidence baseline finds 188 at 12px, 72 at 13px, and
  206 at 11px. This is a current source count, not a measure of visible text
  or readability.
- Fact: the notification renderer emits notification body, severity, status,
  target, time, and a conditional row action in one table.
- Assumption: 13px primary prose and 12px table/support text preserve the
  existing information density while improving the intended hierarchy.
  Validate through the screenshots and a short task walkthrough by a
  non-developer; revisit if text is still difficult to scan or the table
  becomes harder to use at 768px.
- Assumption: the notification center is representative enough for one
  typography pilot. Revisit before applying this baseline to forms, charts,
  or dense monitoring views.

No CSS, API, data, or permission changes are part of this Spike.
