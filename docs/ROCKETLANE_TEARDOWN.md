# Rocketlane teardown: what they build, how, and what Govern should take

Prepared 2026-10-20. Sources: Rocketlane's site and help center, read in the browser, and their public job postings. Where a point about their backend is inferred rather than published, it is marked **(inferred)**.

---

## 1. Is it the same product?

**No. Rocketlane and Govern sit next to each other in the same customer's life, but they do different jobs.**

| | Rocketlane | Blue-IQ Govern |
|---|---|---|
| Category | PSA (professional services automation), plus a client onboarding portal | Contract review and workflow (pre-signature CLM), plus obligation tracking |
| Who uses it | A vendor's delivery team: PMs, consultants, resource managers, finance | A buyer or research office: reviewers, sponsored programs, legal, procurement leaders |
| The unit of work | **Project**: phases, tasks, people, hours, money | **Contract**: clauses, matrix findings, rounds, approvals, signature, obligations |
| Starting point | A closed-won deal or a signed SOW becomes a project | An incoming agreement becomes a review |
| Its AI ("Nitro" vs "Sonar") | Turns a SOW into a project plan; watches emails and calls for risk; writes docs | Reads the agreement, grades it against the matrix, answers questions with citations |

**Where they meet:** a signed SOW. Rocketlane picks up *after* signature to deliver the work. Govern decides *whether and on what terms* to sign. A workforce customer could use both.

**What to copy is their platform discipline, not their feature list.** List views, saved views, bulk operations, permissions, automations, templates, the portal model, and the density and calm of their UI all apply to any serious work-management product. Govern should not grow time tracking, invoicing or staffing.

---

## 2. Feature by feature

Legend: ✅ Govern has it · 🟡 partly · ❌ missing · — not relevant to Govern.

| Rocketlane capability | How they do it | Govern today | Verdict |
|---|---|---|---|
| **Projects list page** | Dense table. Toolbar: view picker, filter, Save view as, clear, Group by, search, column settings, export, one primary button | 🟡 Good table, but 8 filter pills in two rows, fixed quick views, no save | **Adopt.** Being built: toolbar, filter builder, group by, column order, CSV |
| Default + saved views | "My projects", "All projects"; private, everyone, or specific users/groups; only the creator edits; RBAC still governs rows | ❌ Fixed presets only | **Adopt.** Being built: `/views` API (DynamoDB) + view picker |
| Filter builder | Field / operator / value rows; match All / Any | ❌ One pill per field | **Adopt.** Being built |
| Group by | Any field, collapsible, counted | 🟡 Table supports it; contracts didn't use it | **Adopt.** Being built |
| Column customisation | Show/hide plus drag to reorder; sort by any header | 🟡 Hide only | **Adopt.** Being built |
| Export | CSV of the visible columns; >200 rows are emailed | 🟡 Excel via bulk action only | **Adopt** CSV now; **later**, async export by email for large lists (Step Functions → S3 → SES link) |
| Bulk update | Up to 1,000 rows; bottom bar; "Edit fields" side panel with search; "Multiple values" | 🟡 Bulk assign only | **Adopt.** Being built: `POST /contracts/bulk` + edit panel |
| Global search ⌘K | Center palette; categories; attribute filters; Tab preview; open in new tab; copy link | 🟡 Top-bar typeahead, 8 results | **Adopt.** Being built |
| Hotkeys | P projects, H home, N new task; ⌘K navigation | ❌ | **Adopt.** Being built (H, W, C, O, R, U, S, ?, /) |
| Project overview | Status, dates, completion; overdue / at-risk / blocked boards; planned vs actual; goals; last updates | ✅ Contract page: status banner, steps, matrix, blockers, money, activity | **Adapt.** Add counted boards (open blockers, overdue obligations) and a "days vs target" line, as their planned-vs-actual |
| Spotlight | Flag a task, file, chat or view so others act on it | ❌ | **Later.** "Pin a clause or blocker for the counterparty or approver" |
| RBAC | Three levels: account, project, customer; custom permission sets; global vs group access | 🟡 Owner / editor / viewer per document; Govern roles reviewer / leader / admin | **Adapt.** Add office or department groups (their "US vs EU" groups ≈ our colleges and departments) |
| Templates | Reusable delivery playbooks; auto-filled from CRM | 🟡 The matrix is our playbook; SOW drafting in the workforce edition | Keep |
| Automations | Triggers (deal closed, kickoff, delay, low CSAT) → actions (email, Slack, escalate by matrix, CRM push); template- or project-level | ✅ Automations (Settings → Automations): rules in govern-config, time triggers in the hourly sweeper, event triggers in govern-stream; notify / escalate / assign / comment, once per contract per episode | **Built** (`shared/govern/automations.py`, `/automations`). Next: per-rule quiet hours and a run log |
| Customer portal | Branded hub: plan, chat, files, feedback | ❌ Counterparty gets email and redlines | **Later.** A counterparty link to see open clauses and upload a revision (pre-signed S3 + scoped token) |
| Reporting | Utilization, margins, milestone pacing, CSAT; natural-language questions | ✅ Value, bottlenecks, trends, capture; Sonar | Keep; add saved and scheduled reports later |
| AI agents (Nitro) | Plan from SOW, signals from emails/calls, documentation, finance control | ✅ Sonar: review, Q&A, obligations | Keep the focus; Sonar could flag "this revision reopened clause X" |
| Time tracking, resourcing, invoicing, margins | Core PSA | — | **Skip.** Not our product |
| Integrations | Salesforce, HubSpot, Slack, Jira, Zapier, Workato, NetSuite | 🟡 Huron, Workday, DocuSign, Teams | Keep ours; Slack is the next most asked-for |

