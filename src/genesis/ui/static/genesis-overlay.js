/**
 * Genesis UI Overlay
 *
 * Injected into the AZ index.html via after_request hook.
 * Monkey-patches Alpine stores, manipulates DOM, and rewires
 * AZ UI elements to show Genesis data and branding.
 *
 * Zero AZ core files modified — all customization happens here.
 *
 * NOTE: All data rendered in this overlay originates from Genesis's
 * internal APIs (local-only Flask server). No user-supplied or external
 * content is rendered. All dynamic content uses textContent for plain
 * text values and controlled DOM construction for structured elements.
 */

// ── Helpers ───────────────────────────────────────────────────────────

function waitForAlpine(callback, maxRetries = 50) {
  let attempts = 0;
  const check = () => {
    if (window.Alpine && Alpine.store) {
      callback();
    } else if (attempts++ < maxRetries) {
      setTimeout(check, 100);
    }
  };
  check();
}

function waitForElement(selector, callback, maxRetries = 30) {
  let attempts = 0;
  const check = () => {
    const el = document.querySelector(selector);
    if (el) {
      callback(el);
    } else if (attempts++ < maxRetries) {
      setTimeout(check, 200);
    }
  };
  check();
}

async function fetchJson(url) {
  try {
    const resp = await fetch(url);
    if (resp.ok) return await resp.json();
  } catch (e) {
    console.warn("[Genesis] fetch failed:", url, e);
  }
  return null;
}

/** Create a DOM element with optional class and text. */
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

// ── Batch 2: Branding ─────────────────────────────────────────────────

function rebrandLogo() {
  waitForElement("#logo-container", (container) => {
    const img = container.querySelector("img");
    if (img) {
      img.src = "/genesis-ui/genesis-logo.svg";
      img.alt = "Genesis";
    }
    const link = container.querySelector("a");
    if (link) {
      link.href = "/genesis";
      link.removeAttribute("target");
      link.removeAttribute("rel");
    }
  });
}

function rebrandVersionLabel() {
  waitForAlpine(() => {
    const interval = setInterval(() => {
      const elem = document.getElementById("a0version");
      if (elem) {
        clearInterval(interval);
        elem.textContent = "Genesis v3";
      }
    }, 500);
    // Stop trying after 10s
    setTimeout(() => clearInterval(interval), 10000);
  });
}

// ── Batch 3: Hide AZ Features ─────────────────────────────────────────

function rewireDropdownItems() {
  // The dropdown items don't have unique IDs, so find by text content
  waitForElement(".quick-actions-dropdown", (dropdown) => {
    const items = dropdown.querySelectorAll(".dropdown-item");
    for (const item of items) {
      const spans = item.querySelectorAll("span");
      const iconSpan = spans[0]; // material-symbols-outlined
      const textSpan = spans[1]; // label
      if (!textSpan) continue;
      const text = textSpan.textContent.trim();

      if (text === "Scheduler") {
        textSpan.textContent = "Sessions";
        if (iconSpan) iconSpan.textContent = "forum";
        const newItem = item.cloneNode(true);
        item.parentNode.replaceChild(newItem, item);
        newItem.addEventListener("click", (e) => {
          e.preventDefault();
          e.stopPropagation();
          openGenesisSessionsModal();
        });
      }

      if (text === "Projects") {
        textSpan.textContent = "Tasks";
        if (iconSpan) iconSpan.textContent = "task_alt";
        const newItem = item.cloneNode(true);
        item.parentNode.replaceChild(newItem, item);
        newItem.addEventListener("click", (e) => {
          e.preventDefault();
          e.stopPropagation();
          openGenesisTasksModal();
        });
      }
    }
  });
}

function rewireSchedulerButton() {
  // Replace the scheduler quick-action button with a Sessions button
  waitForElement("#scheduler", (btn) => {
    const icon = btn.querySelector(".material-symbols-outlined");
    if (icon) icon.textContent = "forum";
    btn.title = "Sessions";
    btn.id = "genesis-sessions";
    const newBtn = btn.cloneNode(true);
    btn.parentNode.replaceChild(newBtn, btn);
    newBtn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      openGenesisSessionsModal();
    });
  });
}

function hideSettingsTabs() {
  waitForAlpine(() => {
    const check = setInterval(() => {
      const store = Alpine.store("settings");
      if (!store) return;
      clearInterval(check);

      const origOnOpen = store.onOpen?.bind(store);
      if (origOnOpen) {
        store.onOpen = function () {
          origOnOpen();
          // Default to 'external' tab instead of 'agent'
          this.activeTab = "external";
        };
      }
    }, 200);
    setTimeout(() => clearInterval(check), 10000);
  });
}

