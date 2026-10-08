# Blue-IQ Govern: competitive analysis for the the customer university opportunity

Prepared 2026-10-08 for the Govern team ahead of the the customer (the research enterprise / Technology Commercialization / Legal Affairs) follow-up demo.
Scope: research-administration and tech-transfer systems, enterprise CLM and AI contract-review tools, and what that means for Govern's product, design system and go-to-market at a public university.

**How to read citations.** Numbers in square brackets, such as [12], point to the Sources list in section 9. A claim marked **(unverified)** could not be confirmed from a primary or reputable secondary source. A claim marked **(vendor claim)** comes from a competitor's own marketing or comparison page. Treat those as positioning, not fact. Pricing from aggregators such as Vendr is marked **(third-party estimate)**.

---

## 1. Executive summary

the customer is buying across three markets that rarely meet:

1. **Research-administration suites.** the customer has already chosen one: Huron Research Suite. Huron IRB is live, Huron Safety went live in March 2026, and IACUC is scheduled for 2027 [1]. Huron's Agreements module is a solid repository and routing tool, but it does not do AI matrix review today [2][9]. Peer contracts show it is expensive: Purdue approved $6.8M over five years [3].
2. **Enterprise CLM.** Ironclad, Icertis, Agiloft, DocuSign IAM, Sirion, Conga, and Workday Contract Intelligence (formerly Evisort). All moved to "agentic" AI playbook review in 2025–26 [27][28][32][33][40][45]. They are built for corporate legal and procurement, not for sponsored research or licensing. Typical spend is $40k to over $250k a year, with long implementations (third-party estimates [39][83]).
3. **AI review point tools.** goHeather, Streamlyne's Lyn and LegalSifter now sell playbook redlining directly to sponsored-programs offices [20][24][25]. This is the real near-term threat to Govern's AI wedge.

**Where Govern wins:** it sits *beside* Huron and Workday rather than replacing them. This is a pattern the customer's Big Ten peer Michigan already runs: Ironclad CLM is integrated with its Huron/Click-based eRPM, and status flows back to the research system [7]. Govern adds four things the incumbents do not:
- a deterministic, versioned the customer matrix review that covers licensing and option agreements, not only sponsored research;
- a plain-language "where is it stuck" view for non-technical leaders;
- current vs potential vs held-up value;
- time to value measured in weeks, at a price below enterprise CLM.

**What could lose the deal is not features but procurement readiness.** the customer will expect:
- a WCAG 2.1 AA conformance report and an accessibility test record [66];
- a HECVAT;
- Shibboleth/InCommon SSO [68];
- a clear answer on the external AI processor (OpenAI).

The top priorities are to close those, ship tracked-changes redlines and a Word add-in, and harden the obligation and licensing-income tracking that tech-transfer offices get from Wellspring and Inteum [22][23].

---

## 2. the customer context that shapes the competition

| Fact | Implication for Govern | Source |
|---|---|---|
| the research enterprise is moving research administration to Huron Research Suite. IRB is live (2025), Safety/IBC went live on 30 Mar 2026, and IACUC/Animal Operations are due in 2027. The public transition page does not list a date for Contracts & Agreements. | Huron Agreements is a *future* the customer module, which leaves a window of several years. Position Govern as the review and visibility layer that will feed Huron Agreements when it arrives. | [1][81] |
| Workday HCM and Finance/Supply Chain are live at the customer. Supplier contracts from MediTract and OnBase are loaded into Workday. | Spend data and supplier contracts already flow into Workday, so Govern should *read* from Workday, not compete with MediTract on procurement CLM. | [73]; go-live date Jan 2021 per the customer IT search excerpt [74] **(unverified: page fetch failed)** |
| the customer's SSO uses Shibboleth over InCommon, with "Cooperative Authentication" bridging to Microsoft Entra ID. | Govern must federate over SAML with the customer's Shibboleth IdP (Cognito SAML is planned in the architecture), and should show it can consume eduPerson attributes for roles. | [68] |
| the customer purchasing requires WCAG 2.1 AA, conformance to the customer's Minimum Digital Accessibility Standards, testing by a qualified party, and a remediation guarantee before go-live. | An accessibility conformance report (VPAT 2.x/ACR) and a third-party audit are entry tickets. | [66] |
| The DOJ ADA Title II rule makes WCAG 2.1 AA mandatory for public entities. An April 2026 interim final rule moved the large-entity deadline to 26 Apr 2027. | the customer will be in compliance mode during the 2026–27 purchasing cycle and will scrutinise new vendors hard. | [67] |
| the customer's industry-sponsored research uses fixed IP options: negotiated license; non-exclusive royalty-free license for a 10% Technology Access Fee (TAF, minimum $6,000); assignment for a 25% TAF (minimum $15,000). the customer keeps a royalty-free research-use right. | These are ready-made matrix rows, and a demo-winning detail. Govern should check TAF math and option choice automatically. | [75] |
| Technology Commercialization: about 400 invention disclosures a year and about 300 active licenses. | Licensing obligations (royalty reports, diligence milestones) are a real post-signature workload. | [76] |
| the customer's brand system is brand (Lakeshore UX). It is open to vendors, built to WCAG 2.1 AA, with brand red #BA0C2F and Gray #A7B1B7. | Supports an the customer-themed skin (see section 6). | [69] |

---

## 3. Competitor profiles

### 3A. Research-administration and tech-transfer systems

#### Huron Research Suite: Agreements module (the incumbent at the customer)
- **Positioning and buyer.** A 10-module eRA suite (Grants, Agreements, COI, IRB, IACUC, Research Analytics, Export Control, ECC, Animal Ops, Safety) sold to VPRs and research CIOs. Huron says it serves "nearly 70 R1 universities" [2].
- **Agreements capabilities.** A single repository for research contract types, including outgoing subawards, NDAs, DUAs and MTAs. It covers internal and external routing, negotiation tracking, logging of third-party communications, configurable milestone and deadline notifications, and electronic execution [9] (Huron brochure, via search excerpt; direct fetch returned 404).
- **Integrations.** Data import, web services, intermediary tables and the "Click Connector" store-and-forward interface, with suite-wide data sync via "CPIP" [10]. At UVA and the Universities of Wisconsin it runs alongside Workday HCM and Financials, and Workday Grants Management handles post-award finance [4][5].
- **AI.** No Agreements AI review feature is documented publicly. Huron's investor materials describe "AI-enabled research administration tools" focused on post-award quality control, and an AI partner ecosystem that includes Anthropic, Microsoft and AWS [8] (search excerpt). Huron's 2026 Hippocratic AI partnership is healthcare-only [77].
- **UX.** Workflow forms ("SmartForms") with a state-based project workspace, inherited from the Click Commerce platform **(unverified detail)**. UVA's selection committee called it "the most intuitive user interface" among the systems it compared [4].
- **Price.** Purdue paid $4.68M in subscription fees over five years plus $2.14M in implementation services, about $9.9M all-in including internal costs, for the full suite [3]. The Universities of Wisconsin paid Huron at least $51M across consulting and software in 2019–23 [78].
- **Weaknesses.** Streamlyne claims Huron involves heavy services, upgrade regression work, and reporting fed by "overnight feeds" [19] **(vendor claim)**. Huron is sunsetting the legacy Click-based "eResearch" platform by the end of 2031 [6] (via search excerpt; direct fetch blocked). That forces migrations and keeps Huron's attention on platform moves, not AI review.
- **Implication.** Govern should not compete with Huron. It should complement it (see section 7).