---

## 3. Why their UI reads "serious" and ours reads "playful"

Evidence: our showcase screens (`/showcase/home`, `/contracts`, `/contract`, `/board`) side by side with Rocketlane's Projects page.

| | Rocketlane | Govern today | Fix |
|---|---|---|---|
| **Density** | ~13px table text, ~36px rows, small header | 16px text, ~58px rows, a lot of air | Compact type scale (13/14px UI, 12px meta); 40px rows by default; page titles 20px, not 28–32px |
| **Page chrome** | Title + one toolbar line; no subtitle sentences | Title, then a sentence explaining the page, then 2 rows of pills | No explanatory sentence under titles; one toolbar row |
| **Voice** | Labels and nouns: "Projects", "Status", "Due date" | Questions and coaching: "1 What is the money?", "Good afternoon, Jordan", "It's time for you to review" | Neutral labels ("Value", "Stuck", "Next step") |
| **Colour** | Grey and white; colour only in status rings and progress | Warm beige canvas, many tinted pills, coloured avatars, blue-filled chips | Neutral cool-grey canvas; colour only for status, tier and SLA meaning |
| **Shape** | Small radii (4–6px), hairline borders, flat | 12–16px radii, big rounded cards, soft shadows | 6–8px radius on cards, 4–6px on controls, 1px borders, no shadows except overlays |
| **Shell** | Slim nav, many modules, workspace switcher | Wide sidebar with big highlighted items and an edition pill | Narrower sidebar (≈220px), 32px items, grouped sections (Work / Insight / Admin) |
| **Status display** | A progress ring icon + word; phase chips | Dots + a sentence; "45d" chips in red and amber | One status column: icon + word; days as plain tabular numbers, coloured only when late |
| **Interaction model** | Keyboard first (⌘K, hotkeys), views, bulk edit | Mouse only; one record at a time | Being built: ⌘K, hotkeys, views, bulk |

These are the main reasons ours looks like a consumer app: big type, rounded cards, explanatory copy and a warm canvas. None of them is a feature gap. They are a design-system choice, so they can be fixed in one pass on the tokens and shared components (section 6).

---

## 4. How their backend works, and our AWS equivalent

Rocketlane's job postings name **Java / Spring, PostgreSQL, Redis, Docker, message brokers, AWS** on the backend, and **React SPA with real-time collaboration** (live documents, spreadsheet-style grids, Kanban) on the frontend. Their help center shows the product behaviour that rides on it.

**Our stack stays Python on AWS with React. No Java, no Redis.** Each of their mechanisms maps to a service we already run or can add:

