# Org Admin Overview Decision

Status: decision for review
Scope: `org_admin` landing page and optional tenant summary
Evidence baseline: `origin/main` at `7890a3829c717b7a674d87733b1e23ddcc275e3e`

## Decision

Do not add an organization overview or change the default landing page yet.
Keep `/admin?tab=workspaces` as the `org_admin` landing page. The absence of
an overview is an observed UI fact; the need for a new overview is still an
unvalidated assumption, not a product requirement.

If a task walkthrough validates the need for a quick organization summary,
the smallest candidate is an optional view with:

| Candidate indicator | Tenant-scoped source |
| --- | --- |
| Workspace name, status, member count, Agent count, and knowledge-base count | `GET /api/admin/workspaces`; non-platform admins receive only their current workspace |
| Unread notification count | `GET /api/admin/notifications`; summary is resolved for the authenticated tenant |

These are candidate indicators, not confirmed user needs. Do not add evaluation
health, platform capability queues, global knowledge-gap counts, or other
platform-wide operational metrics to an `org_admin` view.

## Permission and Data Boundary

| Role | Allowed overview data | Enforcement |
| --- | --- | --- |
| `org_admin` | Only the workspace identified by the authenticated principal's `tenant_id`, and notifications in that same tenant | Reuse tenant-scoped endpoints; the server resolves scope from the principal and rejects a different requested tenant |
| `platform_admin` | Existing platform-level overview | Keep behind `platform.manage`; this decision does not change its access |
| Unauthenticated or other roles | No overview data | Existing authentication and permission checks remain authoritative |

Do not reuse `/api/admin/operations/overview` for `org_admin`: its route
requires `platform.manage`. Its current aggregation also includes unscoped
counts for open knowledge gaps, pending capability extensions, and failed
Agent evaluation runs (`packages/agent/src/memory.py`). Those values cannot
be presented as organization-scoped indicators without a separately reviewed
tenant-scoped query and authorization test.

Never trust a query-string `tenant_id` as the data boundary. Any future
endpoint must resolve and filter the tenant on the server from the authenticated
principal before reading data.

## Follow-up Boundary

If target-user validation supports an overview, create one implementation
Issue for an optional overview tab that consumes only the two existing
tenant-scoped endpoints above. Keep it under four hours and do not change the
default landing page in that slice. Reconsider the default route only after
the optional view has been used in a task walkthrough; if a route change is
then justified, track it separately.

No page, endpoint, schema, role, or default-route changes are part of this
Spike.

## Evidence and Assumptions

- Fact: the `org_admin` navigation has no overview tab and currently lands on
  Workspaces (`docs/ui/admin-navigation-contract.md`).
- Fact: `GET /api/admin/workspaces` scopes non-platform admins to their
  authenticated tenant (`packages/agent/src/routes_api.py:244`,
  `packages/agent/src/routes_api.py:1373`).
- Fact: `GET /api/admin/notifications` uses the same tenant scope
  (`packages/agent/src/routes_api.py:1035`).
- Fact: `/api/admin/operations/overview` requires platform permission
  (`packages/agent/src/routes_api.py:2294`); several counts in its current
  aggregate are not tenant-filtered (`packages/agent/src/memory.py:1868`,
  `packages/agent/src/memory.py:1870`,
  `packages/agent/src/memory.py:1872`).
- Assumption: an optional workspace and unread-notification summary would
  help an `org_admin` decide where to act. Validate with five target-user
  interviews or a short task walkthrough by a non-developer. Do not implement
  if existing navigation and workspaces already answer their tasks.
- Assumption: Workspaces remains the least surprising default until that
  validation. Revisit only if task evidence shows administrators routinely
  need a different first destination.

## Verification

Review the endpoint authorization and aggregation queries listed above.
For any follow-up implementation, test an `org_admin` request against its own
tenant and a different tenant, and verify the latter is rejected or excluded
server-side. Do not use real production data for the walkthrough.