#### Kuali Research (Kuali Coeus lineage, Negotiations module, Kuali Build)
- SaaS eRA descended from the open-source Kuali Coeus. Its **Negotiations** module tracks the status of awards and unfunded agreements (MTAs, NDAs, DUAs) from receipt to execution [11]. It is a status log, not clause analysis.
- **AI.** Kuali GrantRisk (March 2025) scores sponsor-risk signals from SAM.gov data [12]. At Kuali Days 2026 Kuali presented an "AI Gateway" and "AI Connector" for governed AI across campus workflows [13]. No contract-clause AI is documented.
- **Price.** Quote-based and per-module. Example: Coventry University's annual renewal was £59,466 including VAT [14].

#### Cayuse (and the Evisions question)
- Evisions sold Cayuse's research business to Quad Partners in 2017, and Cayuse has been independent since. **No Cayuse–Evisions–InfoEd consolidation in 2024–26 was found.** The last Cayuse acquisition Tracxn lists is iMedRIS (2021) [15][16].
- Cayuse is winning in our region: the home state University launched Cayuse Sponsored Projects in March 2025 [17].
- **AI.** No public Cayuse contract-AI feature found. Streamlyne says it knows of "no directly equivalent capability" [18] **(vendor claim)**.

#### InfoEd Global
- More than 20 modules. Its Agreements module handles NDA, MTA and DUA submission, negotiation status tracking and captured correspondence; Clemson went live in 2025 [21]. No AI review was found, and no 2024–26 ownership change was found **(unverified)**.

#### Streamlyne Research and Lyn
- A unified eRA covering proposals, awards, negotiations and compliance. Its **Lyn** AI "redlines an agreement against your institution's own policy language and names the policy it reasoned from" [20]. Streamlyne markets HECVAT and SOC 2 Type II availability [20]. It claims 130+ BI reports and staff-configurable forms [18] **(vendor claim)**.
- **Implication.** Streamlyne is the closest eRA analogue to Govern's AI review. Its "cite the policy" framing is the right trust pattern.

#### Click Commerce / Huron legacy
- Click Commerce's research portal is the root of the Huron platform. Michigan's eRPM runs on it and is being retired by 2031 [6].

#### Tech-transfer systems: Wellspring and Inteum
- **Wellspring** (Sophia, now **Evolve**) covers licenses, patents and portfolio for technology-transfer offices. Evolve advertises "automatic extraction of agreement terms" and a Partner Portal for "tracking financial and non-financial commitments, submitting reports" [22].
- **Inteum** (Minuet) has more than 400 installations. It was acquired by Merit Holdings in 2025, and Merit bought FirstIgnite (AI licensee matching) in February 2026 to build an "AI-powered" tech-transfer suite [23] (via search excerpt; **partially verified**).
- **Implication.** Licensing obligations, royalty reporting and diligence tracking are table stakes for the licensing director's team. If the customer's Technology Commercialization office already runs Wellspring or Inteum (**unknown, ask**), Govern must feed it or link to it, not duplicate it.

#### AI-native tools sold to sponsored programs offices
| Tool | What it does | Evidence |
|---|---|---|
| **goHeather** | Playbook check of sponsored research, clinical trial and short-form agreements (CDA/MTA/DUA); a Word add-in on AppSource; tracks redlines across rounds, "every change in a single list"; uses models from OpenAI, Anthropic, Google and Meta; names NYU as a customer. | [24] **(vendor claim)** |
| **LegalSifter** | AI plus expert service for universities (SRAs, procurement); case studies claim review time "cut in half" and "75%" faster SRA review at an unnamed large public university. | [25] **(vendor claim)** |
| **Streamlyne Lyn** | See above. | [20] |

**Symplectic Elements** is a research-information and faculty-activity system, not a contracts tool, so it is not relevant to this deal.

### 3B. Enterprise CLM and AI document extraction

| Vendor | 2024–26 corporate events | AI launches | Notes |
|---|---|---|---|
| **Workday Contract Intelligence (Evisort)** | Workday closed the Evisort acquisition on 8 Oct 2024 for $311M cash [26]. | Evisort became available through Workday in March 2025 [27]. The Contract Intelligence and Contract Negotiation agents followed. A Custom AI Model Library launched in October 2025 with more than 120 pre-built models, refinable by "simply providing feedback", with no code [28][29]. | Biggest strategic risk at the customer: same vendor as the customer's ERP. Aimed at procurement, HR and sales contracts, not research or licensing. |
| **DocuSign IAM** (with Lexion) | Lexion was acquired on 31 May 2024 for $154.0M [30]. | Navigator repository, AI Contract Assist (playbook review), intake via email, Teams or Slack [31]. Iris AI assistant and agents (playbooks, obligation monitoring, approvals) announced in May 2026 and generally available from July 2026, North America and English only [32]. | E-signature is already common on campus. CLM is weaker. |
| **Ironclad** | Passed $200M ARR [35]. | Jurist redlining agent with playbooks and precedents: tracked changes with reasoning, risk level and source [36]. A recipe for "AI Playbook for Non-Legal Users" [37]. Agent wave in November 2025, Ironclad Assistant in March 2026 [33][34]. | Already used for sponsored projects at **Michigan**, integrated with eRPM [7]. The most direct CLM precedent at a Big Ten school. |
| **Icertis** | Acquired Dioptra in November 2025 (redlining, automated playbook creation) [41]. | Vera AI and Vera Agents in September 2025 [40]. 8.7 UX: "My Work" home, deviation approvals with highlighted text, email approvals with action buttons [42]. New Vera-powered experience in June 2026 [43]. | Enterprise-only. Commonly quoted above $200k a year (third-party estimate [83]). Reviewers cite a steep learning curve [44] (competitor blog). |
| **Agiloft** | Taken private by KKR in May 2024. Acquired Screens (AI review and redlining) in January 2025 [45]. | AI Obligation Management in December 2025 [46]. Astra contract-AI platform with a free tier, generally available July 2026 [47]. | Universities use it: Saint Louis University runs research contracts in Agiloft [48]. Reviews cite a dated UI and configuration complexity [49] (competitor page). |
| **Conga** | Completed acquisition of the PROS B2B business in February 2026 [50]. | Focused on CPQ, pricing and CLM. | Revenue-ops focused, so not a fit for research. |
| **LinkSquares** | Independent; on the Inc. 5000 for 2026 [51]. | "Smart Values" extraction with confidence icons: green for high confidence, blue for low, pencil for human-edited [52]. | A good provenance and confidence pattern. |
| **Sirion** | Haveli majority investment completed February 2026 [53]. | Publishes a clause-extraction benchmark claiming error rates below 6% [54] **(vendor claim)**. Agentic CLM with "conversational contracting" [55] (date not verified). | Post-signature obligations are its strength. |
| **SpotDraft** | Raised $54M Series B in February 2025. Qualcomm-backed round in January 2026, valuation near $400M [56]. | VerifAI: plain-English guidelines with source highlighting for each guideline, and on-device AI [57]. | Mid-market. |
| **Juro** | — | Juro AI. Consumer-grade UI for non-legal users [59]. | Unlimited users. Median buyer about $31k a year (third-party estimate [58]). |
| **Leah** (formerly ContractPodAi) | Rebranded January 2026. About 40% of revenue now comes from outside CLM [60]. | Agentic platform. | Broadening away from CLM. |
| **Luminance** | Raised $75M Series C in February 2025 [61]. | "Lumi Go" autonomous negotiation; "Panel of Judges" model [61]. | Legal-team tool. |
| **Spellbook** | Raised $50M Series B in October 2025 [62]. | A Word-native review, draft and playbook tool, plus "Associate" agent. | A per-seat Word tool. |
| **Harvey** | — | Word add-in with pre-defined acceptable and unacceptable language, and Vault bulk review [63]. | Law-firm and enterprise legal. Expensive per seat **(third-party estimate)**. |

