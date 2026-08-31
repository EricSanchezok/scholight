---
name: Scholight
description: A quiet academic search interface built for AI research.
colors:
  brand-blue: "#1F45B8"
  result-blue: "#18389E"
  canvas: "#FBFAF5"
  surface: "#FFFEFC"
  surface-raised: "#FCFBF7"
  surface-muted: "#F4F2EC"
  ink: "#0E0F14"
  ink-soft: "#2E2F36"
  text-muted: "#61636E"
  text-subtle: "#737069"
  line: "#CCC9BD"
  line-soft: "#DBD9CC"
  input-line: "#B5B2A8"
  danger: "#B23321"
  danger-surface: "#FBF1ED"
  focus: "#5578DD"
typography:
  display:
    fontFamily: "Literata, Georgia, serif"
    fontSize: "70px"
    fontWeight: 600
    lineHeight: 1.114
    letterSpacing: "-0.02em"
  headline:
    fontFamily: "Literata, Georgia, serif"
    fontSize: "36px"
    fontWeight: 600
    lineHeight: 1.278
    letterSpacing: "-0.02em"
  title:
    fontFamily: "Literata, Georgia, serif"
    fontSize: "22px"
    fontWeight: 600
    lineHeight: 1.3
  body:
    fontFamily: "Manrope, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.6
  label:
    fontFamily: "Manrope, system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 600
    lineHeight: 1.4
rounded:
  control: "6px"
  surface: "8px"
  search: "10px"
spacing:
  xs: "8px"
  sm: "12px"
  md: "16px"
  lg: "24px"
  xl: "32px"
components:
  button-primary:
    backgroundColor: "{colors.brand-blue}"
    textColor: "{colors.surface}"
    rounded: "{rounded.control}"
    padding: "0 24px"
    height: "48px"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "0 18px"
    height: "42px"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "0 15px"
    height: "48px"
  search-field:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.search}"
    padding: "8px 8px 8px 22px"
    height: "72px"
---

# Design System: Scholight

## 1. Overview

**Creative North Star: "The Research Desk"**

Scholight should feel like a well-kept research desk: a generous reading surface, a small number of precise instruments, and no ornament competing with the work. Literata brings the authority of scholarly publishing to headings while Manrope keeps controls, metadata, and dense account information practical.

The system is deliberately restrained. Flat surfaces, thin dividers, an asymmetric reading column, and one blue accent create continuity between public search and authenticated account tools. It explicitly rejects AnySearch imitation, generic AI-tool marketing, card-heavy SaaS dashboards, exposed retrieval jargon, duplicate lettermark branding, and gratuitous motion.

**Key Characteristics:**

- Continuous editorial reading flow instead of repeated result cards.
- Restrained blue used for actions, selection, and meaningful emphasis only.
- Flat layout with hairline separation; elevation is reserved for temporary surfaces.
- Short, low-amplitude motion that communicates state and respects reduced motion.
- Desktop precision with structural mobile reflow, not a separate visual language.

## 2. Colors

The palette is a near-neutral research surface with one disciplined blue voice and explicit semantic danger states.

### Primary

- **Scholight Blue:** The only product accent. Use it for primary actions, current selection, meaningful status, and the `AI.` emphasis.
- **Result Blue:** A darker reading link reserved for paper titles and durable destinations.

### Neutral

- **Research Canvas:** The page background that carries long reading sessions.
- **Reading Surface:** Inputs, dialogs, menus, and paper-like raised content.
- **Primary Ink:** Headings and high-importance text.
- **Muted Ink:** Supporting copy and metadata; never use it below WCAG AA contrast.
- **Hairlines:** Structural borders and dividers; use hierarchy before adding a container.

### Named Rules

**The One Blue Voice Rule.** Blue is functional, not decorative. It should occupy less than roughly ten percent of a typical screen.

**The Semantic Color Rule.** Danger colors communicate destructive or failed states only. Never use them as ornamental accents.

## 3. Typography

**Display Font:** Literata (with Georgia fallback)<br>
**Body Font:** Manrope (with system-ui fallback)

**Character:** The pairing separates scholarship from interface mechanics. Literata belongs to the product wordmark, major headings, paper titles, and dialog decisions; Manrope owns controls, body copy, metadata, tables, and labels.

