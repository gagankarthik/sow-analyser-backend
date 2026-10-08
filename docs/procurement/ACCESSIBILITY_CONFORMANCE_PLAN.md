# Blue-IQ Govern: accessibility conformance plan (WCAG 2.1 AA, Section 508)

Prepared 2026-10-08 for the customer Digital Accessibility Services and Purchasing.

**Status:** Govern targets WCAG 2.1 Level AA. **No conformance claim is made yet.** No Accessibility Conformance Report (ACR/VPAT) has been published and no third-party audit has been done. This plan explains how we will produce both before the ADA Title II compliance date for large public entities, **26 April 2027** (DOJ interim final rule, April 2026; see `COMPETITIVE_ANALYSIS.md` [67]). the customer purchasing also requires conformance to the customer's Minimum Digital Accessibility Standards, testing by a qualified party, and a remediation guarantee before go-live ([66]).

## 1. Deliverables

| Deliverable | Format | Status |
|---|---|---|
| ACR on **VPAT 2.5 INT edition** (WCAG 2.1 + Revised Section 508 + EN 301 549), covering the Govern web app | PDF and accessible HTML | Not started |
| Third-party WCAG 2.1 AA audit report and attestation letter | From an independent firm | Not started (vendor to be selected) |
| Internal accessibility test record (per release) | Spreadsheet / CI report | Not started |
| Accessibility roadmap and remediation SLA | In the the customer contract | Proposed below |

## 2. Measures already in the front end (verified in `sow-analyzer`)