// ── Batch 4: Welcome Screen ───────────────────────────────────────────

function rewireWelcomeCards() {
  // Watch for the welcome screen to appear
  const observer = new MutationObserver(() => {
    const cards = document.querySelectorAll(".welcome-action-card");
    if (cards.length === 0) return;

    for (const card of cards) {
      const title = card.querySelector(".welcome-action-title");
      const icon = card.querySelector(".welcome-action-icon");
      if (!title) continue;
      const text = title.textContent.trim();

      // Only change text + icon here. Click handling is done by
      // patchWelcomeStore() which monkey-patches executeAction().
      // Do NOT cloneNode or addEventListener — that causes duplicates
      // because Alpine's @click still fires executeAction too.

      if (text === "New Chat") {
        title.textContent = "Genesis";
        if (icon) icon.textContent = "neurology";
      }

      if (text === "Projects") {
        title.textContent = "Tasks";
        if (icon) icon.textContent = "task_alt";
      }

      if (text === "Scheduler") {
        title.textContent = "Sessions";
        if (icon) icon.textContent = "forum";
      }

      if (text === "Files") {
        title.textContent = "Inbox";
        if (icon) icon.textContent = "inbox";
      }
    }
  });

  observer.observe(document.body, { childList: true, subtree: true });
  // Stop observing after 15s to avoid overhead
  setTimeout(() => observer.disconnect(), 15000);
}

// Also patch the welcome store's executeAction for dropdown items
function patchWelcomeStore() {
  waitForAlpine(() => {
    const check = setInterval(() => {
      const ws = Alpine.store("welcomeStore");
      if (!ws) return;
      clearInterval(check);

      const orig = ws.executeAction.bind(ws);
      ws.executeAction = function (actionId) {
        switch (actionId) {
          case "genesis":
          case "new-chat":
            window.location.href = "/genesis";
            break;
          case "files":
            openGenesisInboxModal();
            break;
          case "memory":
            openGenesisMemoryModal();
            break;
          case "scheduler":
            openGenesisSessionsModal();
            break;
          case "projects":
            openGenesisTasksModal();
            break;
          default:
            orig(actionId);
        }
      };
    }, 200);
    setTimeout(() => clearInterval(check), 10000);
  });
}

// ── Batch 5: Memory Button → Genesis Memory ──────────────────────────

function rewireMemoryButton() {
  waitForElement("#memory-dash", (btn) => {
    const newBtn = btn.cloneNode(true);
    btn.parentNode.replaceChild(newBtn, btn);
    newBtn.id = "memory-dash";
    newBtn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      openGenesisMemoryModal();
    });
  });

  // Also rewire dropdown "Memories" item
  waitForElement(".quick-actions-dropdown", (dropdown) => {
    const items = dropdown.querySelectorAll(".dropdown-item");
    for (const item of items) {
      if (item.textContent.trim() === "Memories") {
        const newItem = item.cloneNode(true);
        item.parentNode.replaceChild(newItem, item);
        newItem.addEventListener("click", (e) => {
          e.preventDefault();
          e.stopPropagation();
          openGenesisMemoryModal();
        });
      }
    }
  });
}

