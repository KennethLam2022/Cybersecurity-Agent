# Shared Visual Tokens: Minimal Set and First Pilot

Status: decision for review
Scope: select shared token roles and one independently verifiable migration
Evidence baseline: `origin/main` at `028a90535e2d05aea176be1bfa6e157541148424`

## Decision

Start with one shared stylesheet at `packages/agent/src/static/tokens.css`.
Use the following semantic roles; do not add component-specific tokens in
this slice.

| Token group | Names | Initial value | Purpose |
| --- | --- | --- | --- |
| Brand and interaction | `--brand`, `--accent`, `--accent-hover` | `#172033`, `#0b6bcb`, `#0957a6` | Brand surfaces and interactive controls |
| Semantic status | `--success`, `--warning`, `--danger` | `#157347`, `#b26a00`, `#b42318` | Preserve success, warning, and error meaning |
| Surfaces | `--bg-page`, `--bg-card`, `--bg-subtle` | `#f5f6f7`, `#ffffff`, `#f0f1f3` | Page, primary content, and secondary content backgrounds |
| Text and borders | `--text-1`, `--text-2`, `--text-3`, `--border`, `--border-strong` | `#1d1d1f`, `#5f6368`, `#8a8f98`, `#e3e5e8`, `#c8cbd0` | Text hierarchy and default/emphasized separators |
| Typography | `--fs-xs`, `--fs-sm`, `--fs-md`, `--fs-lg` | `12px`, `13px`, `15px`, `18px` | Compact metadata, supporting text, body, and section headings |
| Radius | `--r-sm`, `--r-md` | `6px`, `10px` | Controls/small surfaces and larger containers |
| Spacing | `--sp-1` through `--sp-6` | `4px`, `8px`, `12px`, `16px`, `24px`, `32px` | A shared 4px-based spacing scale |

These values come from UI/UE report §7.A and are the initial proposal, not a
requirement to recolor every page. Typography values are provisional pending
the separate font-baseline review in Issue #71. Keep existing status meaning
and adjust any value that fails contrast checks during the pilot.

Do not include component styles, shadows, animation, breakpoints, or page
layout rules in `tokens.css`. Do not replace page-specific values unless the
pilot needs them.

## First migration slice

Pilot on `/admin/login` (`packages/agent/src/templates/admin_login.html`).
It is a standalone page with its own small palette and form layout, so token
adoption can be reviewed without changing the dense `/admin` workspace or
several static child pages at once.

The implementation slice should add `tokens.css`, load it from the admin
login template, and replace only that page's matching brand, semantic color,
surface, text, border, typography, radius, and spacing literals. Keep the
existing structure and responsive breakpoint. Do not change labels, form
behavior, authentication, or navigation.

Acceptance for the implementation slice:

- The admin login page uses the shared token roles for the categories above.
- The rendered page remains legible and usable at desktop and narrow widths;
  the existing form fields, error state, and submit action remain unchanged.
- The login page's brand area and interactive blue remain visually distinct.
- Other pages remain unchanged.

Expected implementation scope: one stylesheet and one template, under four
hours. This is a planning estimate, not measured effort.

## Explicitly out of scope

- Migrating the user login page, `/admin`, or any static admin child page.
- Extracting `components.css` or standardizing buttons, cards, tables, toasts,
  or modals.
- Bulk replacing CSS literals, changing information density, or redesigning
  navigation.
- Changing backend behavior, API contracts, roles, or permissions.

## Evidence and assumptions

- Source note: Issue #70 cites the UI/UE report. The report file is ignored
  and is not present in `origin/main`; this decision records the relevant
  proposed token values and pilot scope so it can be reviewed independently.
- Fact: UI/UE report §2.1 identifies four visual languages across login,
  frontend chat, admin shell, and static child pages.
- Fact: UI/UE report §7.A proposes semantic color, surface, typography,
  radius, and spacing roles; current templates contain separate local
  palettes.
- Fact: `admin_login.html` is a standalone template with its own `:root`
  variables and responsive layout.
- Assumption: migrating this small page first will expose token naming and
  loading issues at lower risk than starting in the large admin shell.
- Assumption: the proposed color values remain suitable after contrast and
  screenshot review. Revisit if the pilot fails contrast checks or obscures
  state distinctions.
- Review the pilot at desktop and narrow widths before selecting another page.
  Do not treat completion of this spike as evidence that a shared system has
  already been implemented.

## Verification

Review the proposed roles against UI/UE report §7.A and the current
`admin_login.html` styles. The implementation issue should add focused
automated checks for stylesheet loading and unchanged login controls, then
capture desktop and narrow-width screenshots. No CSS or runtime changes are
part of this spike.