**Integration footprint (the customer-relevant).**
- Ironclad: Word add-in, Salesforce, DocuSign and a broad iPaaS catalogue [38]. No native Workday or Huron connector was found publicly.
- Workday Contract Intelligence: native to Workday.
- DocuSign IAM: Teams and Slack intake [31].
- **None of the enterprise CLMs publish a Huron Research Suite connector. That gap is Govern's to own.**

**Tech-stack signals.** Ironclad uses React **(unverified, job and tech-stack aggregator)**. DocuSign's "Olive" design system is React, with 74 components that are WCAG 2.1 AA compliant [71]. Workday's Canvas is an open-source React design system with a public token repository [70]. No public design system was found for Ironclad, Icertis, Agiloft, Sirion or the eRA vendors. Icertis's "design system" by Blend is for its marketing site, not the product **(verified as marketing-site scope)**.

---

## 4. Market map

```
                    RESEARCH / LICENSING SPECIFIC
                               ▲
   Wellspring · Inteum         │        goHeather · LegalSifter
   (OTT portfolio, royalties)  │        Streamlyne Lyn
                               │        (AI review for SPOs)
   Huron Agreements ·          │
   Kuali Negotiations ·        │   ★ Blue-IQ Govern
   InfoEd · Cayuse             │   (the customer matrix review + workflow
   (system of record,          │    + leader visibility, beside Huron/Workday)
    routing, status)           │
 RECORD / WORKFLOW ◄───────────┼───────────► AI REVIEW / INTELLIGENCE
                               │
   Agiloft · Conga ·           │   Ironclad Jurist · Icertis Vera ·
   DocuSign CLM                │   Workday Contract Intelligence · Sirion
   (configurable CLM)          │   Spellbook · Harvey · Luminance · SpotDraft
                               │
                               ▼
                    GENERIC COMMERCIAL / CORPORATE LEGAL
```

**Govern's slot.** Research- and licensing-specific *and* AI-first, but with the workflow and portfolio visibility the point AI tools lack. goHeather and Lyn review documents but give no portfolio "where is it stuck" or value view. Huron and Kuali track status but do not read the document. Enterprise CLMs do both, but for corporate contracts, at enterprise price and implementation length.

---

## 5. Feature comparison on the customer's requirements

Legend:
- ● = documented capability
- ◐ = partial, or needs configuration or services
- ○ = not found
- ? = not verifiable

Govern's column reflects the code described in `GOVERN_ARCHITECTURE.md` and `GOVERN_DELIVERY_PLAN.md`. Items marked "built, not live" are coded but wait on hardening, the customer credentials or security review.

| the customer requirement | **Govern** | Huron Agreements | Kuali Negotiations | Streamlyne + Lyn | goHeather | Wellspring Evolve | Ironclad | Icertis | Agiloft | Workday CI (Evisort) | DocuSign IAM |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Matrix/playbook review with standard, fallback and unacceptable positions | ● deterministic rules, versioned matrix, Excel import | ○ [9] | ○ [11] | ● cites policy [20] | ● compliant/non-compliant [24] | ○ (term extraction only) [22] | ● preferred/fallback [36] | ● [41][42] | ● Screens [45] | ● [28] | ● [31][32] |
| Beneficial-term tagging, not only risk | ● | ○ | ○ | ? | ? | ○ | ○ | ? | ? | ? | ? |
| Research and licensing clause types (publication, background IP, royalties, sovereign immunity, export control) | ● 10 built-in types | ◐ metadata only | ◐ | ● | ● research only [24] | ◐ licensing data | ◐ needs custom playbook | ◐ | ◐ | ◐ custom models | ◐ |
| Escalation office per clause | ● | ◐ routing | ○ | ? | ○ | ○ | ● approvers per clause [36] | ● | ● | ? | ● |
| Tracked-changes redline export or Word add-in | ○ **gap** (suggested language only) | ○ | ○ | ● | ● Word add-in [24] | ○ | ● [36][38] | ● | ● | ● | ● [31] |
| Workflow with owner, days in stage and waiting-on | ● | ● routing and states | ◐ status log | ● | ◐ rounds only | ● | ● | ● | ● | ● | ● |
| Blocker list and one recommended next step | ● | ○ | ○ | ○ | ○ | ○ | ◐ | ◐ guided workflows [42] | ○ | ◐ | ◐ agents [32] |
| Bottleneck view by stage and by waiting-on | ● | ◐ via Research Analytics [2] | ◐ reports | ● BI reports [18] | ○ | ◐ | ● dashboards | ● | ● | ● | ◐ |
| Spend and value: current vs potential vs held-up | ● | ○ | ○ | ○ | ○ | ◐ license income | ◐ | ◐ | ◐ | ◐ spend in Workday | ○ |
| Licensing income and obligations after signing | ◐ built, sprint 3 | ◐ milestones | ○ | ? | ○ | ● partner portal [22] | ● | ● | ● [46] | ● | ● agents [32] |
| Leader-simple home view | ● three-question home | ○ | ○ | ? | ○ | ? | ◐ | ● "My Work" [42] | ○ dated UI [49] | ◐ | ◐ |
| Huron integration | ◐ built, not live | — | ○ | ○ | ○ | ? | ◐ custom (Michigan eRPM) [7] | ○ | ○ | ○ | ○ |
| Workday integration | ◐ built, not live | ● [4][5] | ? | ? | ○ | ◐ | ◐ iPaaS | ● | ● | ● native | ◐ |
| DocuSign / e-signature | ◐ webhook built | ● e-sign [9] | ? | ? | ○ | ? | ● | ● | ● | ● | ● native |
| SSO over Shibboleth/InCommon | ◐ Cognito SAML planned | ● | ● | ? | ? | ? | ● SAML | ● | ● | ● | ● |
| HECVAT / SOC 2 / VPAT on file | ○ **gap** | ? | ? | ● HECVAT, SOC 2 [20] | ◐ "25+ controls" [24] | ? | ? | ? | ? | ? | ? |
| Time to value for about 2,500 agreements a year | weeks (target) | 13 months at UVA [4]; 28 months for Purdue's full suite [3] | months | months | days | months | months | 6–12 months reported [44] | months | months | months |