async function openGenesisMemoryModal() {
  // Remove existing modal if any
  const existing = document.getElementById("genesis-memory-modal");
  if (existing) existing.remove();

  const stats = (await fetchJson("/api/genesis/ui/memory/stats")) || {};
  const types = ["observations", "procedural"];
  const contentCols = {
    procedural: "procedure_text",
    observations: "content",
  };
  const timeCols = {
    procedural: "created_at",
    observations: "created_at",
  };

  // Build modal using safe DOM methods
  const overlay = el("div", "genesis-modal-overlay");
  overlay.id = "genesis-memory-modal";
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeGenesisModal(overlay);
  });

  const modal = el("div", "genesis-modal");

  // Header
  const header = el("div", "genesis-modal-header");
  header.appendChild(el("span", null, "Genesis Memory"));
  const closeBtn = el("button", "genesis-modal-close", "\u00d7");
  closeBtn.addEventListener("click", () => closeGenesisModal(overlay));
  header.appendChild(closeBtn);
  modal.appendChild(header);

  // Body
  const body = el("div", "genesis-modal-body");

  // Stats bar
  const statsBar = el("div", "genesis-mem-stats");
  for (const t of types) {
    const span = el("span", null, `${t}: `);
    span.appendChild(el("span", "genesis-mem-stat-value", String(stats[t] || 0)));
    statsBar.appendChild(span);
  }
  body.appendChild(statsBar);

  // Tabs
  const tabsDiv = el("div", "genesis-mem-tabs");
  const tabButtons = [];
  for (let i = 0; i < types.length; i++) {
    const tab = el("button", "genesis-mem-tab" + (i === 0 ? " active" : ""), types[i]);
    tab.dataset.type = types[i];
    tabButtons.push(tab);
    tabsDiv.appendChild(tab);
  }
  body.appendChild(tabsDiv);

  // Search
  const searchInput = document.createElement("input");
  searchInput.className = "genesis-mem-search";
  searchInput.placeholder = "Search memories...";
  searchInput.type = "text";
  body.appendChild(searchInput);

  // Results container
  const resultsDiv = el("div");
  resultsDiv.id = "genesis-mem-results";
  body.appendChild(resultsDiv);

  modal.appendChild(body);
  overlay.appendChild(modal);
  document.body.appendChild(overlay);
  requestAnimationFrame(() => overlay.classList.add("visible"));

  // State
  let activeType = "observations";

  async function loadMemories() {
    const q = searchInput.value.trim();
    const url = `/api/genesis/ui/memory/search?type=${activeType}&limit=30${q ? `&q=${encodeURIComponent(q)}` : ""}`;
    const data = await fetchJson(url);
    const results = data?.results || [];

    // Clear results using safe DOM methods
    resultsDiv.replaceChildren();

    if (results.length === 0) {
      resultsDiv.appendChild(el("div", "genesis-mem-empty", "No memories found."));
      return;
    }

    const cCol = contentCols[activeType];
    const tCol = timeCols[activeType];

    for (const r of results) {
      const item = el("div", "genesis-mem-item");
      item.appendChild(el("div", "genesis-mem-item-time", r[tCol] || ""));
      item.appendChild(el("div", "genesis-mem-item-content", r[cCol] || "(empty)"));
      resultsDiv.appendChild(item);
    }
  }

  // Wire tab clicks
  for (const tab of tabButtons) {
    tab.addEventListener("click", () => {
      tabButtons.forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      activeType = tab.dataset.type;
      loadMemories();
    });
  }

  // Wire search
  let searchTimeout;
  searchInput.addEventListener("input", () => {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(loadMemories, 300);
  });

  loadMemories();
}

// ── Sessions Modal ───────────────────────────────────────────────────

async function openGenesisSessionsModal() {
  const existing = document.getElementById("genesis-sessions-modal");
  if (existing) existing.remove();

  const sessions = (await fetchJson("/api/genesis/ui/sessions?limit=50")) || [];
  const filters = ["all", "active", "completed", "background"];

  const overlay = el("div", "genesis-modal-overlay");
  overlay.id = "genesis-sessions-modal";
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeGenesisModal(overlay);
  });

  const modal = el("div", "genesis-modal");

  const header = el("div", "genesis-modal-header");
  header.appendChild(el("span", null, "Genesis Sessions"));
  const closeBtn = el("button", "genesis-modal-close", "\u00d7");
  closeBtn.addEventListener("click", () => closeGenesisModal(overlay));
  header.appendChild(closeBtn);
  modal.appendChild(header);

  const body = el("div", "genesis-modal-body");

  // Stats
  const active = sessions.filter((s) => s.status === "active").length;
  const total = sessions.length;
  const statsBar = el("div", "genesis-mem-stats");
  const totalSpan = el("span", null, "Total: ");
  totalSpan.appendChild(el("span", "genesis-mem-stat-value", String(total)));
  statsBar.appendChild(totalSpan);
  const activeSpan = el("span", null, "Active: ");
  activeSpan.appendChild(el("span", "genesis-mem-stat-value", String(active)));
  statsBar.appendChild(activeSpan);
  body.appendChild(statsBar);

  // Filter tabs
  const tabsDiv = el("div", "genesis-mem-tabs");
  const tabButtons = [];
  for (let i = 0; i < filters.length; i++) {
    const tab = el("button", "genesis-mem-tab" + (i === 0 ? " active" : ""), filters[i]);
    tab.dataset.filter = filters[i];
    tabButtons.push(tab);
    tabsDiv.appendChild(tab);
  }
  body.appendChild(tabsDiv);

  const resultsDiv = el("div");
  body.appendChild(resultsDiv);
  modal.appendChild(body);
  overlay.appendChild(modal);
  document.body.appendChild(overlay);
  requestAnimationFrame(() => overlay.classList.add("visible"));

  let activeFilter = "all";

  function renderSessions() {
    resultsDiv.replaceChildren();
    const filtered = activeFilter === "all" ? sessions
      : activeFilter === "background" ? sessions.filter((s) => s.session_type && s.session_type.startsWith("background"))
      : sessions.filter((s) => s.status === activeFilter);

    if (filtered.length === 0) {
      resultsDiv.appendChild(el("div", "genesis-mem-empty", "No sessions found."));
      return;
    }

    for (const s of filtered) {
      const item = el("div", "genesis-mem-item");
      const timeRow = el("div", "genesis-mem-item-time");
      timeRow.textContent = (s.started_at || "") + "  ";
      const badge = el("span", "genesis-session-badge", s.status);
      timeRow.appendChild(badge);
      item.appendChild(timeRow);

      const content = el("div", "genesis-mem-item-content");
      const parts = [];
      if (s.session_type) parts.push(s.session_type);
      if (s.model) parts.push(s.model);
      if (s.channel) parts.push(s.channel);
      content.textContent = parts.join(" | ") || s.name || "(unnamed)";
      item.appendChild(content);
      resultsDiv.appendChild(item);
    }
  }

  for (const tab of tabButtons) {
    tab.addEventListener("click", () => {
      tabButtons.forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      activeFilter = tab.dataset.filter;
      renderSessions();
    });
  }

  renderSessions();
}

