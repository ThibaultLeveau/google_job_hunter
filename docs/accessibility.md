# Core-interface accessibility verification

Issue #73 checks the implemented authentication, profile review, search, job inspection and
account settings screens against the [interface contract](architecture.md#interface-contract).
Use the [real-stack browser harness](../README.md#browser-checks) with synthetic data.
The API, worker and PostgreSQL are real; external providers remain deterministic fixtures.

## Repeatable checks and evidence

Run `npm run test:e2e` from `frontend` with the documented local PostgreSQL configuration.
For the focused audit, run `npm run build && npx playwright test e2e/accessibility.pw.ts`.
Run frontend lint, strict types and coverage as documented in the README. CI runs the complete
browser suite and keeps `browser-reports-<run>-<attempt>` artifacts for three days, including
successful screenshots, the HTML report and axe results. Download them from the PR's Browser run.

The audit uses [axe with Playwright](https://playwright.dev/docs/accessibility-testing), including
WCAG 2.0/2.1/2.2 A/AA tags, with no disabled rules or element exclusions. It requires zero reported
violations, including serious/critical ones. Unexpected incomplete assessments also fail.
Native-dialog background heuristics can leave the deletion explanation or mobile Close filters
button unmeasured. Both use direct computed foreground/background checks at 4.5:1; the button
must also be fully in the viewport. CI's captured button was visibly unobscured despite axe's
partial-overlap report. No other incomplete targets are accepted.
The JSON attachments retain those incomplete reports for review rather than presenting them as passes.

At widths 320, 768 and 1440 CSS pixels, each covered state is scanned at 100% and 200% root text
size. Mobile filter drawers are checked at 320px in both sizes; wider layouts use inline filters.
Assertions cover page/dialog horizontal overflow, keyboard reachability, visible unobscured focus,
dialog wrapping and return focus, dynamic term removal, and reduced-motion loading indicators.
Root text resizing exercises rem-based text and controls; it is not a claim to test every browser's
zoom implementation. See [text resizing](https://www.w3.org/WAI/WCAG22/Understanding/resize-text.html)
and [reflow](https://www.w3.org/WAI/WCAG22/Understanding/reflow.html).

Screenshots cover desktop/mobile and doubled mobile text without fragile pixel comparisons.
The long-title/missing-metadata cases deliberately transform display fields of real job responses;
they test rendering, not provider extraction accuracy. Authentication, profile writes, search execution
and the first job-detail inspection use unmodified API responses. The OS file chooser receives a synthetic TXT fixture;
it is not counted as a keyboard-operated native file-picker test.

## Recorded keyboard and visual inspection

On 2026-11-07, the coding agent operated a separate Chromium 158 session through interactive
Tab, Shift+Tab, Enter, Space, Escape, arrow keys and text entry. The inspection used the synthetic
Alice account at 1440px and 320px, including 200% text on mobile. This was a separate inspection
from the scripted audit; it was not a human participant or screen-reader study.

| Journey | Observed result |
| --- | --- |
| Authentication | Skip link was visible on focus and moved to main; sign-in completed with keys and focused the profile page. Sign-out returned to main with the signed-out status. |
| Profile review | A prepared synthetic upload supplied candidates. Space selected Python and experience; acceptance linked the existing skill and created experience. Evidence stayed available. |
| Profile editor | Shift+Tab wrapped from the evidence disclosure to Save. Escape restored Edit experience. Editing the title and saving restored that opener with the new title visible. |
| Search | Keyboard creation, adding/removing an OR term, saving, selecting the saved search and running discovery completed. Removing a term focused its remaining sibling; saving focused New search. |
| Mobile filters | At 320px/200%, the keyword field scrolled into view. Shift+Tab wrapped to Close filters; Escape restored Filters and sorting. No horizontal page scroll occurred. |
| Job details | Keyboard navigation opened the discovered job, checklist and source evidence. The quoted Python requirement and focus outline were visible at 320px/200%. |
| Account settings | Account-menu Escape restored its button. The deletion dialog focused Cancel, trapped enabled controls after typing DELETE, and Escape restored Delete my account without deleting. |
| Private export | Password reauthentication focused its field, then the confirmation status. Repeating export started the synthetic download and focused its completion message. |

Local review evidence is in ignored `frontend/reports/browser/manual/`: `keyboard-actions.jsonl`,
`keyboard-trace.zip` and numbered screenshots. Representative inspected captures include
`01-skip-link.png`, `08-edit-saved.png`, `14-mobile-filters-200.png`, `17-job-evidence-mobile.png`
and `21-delete-return-mobile.png`. CI's automated screenshots are separate reproducible evidence.
All evidence uses synthetic data; do not publish traces containing real account/document data.

Visual review found no clipped text loss or page-level horizontal overflow in the covered states;
200% mobile text requires substantial vertical scrolling. Token contrast calculations gave
13.19:1 text/canvas, 5.71:1 muted/canvas, 9.24:1 accent/white, 7.38:1 danger/white,
3.85:1 border/white and 3.54:1 border/canvas; the accent focus outline is 8.48:1 against canvas.
The naming review corrected document pagination to a navigation landmark and search logic/query
containers to named groups, following the [HTML naming rules](https://www.w3.org/TR/html-aria/#naming-prohibited).
Browser assertions verify those exposed roles.

This evidence addresses the covered components and states. Axe, screenshots and an agent keyboard
inspection do not establish full WCAG conformance or replace assistive-technology/user testing.
Screen readers, other browser engines, OS file pickers and real-provider pages were not tested here.
Repeat this audit and inspect new incomplete results after interface changes; record current commands,
results, screenshots and limitations in the PR rather than treating this dated record as perpetual approval.
