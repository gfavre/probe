# Interface design guide

## Intent

A calm technical monitoring console that makes it easy to identify unavailable
services, understand failed checks, inspect history and adjust probes without
unnecessary navigation.

The visual identity uses a navy grid, cyan/violet accents, Michroma headings and
navigation. Data readability takes precedence over lighting effects.
[AGENTS.md](AGENTS.md) describes scope and invariants; [README.md](README.md)
covers operation. This guide adapts shared-shell, component, form, table and
state principles from eFBL Admin to the monitoring application.

## Style sources

| File | Responsibility |
| --- | --- |
| `monitor/templates/monitor/base.html` | Internal page shell |
| `monitor/templates/monitor/login.html` | Minimal landing and invitation activation |
| `monitor/templates/admin/base_site.html` | Shared admin branding |
| `monitor/static/monitor/app.css` | Existing structure and components |
| `monitor/static/monitor/dark.css` | Dark palette, surfaces, backgrounds and navigation |
| `monitor/static/monitor/typography.css` | Local fonts, sizes and weights |
| `monitor/static/monitor/buttons.css` | Shared buttons |
| `monitor/static/monitor/admin-dark.css` | Django admin theme adaptation |
| `monitor/static/monitor/styleguide.css` | Visual reference specimens |
| `monitor/design.py` | Documented palette and contrast calculations |

Preserve the shared loading order. Change existing rules where possible instead
of layering contradictory overrides. Move repeated patterns into shared styles.
Do not add component CSS to business templates.

## Palette and surfaces

| Purpose | Token | Colour |
| --- | --- | --- |
| Base background | `--bg` | `#080C1C` |
| Panel | `--surface` | `#11172B` |
| Primary text | `--ink` | `#E8EDFF` |
| Secondary text | `--muted` | `#A0AEC9` |
| Measurements and focus | `--cyan` | `#67DDFF` |
| Navigation and accents | `--purple` | `#B6A5FF` |
| Available | `--green` | `#79E7BD` |
| Incident | `--red` | `#FF9BB0` |
| Control borders | `--control-border` | `#586784` |

Use tokens rather than inventing shades for individual pages.
`grid-background.png` covers the viewport on internal pages and admin.
The landing page uses the full-screen `login-background.png` map-and-servers
illustration. Panels are approximately 97% opaque so text does not sit directly
over bright grid lines.

Cyan highlights measurements and focus. Violet guides actions and navigation.
Green, pink and amber communicate states. Avoid glowing text, blinking and large
saturated areas beneath data.

## Typography

| Element | Font | Weight | Indicative size |
| --- | --- | --- | --- |
| Main headings | Michroma | 400 | 20–28 px |
| Section headings | Michroma | 400 | 15 px |
| Menus, tabs and logout | Michroma | 400 | 14 px |
| Body | Inter | 400 | 14 px |
| Captions and metadata | Inter | 500 | 12–14 px |
| Labels and buttons | Inter | 600 | 12–14 px |
| Metric values | Inter | 600 | 30–36 px |
| Occasional emphasis | Inter | 700 | Context-dependent |

Michroma keeps its native weight; `font-synthesis: none` prevents synthetic bold.
Use short menus with enough room rather than shrinking text to fit an action.
Keep logout on one line alongside its exit icon, including in French.

Use `tabular-nums` for numbers. Units are smaller and separated from values.
Inter serves tables, help, forms and longer diagnostics. Latin/Latin Extended
WOFF2 fonts and their OFL licences are served locally.

French interface copy, including errors and specimens, follows Swiss Romand
rules: no space before `: ; ? !`, apostrophes for thousands. Preserve explicitly
defined data formats. All project documentation is written in English.

## Shell and navigation

```text
sidebar                         264 px on large screens
  identity                      icon and product logo
  navigation                    Probes / Incidents / Invitations / Platform
  footer                        profile, FR/EN and logout with icon
shell                           left offset matches sidebar
  main                          useful heading, actions and content
```

- Intermediate width: 248 px. At 720 px and below, navigation moves above content;
  profile and logout remain accessible.
- Sidebar width and content offset share `--sidebar-width`.
- No top header repeating the domain or HTTP monitoring description.
- No monitoring heading above navigation.
- No Styleguide navigation link. Authenticated users can open `/styleguide/`
  directly for design reference.
- Violet fill and a side marker indicate the active item. Icons complement
  readable labels rather than replacing them.
- Invitations and Platform appear only for platform administrators. Probe editing
  actions require editor rights. Enforce these restrictions server-side as well.
- Group profile, language and logout into one quiet footer block. Clicking the
  identity opens the profile. FR/EN saves immediately; no Auto button.
- When a display name is available, omit the secondary email line. Keep the email
  in the identity tooltip and profile. Otherwise show username and email without
  wrapping, using an ellipsis when the sidebar cannot fit the full value.

## Pages and content