// ── Tasks Modal ─────────────────────────────────────────────────────

async function openGenesisTasksModal() {
  const existing = document.getElementById("genesis-tasks-modal");
  if (existing) existing.remove();

  const data = (await fetchJson("/api/genesis/ui/tasks")) || {};
  const jobs = data.jobs || [];

  const overlay = el("div", "genesis-modal-overlay");
  overlay.id = "genesis-tasks-modal";
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeGenesisModal(overlay);
  });

  const modal = el("div", "genesis-modal");

  const header = el("div", "genesis-modal-header");
  header.appendChild(el("span", null, "Genesis Tasks"));
  const closeBtn = el("button", "genesis-modal-close", "\u00d7");
  closeBtn.addEventListener("click", () => closeGenesisModal(overlay));
  header.appendChild(closeBtn);
  modal.appendChild(header);

  const body = el("div", "genesis-modal-body");

  // Tabs: Tasks | Scheduled Jobs
  const tabs = ["Tasks", "Scheduled Jobs"];
  const tabsDiv = el("div", "genesis-mem-tabs");
  const tabButtons = [];
  for (let i = 0; i < tabs.length; i++) {
    const tab = el("button", "genesis-mem-tab" + (i === 0 ? " active" : ""), tabs[i]);
    tab.dataset.tab = tabs[i];
    tabButtons.push(tab);
    tabsDiv.appendChild(tab);
  }
  body.appendChild(tabsDiv);

  const resultsDiv = el("div");
  body.appendChild(resultsDiv);

  let activeTab = "Tasks";

  function renderTasksTab() {
    resultsDiv.replaceChildren();
    if (activeTab === "Tasks") {
      const empty = el("div", "genesis-mem-empty");
      empty.textContent = "No active tasks. Tasks are dispatched work items like research, code changes, or analysis.";
      resultsDiv.appendChild(empty);
    } else {
      // Scheduled Jobs
      if (jobs.length === 0) {
        resultsDiv.appendChild(el("div", "genesis-mem-empty", "No scheduled jobs running."));
      } else {
        for (const j of jobs) {
          const item = el("div", "genesis-mem-item");
          const timeRow = el("div", "genesis-mem-item-time");
          timeRow.textContent = "Last: " + (j.last_run || "never") + "  ";
          const badge = el("span", "genesis-session-badge", String(j.total_runs) + " heartbeats");
          timeRow.appendChild(badge);
          item.appendChild(timeRow);
          item.appendChild(el("div", "genesis-mem-item-content", j.name));
          resultsDiv.appendChild(item);
        }
      }
    }
  }

  for (const tab of tabButtons) {
    tab.addEventListener("click", () => {
      tabButtons.forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      activeTab = tab.dataset.tab;
      renderTasksTab();
    });
  }

  renderTasksTab();
  modal.appendChild(body);
  overlay.appendChild(modal);
  document.body.appendChild(overlay);
  requestAnimationFrame(() => overlay.classList.add("visible"));
}

// ── Inbox Modal ─────────────────────────────────────────────────────