**Readout.** Govern is the only entry that combines review against a university's own matrix, beneficial terms, blockers with a next step, and a value pipeline. Its exposed flanks:
- **Redlines.** Everyone else produces tracked changes or works inside Word.
- **Procurement documentation.** Streamlyne already advertises HECVAT and SOC 2.
- **Post-signature licensing obligations.** Wellspring, Agiloft and DocuSign agents ship this today.

---

## 6. UI/UX and design-system lessons

### 6.1 Patterns to adopt
1. **A "My Work" home as the default landing page** (Icertis 8.7: tasks, recent items and owned agreements on one page [42]). Govern's leader home should be this, phrased as the customer's three questions: *What needs my attention / What is stuck and why / What is the money.* Reviewers get a separate "My queue" home that defaults to their own items.
2. **Deviation shown in place, with the source text highlighted** (Icertis "deviation approvals with highlighted text" [42]; SpotDraft "source references for each guideline" [57]; Lyn "names the policy it reasoned from" [20]). Every Govern finding should show three things side by side: the contract quote, the matrix position (with matrix version), and the outcome.
3. **One-click preferred or fallback swap** (Ironclad Playbooks: "swap a detected clause with preferred or fallback language with a click" [36]). Pair it with Govern's "send back with suggested language".
4. **Explained AI suggestions**: each edit carries its reasoning, risk level and source (Jurist [36]). Govern's deterministic rules make this easier: show *which rule fired*.
5. **Confidence made visible at field level** (LinkSquares: green for high confidence, blue for not confident, pencil for human-edited [52]). Govern should show three states: AI high confidence, needs check, and confirmed by a named person on a date.
6. **Actionable approvals in email and Teams** (Icertis email approvals with buttons [42]; DocuSign intake through Teams [31]). For the general counsel, the notification *is* the UI. Use Teams Adaptive Cards with Approve or Send back and a deep link.
7. **Version and round history as one list** (goHeather "every change in a single list", including unmarked edits [24]). Govern already has amendment diffs. Expose a "Round 3 vs Round 2: 4 changes, 1 new deviation" summary.
8. **Plain-English rule authoring** (SpotDraft guidelines written "in plain English" [57]; Workday tunes models by "simply providing feedback" [28]). The Excel matrix import is right. Add a plain-sentence description per rule that is shown to reviewers.

### 6.2 Patterns to avoid
- **Form-heavy "SmartForm" wizards and state codes** of the eRA systems. These are what the general counsel called "crude" in the MS Project build. Govern's rule stands: status changes only through actions people take, with no manual status fields and no Gantt charts.
- **Over-configurable admin surfaces** (Agiloft reviews: "it can only be used by someone who knows how to code" [49]). Ship opinionated defaults: the the customer matrix, eight agreement types and default SLAs.
- **Risk heat maps as the first screen.** Enterprise CLM dashboards lead with analytics. Leaders need verbs and named people instead.
- **Autonomous negotiation** (Luminance "Lumi Go" [61]). A public university's counsel will not accept AI sending terms to sponsors unreviewed. Keep the human in the loop as a *selling point*.
- **Colour-only status.** Required by WCAG 1.4.1 and already a Govern chart rule (`lib/chart-theme.ts`).

### 6.3 Recommended Govern design system

Govern already has a reasonable base: Tailwind v4 CSS-variable tokens in `app/globals.css`, Radix/shadcn primitives, and a single chart-theme module. Recommendations:

**Foundations and tokens**
- **Formalise tokens in three tiers** (primitive → semantic → component) in W3C DTCG JSON, compiled to CSS variables. This is the same model as Workday Canvas tokens [70]. It lets one build emit an the customer theme and a default Blue-IQ theme.
- **Add a dedicated *matrix-outcome* semantic set, separate from risk and from brand.** Use colour plus icon plus word, never colour alone, and check contrast for each pair (≥ 4.5:1 for text, ≥ 3:1 for icons and boundaries):

| Token | Meaning | Suggested pairing |
|---|---|---|
| `--outcome-within` | Within matrix | green check, "Within the customer terms" |
| `--outcome-fallback` | Acceptable fallback | teal or blue half-circle, "Acceptable fallback" |
| `--outcome-deviates` | Deviates, needs review | amber triangle, "Needs review" |
| `--outcome-unacceptable` | Unacceptable | red octagon, "Not acceptable to the customer" |
| `--outcome-beneficial` | Beneficial to the customer | violet or gold star, "Good for the customer" (independent badge) |
| `--outcome-missing` | Clause missing | grey dashed outline, "Missing" |

- **SLA tokens** `--sla-on-track / --sla-due-soon / --sla-overdue` mapped to text ("3 days left", "2 days over"), not only amber and red.
- **the customer theme:** support a brand-aligned skin, with brand red #BA0C2F, Gray #A7B1B7, and brand typography where licensed [69]. However, **keep brand red for brand chrome (header, logo bar) and never for primary buttons or risk.** In a review tool, red must mean "unacceptable". brand's own guidance reserves scarlet for calls to action [69], so this needs an explicit exception agreed with the customer Marketing, or a neutral-blue action colour. Raise this in the demo as evidence of design care.

**Core components (beyond the existing primitives)**
- `ContractCard` (board and list): title, counterparty, owner avatar, "Waiting on: Sponsor", days in stage with SLA chip, a single primary action, an overflow menu.
- `FindingRow`: clause type, outcome badge, contract quote with page anchor, matrix position with version, suggested language, and buttons for Accept, Edit and Dismiss with reason.
- `BlockerList` + `NextStepBanner` ("Ready to sign" / "Send back: 3 clauses" / "Escalate to Export Control").
- `ValueTriad` tiles: Current, Potential, Held up. Each tile links to the contracts behind it.
- `BottleneckBar`: a horizontal stacked bar per stage, split by waiting-on, with a data table alternative.
- `EvidenceDrawer`: a side sheet with the PDF page and the highlighted span. It needs to be keyboard-operable and to return focus on close.
- `ConfidenceBadge` (high / check / confirmed by name).