| Measure | WCAG SC | Evidence |
|---|---|---|
| Visible focus ring on every interactive element (2px, brand blue, offset) | 2.4.7 | `app/globals.css`, `:focus-visible` rule |
| Skip links ("Skip to main content" / "Skip to content") | 2.4.1 | `components/shell/AppShell.tsx`, `components/landing/MarketingShell.tsx` |
| Page language set (`lang="en"`) | 3.1.1 | `app/layout.tsx` |
| Reduced motion: all duration tokens set to 0 under `prefers-reduced-motion: reduce`; theme transition only when motion is allowed | 2.3.3 (AAA, good practice) | `globals.css`; 39 files reference reduced motion |
| Windows High Contrast / forced-colors handling for hatch patterns | 1.4.11 support | `globals.css` `@media (forced-colors: active)` |
| Contrast-checked tokens: text tokens 17.4 / 9.6 / 6.0 :1; status text ≥ 4.5:1 on soft fills (warning darkened to #A3480A for this); form control borders 3.1:1; matrix-outcome badges 4.75–6.15:1 | 1.4.3, 1.4.11 | `globals.css` comments and token values |
| Matrix outcomes use icon plus word plus colour, never colour alone | 1.4.1 | `globals.css` outcome token table; chart rule in `lib/chart-theme.ts` |
| Minimum target size token 24px | 2.5.8 (WCAG 2.2) | `--target-min` in `globals.css` |
| Drag-and-drop upload and matrix import also have a keyboard-operable file input | 2.1.1 | `UploadDropzone.tsx`, `ImportDialog.tsx` |
| Contract board moves by buttons, not drag | 2.1.1, 2.5.7 | Govern design rule; no drag handlers on the board |
| ARIA in use (≈540 `aria-*` attributes); live regions or status roles in ≈90 places | 4.1.2, 4.1.3 | grep across `app/` and `components/` |
| Accessible primitives (Radix UI): dialogs, menus, tabs with focus management | 2.1.1, 2.4.3, 4.1.2 | `package.json` (`radix-ui`) |
| the customer tenant theme: brand brand red #BA0C2F used for brand chrome, contrast noted | 1.4.3 | `globals.css` tenant brand block |

These are design intentions verified as present in code. They have **not** been tested with assistive technology yet.

**Known risk areas to test first:** charts (Recharts SVG; a "View as table" alternative is the design rule but needs checking on every chart); the contract board and dense tables; the PDF evidence drawer (focus return, page anchors); toasts (`sonner`) and live AI status; PDF and DOCX exports (`jspdf`, `docx`) are not tagged by default, so exported documents are likely **not** accessible; date pickers; reflow of data tables at 320 CSS px.

## 3. Testing plan

| Layer | Tool / method | Scope | Frequency |
|---|---|---|---|
| Automated, components | `@axe-core/react` or `jest-axe` / Storybook a11y addon | Every shared component, every state | Every PR |
| Automated, pages | `@axe-core/playwright` across all routes (signed-in fixtures), zero serious or critical violations gate | All app routes, light and dark themes, the customer theme | Every PR (CI). **Not yet set up**: the front end has no test runner or CI step today |
| Lint | `eslint-plugin-jsx-a11y` | All TSX | Every PR |
| Keyboard-only | Manual script: tab order, focus visible, no traps, all actions reachable, Escape closes overlays, focus returns | Top 12 task flows (§4) | Every release |
| Screen readers | NVDA + Chrome, JAWS + Edge (Windows), VoiceOver + Safari on iPad (required by an the customer user), VoiceOver + Safari on macOS | Top 12 task flows | Every minor release; full pass before each ACR update |
| Reflow and zoom | 320 CSS px width (1.4.10), 200% text resize (1.4.4), text spacing bookmarklet (1.4.12) | All pages | Every release |
| Contrast | Token-level script check; manual check of charts and states | All themes | On token change |
| Forced colours | Windows High Contrast | Key flows | Every release |
| Documents produced | PAC 2024 / Acrobat checker for PDF exports; Word Accessibility Checker for DOCX | Exports and redlines | Before ACR |
| Cognitive / plain language | Error messages and instructions review (3.3.x) | Forms | Before ACR |

**Top task flows:** sign in (incl. SSO), upload agreement, open contract, read findings, view evidence drawer, accept/edit/dismiss a finding, send back, approve, reassign, search/filter the board, leader home and reports (incl. table views), matrix import and edit, export.

## 4. Third-party audit

1. Select an independent firm with higher-education experience. Ask the customer Digital Accessibility Services whether it has a preferred list.
2. Scope: the Govern web app (all task flows in §3), exports, and the sign-in flow. WCAG 2.1 AA plus the 508 functional performance criteria.
3. Output: an issue log with SC mapping and severity, a retest after remediation, an attestation letter, and input to the ACR.
4. Blue-IQ writes the ACR (VPAT 2.5 INT) from the audit results. The ACR states "Supports / Partially Supports / Does Not Support / Not Applicable" honestly, with remarks for each partial item.

## 5. Remediation SLA (proposed for the the customer contract)

| Severity | Definition | Fix in production |
|---|---|---|
| Critical | Blocks a core task for an assistive-technology user with no workaround | 10 business days |
| High | Core task possible only with significant difficulty, or a workaround exists | 30 calendar days |
| Medium | Non-core task affected, or a moderate barrier | 60 calendar days |
| Low | Minor or cosmetic non-conformance | 90 calendar days or next scheduled release |

Plus: an interim workaround communicated to the customer within 5 business days for Critical/High; an accessibility contact; a regression gate in CI; an ACR refresh at least yearly and after major releases; and the customer's right to have remediation verified by a qualified party.

## 6. Timeline to 26 April 2027

| Window | Work | Exit criterion |
|---|---|---|
| Oct 2026 | Add jsx-a11y, Playwright + axe CI gate; internal keyboard and NVDA pass of top flows | CI gate live; internal issue log |
| Nov 2026 | Fix Critical/High from internal pass; chart table views; live regions; 320px reflow fixes; tagged PDF export or accessible HTML alternative | No known Critical issues |
| Dec 2026 | Contract third-party auditor; VoiceOver iPad and JAWS passes | Audit booked |
| Jan 2027 | Third-party audit | Audit report |
| Feb 2027 | Remediate, auditor retest | Retest letter |
| Mar 2027 | Publish ACR (VPAT 2.5 INT) and attestation | ACR delivered to the customer |
| By 26 Apr 2027 | Contractual remediation SLA in force; regression gate live | Compliance date |

If the customer's pilot starts before March 2027, we will provide the internal test record and a dated remediation plan in place of the ACR, and say so plainly.

## 7. Owner

Blue-IQ front-end lead (accessibility owner), with sign-off by the product lead. The contact for the customer is to be named.