async function openGenesisInboxModal() {
  const existing = document.getElementById("genesis-inbox-modal");
  if (existing) existing.remove();

  const items = (await fetchJson("/api/genesis/ui/inbox?limit=50")) || [];
  const filters = ["all", "pending", "completed", "failed"];

  const overlay = el("div", "genesis-modal-overlay");
  overlay.id = "genesis-inbox-modal";
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeGenesisModal(overlay);
  });

  const modal = el("div", "genesis-modal");

  const header = el("div", "genesis-modal-header");
  header.appendChild(el("span", null, "Genesis Inbox"));
  const closeBtn = el("button", "genesis-modal-close", "\u00d7");
  closeBtn.addEventListener("click", () => closeGenesisModal(overlay));
  header.appendChild(closeBtn);
  modal.appendChild(header);

  const body = el("div", "genesis-modal-body");

  // Stats
  const statsBar = el("div", "genesis-mem-stats");
  const totalSpan = el("span", null, "Total: ");
  totalSpan.appendChild(el("span", "genesis-mem-stat-value", String(items.length)));
  statsBar.appendChild(totalSpan);
  const pending = items.filter((i) => i.status === "pending").length;
  if (pending > 0) {
    const pendSpan = el("span", null, "Pending: ");
    pendSpan.appendChild(el("span", "genesis-mem-stat-value", String(pending)));
    statsBar.appendChild(pendSpan);
  }
  body.appendChild(statsBar);

  // Filter tabs
  const tabsDiv = el("div", "genesis-mem-tabs");
  const tabButtons = [];
  for (let i = 0; i < filters.length; i++) {
    const tab = el("button", "genesis-mem-tab" + (i === 0 ? " active" : ""), filters[i]);
    tab.dataset.filter = filters[i];
    tabButtons.push(tab);
    tabsDiv.appendChild(tab);
  }
  body.appendChild(tabsDiv);

  const resultsDiv = el("div");
  body.appendChild(resultsDiv);
  modal.appendChild(body);
  overlay.appendChild(modal);
  document.body.appendChild(overlay);
  requestAnimationFrame(() => overlay.classList.add("visible"));

  let activeFilter = "all";

  function renderInbox() {
    resultsDiv.replaceChildren();
    const filtered = activeFilter === "all" ? items
      : items.filter((i) => i.status === activeFilter);

    if (filtered.length === 0) {
      resultsDiv.appendChild(el("div", "genesis-mem-empty", "No inbox items found."));
      return;
    }

    for (const item of filtered) {
      const row = el("div", "genesis-mem-item");
      const timeRow = el("div", "genesis-mem-item-time");
      timeRow.textContent = (item.created_at || "") + "  ";
      const badge = el("span", "genesis-session-badge", item.status);
      timeRow.appendChild(badge);
      row.appendChild(timeRow);

      // Show filename (basename of file_path)
      const filename = (item.file_path || "").split("/").pop() || "(unknown)";
      row.appendChild(el("div", "genesis-mem-item-content", filename));
      resultsDiv.appendChild(row);
    }
  }

  for (const tab of tabButtons) {
    tab.addEventListener("click", () => {
      tabButtons.forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      activeFilter = tab.dataset.filter;
      renderInbox();
    });
  }

  renderInbox();
}

function closeGenesisModal(overlay) {
  overlay.classList.remove("visible");
  setTimeout(() => overlay.remove(), 150);
}

// ── Batch 6: Sidebar — Sessions Label ────────────────────────────────

function rewireSidebarLabels() {
  waitForAlpine(() => {
    const observer = new MutationObserver(() => {
      const headers = document.querySelectorAll(".section-header, h3");
      for (const h of headers) {
        if (h.textContent.trim() === "Chats") {
          h.textContent = "Sessions";
        }
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
    setTimeout(() => observer.disconnect(), 15000);
  });
}

// ── Initialization ───────────────────────────────────────────────────

function init() {
  console.log("[Genesis] UI overlay initializing");

  // Batch 2: Branding
  rebrandLogo();
  rebrandVersionLabel();

  // Batch 3: Rewire AZ features to Genesis equivalents
  rewireDropdownItems();
  rewireSchedulerButton();
  hideSettingsTabs();

  // Batch 4: Welcome screen
  rewireWelcomeCards();
  patchWelcomeStore();

  // Batch 5: Memory
  rewireMemoryButton();

  // Batch 6: Sidebar
  rewireSidebarLabels();

  console.log("[Genesis] UI overlay initialized");
}

// Run when DOM is ready
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