**Data-viz conventions** (extending `lib/chart-theme.ts`)
- Bars and stacked bars for stage and owner counts, and horizontal bars for ranked sponsors. No pies above five slices, no 3D, no dual axes.
- Money is formatted in one place (`lib/format.ts`) with an explicit "includes N contracts with no value" footnote. Never present a silently incomplete total (Requirement 4).
- Every chart has a visible title as a question ("Where is the queue backing up?"), a one-line takeaway sentence, and a "View as table" toggle. The table is the accessible alternative for WCAG 1.1.1 and 1.3.1.
- Use direct labels instead of legends where possible. Order categories fixed as the workflow order, not by size.

**Accessibility (WCAG 2.1 AA, Section 508, the customer MDAS)**
- Test with **NVDA plus Chrome, JAWS plus Edge, and VoiceOver plus Safari (iPad)**. the general counsel's iPad requirement makes VoiceOver mandatory.
- Kanban board: provide a list or table view as the accessible equivalent. Cards move only through buttons, never drag-only. This matches Govern's existing "no drag-and-drop" decision.
- Focus visible ≥ 3:1. Reflow at 320 CSS px (1.4.10), which the delivery plan's 360px target nearly meets, so test at 320. Target size ≥ 24px, and 44px on touch.
- Live regions for asynchronous AI results ("Review complete: 3 items need attention").
- Publish an **ACR on the VPAT 2.5 template (WCAG edition) and a third-party audit letter**. Tie remediation SLAs into the contract, as the customer's language requests [66].
- Run axe-core in CI on every page, with Storybook for components, and add a manual screen-reader pass per release.

---

## 7. Document extraction and CLM best practices

### 7.1 What leaders do
| Practice | Who demonstrates it | Govern today | Recommendation |
|---|---|---|---|
| Layout-aware parsing and OCR for scanned PDFs | Industry standard; Govern uses Textract + pdfplumber + python-docx | ● (two-column handling, header/footer stripping, warnings surfaced: `ENGINE_AUDIT.md`) | Add a per-page OCR quality score. Route pages below a threshold to "needs check" instead of reviewing them silently. |
| Clause segmentation, then classification | Workday's 120+ pre-built clause models [28]; Sirion "1,200 fields" [54] **(vendor claim)** | ● the model labels clause types, and the matrix grades them deterministically | Keep this split: the LLM labels, the code grades. It is explainable and free to re-run. Add a regression set of about 50 the customer agreements with gold labels before go-live. |
| Field-level confidence with thresholds | LinkSquares confidence icons [52]; common practice routes < 0.70 to human review and 0.70–0.89 to a secondary check [82] (vendor blog) | ◐ money is re-read with quotes; no per-field confidence in the UI | Store confidence and evidence per extracted field. Expose the three-state badge. Use per-field-type thresholds that are tunable by admins. |
| Field-level provenance (quote, page, span) | SpotDraft source highlighting [57]; Jurist "supporting source" [36]; Lyn policy citation [20] | ● verbatim quotes for money; ◐ elsewhere | Require a quote plus page plus character span for *every* matrix finding and extracted obligation. Reject findings whose quote is not in the text (extend the existing money check). |
| Human review queue | Industry pattern [82]; Agiloft obligation review [46] | ○ | Add an "Extraction check" queue for low-confidence fields, missing values and OCR warnings. A reviewer confirms with one keystroke. The confirmation is logged with name and time, and it feeds evaluation metrics. |
| Learn from corrections | Workday model refinement through feedback [28] | ○ | Log every reviewer override (outcome changed, value edited). Do not fine-tune models. Use the overrides to update phrase lists and thresholds, and report accuracy per clause type monthly. |
| Measured accuracy | Sirion publishes a benchmark [54] **(vendor claim)** | ○ public number | Publish Govern's own precision and recall per research/licensing clause type on a disclosed test set. Universities respond to evidence. |
| Obligation management after signing | Agiloft AI obligations [46]; DocuSign agents monitor obligations [32]; Wellspring partner portal [22] | ◐ built (sprint 3) | Extract obligation type, owner, due date or recurrence, trigger, evidence quote, and system of record. Send 30-, 14- and 0-day alerts to the owner via Teams and email. Show obligations on the leader home under "What needs my attention". |
| Versioned playbooks | Common in CLM | ● matrix versioning | A differentiator for audit. Show "reviewed against Matrix v3 (2026-10-09)" on every finding. |

### 7.2 Research- and licensing-specific extraction targets
Use these to seed obligation extraction and the licensing-income report:
- **License or option agreements:** upfront fee, annual minimums, running royalty percentage and base, sublicense income share, milestone payments with triggers, equity percentage and anti-dilution, diligence milestones, patent-cost reimbursement, reporting cadence, field of use, territory, exclusivity.
- **Sponsored research:** budget and payment schedule, publication review period (days), confidentiality term, background and foreground IP, the TAF option selected (10% or 25%, with minimums [75]), sponsor reports, and flow-down terms.
- **Public university specifics:** indemnification limits, the home state governing law, sovereign immunity, export control, use of name.

---

## 8. Where we can win the customer's business

### 8.1 Wedges
1. **Complement Huron, don't replace it.** Michigan already proves the "CLM beside Huron/Click" model: Ironclad handles negotiation and pushes status into eRPM [7]. the customer's Huron Agreements is not on the published transition timeline yet [1]. Pitch Govern as the AI review and visibility layer **now**, which later reads from and writes to Huron Agreements (record ID on every contract, findings pushed back). This lowers political risk for the research enterprise-IT.
2. **Licensing and OTT matrix review is unoccupied.** The AI tools sold to research offices market to sponsored programs (goHeather [24], Lyn [20], LegalSifter [25]), not licensing. the licensing director runs licensing. Lead with license and option agreements, TAF option checks [75], royalty and milestone extraction, and diligence obligations.
3. **Leader simplicity.** Competitors design for contract professionals. the general counsel's "three questions" home, plain-language status and one-button next step are differentiators *if demonstrated live*.
4. **Price and time to value vs enterprise CLM and eRA.** Purdue's Huron full suite cost about $1M a year in subscriptions [3]. Enterprise CLM medians are about $40k a year for small teams and $200k+ for enterprise (third-party estimates [39][83]). Govern's infrastructure cost at the customer volume is small (`GOVERN_ARCHITECTURE.md` §2.7), so it can price under the CLM band and still be profitable.
5. **Deterministic, explainable review.** "The matrix decides, the model only labels clauses" answers the AI-governance question better than "agents". It suits a counsel-led buyer (the general counsel is Senior Associate General Counsel).