### Hierarchy

- **Display** (600, 70px desktop, 1.114): Home hero only; mobile may scale down structurally but never exceed 96px.
- **Headline** (600, 36px, 1.278): Page titles and major reading states.
- **Title** (600, 22px, 1.3): Section and paper-result headings.
- **Body** (400, 14px, 1.6): Product copy with a target line length of 65–75 characters.
- **Label** (600, 12px, 1.4): Compact controls and metadata. Uppercase tracking is reserved for genuinely categorical labels, not every section.

### Named Rules

**The Two-Voice Rule.** Literata names and frames the work; Manrope operates it. Never use the display face for buttons, tables, or form labels.

## 4. Elevation

Scholight is flat by default. Depth comes from surface tone and hairline structure. Shadows appear only on portalled temporary surfaces—dropdowns, selects, and dialogs—where they explain stacking and focus.

### Shadow Vocabulary

- **Popover:** A compact ambient shadow used only for dropdown and select menus.
- **Dialog:** A stronger ambient shadow paired with an overlay to establish modal focus.

### Named Rules

**The Temporary Elevation Rule.** If a surface remains in the document flow, it does not receive a shadow.

## 5. Components

### Buttons

- **Shape:** Gently squared controls (6px radius); search actions may use 7px inside the 10px search field.
- **Primary:** Scholight Blue with light text, 48px minimum height, and no decorative shadow.
- **Hover / Focus:** Darken within the blue family; preserve the global visible focus ring.
- **Secondary:** Reading Surface with a hairline border and Primary Ink.

### Chips

- **Style:** Compact removable filters use a subtle blue-tinted surface and readable blue text.
- **State:** Chips are functional filters, not category decoration; every removable chip has a clear button action.

### Cards / Containers

- **Corner Style:** Containers use 8–10px only when grouping is necessary.
- **Background:** Reading Surface or Raised Surface.
- **Shadow Strategy:** None in document flow.
- **Border:** Hairlines separate content; paper results stay in one continuous list.
- **Internal Padding:** Use the 16px, 24px, and 32px steps according to information density.

### Inputs / Fields

- **Style:** Solid Reading Surface, 1px Input Line, 6px radius, and at least 44px interaction height.
- **Focus:** Brand border plus the shared focus treatment; never remove focus without replacement.
- **Error / Disabled:** Use semantic danger copy and explicit disabled state, never color alone.

### Navigation

The header remains visually still while content changes. Desktop navigation contains only Home, Docs, and identity actions. Authenticated destinations live in one account menu; mobile exposes the same information architecture in a vertical menu. The current destination uses one concise blue marker.

### Authentication

Sign-in, registration, and verification views use a quiet two-column editorial layout on
desktop: the Scholight identity and page explanation sit on the leading side while the
form or status action sits on the trailing side. The page has no decorative card; spacing
and shared alignment provide the grouping. Below 768px the columns stack, with the
wordmark and product mark kept in one compact identity row so authentication does not
become a tall sequence of repeated brand blocks.

### Search Results

Results form a 920px continuous reading column with title, authors, arXiv metadata, score, abstract, and text actions. Hairline dividers create rhythm; individual paper cards are prohibited.

## 6. Do's and Don'ts

### Do:

- **Do** use shared semantic tokens for every color, type role, radius, elevation, and motion duration.
- **Do** keep result pages continuous and optimized for scanning and reading.
- **Do** describe Standard and Thorough in terms of user outcome, speed, and depth.
- **Do** keep motion between 90ms and 280ms, under 6px of travel, and limited to feedback or continuity.
- **Do** preserve keyboard, focus, screen-reader, reduced-motion, zoom, and mobile behavior in every new component.

### Don't:

- **Don't** imitate AnySearch or another search product's layout, branding, or interaction vocabulary.
- **Don't** use generic AI-tool marketing, gradients, agent hype, invented capabilities, or retrieval implementation jargon.
- **Don't** build card-heavy SaaS dashboards or split papers into repeated elevated containers.
- **Don't** add bouncing controls, parallax, animated counters, large slides, or decorative choreography.
- **Don't** add a letter icon beside the `scholight` wordmark.
- **Don't** expose Level 1/Level 2, ANN, BM25, RRF, chunks, or vector-store terminology in product copy.
