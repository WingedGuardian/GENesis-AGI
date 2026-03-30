---
name: browser-automation
description: Web automation error recovery, form filling patterns, and session management via Playwright
consumer: cc_background_task
phase: 7
skill_type: workflow
---

# Browser Automation

## Purpose

Reference playbook for web automation tasks using Playwright MCP. Provides
error recovery strategies, CSS selector patterns, form filling methodology,
and safety gates. Loaded when Genesis performs browser-based tasks.

## Three Browser Layers

Genesis has three layers of browser interaction. Choose the lightest layer
that can accomplish the task.

### Layer 1: Web Fetch (read-only, public data)
- Tools: `genesis.web` (SearXNG + Brave), `WebFetch`
- Use for: public information retrieval, search, reading articles
- No authentication, no interaction, no JavaScript rendering
- Fastest and cheapest option

### Layer 2: Managed Browser (persistent profile, agent-owned logins)
- Tools: Playwright MCP with `--user-data-dir ~/.genesis/browser-profile/`
- Use for: authenticated automation, form filling, multi-step workflows
- Profile persists across sessions: cookies, localStorage, login state survive
- **Agent-owned accounts**: log into accounts created FOR the agent, never the
  user's personal accounts. Treat the agent like a new employee — give it its
  own Google account, its own service logins, etc.
- To log in: navigate to the login page, fill credentials, submit. The profile
  saves the session automatically.

### Layer 3: Browser Relay (connect to user's browser)
- Tools: Playwright MCP with `--extension` flag
- Use for: interacting with services the user is logged into
- Requires: user installs "Playwright MCP Bridge" Chrome extension
- User must be present and approve the connection
- Use sparingly — only when the user explicitly requests it

### Layer Selection Guide
| Need | Layer | Why |
|------|-------|-----|
| Read a public page | Fetch | No login needed |
| Search the web | Fetch | API-based, fast |
| Fill a form on agent's account | Managed | Persistent login |
| Order groceries from agent's account | Managed | Persistent login |
| Check user's Gmail | Relay | User's login |
| Take action in user's banking app | Relay | User's login, MUST confirm |

## When to Use

- Any task requiring interaction with a web page beyond simple fetching.
- Form filling, multi-step workflows, authenticated sessions.
- Data extraction requiring JavaScript rendering.
- Browser-based testing or verification.

## Selector Strategy

Try selectors in this priority order:

| Priority | Selector Type | Example | When |
|----------|--------------|---------|------|
| 1 | ID | `#submit-btn` | Element has unique ID |
| 2 | data-testid | `[data-testid="login"]` | Modern apps with test attributes |
| 3 | name attribute | `input[name="email"]` | Form fields |
| 4 | type attribute | `input[type="submit"]` | Standard form elements |
| 5 | Specific class | `.btn-primary` | Semantic class names |
| 6 | Visible text | text="Sign In" | Buttons and links |
| 7 | Composite | `form.login input[type="email"]` | When simple selectors aren't unique |

**Common patterns:**
```
# Forms
input[name="username"]
input[type="password"]
button[type="submit"]
select[name="country"]
textarea[name="message"]

# Navigation
nav a[href="/dashboard"]
header .menu-item
a:has-text("About")

# E-commerce
.product-card .price
button:has-text("Add to Cart")
.cart-total
```

## Error Recovery

| Error | Recovery Steps |
|-------|---------------|
| Element not found | 1. Try alternative selector 2. Try visible text 3. Scroll page 4. Wait for dynamic load |
| Page timeout | 1. Retry navigation 2. Check if URL redirected 3. Verify network connectivity |
| Login required | Inform user. Ask for credentials. Never guess passwords. |
| CAPTCHA | Cannot solve. Inform user. Suggest manual completion. |
| Pop-up / modal | Click dismiss/close button. Look for `[aria-label="Close"]` or `.modal-close` |
| Cookie consent | Click "Accept" or dismiss. Look for `#cookie-accept` or text="Accept All" |
| Rate limited | Wait 30 seconds. Retry once. If still limited, back off exponentially. |
| Wrong page | Use page snapshot to verify. Navigate back. Check URL. |
| Stale element | Re-query the selector. Page may have re-rendered. |

## Form Filling Workflow

1. **Read page** — Take snapshot to understand form structure
2. **Identify fields** — Map each required field to a selector
3. **Fill sequentially** — One field at a time, verify each
4. **Handle dropdowns** — Use select_option for `<select>`, click+text for custom dropdowns
5. **Handle checkboxes** — Click to toggle, verify state after
6. **Screenshot before submit** — Visual verification before irreversible action
7. **Submit** — Click submit button
8. **Verify result** — Read resulting page to confirm success

## Safety Gates

**MANDATORY before any financial transaction:**
1. Summarize what will be purchased/paid
2. Show total cost
3. Get explicit user confirmation
4. Never auto-complete purchases
5. Never click "Place Order", "Pay Now", "Confirm Purchase" without approval

**MANDATORY for credential entry:**
1. Verify the domain is correct (check URL bar, not page content)
2. Warn on HTTP (non-HTTPS) credential pages
3. Never store passwords
4. Never enter credentials on unfamiliar domains without user confirmation

## Session Management

- Browser sessions persist within a task/conversation
- Cookies and login state are maintained across page navigations
- Close browser explicitly when done to free resources
- If session needs to survive across tasks, document the auth state needed

## Multi-Step Workflow Pattern

For complex workflows (e.g., fill form → verify → submit → navigate → extract):

1. **Plan the steps** — List all pages and actions before starting
2. **Checkpoint after each page** — Take snapshot, verify you're in the right place
3. **Handle branching** — If the workflow can branch (success/error), plan for both
4. **Limit scope** — Max 10-20 page navigations per task to prevent runaway browsing
5. **Report progress** — Log each completed step

## Output Format

```yaml
task_id: <BROWSER-YYYY-MM-DD-NNN>
pages_visited: <count>
actions_taken:
  - action: <navigate | click | type | select | screenshot>
    target: <selector or URL>
    result: <success | failed | recovered>
errors_recovered: <count>
screenshots: [<file paths>]
result: <task outcome description>
```

## References

- Playwright MCP tools (browser_navigate, browser_click, etc.)
- `src/genesis/skills/osint/SKILL.md` — For web-based investigation