### 8.2 Likely objections and answers

What public universities actually require is visible in their CLM RFPs.
- **University of Central Arkansas RFP UCA-25-008** [64] requires:
  - a hosted cloud solution;
  - SSO via "CAS and SAML";
  - separation-of-duties security levels;
  - Word/Excel compatibility;
  - redlining with change tracking;
  - parallel and sequential e-signature;
  - a clause library;
  - an audit trail of routing;
  - renewal notifications;
  - integration with its ERP (Banner);
  - a VPAT or WCAG 2.1 conformance statement covering keyboard and screen-reader use.
  The demonstration carries a large share of the scoring points.
- **University of Utah** issued its CLM RFP from the **Office of the VP for Research** [65]. Research offices now buy CLM directly.
- Expect the customer's checklist to look like this.

| Objection (who raises it) | Answer | Proof to bring |
|---|---|---|
| "We're implementing Huron. Why another system?" (the research enterprise-IT) | Govern does not hold the system of record. It reviews and reports, reads Huron and Workday, and pushes findings back. The "system of record wins" rule is in the architecture. | Integration diagram; sync-log screen; Michigan precedent [7] |
| "Workday now has contract AI (Evisort)." (IT, Procurement) | Workday CI targets procurement, HR and sales contracts [28]. Govern ships research and licensing clause logic, the the customer matrix, the home state and public-university terms, and leader views. Govern can also consume Workday data. | Side-by-side on a license agreement |
| HECVAT / security review (Office of Technology & Digital Innovation) | Complete **HECVAT 3.x Full** now. Map controls to the existing posture: AWS, KMS, least-privilege IAM, put-only audit log, PII pseudonymisation before AI calls. | HECVAT; architecture and data-flow doc; pen-test summary (to commission) |
| "Where does our data go? Is AI trained on it?" (Legal, Privacy) | Document data stays in AWS (US region). OpenAI is the only external processor, through the API, and is not used for training. **Enable Zero Data Retention and sign a DPA before the pilot** (currently pending per `PROJECT_OVERVIEW_AND_SECURITY.md`). Offer a provider option (Bedrock-hosted models) if the customer wants no third-party AI processor. | Signed DPA; ZDR confirmation; subprocessor list |
| Accessibility (Digital Accessibility Services) | WCAG 2.1 AA target, Title II alignment [67], ACR plus third-party audit, remediation SLA in contract [66]. | ACR (VPAT 2.5); audit letter; screen-reader demo on iPad |
| SSO and roles (IAM team) | SAML 2.0 with the customer's Shibboleth IdP via InCommon metadata [68], with eduPerson attributes or group claims mapped to Govern roles. No local passwords. | Test SP metadata; attribute-release request |
| Export control / CUI / ITAR content (Export Control office) | Excluded from the pilot by policy. Add a classification flag that blocks AI processing of marked agreements, and offer a US-person-only support commitment if needed. | Data classification matrix |
| Vendor viability (Procurement) | Escrow or exit clause, full data export (Excel, PDF, JSON), and no proprietary lock-in of the matrix. | Exit plan; export demo |
| Records retention / public records (Legal) | Immutable activity log; configurable retention aligned to the customer's records schedule; and the ability to answer public records requests via export. | Retention settings |

### 8.3 Pricing and packaging suggestion
A suggestion only. Validate against the customer budget authority and state contract vehicles.

| Package | Scope | Suggested model |
|---|---|---|
| **Pilot (90 days)** | One office (Licensing), one matrix, up to 300 agreements, SSO, no live integrations | Fixed fee, credited toward year 1 |
| **Govern Research** | Unlimited reviewer and leader seats; up to 3,000 new agreements a year plus backlog load; matrix, workflow, value reporting; DocuSign, Teams and email | Annual subscription by agreement-volume band, **not per seat**, so leaders and PIs are free. Position below enterprise-CLM medians. |
| **Integration add-on** | Huron Agreements and Workday connectors, field mapping, sync monitoring | One-time setup plus a small annual fee |
| **Licensing add-on** | Obligations, royalty and milestone calendar, licensing-income dashboard, link to Wellspring or Inteum | Annual add-on |

Unlimited users matters: Juro uses the same unlimited-user model [58], and per-seat models like Spellbook and Harvey penalise wide leader access.

### 8.4 Prioritised product gaps to close
| # | Gap | Competitor capability it answers | Why it matters at the customer |
|---|---|---|---|
| 1 | **Procurement pack: HECVAT Full, ACR (VPAT 2.5) plus third-party WCAG 2.1 AA audit, SOC 2 Type I plan, OpenAI ZDR plus DPA, subprocessor list** | Streamlyne advertises HECVAT and SOC 2 [20]; the customer requires accessibility testing [66] | Without these there is no purchase, whatever the demo shows |
| 2 | **Shibboleth/InCommon SAML SSO with role mapping** | Every eRA and CLM product supports SAML SSO | Required by the customer IAM [68]; a delivery-plan item, so pull it forward |
| 3 | **Tracked-changes redline export (.docx) and a Word add-in on Fluent UI** | Ironclad Jurist [36], goHeather [24], Spellbook, Harvey [63], DocuSign [31] | Reviewers negotiate in Word. "Send back" must produce a redline the sponsor can open. |
| 4 | **Field-level provenance and confidence, plus an extraction-check queue** | LinkSquares confidence [52], SpotDraft source highlights [57], Lyn citations [20] | Trust for counsel; stops totals from being silently incomplete |
| 5 | **Licensing obligations and income calendar** (royalties, milestones, diligence, reports) with alerts | Wellspring partner portal [22], Agiloft obligations [46], DocuSign obligation agents [32] | This is the licensing director's day-to-day work after signing |
| 6 | **Huron Agreements connector, live** (pull records and documents, push findings and status, record IDs both ways) | Michigan's Ironclad–eRPM status integration [7] | The core of the "complement Huron" wedge |
| 7 | **Teams and email actionable notifications** (approve, send back, deep link) | Icertis email approvals [42], DocuSign Teams intake [31] | Leaders act without logging in |
| 8 | **Round comparison: "what changed since last round", including unmarked edits** | goHeather round tracking [24] | Sponsors often change text without tracking it |
| 9 | **Published accuracy benchmark per research/licensing clause type** | Sirion benchmark [54] | Evidence for an AI-cautious buyer |
| 10 | **Workday spend and award pull with a manual match screen** | Workday CI's native data [29]; Huron with Workday at UVA and UW [4][5] | Powers the "what is the money" answer with real figures |

---

## 9. Sources