| Their mechanism | Theirs (inferred) | Govern on AWS (Python, React) |
|---|---|---|
| Saved views, per module, with visibility | A relational table keyed by user and module **(inferred)** | DynamoDB govern-config: `T#tenant#VIEWS / V#module#id`, optimistic `rev`. **Being built** |
| Bulk update up to 1,000 | A batch API + async job for large sets **(inferred)** | `POST /contracts/bulk` (≤200 per call, the client chunks); a later Step Functions map job for very large sets |
| Large export emailed | Background job + email link | Step Functions → CSV to S3 → presigned link by SES (we already have SES in the notifier) |
| RBAC: account / project / customer, groups | Permission sets in Postgres **(inferred)** | Cognito groups (already used for Govern roles) + a groups table in govern-config; enforced in `shared/access.py`, as today |
| Global search across tasks, files, chats | Search service **(inferred)** | OpenSearch (already running for clauses): add a `govern-records` index fed from the contracts stream for server-side ⌘K at scale |
| Real-time collaboration | WebSockets **(inferred)** | AppSync Events, one channel per tenant: the activity stream pushes small "contract changed" invalidations to open boards and contract pages. **Built** |
| Automations, nudges, escalations | Rules engine on a message broker **(inferred)** | Rules in govern-config, evaluated by the existing govern-stream (on change) and govern-sweeper (EventBridge Scheduler, on time); actions through the notifier (SES / Teams) |
| Caching | Redis | Not needed now. DynamoDB reads are single-digit ms, and React Query caches on the client. Revisit only if a hot read shows up in metrics |
| Observability | An observability platform (from postings) | Powertools JSON logs, X-Ray tracing (Tracer is already imported), CloudWatch alarms on DLQ depth and errors |

---

## 5. What went wrong with us

1. **We built features before a system.** Every page invented its own tiles, bars and pills. That's why colour drifted, and why the app feels assembled rather than designed. The design-system components (`KpiTile`, `BarMeter`, `StatusPill`, `OutcomeBadge`) existed but went unused.
2. **We wrote to explain instead of to work.** Subtitles, numbered questions and greetings suit onboarding, not a tool people live in all day.
3. **We optimised for the first visit, not the hundredth.** No saved views, no keyboard, no bulk edit, so every session starts from scratch.
4. **Air instead of information.** Big type and roomy rows make a 13-contract list look like a lot. Enterprise users expect 25–50 rows per screen.

### Found on the live, signed-in app (localhost, 2026-10-20)

| # | Where | What | Fix |
|---|---|---|---|
| 1 | Contract page | A "Website Design & Development" SOW shows **"Other agreement"** and is graded against the generic playbook (Ohio governing law, sovereign immunity "missing"). The organization is on the **Campus** edition, which has no SOW type | When the classifier is confident a document is an SOW or MSA, say so ("Looks like a statement of work: switch edition or add the type"), instead of silently grading it as "other" |
| 2 | Contract header | "Just arrived" chip while the banner says **11 days · overdue** | The chip shows the state *label* ("In review"); "just arrived" is only for the first day |
| 3 | Contract page | Three calls to assign a reviewer (header, banner, side panel); the side-panel chip overflows its card | One primary in the banner; the others become plain text ("No owner") |
| 4 | Contract tabs | Tabs wrap to two lines at laptop width | Single row with horizontal overflow, or move Activity into a right-side feed |
| 5 | Everywhere | Titles in ALL CAPS straight from the document | Display in title case when the source is all caps; keep the original in Details |
| 6 | Every section | A sentence of explanation under each heading | Remove; put the help behind an info tip |
| 7 | Home | Each attention row takes three lines; the money bar is a solid hatched block when all value is held up | One line per row (title · party · days · next step); a bar only when there are two or more parts |
| 8 | All pages | The dev server was serving stale CSS (the old rainbow Sonar ring still showed) | Restart `next dev` after pulling these changes |

---

## 6. Plan

**In progress now (not committed; for your review):**
- Contracts list rebuilt on their model: view picker, filter builder, Save view as, Group by, column order, density, CSV, bulk edit panel. Backend: `/views`, `/contracts/bulk`, `/govern/people`
- ⌘K palette and hotkeys

**Next (design system pass, so every page reads serious):**
- Tokens: neutral cool-grey canvas, 13/14px UI scale, 6–8px radii, 40px rows, hairline borders, no decorative shadows
- Remove explanatory subtitles, greetings and numbered questions; neutral labels
- Shell: narrower sidebar with grouped sections; a compact top bar
- One status vocabulary (icon + word), plain tabular numbers

**Later:**
- ~~Automations rules (EventBridge Scheduler + stream)~~ built: Settings → Automations
- ~~Counterparty portal link (pre-signed S3 + scoped token)~~ built: Actions → Share with counterparty
- Server-side search index in OpenSearch
- ~~Real-time board updates over AppSync~~ built: AppSync Events, per-tenant channel (GOVERN_ARCHITECTURE.md §2.3.1)
- Department groups in RBAC
- Async large exports by email