Start each page with an informative heading and, where useful, a back link and
one primary action. Keep useful technical labels beside their values instead
of adding a redundant header.

The dashboard shows status, latest observation, uptime and coverage. Probe
pages organize overview, failed checks, alerts and settings. There is no location tab.

The landing page stays minimal: logo, illustration, Google and local sign-in.
Do not reintroduce slogans, decorative badges or promotional copy.

## Forms

- Stable probe sections: General, Checks, HTTP request, Expected response and Alerts.
- Associate every input with a label. Help text explains only useful constraints.
- Place errors near fields, with a short summary when saving fails.
- Preserve entered values on errors and saved secrets when their fields are blank.
- Use consistent sections in read and edit views.
- Probe and connector forms have an explicit save action and cancellation where
  appropriate. Clearly distinguish entered configuration from saved configuration.
- Account role selectors save immediately on change and show server feedback.
  Keep deactivation and deletion separate from permission editing.
- Request tests explicitly use saved configuration.

## Buttons

| Purpose | Application class | Treatment |
| --- | --- | --- |
| Primary action | `.button.primary` | Bevelled violet |
| Secondary action | `.button` | Dark glass |
| Deletion | `.button.danger` | Pink variant |
| Compact action | `.button.small` | Same treatment, reduced height |

Use Inter 600, a top highlight, short shadow and pressed motion. One primary
action per group. Disabled buttons must not react to hover as active ones do.
Keep cyan focus visible and respect `prefers-reduced-motion`.

Shared styles cover landing, invitations and admin. Logout is a Michroma
navigation item rather than a raised primary button. The styleguide’s cyan-outline
variant is a reference, not another hierarchy to introduce throughout the app.

Links navigate; buttons act. Provide accessible names for icon actions and hide
decorative SVGs from screen readers. State changes use POST forms with CSRF.

## Tables and histories

- Short headers, aligned numbers and consistent dates.
- Status labels accompany colours.
- Filters stay near tables and remain selected during pagination.
- Scroll containers on narrow screens; never crush columns until unreadable.
- Real keyboard-accessible links to details. Avoid row-wide `onclick` handlers
  that interfere with other actions.
- Accurate empty states: no probes, no incidents or no failures matching the filter.

## States, charts and metrics

Use Available, Unavailable, Paused and Unknown. Colour supplements labels and
indicators. Never equate stale observations with availability. Missing coverage
is unknown data.

Charts use cyan on dark panels, a quiet grid, explicit units and periods. Do not
invent points or imply that unobserved periods were healthy. Label fictitious
styleguide specimens. Daily history has a legend and accessible colour descriptions.

## Messages, confirmation and accessibility

- Short concrete messages, such as “Probe saved”, “Delivery rejected” and
  “Invitation expired”.
- Distinguish message acceptance from actual receipt.
- Confirm deletion on a dedicated page describing its effect on history and
  offering cancellation. Pausing preserves data. Account deletion preserves probes.
- Do not substitute `alert()` or `confirm()` for designed interactions. Any future
  modal must manage focus, keyboard return and dismissal; this alone does not
  justify a framework.
- Target at least 4.5:1 for body text and 3:1 for necessary control indicators.
  Measured colour pairs are documented at `/styleguide/`.
- Check actual contrast with transparency and gradients, focus, wrapping and
  mobile rendering. These rules do not constitute a compliance audit.

## Adaptation from eFBL Admin

Retained principles include the shared shell, reusable components, tokens,
consistent buttons, form sections, paginated tables and semantic states.
Source-specific choices are not carried over: FIATA palette, topbar, business
layouts, roles, crispy-forms, Bootstrap, HTMX, Alpine and transport integrations.

The local visual reference compares components. CSS files determine rendering;
this document records decisions to preserve as the application evolves.

## Logo

The supplied logo is shared by landing, navigation and admin through
`monitor/templates/monitor/logo.html`. The production asset is
`monitor/static/monitor/brand/logo.svg`, with outlined Michroma lettering and
trimmed right-side transparent padding. Colours and proportions are preserved;
the original is retained alongside it.

Preserve the 1068:240 ratio, automatic height and maximum width of 100% of the
container. The logo fills the sidebar’s usable width; the landing page may show
it up to 420 px wide (340 px on small screens). Do not duplicate the product name
beside it or use `object-fit: cover`. Alternative text uses the product name.

Platform administrators may override this built-in logo in Platform settings.
Use the shared `monitor/logo.html` include with its context-provided URL and
intrinsic dimensions. Preserve proportions with `object-fit: contain`; constrain
header height for square and tall logos instead of cropping or stretching them.
The settings preview uses the same component on a dark surface. Keep the default
asset as the fallback and retain immutable URLs for previously sent emails.

The product name is a configurable text value. Use `product_name` from the shared
context in page titles and accessible labels. A renamed installation without a
custom logo displays a Michroma text wordmark; uploaded logo artwork remains
unchanged. Email text fallbacks and invitations also use the configured name.