All accessed 2026-10-08. "Excerpt" means the content was confirmed through a search-result excerpt because the page itself blocked automated fetch.

1. Customer-specific source (removed).
2. Huron, Huron Research Suite. https://www.huronconsultinggroup.com/en/services/research/huron-research-suite
3. Purdue Board of Trustees, Huron eRA purchase approval (14 Apr 2023). https://www.purdue.edu/bot/meetings/past-meetings/2023/04.%20april/fic/huron%20contract%20re%20eRA%20systems%20replacement.pdf
4. Huron, UVA launches Huron Grants and Agreements. https://www.huronconsultinggroup.com/insights/uva-launches-huron-grants
5. Universities of Wisconsin, RAMP (Workday Grants Management + Huron). https://atp.wisconsin.edu/research-admin/ramp-scope-and-timeline/ (excerpt)
6. University of Michigan ITS, "U-M Begins Planning for eResearch Platform Transition" (Huron sunsetting eResearch by end of 2031). https://its.umich.edu/news/article/u-m-begins-planning-eresearch-platform-transition (excerpt)
7. University of Michigan ORSP, Sponsored contracts / Working with Ironclad CLM. https://orsp.umich.edu/project-lifecycle/negotiate-and-accept/sponsored-contracts/ and https://orsp.umich.edu/working-ironclad-clm (excerpt)
8. Huron investor presentation 2026. https://ir.huronconsultinggroup.com/static-files/ceb49213-5fff-43c0-b7f3-58dbe13759eb (excerpt)
9. Huron, Huron Agreements brochure. https://www.huronconsultinggroup.com/-/media/Resource-Media-Content/Education/research-suite/Huron-Research-Suite-Agreements.pdf?la=en (excerpt; fetch 404)
10. WVU, Huron Research Suite overview PDF. https://researchportal.wvu.edu/files/d/3aa6ed76-5a2c-443d-a83e-e0ec2468405e/huron-research-suite.pdf (excerpt)
11. UC Irvine, KR Negotiations. https://research.uci.edu/electronic-research-administration/kuali-research/kr-negotiations/
12. Kuali, Kuali launches GrantRisk. https://www.kuali.co/post/kuali-launches-grantrisk-tm-ai-powered-risk-analysis-tool-to-help-institutions-safeguard-their-research-funding
13. Tambellini Group, Kuali Days 2026. https://www.thetambellinigroup.com/kuali-days-2026-a-company-the-market-should-reconsider/
14. UK Find a Tender, Coventry University / Kuali renewal notice 034461-2025. https://www.find-tender.service.gov.uk/Notice/034461-2025/PDF (excerpt)
15. Evisions, Evisions announces sale of Cayuse research business. https://www.evisions.com/blog/evisions-inc-announces-sale-cayuse-research-business/
16. Tracxn, Acquisitions by Cayuse. https://tracxn.com/d/acquisitions/acquisitions-by-cayuse/__N-n-kbSkZywewUcj0uyCf4Px64i4oCscNzhQwxF2QXY
17. Customer-specific source (removed).
18. Streamlyne, Streamlyne vs Cayuse (vendor comparison). https://streamlyne.com/compare/cayuse/
19. Streamlyne, Streamlyne vs Huron (vendor comparison). https://streamlyne.com/streamlyne-vs-huron/
20. Streamlyne, Lyn / Streamlyne Research. https://streamlyne.com/lyn-pro/ and https://streamlyne.com/streamlyne-research/
21. Clemson Division of Research, InfoEd Agreements module now live. https://blogs.clemson.edu/clemsonresearch/2025/04/22/infoed-agreements-module-now-live/
22. Wellspring, Evolve. https://www.wellspring.com/evolve
23. Seedtable, FirstIgnite exit (Merit Holdings / Inteum). https://seedtable.com/exits/firstignite (excerpt)
24. goHeather, Contract AI for Sponsored Programs Offices. https://www.goheather.io/roles/sponsored-programs
25. LegalSifter, Universities. https://www.legalsifter.com/industries/universities and case studies https://www.legalsifter.com/blog/tag/case-study
26. Workday Form 10-Q (quarter ended 31 Oct 2024), Evisort acquisition. https://www.sec.gov/Archives/edgar/data/1327811/000132781124000242/wday-20241031.htm
27. Workday newsroom, Evisort available through Workday (27 Mar 2025). https://newsroom.workday.com/2025-03-27-Evisort-AI-Powered-Contract-Intelligence-Now-Available-Through-Workday
28. Workday newsroom, Custom AI Model Library (22 Oct 2025). https://newsroom.workday.com/2025-10-22-Workday-Introduces-New-Custom-AI-Model-Library-to-Power-Smarter,-Faster-Contract-Reviews
29. Workday, Contract Intelligence powered by Evisort AI. https://www.workday.com/en-us/products/contract-management/contract-intelligence.html
30. DocuSign Form 10-K FY2025 (Lexion acquisition). https://www.sec.gov/Archives/edgar/data/1261333/000126133325000024/docu-20250131.htm
31. DocuSign, Lexion acquisition and IAM. https://www.docusign.com/blog/lexion-acquisition-intelligent-agreement-management
32. DocuSign IR, Docusign unveils AI assistant and agents (2026). https://investor.docusign.com/news-and-events/press-releases/news-details/2026/Docusign-Unveils-AI-Assistant-and-Agents-to-Power-the-Next-Era-of-Agreement-Work/default.aspx
33. Ironclad, Next wave of AI agents. https://ironcladapp.com/resources/articles/ai-agentic-launch
34. Law.com, Ironclad launches Ironclad Assistant (19 Mar 2026). https://www.law.com/legaltechnews/2026/03/19/ironclad-launches-ironclad-assistant-expands-agentic-ai-capabilities/
35. PR Newswire, Ironclad surpasses $200M ARR. https://www.prnewswire.com/news-releases/ironclad-surpasses-200-million-in-annual-recurring-revenue-entering-a-new-phase-of-ai-growth-302686054.html
36. Ironclad Support, Jurist Redlining Agent with Playbooks and Precedents. https://support.ironcladapp.com/hc/en-us/articles/34188767294359-Use-Jurist-Redlining-Agent-with-Playbooks-and-Precedents
37. Ironclad Support, AI Playbook for non-legal users. https://support.ironcladapp.com/hc/en-us/articles/34164717490327-Recipe-Build-an-AI-Playbook-for-Non-Legal-Users-to-Handle-First-Pass-Redlining
38. Ironclad, Integrations / Word and Salesforce updates. https://ironcladapp.com/product/integrations and https://ironcladapp.com/resources/articles/jurist-microsoft-word-salesforce-ironclad-updates
39. Vendr, Ironclad pricing (third-party). https://www.vendr.com/marketplace/ironclad
40. Icertis, Icertis launches Vera. https://www.icertis.com/company/news/icertis-launches-vera-to-power-contract-intelligence-with-smarter-ai/
41. Startup Researcher, Icertis acquires Dioptra. https://www.startupresearcher.com/news/icertis-acquires-dioptra-to-advance-ai-first-contracting
42. Icertis, Explore the new Icertis experience (8.7, Dec 2025). https://www.icertis.com/research/blog/new-icertis-ux-experience-redefines-contracting/
43. Business Wire, Icertis reimagines enterprise contracting experience powered by Vera (June 2026). https://www.businesswire.com/news/home/20260602537543/en/Icertis-Reimagines-Enterprise-Contracting-Experience-Powered-by-Vera (title only; fetch 403)
44. HyperStart, Icertis reviews (competitor blog). https://www.hyperstart.com/blog/icertis-reviews/
45. PR Newswire, Agiloft acquires Screens. https://www.prnewswire.com/news-releases/agiloft-acquires-screens-to-deliver-ai-powered-contract-review-for-data-first-contract-lifecycle-management-302349454.html
46. LawSites, Agiloft launches AI obligation management (Dec 2025). https://www.lawnext.com/2025/12/agiloft-launches-ai-powered-obligation-management-system-for-contract-lifecycle.html
47. PR Newswire, Agiloft Astra general availability. https://www.prnewswire.com/news-releases/agiloft-announces-general-availability-of-agiloft-astra-expanding-access-to-contract-ai-for-legal-procurement-and-finance-teams--with-new-users-getting-to-value-in-five-minutes-302824391.html
48. Saint Louis University, Agiloft for Research. https://www.slu.edu/news/announcements/2021/april/agiloft-for-research.php
49. ContractSafe, ContractSafe vs Agiloft (competitor page summarising reviews). https://www.contractsafe.com/contractsafe-vs-agiloft-comparison
50. Conga, Conga completes acquisition of PROS B2B business. https://conga.com/press/conga-completes-acquisition-pros-b2b-business
51. PR Newswire, LinkSquares on 2026 Inc. 5000. https://www.prnewswire.com/news-releases/linksquares-ranks-on-the-2026-inc-5000-list-of-americas-fastest-growing-private-companies-for-6th-straight-year-302847977.html
52. LinkSquares Help, Glossary (Smart Value confidence icons). https://help.linksquares.com/hc/en-us/articles/14515422699031-LinkSquares-Glossary
53. Business Wire, Sirion completes Haveli majority investment. https://www.businesswire.com/news/home/20260223223160/en/Sirion-Announces-Completion-of-Majority-Investment-from-Haveli-to-Help-Accelerate-the-Future-of-AI-Native-Contract-Lifecycle-Management
54. Sirion, 2026 clause-extraction accuracy benchmark. https://www.sirion.ai/library/contract-insights/clause-extraction-benchmark-sirion-vs-llms/
55. Sirion, Next-gen agentic CLM. https://www.sirion.ai/press/next-gen-agentic-clm-360-conversational-contracting/
56. TechCrunch, Qualcomm backs SpotDraft (26 Jan 2026). https://techcrunch.com/2026/01/26/qualcomm-backs-spotdraft-to-scale-on-device-contract-ai-with-valuation-doubling-toward-400m/
57. SpotDraft, Introducing VerifAI for Teams. https://releases.spotdraft.com/announcements/introducing-verifai-for-teams-ai-powered-contract-review-inside-spotdraft
58. Vendr, Juro pricing (third-party). https://www.vendr.com/marketplace/juro
59. Legaltech Tapas, Juro / Richard Mabey interview. https://legaltechtapas.substack.com/p/6-juro-richard-mabey-interview
60. Legal IT Insider, ContractPodAi rebrands as Leah. https://legaltechnology.com/2026/01/05/contractpodai-rebrands-as-leah/
61. Sifted, Luminance raises $75M. https://sifted.eu/articles/luminance-ai-agent-raise-news
62. SiliconANGLE, Spellbook raises $50M. https://siliconangle.com/2025/10/09/legal-ai-firm-spellbook-raises-50m-expand-contract-review-platform/
63. Harvey, Word Add-In. https://harvey.ai/platform/word-add-in
64. University of Central Arkansas, RFP UCA-25-008 Contract Lifecycle Management. https://uca.edu/procurement/files/2024/05/UCA-25-008-Contract-Lifecycle-Management-Software-System-Final.pdf
65. University of Utah, RFP for CLM Software (Office of the VP for Research). https://www.bidnetdirect.com/utah/universityofutah-campus/solicitations/Request-for-Proposal-For-Contract-Lifecycle-Management-Software/0000413312 (excerpt)
66. Customer-specific source (removed).
67. Federal Register 2026-07663, Extension of compliance dates (ADA Title II web rule). https://www.federalregister.gov/documents/2026/04/20/2026-07663/extension-of-compliance-dates-for-nondiscrimination-on-the-basis-of-disability-accessibility-of-web
68. Customer-specific source (removed).
69. Customer-specific source (removed).
70. Workday Canvas, Tokens explained. https://canvas.workday.com/styles/tokens/overview
71. DocuSign Developers, Docusign Olive design system. https://www.docusign.com/blog/developers/docusign-olive-style-guide-to-front-end-system
72. Microsoft Learn, Office Add-in design (Fluent UI). https://learn.microsoft.com/en-us/office/dev/add-ins/design/add-in-design
73. Customer-specific source (removed).
74. Customer-specific source (removed).
75. Customer-specific source (removed).
76. Customer-specific source (removed).
77. Business Wire, Hippocratic AI and Huron collaboration (Jan 2026). https://www.businesswire.com/news/home/20260108049774/en/Hippocratic-AI-and-Huron-Consulting-Group-Announce-Strategic-Collaboration-to-Transform-Healthcare-Delivery-and-Innovation
78. Inside Higher Ed, UW system paid Huron $51M 2019–23. https://www.insidehighered.com/news/quick-takes/2024/12/17/report-u-wisconsin-system-paid-huron-51m-2019-23
79. G2, Ironclad reviews. https://g2.com/products/ironclad/reviews?page=2
80. Juro, DocuSign CLM alternatives (competitor page). https://www.juro.com/alternatives/docusign-clm
81. Customer-specific source (removed).
82. Unsiloed AI, Confidence score thresholds (vendor blog). https://www.unsiloed.ai/blog/confidence-score-thresholds-document-automation
83. Contracko, Icertis vs Ironclad / Agiloft vs Ironclad pricing (third-party estimates). https://contracko.com/clm-comparisons/icertis-vs-ironclad

**Not verified, so ask the customer:** which tech-transfer system the Technology Commercialization office uses (Wellspring, Inteum or other); the Huron Agreements go-live date; whether the customer signs through DocuSign; and the customer's current HECVAT version and data-classification level for research agreements.
