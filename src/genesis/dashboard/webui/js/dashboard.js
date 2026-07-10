    import { fetchApi, getBackoffFailures } from "/js/api.js";
    import { fmtAge, chipState, chipGlyph } from "/js/ui-utils.js";

    document.addEventListener("alpine:init", () => {
      Alpine.store("genesisDashboard", {
        // Tab state
        activeTab: "overview",
        _tabInitialized: { overview: false, chat: false, internals: false, config: false, files: false, work: false, "follow-ups": false, observations: false, traces: false, autonomy: false, memory: false, knowledge: false, campaigns: false, references: false, backup: false },
        // ── Campaigns tab state ──
        campaignsList: [],
        campaignsMutating: {},
        campaignDetail: null,
        campaignEdit: null,

        // State
        health: {},
        activity: [],
        sessions: [],

        configFiles: [],
        // providerActivity moved to operationalVitals
        errorSummary: { groups: [], active_alerts: [], totals: { events: 0, dead_letters: 0, deferred_failures: 0 } },
        budgets: [],
        // providerHealthSummary removed — provider data now in health.api_keys
        routingConfig: null,
        approvals: [],
        queueReview: {
          clearingIds: {},
          clearingAll: false,
          message: null,
          error: null,
        },
        budgetEditor: {
          budget_type: "daily",
          limit_usd: "",
          warning_pct: "0.8",
          saving: false,
          message: null,
          error: null,
        },
        routingEditor: {
          siteId: null,
          chainText: "",
          default_paid: false,
          never_pays: false,
          saving: false,
          reloading: false,
          message: null,
          error: null,
          reloadMessage: null,
          reloadError: null,
        },
        filters: { subsystem: "", minSeverity: "info" },
        sessionFilters: { status: "", sessionType: "", channel: "" },
        pauseState: { paused: false, reason: null, since: null, toggling: false },
        activityTimeRange: "24h",
        expandedSession: null,
        sessionEvents: [],
        selectedModule: null,
        loading: true,
        lastHealthUpdate: null,
        configExpanded: false,
        configCategoryFilter: null,
        configModal: {
          open: false,
          name: "",
          content: "",
          loadedName: "",
          syntax: "markdown",
          editable: true,
          deletable: false,
          dirty: false,
          saving: false,
          saveMessage: null,
          saveError: null,
          fetch: { state: "idle", lastSuccess: null, error: null },
        },
        surplusModal: {
          open: false,
          data: null,
          filter: null,
          fetch: { state: "idle", lastSuccess: null, error: null },
        },
        outreachModal: {
          open: false,
          messages: [],
          pendingApprovals: [],
          categoryFilter: null,
          fetch: { state: "idle", lastSuccess: null, error: null },
        },
        mcpModal: {
          open: false,
          data: null,
          fetch: { state: "idle", lastSuccess: null, error: null },
        },
        egoModal: {
          open: false,
          tab: "overview",
          proposalFilter: null,
          cycles: [],
          proposals: [],
          cadence: null,
          followUps: [],
          vcr: null,
          expandedCycle: null,
          rejectingId: null,
          rejectReason: "",
          fetch: { state: "idle", lastSuccess: null, error: null },
        },
        egoStatus: null,
        apiKeysModal: { open: false },
        modules: [],
        providersDetail: [],
        operationalVitals: null,
        autonomousCliPolicy: null,
        cognitiveState: null,
        essentialKnowledge: null,
        awarenessSignals: null,
        jobHealth: null,
        schedulerData: { subsystems: [], total_jobs: 0 },
        userJobs: { jobs: [] },
        evalMetrics: null,
        subsystemGrades: null,
        autonomyConfig: null,
        approvalActions: {},
        _restartingBridge: false,
        _restartingHF: false,
        error: null,

        // ── Chat terminal state ───────────────────────────────────
        _terminalWindow: null,

        // ── Files tab state ────────────────────────────────────────
        fileBrowser: {
          path: null,
          parent: null,
          entries: [],
          loading: false,
          selectedFile: null,
          fileContent: null,
          fileMode: "text",
          fileWritable: false,
          fileDirty: false,
          saving: false,
          sidebarCollapsed: false,
          _resizing: false,
        },
        fileUpload: { dragging: false, uploading: false, error: null, success: null, progress: null },

        // ── Work tab state (tasks + sessions + follow-ups) ──────
        taskList: [],
        taskActive: {},
        taskExpanded: null,
        taskDetail: null,
        taskDetailSteps: [],
        taskTimeline: [],
        taskLinkedFollowUps: [],
        tasksView: 'active',       // 'active' or 'completed'
        sessionsList: [],
        sessionsView: 'active',    // 'active' or 'history'
        sessionsExpanded: null,

        // ── Comms state (on Chat tab) ────────────────────────────
        commsOutreach: [],
        commsProposals: [],
        commsPendingApprovals: [],
        commsCounts: {},
        commsView: 'pending',
        commsTab: 'proposals',
        commsProposalFilter: null,
        commsCategoryFilter: null,
        commsRejectingId: null,
        commsRejectReason: '',

        // ── Memory tab state ──────────────────────────────────────
        memorySearch: { query: "", results: [], loading: false },
        memoryRecent: { items: [], total: 0, loading: false },
        memoryStats: {},
        memoryDetail: null,
        knowledgeQuery: "",
        knowledgeSearchResults: [],
        knowledgeRecent: [],
        knowledgeStats: null,
        knowledgeDetail: null,
        // Upload state
        knowledgeUpload: {
          dragging: false,
          file: null,
          uploading: false,
          confirm: false,
          uploadId: null,
          projectType: "",
          domain: "",
          purpose: "",
          context: "",
          mode: "extract",
          error: null,
        },
        knowledgeUploads: [],
        knowledgeTaxonomy: { project_types: [], domains: [] },
        _uploadPollInterval: null,
        // Tracked GitHub repositories (recon watchlist)
        watchlistEntries: [],
        watchlistForm: { name: '', repo: '', track: 'releases,commits', priority: 'medium', notes: '' },
        watchlistMsg: null,
        watchlistSaving: false,

        // ── References tab state ──────────────────────────────────
        referenceByKind: {},
        referenceTotal: 0,
        referenceQuery: "",
        referenceSearchResults: [],
        referenceKindFilter: "",
        referenceKinds: [],
        referenceStats: null,
        referenceDetail: null,
        revealedValues: {},

        // ── Confirm modal state ──────────────────────────────────
        confirmModal: { show: false, title: '', message: '', _resolve: null },

        // ── Backup tab state ──────────────────────────────────────
        backupStatus: null,
        backupConfig: null,
        backupConfigForm: {
          repo: '', tier2_backend: 'none', local_path: '',
          nas: '', nas_user: '', nas_pass: '', passphrase: '',
          schedule_interval: '6h', schedule_enabled: true,
        },
        backupConfigSaving: false,
        backupConfigMsg: null,
        // Only POST the schedule (a stateful cron side-effect) when the user
        // actually touches the schedule controls — a plain Save must not
        // silently install/replace the cron job.
        scheduleDirty: false,

        // ── Update state ─────────────────────────────────────────
        updateStatus: null,
        updateProgress: null,
        _updating: false,
        _updateReconnecting: false,
        _checkingForUpdates: false,
        _checkUpToDate: false,
        _resolvingConflicts: false,

        // ── Settings hub state ────────────────────────────────────
        settingsDomains: null,
        settingsData: {},         // domain_name → config object
        settingsEditing: null,    // domain being edited
        settingsViewing: null,    // readonly domain being viewed
        systemTimezone: null,     // from /api/genesis/settings/timezone
        settingsSaving: false,
        settingsRestartMessage: null,

        // ── Auth state ────────────────────────────────────────────
        authEnabled: false,

        // ── Provider Keys state ──────────────────────────────────
        secretsGroups: null,
        secretsEditing: {},       // key_name → true when input open
        secretsSaving: false,
        secretsValues: {},        // key_name → input value during edit
        secretsMessage: null,     // {type, text}

        fetchState: {
          health: { state: "idle", lastSuccess: null, error: null },
          activity: { state: "idle", lastSuccess: null, error: null },
          sessions: { state: "idle", lastSuccess: null, error: null },

          // providerActivity removed — data now in operationalVitals
          attention: { state: "idle", lastSuccess: null, error: null },
          budgets: { state: "idle", lastSuccess: null, error: null },
          routing: { state: "idle", lastSuccess: null, error: null },
          approvals: { state: "idle", lastSuccess: null, error: null },
          modules: { state: "idle", lastSuccess: null, error: null },
          providersDetail: { state: "idle", lastSuccess: null, error: null },
          cognitive: { state: "idle", lastSuccess: null, error: null },
          awarenessSignals: { state: "idle", lastSuccess: null, error: null },
          jobHealth: { state: "idle", lastSuccess: null, error: null },
          autonomyConfig: { state: "idle", lastSuccess: null, error: null },
          autonomousCliPolicy: { state: "idle", lastSuccess: null, error: null },
          // Tab-list panels: gate "No X found" empty states on first load
          // (state === "loading") so a slow fetch doesn't flash a lie.
          observations: { state: "idle", lastSuccess: null, error: null },
          tasks: { state: "idle", lastSuccess: null, error: null },
          workSessions: { state: "idle", lastSuccess: null, error: null },
          cockpit: { state: "idle", lastSuccess: null, error: null },
          comms: { state: "idle", lastSuccess: null, error: null },
          autonomyGrants: { state: "idle", lastSuccess: null, error: null },
          autonomySends: { state: "idle", lastSuccess: null, error: null },
        },

        // Polling interval IDs
        _healthInterval: null,
        _activityInterval: null,

        _sessionsInterval: null,
        _modulesInterval: null,
        _routingInterval: null,
        _approvalsInterval: null,
        _cognitiveInterval: null,
        _ekInterval: null,
        _awarenessInterval: null,
        _jobHealthInterval: null,
        _tasksInterval: null,
        _workSessionsInterval: null,
        _observationsInterval: null,
        _cockpitInterval: null,
        _commsInterval: null,
        _tracesInterval: null,

        // Observations tab state
        observationsList: [],
        observationsHasMore: false,
        observationsExpanded: null,
        observationsView: 'unresolved',
        observationsFilters: { priority: '', type: '', source: '' },
        observationsAvailableFilters: { types: [], sources: [] },
        observationsCounts: { counts: {}, total_unsurfaced: 0, total_unresolved: 0 },
        observationsResolving: {},
        observationsMarkingRead: {},
        observationsBatchIds: {},

        // Follow-ups cockpit tab state
        cockpitList: [],
        cockpitTotal: 0,
        cockpitPage: 1,
        cockpitPageSize: 50,
        cockpitFilters: { kind: 'follow_up', domain: '', status: '', source: '', search: '' },
        cockpitSort: 'priority',
        cockpitHideDone: true,   // default: hide completed/failed (actionable view)
        cockpitAvailableFilters: { sources: [], statuses: [] },
        cockpitExpanded: null,
        cockpitBatchIds: {},
        cockpitMutating: {},

        // Traces tab state
        tracesList: [],
        tracesExpanded: null,
        tracesWaterfall: { rows: [], traceStart: 0, traceTotal: 0 },
        tracesSpanExpanded: null,
        tracesLoadingTrace: false,
        _traceReq: 0,

        // Autonomy tab state
        autonomyGrants: [],
        autonomySends: [],
        autonomyLoading: false,
        autonomyFlagging: {},
        _autonomyInterval: null,

        // Tab management
        _TAB_INTERVALS: {
          overview:  ["_cognitiveInterval", "_ekInterval", "_modulesInterval"],
          chat:      ["_commsInterval", "_approvalsInterval"],
          internals: ["_awarenessInterval", "_routingInterval", "_jobHealthInterval", "_activityInterval", "_sessionsInterval"],
          config:    [],
          files:     [],
          work:      ["_tasksInterval", "_workSessionsInterval"],
          observations: ["_observationsInterval"],
          "follow-ups": ["_cockpitInterval"],
          traces:    ["_tracesInterval"],
          autonomy:  ["_autonomyInterval"],
          memory:    [],
          knowledge: [],
          campaigns: ["_campaignsInterval"],
          backup:    [],
        },

        // ── Campaigns ──
        async fetchCampaigns() {
          try {
            const resp = await fetchApi("/api/genesis/campaigns/list");
            if (resp && resp.ok) {
              const data = await resp.json();
              this.campaignsList = data.campaigns || [];
            }
          } catch (e) { console.warn("fetchCampaigns failed:", e); }
        },
        async openCampaignDetail(name) {
          try {
            const resp = await fetchApi(`/api/genesis/campaigns/${encodeURIComponent(name)}/detail`);
            if (resp && resp.ok) { this.campaignDetail = await resp.json(); }
          } catch (e) { console.warn("campaign detail failed:", e); }
        },
        closeCampaignDetail() { this.campaignDetail = null; },
        async _campaignMutate(name, path, body) {
          this.campaignsMutating = { ...this.campaignsMutating, [name]: true };
          try {
            const resp = await fetchApi(`/api/genesis/campaigns/${encodeURIComponent(name)}${path}`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body || {}),
            });
            const data = await resp.json().catch(() => ({}));
            if (resp && resp.ok) {
              await this.fetchCampaigns();
              if (this.campaignDetail && this.campaignDetail.name === name) {
                await this.openCampaignDetail(name);
              }
              return true;
            }
            alert("Campaign action failed: " + (data.error || resp.status));
          } catch (e) {
            console.warn("campaign mutate failed:", e);
            alert("Campaign action failed: " + e);
          } finally {
            const m = { ...this.campaignsMutating }; delete m[name]; this.campaignsMutating = m;
          }
          return false;
        },
        pauseCampaign(name) { return this._campaignMutate(name, "/pause", {}); },
        resumeCampaign(name) { return this._campaignMutate(name, "/resume", {}); },
        triggerCampaign(name) {
          if (!confirm(`Trigger '${name}' now? This dispatches a session and may incur cost.`)) return;
          return this._campaignMutate(name, "/trigger", {});
        },
        startCampaignEdit(c) {
          this.campaignEdit = {
            name: c.name,
            cron_cadence: c.cadence || "",
            model: c.model || "sonnet",
            effort: c.effort || "medium",
            max_daily_cost_usd: c.max_daily_cost_usd ?? 1.0,
            jitter_seconds: c.jitter_seconds ?? 0,
          };
        },
        cancelCampaignEdit() { this.campaignEdit = null; },
        async saveCampaignEdit() {
          const e = this.campaignEdit; if (!e) return;
          const ok = await this._campaignMutate(e.name, "/update", {
            cron_cadence: e.cron_cadence,
            model: e.model,
            effort: e.effort,
            max_daily_cost_usd: e.max_daily_cost_usd,
            jitter_seconds: e.jitter_seconds,
          });
          if (ok) this.campaignEdit = null;
        },

        initTab() {
          const hash = location.hash.replace("#", "") || "overview";
          const valid = ["overview", "chat", "internals", "config", "files", "work", "follow-ups", "observations", "traces", "autonomy", "memory", "knowledge", "campaigns", "references", "backup"];
          this.activeTab = valid.includes(hash) ? hash : "overview";
          window.addEventListener("hashchange", () => {
            const h = location.hash.replace("#", "");
            if (valid.includes(h) && h !== this.activeTab) {
              this._onTabChange(this.activeTab, h);
              this.activeTab = h;
            }
          });
        },

        _onTabChange(oldTab, newTab) {
          this._stopTabIntervals(oldTab);
          this._startTabIntervals(newTab);
        },

        _stopTabIntervals(tab) {
          for (const key of (this._TAB_INTERVALS[tab] || [])) {
            if (this[key]) { clearInterval(this[key]); this[key] = null; }
          }
        },

        _startTabIntervals(tab) {
          const first = !this._tabInitialized[tab];
          this._tabInitialized[tab] = true;
          switch (tab) {
            case "overview":
              if (first) { this.fetchCognitive(); this.fetchEssentialKnowledge(); this.fetchModules(); this.fetchProvidersDetail(); }
              this._cognitiveInterval = setInterval(() => this.fetchCognitive(), 60000);
              this._ekInterval = setInterval(() => this.fetchEssentialKnowledge(), 120000);
              this._modulesInterval = setInterval(() => { this.fetchModules(); this.fetchProvidersDetail(); }, 30000);
              break;
            case "internals":
              if (first) { this.fetchAwarenessSignals(); this.fetchOperationalVitals(); this.fetchAutonomyConfig(); this.fetchAutonomousCliPolicy(); this.fetchRoutingConfig(); this.fetchJobHealth(); this.fetchSchedulerData(); this.fetchEvalMetrics(); this.fetchApprovals(); this.fetchActivity(); this.fetchSessions(); }
              this._awarenessInterval = setInterval(() => this.fetchAwarenessSignals(), 30000);
              this._routingInterval = setInterval(() => this.fetchRoutingConfig(), 15000);
              this._jobHealthInterval = setInterval(() => { this.fetchJobHealth(); this.fetchSchedulerData(); }, 60000);
              this._activityInterval = setInterval(() => this.fetchActivity(), 10000);
              this._sessionsInterval = setInterval(() => this.fetchSessions(), 30000);
              break;
            case "config":
              if (first) { this.fetchConfigFiles(); this.fetchBudgets(); this.fetchSettingsDomains(); this.fetchAutonomousCliPolicy(); this.fetchSecrets(); }
              break;
            case "chat":
              // Re-fit terminal on tab re-entry
              if (!first && this._xterm && this._fitAddon) { this._fitAddon.fit(); }
              if (first) { this.fetchComms(); this.fetchApprovals(); }
              this._commsInterval = setInterval(() => this.fetchComms(), 15000);
              this._approvalsInterval = setInterval(() => this.fetchApprovals(), 15000);
              break;
            case "files":
              if (first) { this.fetchFiles(); }
              break;
            case "work":
              if (first) { this.fetchTasks(); this.fetchWorkSessions(); }
              this._tasksInterval = setInterval(() => this.fetchTasks(), 5000);
              this._workSessionsInterval = setInterval(() => this.fetchWorkSessions(), 10000);
              break;
            case "observations":
              if (first) { this.fetchObservations(); this.fetchObservationsFilters(); }
              // Summary refresh lives on the global 15s poll (badge is chrome).
              this._observationsInterval = setInterval(() => this.fetchObservations(), 30000);
              break;
            case "follow-ups":
              if (first) { this.fetchCockpit(); this.fetchCockpitFilters(); }
              this._cockpitInterval = setInterval(() => this.fetchCockpit(), 30000);
              break;
            case "traces":
              if (first) { this.fetchSpansRecent(); }
              this._tracesInterval = setInterval(() => this.fetchSpansRecent(), 30000);
              break;
            case "autonomy":
              if (first) { this.refreshAutonomy(); }
              this._autonomyInterval = setInterval(() => { this.fetchAutonomyGrants(); this.fetchAutonomySends(); }, 30000);
              break;
            case "memory":
              if (first) { this.fetchMemoryRecent(); this.fetchMemoryStats(); }
              break;
            case "knowledge":
              if (first) { this.fetchKnowledgeRecent(); this.fetchKnowledgeStats(); this.fetchKnowledgeUploads(); this.fetchKnowledgeTaxonomy(); this.fetchWatchlist(); }
              // Resume polling if any uploads are processing
              if (this.knowledgeUploads.some(u => u.status === "processing")) { this._startUploadPolling(); }
              break;
            case "campaigns":
              if (first) { this.fetchCampaigns(); }
              this._campaignsInterval = setInterval(() => this.fetchCampaigns(), 30000);
              break;
            case "references":
              if (first) { this.fetchReferenceList(); this.fetchReferenceStats(); }
              break;
            case "backup":
              if (first) { this.fetchBackupStatus(); this.fetchBackupConfig(); this.fetchUpdateStatus(); this.fetchUpdateProgress(); }
              break;
          }
        },

        navigateTo(tab, anchor) {
          if (this.activeTab !== tab) {
            location.hash = tab;
            if (anchor) {
              setTimeout(() => {
                const el = document.getElementById(anchor);
                if (el) el.scrollIntoView({ behavior: "smooth" });
              }, 50);
            }
          } else if (anchor) {
            const el = document.getElementById(anchor);
            if (el) el.scrollIntoView({ behavior: "smooth" });
          }
        },

        // Lifecycle
        async onOpen() {
          // Check auth status (non-blocking, best-effort)
          this.checkAuth();

          // Always fetch: health, errors, pause, update status, approvals,
          // observations summary (badge is chrome — needed on all tabs)
          await Promise.all([
            this.fetchHealth(),
            this.fetchErrorSummary(),
            this.fetchPauseState(),
            this.fetchUpdateStatus(),
            this.fetchEgoStatus(),
            this.fetchApprovals(),
            this.fetchObservationsSummary(),
          ]);

          // Initialize tab from URL hash and fetch tab-specific data
          this.initTab();
          this._startTabIntervals(this.activeTab);

          this.loading = false;

          // Always-on polling: health snapshot drives the attention strip on all tabs.
          // Uses probabilistic skip when server is failing to reduce load during
          // recovery (exponential backoff is in api.js; this reduces poll frequency).
          this._healthInterval = setInterval(() => {
            const failures = typeof getBackoffFailures === "function" ? getBackoffFailures() : 0;
            if (failures > 0) {
              const skipProb = 1 - (1 / Math.pow(2, Math.min(failures, 6)));
              if (Math.random() < skipProb) return;
            }
            this.fetchHealth();
            this.fetchErrorSummary();
            this.fetchPauseState();
            this.fetchEgoStatus();
            this.fetchObservationsSummary();
          }, 15000);
        },

        cleanup() {
          if (this._healthInterval) clearInterval(this._healthInterval);
          for (const tab of ["overview", "chat", "internals", "config", "work", "observations", "traces", "autonomy"]) {
            this._stopTabIntervals(tab);
          }
          // Terminal runs in its own window — no cleanup needed here
        },

        // Fetch methods
        async fetchHealth() {
          this.startFetch("health");
          try {
            const resp = await fetchApi("/api/genesis/health");
            if (resp && resp.ok) {
              this.health = await resp.json();
              this.lastHealthUpdate = Date.now();
              this.error = null;
              this.finishFetch("health");
            } else {
              this.failFetch("health", "Health endpoint returned an error");
            }
          } catch (e) {
            console.warn("Health fetch failed:", e);
            this.error = "Failed to fetch health data";
            this.failFetch("health", "Failed to fetch health data");
          }
        },

        get healthStale() {
          if (!this.lastHealthUpdate) return false;
          return (Date.now() - this.lastHealthUpdate) > 60000;
        },

        // fetchProviderHealth() removed — provider data now in health.api_keys

        async fetchActivity() {
          this.startFetch("activity");
          try {
            let url = "/api/genesis/activity?limit=100";
            if (this.filters.minSeverity) url += `&min_severity=${this.filters.minSeverity}`;
            if (this.filters.subsystem) url += `&subsystem=${this.filters.subsystem}`;
            // Time range filter
            const ranges = { "1h": 1, "6h": 6, "24h": 24, "7d": 168 };
            const hours = ranges[this.activityTimeRange] || 1;
            const since = new Date(Date.now() - hours * 3600000).toISOString();
            url += `&since=${since}`;
            const resp = await fetchApi(url);
            if (resp && resp.ok) {
              this.activity = await resp.json();
              this.finishFetch("activity");
            } else {
              this.failFetch("activity", "Activity feed unavailable");
            }
          } catch (e) {
            console.warn("Activity fetch failed:", e);
            this.failFetch("activity", "Activity feed unavailable");
          }
        },

        async fetchSessions() {
          this.startFetch("sessions");
          try {
            let url = "/api/genesis/sessions?limit=50";
            if (this.sessionFilters.status) url += `&status=${this.sessionFilters.status}`;
            if (this.sessionFilters.sessionType) url += `&session_type=${this.sessionFilters.sessionType}`;
            if (this.sessionFilters.channel) url += `&channel=${this.sessionFilters.channel}`;
            const resp = await fetchApi(url);
            if (resp && resp.ok) {
              this.sessions = await resp.json();
              this.finishFetch("sessions");
            } else {
              this.failFetch("sessions", "Session history unavailable");
            }
          } catch (e) {
            console.warn("Sessions fetch failed:", e);
            this.failFetch("sessions", "Session history unavailable");
          }
        },

        async toggleSession(sessionId) {
          if (this.expandedSession === sessionId) {
            this.expandedSession = null;
            this.sessionEvents = [];
            return;
          }
          this.expandedSession = sessionId;
          try {
            const resp = await fetchApi(`/api/genesis/sessions/${sessionId}/events`);
            if (resp && resp.ok) this.sessionEvents = await resp.json();
          } catch (e) { this.sessionEvents = []; }
        },


        async fetchConfigFiles() {
          try {
            const resp = await fetchApi("/api/genesis/config-files");
            if (resp && resp.ok) this.configFiles = await resp.json();
          } catch (e) { console.warn("Config files fetch failed:", e); }
        },

        async fetchModules() {
          this.startFetch("modules");
          try {
            const resp = await fetchApi("/api/genesis/modules");
            if (resp && resp.ok) {
              this.modules = await resp.json();
              this.finishFetch("modules");
            } else {
              this.failFetch("modules", "Modules endpoint unavailable");
            }
          } catch (e) {
            console.error("Modules fetch failed:", e);
            this.failFetch("modules", "Modules endpoint unavailable");
          }
        },

        async fetchProvidersDetail() {
          this.startFetch("providersDetail");
          try {
            const resp = await fetchApi("/api/genesis/providers-detail");
            if (resp && resp.ok) {
              this.providersDetail = await resp.json();
              this.finishFetch("providersDetail");
            } else {
              this.failFetch("providersDetail", "Providers endpoint unavailable");
            }
          } catch (e) {
            console.error("Providers detail fetch failed:", e);
            this.failFetch("providersDetail", "Providers endpoint unavailable");
          }
        },

        async fetchOperationalVitals() {
          try {
            const resp = await fetchApi("/api/genesis/operational-vitals");
            if (resp?.ok) this.operationalVitals = await resp.json();
          } catch (e) {
            console.warn("Operational vitals fetch failed:", e);
          }
        },

        async fetchErrorSummary() {
          this.startFetch("attention");
          try {
            const resp = await fetchApi("/api/genesis/unified-errors?grouped=true&limit=6");
            if (resp && resp.ok) {
              this.errorSummary = await resp.json();
              this.finishFetch("attention");
            } else {
              this.failFetch("attention", "Error summary unavailable");
            }
          } catch (e) {
            console.warn("Unified errors fetch failed:", e);
            this.failFetch("attention", "Error summary unavailable");
          }
        },

        async fetchBudgets() {
          this.startFetch("budgets");
          try {
            const resp = await fetchApi("/api/genesis/budgets");
            if (resp && resp.ok) {
              this.budgets = await resp.json();
              this.syncBudgetEditor();
              this.finishFetch("budgets");
            } else {
              this.failFetch("budgets", "Budget config unavailable");
            }
          } catch (e) {
            console.warn("Budget fetch failed:", e);
            this.failFetch("budgets", "Budget config unavailable");
          }
        },

        // ── Chat terminal (opens in new window) ─────────────────
        openTerminal() {
          if (this._terminalWindow && !this._terminalWindow.closed) {
            this._terminalWindow.focus();
            return;
          }
          this._terminalWindow = window.open("/genesis/terminal", "genesis-terminal");
        },

        // ── Files tab fetches ─────────────────────────────────────
        async fetchFiles(path) {
          this.fileBrowser.loading = true;
          try {
            const url = path ? `/api/genesis/files?path=${encodeURIComponent(path)}` : "/api/genesis/files";
            const resp = await fetchApi(url);
            if (resp && resp.ok) {
              const data = await resp.json();
              this.fileBrowser.path = data.path;
              this.fileBrowser.parent = data.parent;
              this.fileBrowser.entries = data.entries;
              this.fileBrowser.selectedFile = null;
              this.fileBrowser.fileContent = null;
            }
          } catch (e) { console.warn("Files fetch failed:", e); }
          this.fileBrowser.loading = false;
        },
        async openFile(filePath) {
          try {
            const resp = await fetchApi(`/api/genesis/files/read?path=${encodeURIComponent(filePath)}`);
            if (resp && resp.ok) {
              const data = await resp.json();
              this.fileBrowser.selectedFile = data.path;
              this.fileBrowser.fileContent = data.content;
              this.fileBrowser.fileMode = data.mode;
              this.fileBrowser.fileWritable = data.writable;
              this.fileBrowser.fileDirty = false;
            }
          } catch (e) { console.warn("File read failed:", e); }
        },
        async saveFile() {
          if (!this.fileBrowser.selectedFile) return;
          this.fileBrowser.saving = true;
          try {
            const resp = await fetchApi("/api/genesis/files/write", {
              method: "PUT",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ path: this.fileBrowser.selectedFile, content: this.fileBrowser.fileContent }),
            });
            if (resp && resp.ok) { this.fileBrowser.fileDirty = false; }
            else { const d = await resp.json(); alert(d.error || "Save failed"); }
          } catch (e) { alert("Save failed: " + e.message); }
          this.fileBrowser.saving = false;
        },
        // Recursively walk dropped directory entries (File and Directory
        // Entries API) into a flat list of { file, relpath }. Folders dropped
        // onto the zone arrive as FileSystemEntry objects, not plain Files —
        // without this they'd be appended to FormData as unreadable dirs and
        // the request would hang. The entries themselves stay valid across
        // awaits; only the parent DataTransferItemList does not, so callers
        // must capture entries synchronously before the first await.
        async _collectDroppedFiles(entries) {
          const out = [];
          const walk = async (entry) => {
            if (entry.isFile) {
              const file = await new Promise((res, rej) => entry.file(res, rej));
              out.push({ file, relpath: entry.fullPath.replace(/^\/+/, "") });
            } else if (entry.isDirectory) {
              const reader = entry.createReader();
              // readEntries returns at most ~100 children per call; loop until
              // it yields an empty batch.
              while (true) {
                const batch = await new Promise((res, rej) => reader.readEntries(res, rej));
                if (!batch.length) break;
                for (const child of batch) await walk(child);
              }
            }
          };
          for (const e of entries) await walk(e);
          return out;
        },
        async uploadFile(event) {
          event.preventDefault();
          this.fileUpload.dragging = false;
          // Re-entrancy guard: ignore new drops/selections while a batch is in
          // flight, so concurrent runs don't interleave the shared uploading/
          // progress flags and misreport UI state.
          if (this.fileUpload.uploading) return;
          this.fileUpload.error = null;
          this.fileUpload.success = null;

          // Capture directory entries SYNCHRONOUSLY — the DataTransferItemList
          // is invalidated once this handler yields to an await.
          let dirEntries = null;
          const dt = event.dataTransfer;
          if (dt && dt.items && dt.items.length && typeof dt.items[0].webkitGetAsEntry === "function") {
            dirEntries = Array.from(dt.items, (it) => it.webkitGetAsEntry()).filter(Boolean);
          }

          this.fileUpload.uploading = true;
          // Build the work list of { file, relpath }. If an actual folder was
          // dropped, walk all entries (files + folders) preserving structure;
          // otherwise use the flat file list (drop or click-to-browse).
          const droppedFolder = !!(dirEntries && dirEntries.some((e) => e && e.isDirectory));
          let items = [];
          try {
            if (droppedFolder) {
              this.fileUpload.progress = "Reading folder…";
              items = await this._collectDroppedFiles(dirEntries);
            } else {
              const flat = Array.from(dt?.files || event.target?.files || []);
              items = flat.map((f) => ({ file: f, relpath: f.name }));
            }
          } catch (e) {
            this.fileUpload.error = `Could not read dropped items: ${e.message}`;
            this.fileUpload.progress = null;
            this.fileUpload.uploading = false;
            return;
          }
          if (items.length === 0) {
            this.fileUpload.progress = null;
            this.fileUpload.uploading = false;
            if (droppedFolder) this.fileUpload.error = "No files found in the dropped folder.";
            if (event.target && event.target.value !== undefined) { event.target.value = ""; }
            return;
          }

          const uploaded = [];
          const failures = [];
          let lastDir = null;
          // Uploads root (…/uploads), derived from the first success by stripping
          // the returned relative path — used as the destination shown for
          // folder / multi-file uploads instead of one file's deep subfolder.
          let uploadRoot = null;
          // Upload each file sequentially via the single-file endpoint so every
          // file gets the backend's sanitize / dedup / size-cap checks. The
          // file's relative path (relpath) lets the backend recreate folder
          // structure under the uploads root.
          for (let i = 0; i < items.length; i++) {
            const { file: f, relpath } = items[i];
            this.fileUpload.progress = items.length > 1
              ? `Uploading ${i + 1} of ${items.length}: ${relpath}`
              : `Uploading ${relpath}...`;
            try {
              const form = new FormData();
              form.append("file", f);
              form.append("relpath", relpath);
              const resp = await fetchApi("/api/genesis/files/upload", { method: "POST", body: form });
              if (resp && resp.ok) {
                const data = await resp.json();
                uploaded.push(data.filename);
                lastDir = data.path.substring(0, data.path.lastIndexOf("/"));
                if (uploadRoot === null && typeof data.filename === "string") {
                  uploadRoot = data.path.slice(0, data.path.length - data.filename.length).replace(/\/$/, "");
                }
              } else {
                const err = resp ? await resp.json() : {};
                failures.push(`${relpath}: ${err.error || "failed"}`);
              }
            } catch (e) {
              failures.push(`${relpath}: ${e.message}`);
            }
          }
          this.fileUpload.progress = null;
          this.fileUpload.uploading = false;
          if (uploaded.length) {
            // Single file → its own directory; multi/folder → the uploads root.
            const dest = (uploaded.length === 1 ? lastDir : uploadRoot) || "~/.genesis/uploads";
            this.fileUpload.success = uploaded.length === 1
              ? `Uploaded ${uploaded[0]} → ${dest}`
              : `Uploaded ${uploaded.length} files → ${dest} (${uploaded.join(", ")})`;
            // Stay put — refresh the current directory in place (no jump to uploads).
            if (this.fileBrowser.path) { this.fetchFiles(this.fileBrowser.path); }
          }
          if (failures.length) {
            this.fileUpload.error = `${failures.length} failed — ${failures.join("; ")}`;
          }
          // Reset the file input so re-selecting the same file fires @change again.
          if (event.target && event.target.value !== undefined) { event.target.value = ""; }
        },
        _startFileBrowserResize(e, sidebar) {
          this.fileBrowser._resizing = true;
          const startX = e.clientX;
          const startW = sidebar.offsetWidth;
          const container = sidebar.parentElement;
          const maxW = container.offsetWidth * 0.6;
          const onMove = (ev) => {
            const newW = Math.max(150, Math.min(maxW, startW + ev.clientX - startX));
            sidebar.style.flex = '0 0 ' + newW + 'px';
          };
          const onUp = () => {
            this.fileBrowser._resizing = false;
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onUp);
          };
          document.addEventListener('mousemove', onMove);
          document.addEventListener('mouseup', onUp);
        },

        // ── Tasks tab fetches ─────────────────────────────────────
        async fetchTasks() {
          this.startFetch("tasks");
          try {
            const resp = await fetchApi("/api/genesis/tasks?include_completed=true&limit=50");
            if (resp && resp.ok) {
              const data = await resp.json();
              this.taskList = data.tasks || [];
              this.taskActive = data.active || {};
              this.finishFetch("tasks");
            } else {
              this.failFetch("tasks", "Tasks endpoint returned an error");
            }
          } catch (e) {
            console.warn("Tasks fetch failed:", e);
            this.failFetch("tasks", "Failed to fetch tasks");
          }
        },
        async fetchTaskDetail(taskId) {
          if (this.taskExpanded === taskId) { this.taskExpanded = null; this.taskDetail = null; this.taskDetailSteps = []; this.taskTimeline = []; this.taskLinkedFollowUps = []; return; }
          this.taskExpanded = taskId;
          try {
            const resp = await fetchApi(`/api/genesis/tasks/${taskId}`);
            if (resp && resp.ok) {
              const data = await resp.json();
              this.taskDetail = data.task;
              this.taskDetailSteps = data.steps || [];
              this.taskTimeline = data.timeline || [];
              this.taskLinkedFollowUps = data.linked_follow_ups || [];
            }
          } catch (e) { console.warn("Task detail fetch failed:", e); }
        },
        phaseColor(phase) {
          const colors = {
            pending: '#616161', dispatching: '#546e7a', observing: '#78909c', reviewing: '#7e57c2',
            planning: '#9e9e9e', executing: '#2196F3', verifying: '#ab47bc',
            synthesizing: '#26a69a', delivering: '#66bb6a', completed: '#66bb6a',
            failed: '#ef9a9a', cancelled: '#9e9e9e', blocked: '#ffa726', paused: '#ffa726',
          };
          return colors[phase?.toLowerCase()] || '#9e9e9e';
        },
        async taskAction(taskId, action) {
          try {
            await fetchApi(`/api/genesis/tasks/${taskId}/${action}`, { method: "POST" });
            await this.fetchTasks();
            if (this.taskExpanded === taskId) await this.fetchTaskDetail(taskId);
          } catch (e) { console.warn(`Task ${action} failed:`, e); }
        },

        // ── Work tab fetches ──────────────────────────────────────
        async fetchWorkSessions() {
          this.startFetch("workSessions");
          try {
            const resp = await fetchApi(`/api/genesis/work?type=session&view=${this.sessionsView}&limit=50`);
            if (resp && resp.ok) {
              const data = await resp.json();
              this.sessionsList = data.items || [];
              this.finishFetch("workSessions");
            } else {
              this.failFetch("workSessions", "Work endpoint returned an error");
            }
          } catch (e) {
            console.warn("Work sessions fetch failed:", e);
            this.failFetch("workSessions", "Failed to fetch work sessions");
          }
        },

        // ── Observations tab fetches ─────────────────────────────
        async fetchObservations() {
          this.startFetch("observations");
          try {
            const f = this.observationsFilters;
            const params = new URLSearchParams();
            params.set("limit", "50");
            if (this.observationsView === 'unresolved') params.set("resolved", "false");
            if (f.priority) params.set("priority", f.priority);
            if (f.type) params.set("type", f.type);
            if (f.source) params.set("source", f.source);
            const resp = await fetchApi(`/api/genesis/observations?${params}`);
            if (resp && resp.ok) {
              const data = await resp.json();
              this.observationsList = data.observations || [];
              this.observationsHasMore = data.has_more || false;
              this.finishFetch("observations");
            } else {
              this.failFetch("observations", "Observations endpoint returned an error");
            }
          } catch (e) {
            console.warn("Observations fetch failed:", e);
            this.failFetch("observations", "Failed to fetch observations");
          }
        },
        async fetchObservationsSummary() {
          try {
            const resp = await fetchApi("/api/genesis/observations/summary");
            if (resp && resp.ok) {
              this.observationsCounts = await resp.json();
            }
          } catch (e) { console.warn("Observations summary fetch failed:", e); }
        },
        async fetchObservationsFilters() {
          try {
            const resp = await fetchApi("/api/genesis/observations/filters");
            if (resp && resp.ok) {
              this.observationsAvailableFilters = await resp.json();
            }
          } catch (e) { console.warn("Observations filters fetch failed:", e); }
        },
        async resolveObservation(id, notes) {
          this.observationsResolving = { ...this.observationsResolving, [id]: true };
          try {
            const resp = await fetchApi(`/api/genesis/observations/${id}/resolve`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ notes: notes || "" }),
            });
            if (resp && resp.ok) {
              await this.fetchObservations();
              await this.fetchObservationsSummary();
            }
          } catch (e) { console.warn("Resolve failed:", e); }
          finally { const { [id]: _, ...rest } = this.observationsResolving; this.observationsResolving = rest; }
        },
        async markObservationRead(id) {
          this.observationsMarkingRead = { ...this.observationsMarkingRead, [id]: true };
          try {
            const resp = await fetchApi(`/api/genesis/observations/${id}/mark-read`, {
              method: "POST",
            });
            if (resp && resp.ok) {
              const obs = this.observationsList.find(o => o.id === id);
              if (obs) {
                obs.surfaced_at = new Date().toISOString();
                if (obs.status === 'new') obs.status = 'read';
              }
              await this.fetchObservationsSummary();
            }
          } catch (e) { console.warn("Mark read failed:", e); }
          finally { const { [id]: _, ...rest } = this.observationsMarkingRead; this.observationsMarkingRead = rest; }
        },
        async batchObservationsAction(action) {
          const ids = Object.keys(this.observationsBatchIds).filter(k => this.observationsBatchIds[k]);
          if (!ids.length) return;
          try {
            const resp = await fetchApi("/api/genesis/observations/batch", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ action, ids }),
            });
            if (resp && resp.ok) {
              this.observationsBatchIds = {};
              await this.fetchObservations();
              await this.fetchObservationsSummary();
            }
          } catch (e) { console.warn("Batch action failed:", e); }
        },
        toggleObservationBatch(id) {
          if (this.observationsBatchIds[id]) {
            const { [id]: _, ...rest } = this.observationsBatchIds;
            this.observationsBatchIds = rest;
          } else {
            this.observationsBatchIds = { ...this.observationsBatchIds, [id]: true };
          }
        },
        get observationsBatchCount() {
          return Object.keys(this.observationsBatchIds).filter(k => this.observationsBatchIds[k]).length;
        },

        // ── Follow-ups cockpit tab ───────────────────────────────
        async fetchCockpit() {
          this.startFetch("cockpit");
          try {
            const f = this.cockpitFilters;
            const params = new URLSearchParams();
            if (f.kind) params.set("kind", f.kind);
            if (f.domain) params.set("domain", f.domain);
            if (f.status) params.set("status", f.status);
            if (f.source) params.set("source", f.source);
            if (f.search) params.set("search", f.search);
            params.set("sort", this.cockpitSort);
            params.set("hide_done", this.cockpitHideDone ? "1" : "0");
            params.set("page", String(this.cockpitPage));
            params.set("page_size", String(this.cockpitPageSize));
            const resp = await fetchApi(`/api/genesis/follow-ups/cockpit?${params}`);
            if (resp && resp.ok) {
              const data = await resp.json();
              this.cockpitList = data.items || [];
              this.cockpitTotal = data.total || 0;
              this.finishFetch("cockpit");
            } else {
              this.failFetch("cockpit", "Cockpit endpoint returned an error");
            }
          } catch (e) {
            console.warn("Cockpit fetch failed:", e);
            this.failFetch("cockpit", "Failed to fetch follow-ups");
          }
        },
        async fetchCockpitFilters() {
          try {
            const resp = await fetchApi("/api/genesis/follow-ups/filters");
            if (resp && resp.ok) { this.cockpitAvailableFilters = await resp.json(); }
          } catch (e) { console.warn("Cockpit filters fetch failed:", e); }
        },
        cockpitApplyFilter() {
          this.cockpitPage = 1;   // reset paging whenever a filter changes
          this.fetchCockpit();
        },
        get cockpitTotalPages() {
          return Math.max(1, Math.ceil(this.cockpitTotal / this.cockpitPageSize));
        },
        cockpitPrevPage() {
          if (this.cockpitPage > 1) { this.cockpitPage--; this.fetchCockpit(); }
        },
        cockpitNextPage() {
          if (this.cockpitPage < this.cockpitTotalPages) { this.cockpitPage++; this.fetchCockpit(); }
        },
        async _cockpitMutate(id, path, body) {
          this.cockpitMutating = { ...this.cockpitMutating, [id]: true };
          try {
            const resp = await fetchApi(`/api/genesis/follow-ups/${id}${path}`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body || {}),
            });
            if (resp && resp.ok) { await this.fetchCockpit(); return true; }
          } catch (e) { console.warn("Cockpit mutate failed:", e); }
          finally { const { [id]: _, ...rest } = this.cockpitMutating; this.cockpitMutating = rest; }
          return false;
        },
        cockpitDone(id) { return this._cockpitMutate(id, '/done', {}); },
        cockpitPin(id, pinned) { return this._cockpitMutate(id, '/pin', { pinned }); },
        cockpitSetPriority(id, priority) { return this._cockpitMutate(id, '/priority', { priority }); },
        cockpitMoveKind(id, kind) { return this._cockpitMutate(id, '/kind', { kind }); },
        cockpitSetDomain(id, d) {
          const domain = (d === '__clear__') ? '' : d;
          return this._cockpitMutate(id, '/domain', { domain });
        },
        cockpitDelete(id) {
          if (!confirm('Delete this follow-up forever? This cannot be undone.')) return;
          return this._cockpitMutate(id, '/delete', {});
        },
        async cockpitBatch(action) {
          const ids = Object.keys(this.cockpitBatchIds).filter(k => this.cockpitBatchIds[k]);
          if (!ids.length) return;
          if (action === 'delete' && !confirm(`Delete ${ids.length} follow-up(s) forever? This cannot be undone.`)) return;
          try {
            const resp = await fetchApi("/api/genesis/follow-ups/batch", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ action, ids }),
            });
            if (resp && resp.ok) { this.cockpitBatchIds = {}; await this.fetchCockpit(); }
          } catch (e) { console.warn("Cockpit batch failed:", e); }
        },
        toggleCockpitBatch(id) {
          if (this.cockpitBatchIds[id]) {
            const { [id]: _, ...rest } = this.cockpitBatchIds;
            this.cockpitBatchIds = rest;
          } else {
            this.cockpitBatchIds = { ...this.cockpitBatchIds, [id]: true };
          }
        },
        get cockpitBatchCount() {
          return Object.keys(this.cockpitBatchIds).filter(k => this.cockpitBatchIds[k]).length;
        },

        // ── Traces tab (trace waterfall) ─────────────────────────
        SPAN_KIND_COLORS: {
          operation: '#60a5fa',   // blue
          llm: '#a78bfa',         // purple
          tool: '#34d399',        // green (populated after the cc-span deploy)
          cc_session: '#fb923c',  // orange
          internal: '#94a3b8',    // gray
          recall: '#38bdf8',      // cyan (reserved)
          executor: '#fbbf24',    // amber (reserved)
        },
        spanKindColor(kind) { return this.SPAN_KIND_COLORS[kind] || '#888'; },
        formatUs(us) {
          if (us == null) return '·';
          if (us < 1000) return `${us}µs`;
          if (us < 1000000) return `${(us / 1000).toFixed(us < 10000 ? 1 : 0)}ms`;
          return `${(us / 1000000).toFixed(2)}s`;
        },
        computeWaterfall(spans) {
          if (!spans || !spans.length) return { rows: [], traceStart: 0, traceTotal: 0 };
          const spanEnd = (s) => (s.end_unix_us != null) ? s.end_unix_us
            : (s.duration_us != null) ? s.start_unix_us + s.duration_us
            : s.start_unix_us;
          // Loop (not Math.min(...spread)) — a trace can carry many spans.
          let traceStart = spans[0].start_unix_us, traceEnd = spanEnd(spans[0]);
          for (const s of spans) {
            if (s.start_unix_us < traceStart) traceStart = s.start_unix_us;
            const e = spanEnd(s);
            if (e > traceEnd) traceEnd = e;
          }
          const traceTotal = traceEnd - traceStart;
          const MIN_W = 0.6;  // % floor so sub-ms spans stay visible
          const rows = spans.map(s => {
            const e = spanEnd(s);
            const isPoint = e <= s.start_unix_us;  // zero effective width → tick (covers NULL end/duration AND duration=0)
            let offset, width;
            if (traceTotal <= 0) {
              offset = 0;
              width = isPoint ? 0 : 100;  // degenerate: all spans at one instant
            } else {
              offset = (s.start_unix_us - traceStart) / traceTotal * 100;
              width = (e - s.start_unix_us) / traceTotal * 100;
              offset = Math.min(100, Math.max(0, offset));  // clamp clock skew
              if (isPoint) {
                width = 0;  // rendered as a tick, not a bar
              } else {
                width = Math.max(MIN_W, width);  // floor so sub-ms bars stay visible
                // Keep the floored bar inside the track by shifting it left,
                // not shrinking it (shrinking would re-violate the MIN_W floor).
                if (offset + width > 100) offset = Math.max(0, 100 - width);
              }
            }
            return { ...s, offsetPct: offset, widthPct: width, isPoint };
          });
          return { rows, traceStart, traceTotal };
        },
        async fetchSpansRecent() {
          try {
            const resp = await fetchApi("/api/genesis/spans/recent?limit=50");
            if (resp && resp.ok) {
              const data = await resp.json();
              this.tracesList = data.traces || [];
            }
          } catch (e) { console.warn("Traces recent fetch failed:", e); }
        },
        async fetchTraceDetail(traceId) {
          // Toggle collapse when clicking the already-open trace.
          if (this.tracesExpanded === traceId) {
            this.tracesExpanded = null;
            this.tracesSpanExpanded = null;
            this.tracesLoadingTrace = false;
            this.tracesWaterfall = { rows: [], traceStart: 0, traceTotal: 0 };
            return;
          }
          // Token so a slow/stale fetch can't clobber a newer selection.
          const reqId = (this._traceReq || 0) + 1;
          this._traceReq = reqId;
          this.tracesExpanded = traceId;
          this.tracesSpanExpanded = null;
          this.tracesWaterfall = { rows: [], traceStart: 0, traceTotal: 0 };
          this.tracesLoadingTrace = true;
          try {
            const resp = await fetchApi(`/api/genesis/spans/trace/${traceId}`);
            if (reqId !== this._traceReq) return;  // superseded by a newer click
            if (resp && resp.ok) {
              const data = await resp.json();
              if (reqId !== this._traceReq) return;
              this.tracesWaterfall = this.computeWaterfall(data.spans || []);
            } else {
              this.tracesWaterfall = { rows: [], traceStart: 0, traceTotal: 0 };
            }
          } catch (e) {
            console.warn("Trace detail fetch failed:", e);
            if (reqId === this._traceReq) this.tracesWaterfall = { rows: [], traceStart: 0, traceTotal: 0 };
          } finally {
            if (reqId === this._traceReq) this.tracesLoadingTrace = false;
          }
        },

        // ── Autonomy tab fetches ──────────────────────────────────
        async fetchAutonomyGrants() {
          this.startFetch("autonomyGrants");
          try {
            const resp = await fetchApi("/api/genesis/autonomy/grants");
            if (resp && resp.ok) {
              const data = await resp.json();
              this.autonomyGrants = data.grants || [];
              this.finishFetch("autonomyGrants");
            } else {
              this.failFetch("autonomyGrants", "Grants endpoint returned an error");
            }
          } catch (e) {
            console.warn("Autonomy grants fetch failed:", e);
            this.failFetch("autonomyGrants", "Failed to fetch autonomy grants");
          }
        },
        async fetchAutonomySends() {
          this.startFetch("autonomySends");
          try {
            const resp = await fetchApi("/api/genesis/autonomy/sends?limit=100");
            if (resp && resp.ok) {
              const data = await resp.json();
              this.autonomySends = data.sends || [];
              this.finishFetch("autonomySends");
            } else {
              this.failFetch("autonomySends", "Sends endpoint returned an error");
            }
          } catch (e) {
            console.warn("Autonomy sends fetch failed:", e);
            this.failFetch("autonomySends", "Failed to fetch autonomy sends");
          }
        },
        async refreshAutonomy() {
          this.autonomyLoading = true;
          try {
            await Promise.all([this.fetchAutonomyGrants(), this.fetchAutonomySends()]);
          } finally { this.autonomyLoading = false; }
        },
        async flagAutonomousSend(sendId) {
          this.autonomyFlagging = { ...this.autonomyFlagging, [sendId]: true };
          try {
            const resp = await fetchApi(`/api/genesis/autonomy/sends/${sendId}/flag`, {
              method: "POST",
            });
            if (resp && resp.ok) {
              // Flagging records a correction that demotes the cell, so refresh
              // both the sends log and the standing-autonomy grants list.
              await this.fetchAutonomySends();
              await this.fetchAutonomyGrants();
            }
          } catch (e) { console.warn("Flag autonomous send failed:", e); }
          finally { const { [sendId]: _, ...rest } = this.autonomyFlagging; this.autonomyFlagging = rest; }
        },

        // ── Comms fetches (on Chat tab) ──────────────────────────
        async fetchComms() {
          this.startFetch("comms");
          try {
            const resp = await fetchApi(`/api/genesis/comms?view=${this.commsView}&limit=30`);
            if (resp && resp.ok) {
              const data = await resp.json();
              this.commsOutreach = data.outreach || [];
              this.commsProposals = data.proposals || [];
              this.commsPendingApprovals = data.pending_approvals || [];
              this.commsCounts = data.counts || {};
              this.finishFetch("comms");
            } else {
              this.failFetch("comms", "Comms endpoint returned an error");
            }
          } catch (e) {
            console.warn("Comms fetch failed:", e);
            this.failFetch("comms", "Failed to fetch comms");
          }
        },
        async resolveCommsProposal(proposalId, status, userResponse) {
          try {
            await fetchApi(`/api/genesis/comms/proposals/${proposalId}/resolve`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ status, user_response: userResponse || "" }),
            });
            this.commsRejectingId = null;
            this.commsRejectReason = '';
            await this.fetchComms();
          } catch (e) { console.warn("Proposal resolve failed:", e); }
        },

        // ── Memory tab fetches ────────────────────────────────────
        async fetchMemoryRecent() {
          this.memoryRecent.loading = true;
          try {
            const resp = await fetchApi("/api/genesis/memory/recent?limit=50");
            if (resp && resp.ok) {
              const data = await resp.json();
              this.memoryRecent.items = data.memories || [];
              this.memoryRecent.total = data.total || 0;
            }
          } catch (e) { console.warn("Memory recent failed:", e); }
          this.memoryRecent.loading = false;
        },
        async fetchMemoryStats() {
          try {
            const resp = await fetchApi("/api/genesis/memory/stats");
            if (resp && resp.ok) { this.memoryStats = await resp.json(); }
          } catch (e) { console.warn("Memory stats failed:", e); }
        },
        async searchMemory() {
          if (!this.memorySearch.query.trim()) return;
          this.memorySearch.loading = true;
          try {
            const resp = await fetchApi(`/api/genesis/memory/search?q=${encodeURIComponent(this.memorySearch.query)}&limit=30`);
            if (resp && resp.ok) {
              const data = await resp.json();
              this.memorySearch.results = data.results || [];
            }
          } catch (e) { console.warn("Memory search failed:", e); }
          this.memorySearch.loading = false;
        },
        async viewMemoryDetail(memId) {
          try {
            const resp = await fetchApi(`/api/genesis/memory/${memId}`);
            if (resp && resp.ok) {
              const data = await resp.json();
              this.memoryDetail = data.memory;
            }
          } catch (e) { console.warn("Memory detail failed:", e); }
        },
        async deleteMemory(memId) {
          try {
            await fetchApi(`/api/genesis/memory/${memId}`, { method: "DELETE" });
            this.memoryDetail = null;
            await this.fetchMemoryRecent();
            await this.fetchMemoryStats();
          } catch (e) { console.warn("Memory delete failed:", e); }
        },

        // ── Knowledge tab fetches ──────────────────────────────────
        async fetchKnowledgeRecent() {
          try {
            const resp = await fetchApi("/api/genesis/knowledge/recent?limit=50");
            if (resp && resp.ok) {
              const data = await resp.json();
              this.knowledgeRecent = data.units || [];
            }
          } catch (e) { console.warn("Knowledge recent failed:", e); }
        },
        async fetchKnowledgeStats() {
          try {
            const resp = await fetchApi("/api/genesis/knowledge/stats");
            if (resp && resp.ok) { this.knowledgeStats = await resp.json(); }
          } catch (e) { console.warn("Knowledge stats failed:", e); }
        },
        // ── Tracked repositories (recon watchlist) ──────────────────
        async fetchWatchlist() {
          try {
            const resp = await fetchApi("/api/genesis/recon/watchlist");
            if (resp && resp.ok) { this.watchlistEntries = (await resp.json()).entries || []; }
          } catch (e) { console.warn("Watchlist fetch failed:", e); }
        },
        async addWatchlistRepo() {
          this.watchlistSaving = true; this.watchlistMsg = null;
          try {
            const f = this.watchlistForm;
            const body = {
              name: f.name, repo: f.repo, priority: f.priority,
              track: f.track.split(',').map(t => t.trim()).filter(Boolean),
            };
            if (f.notes) body.notes = f.notes;
            const resp = await fetchApi("/api/genesis/recon/watchlist", {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body),
            });
            const data = resp ? await resp.json() : null;
            if (resp && resp.ok) {
              this.watchlistMsg = { ok: true, text: "Added " + data.repo };
              f.name = ''; f.repo = ''; f.notes = '';
              this.fetchWatchlist();
            } else {
              this.watchlistMsg = { ok: false, text: (data && (data.error || (data.details || []).join('; '))) || "Add failed" };
            }
          } catch (e) {
            this.watchlistMsg = { ok: false, text: String(e) };
          } finally { this.watchlistSaving = false; }
        },
        async toggleWatchlistRepo(repo, disabled) {
          try {
            const resp = await fetchApi("/api/genesis/recon/watchlist/disable", {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ repo, disabled }),
            });
            if (resp && resp.ok) { this.fetchWatchlist(); }
            else { const d = resp ? await resp.json() : null; this.watchlistMsg = { ok: false, text: (d && d.error) || "Update failed" }; }
          } catch (e) { this.watchlistMsg = { ok: false, text: String(e) }; }
        },
        async removeWatchlistRepo(repo) {
          if (!await this.showConfirm("Remove repository", "Remove " + repo + " from tracked repositories?")) return;
          try {
            const resp = await fetchApi("/api/genesis/recon/watchlist", {
              method: "DELETE", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ repo }),
            });
            if (resp && resp.ok) { this.fetchWatchlist(); }
            else { const d = resp ? await resp.json() : null; this.watchlistMsg = { ok: false, text: (d && d.error) || "Remove failed" }; }
          } catch (e) { this.watchlistMsg = { ok: false, text: String(e) }; }
        },
        async searchKnowledge() {
          if (!this.knowledgeQuery.trim()) return;
          try {
            const resp = await fetchApi(`/api/genesis/knowledge/search?q=${encodeURIComponent(this.knowledgeQuery)}&limit=30`);
            if (resp && resp.ok) {
              const data = await resp.json();
              this.knowledgeSearchResults = data.results || [];
            }
          } catch (e) { console.warn("Knowledge search failed:", e); }
        },
        async viewKnowledgeDetail(unitId) {
          try {
            const resp = await fetchApi(`/api/genesis/knowledge/${unitId}`);
            if (resp && resp.ok) {
              const data = await resp.json();
              this.knowledgeDetail = data.unit;
            }
          } catch (e) { console.warn("Knowledge detail failed:", e); }
        },
        async deleteKnowledge(unitId) {
          try {
            await fetchApi(`/api/genesis/knowledge/${unitId}`, { method: "DELETE" });
            this.knowledgeDetail = null;
            await this.fetchKnowledgeRecent();
            await this.fetchKnowledgeStats();
          } catch (e) { console.warn("Knowledge delete failed:", e); }
        },

        // ── References tab ─────────────────────────────────────────
        async fetchReferenceList() {
          try {
            const resp = await fetchApi("/api/genesis/references/list");
            if (resp && resp.ok) {
              const data = await resp.json();
              this.referenceByKind = data.by_kind || {};
              this.referenceTotal = data.total || 0;
            }
          } catch (e) { console.warn("Reference list failed:", e); }
        },
        async fetchReferenceStats() {
          try {
            const resp = await fetchApi("/api/genesis/references/stats");
            if (resp && resp.ok) { this.referenceStats = await resp.json(); }
          } catch (e) { console.warn("Reference stats failed:", e); }
          if (!this.referenceKinds.length) { await this.fetchReferenceKinds(); }
        },
        async fetchReferenceKinds() {
          try {
            const resp = await fetchApi("/api/genesis/references/kinds");
            if (resp && resp.ok) { this.referenceKinds = (await resp.json()).kinds || []; }
          } catch (e) { console.warn("Reference kinds failed:", e); }
        },
        async searchReferences() {
          if (!this.referenceQuery.trim()) { this.referenceSearchResults = []; return; }
          try {
            const kind = this.referenceKindFilter ? `&kind=${encodeURIComponent(this.referenceKindFilter)}` : "";
            const resp = await fetchApi(`/api/genesis/references/search?q=${encodeURIComponent(this.referenceQuery)}${kind}`);
            if (resp && resp.ok) { this.referenceSearchResults = (await resp.json()).results || []; }
          } catch (e) { console.warn("Reference search failed:", e); }
        },
        async viewReferenceDetail(id) {
          try {
            const resp = await fetchApi(`/api/genesis/references/${id}`);
            if (resp && resp.ok) { this.referenceDetail = (await resp.json()).reference; }
          } catch (e) { console.warn("Reference detail failed:", e); }
        },
        async revealReference(id) {
          try {
            const resp = await fetchApi(`/api/genesis/references/${id}/reveal`, { method: "POST" });
            if (resp && resp.ok) {
              const data = await resp.json();
              this.revealedValues = { ...this.revealedValues, [id]: data.value ?? "" };
            }
          } catch (e) { console.warn("Reference reveal failed:", e); }
        },
        hideReference(id) {
          const copy = { ...this.revealedValues };
          delete copy[id];
          this.revealedValues = copy;
        },
        async copyReferenceValue(id) {
          const v = this.revealedValues[id];
          if (v != null) {
            try { await navigator.clipboard.writeText(v); } catch (e) { console.warn("clipboard failed:", e); }
          }
        },
        async deleteReference(id) {
          const ok = await this.showConfirm(
            "Delete reference?",
            "This permanently removes the entry and its vector. This cannot be undone.",
          );
          if (!ok) return;
          try {
            await fetchApi(`/api/genesis/references/${id}`, { method: "DELETE" });
            this.referenceDetail = null;
            this.hideReference(id);
            this.referenceSearchResults = this.referenceSearchResults.filter(r => r.id !== id);
            await this.fetchReferenceList();
            await this.fetchReferenceStats();
          } catch (e) { console.warn("Reference delete failed:", e); }
        },
        _provenanceColor(sp) {
          if (sp === "reference_store") return "var(--color-primary)";
          if (sp === "extraction_job") return "var(--color-warning-text)";
          return "var(--color-text-secondary)";
        },
        _provenanceLabel(sp) {
          if (sp === "reference_store") return "verified";
          if (sp === "extraction_job") return "auto-captured";
          return sp || "unknown";
        },


        // ── Knowledge upload methods ────────────────────────────
        async handleKnowledgeFileDrop(event) {
          event.preventDefault();
          this.knowledgeUpload.dragging = false;
          const files = event.dataTransfer?.files || event.target?.files;
          if (!files || files.length === 0) return;
          await this._uploadKnowledgeFile(files[0]);
        },
        async _uploadKnowledgeFile(file) {
          this.knowledgeUpload.error = null;
          this.knowledgeUpload.uploading = true;
          try {
            const form = new FormData();
            form.append("file", file);
            const resp = await fetch("/api/genesis/knowledge/upload", { method: "POST", body: form });
            if (!resp.ok) {
              const err = await resp.json().catch(() => ({}));
              throw new Error(err.error || `Upload failed (${resp.status})`);
            }
            const data = await resp.json();
            this.knowledgeUpload.file = { name: data.filename, size: data.file_size, mime: data.mime_type };
            this.knowledgeUpload.uploadId = data.upload_id;
            this.knowledgeUpload.confirm = true;
            this.knowledgeUpload.uploading = false;
            // Fetch taxonomy for autocomplete
            await this.fetchKnowledgeTaxonomy();
          } catch (e) {
            this.knowledgeUpload.uploading = false;
            this.knowledgeUpload.error = e.message;
          }
        },
        async fetchKnowledgeTaxonomy() {
          try {
            const resp = await fetchApi("/api/genesis/knowledge/taxonomy");
            if (resp && resp.ok) { this.knowledgeTaxonomy = await resp.json(); }
          } catch (e) { console.warn("Taxonomy fetch failed:", e); }
        },
        async confirmKnowledgeIngest() {
          const u = this.knowledgeUpload;
          if (!u.projectType.trim()) { u.error = "Project type is required"; return; }
          u.error = null;
          try {
            const resp = await fetchApi("/api/genesis/knowledge/ingest", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                upload_id: u.uploadId,
                project_type: u.projectType.trim(),
                domain: u.domain.trim() || "auto",
                purpose: u.purpose.trim() || null,
                context: u.context.trim() || "",
                mode: u.mode || "extract",
              }),
            });
            if (!resp.ok) {
              const err = await resp.json().catch(() => ({}));
              throw new Error(err.error || "Ingest failed");
            }
            this.cancelKnowledgeUpload();
            await this.fetchKnowledgeUploads();
            this._startUploadPolling();
          } catch (e) {
            u.error = e.message;
          }
        },
        async cancelKnowledgeUpload() {
          const uploadId = this.knowledgeUpload.uploadId;
          this.knowledgeUpload = {
            dragging: false, file: null, uploading: false, confirm: false,
            uploadId: null, projectType: "", domain: "", purpose: "", context: "", mode: "extract", error: null,
          };
          // Clean up server-side file + DB record
          if (uploadId) {
            try { await fetchApi(`/api/genesis/knowledge/upload/${uploadId}`, { method: "DELETE" }); }
            catch (e) { console.warn("Upload cleanup failed:", e); }
          }
        },
        async fetchKnowledgeUploads() {
          try {
            const resp = await fetchApi("/api/genesis/knowledge/uploads");
            if (resp && resp.ok) {
              const data = await resp.json();
              this.knowledgeUploads = data.uploads || [];
            }
          } catch (e) { console.warn("Uploads list failed:", e); }
        },
        _startUploadPolling() {
          if (this._uploadPollInterval) return;
          this._uploadPollInterval = setInterval(async () => {
            await this.fetchKnowledgeUploads();
            const hasActive = this.knowledgeUploads.some(u => u.status === "processing");
            if (!hasActive) {
              clearInterval(this._uploadPollInterval);
              this._uploadPollInterval = null;
              // Refresh knowledge data since new units may have been created
              await this.fetchKnowledgeRecent();
              await this.fetchKnowledgeStats();
            }
          }, 3000);
        },
        _formatFileSize(bytes) {
          if (!bytes) return "—";
          if (bytes < 1024) return bytes + " B";
          if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
          return (bytes / (1024 * 1024)).toFixed(1) + " MB";
        },

        // ── Confirm modal ─────────────────────────────────────
        showConfirm(title, message) {
          return new Promise(resolve => {
            this.confirmModal = { show: true, title, message, _resolve: resolve };
          });
        },
        _confirmModalResolve(result) {
          if (this.confirmModal._resolve) this.confirmModal._resolve(result);
          this.confirmModal = { show: false, title: '', message: '', _resolve: null };
        },

        // ── Backup tab fetches ───────────────────────────────────
        async fetchBackupStatus() {
          try {
            const resp = await fetchApi("/api/genesis/backup/status");
            if (resp && resp.ok) { this.backupStatus = await resp.json(); }
          } catch (e) { console.warn("Backup status failed:", e); }
        },
        // Human labels for the interval preset keys the API speaks.
        _intervalLabels: { '3h': 'every 3 hours', '6h': 'every 6 hours',
          '12h': 'every 12 hours', daily: 'daily' },
        // Headline health: is the backup system OK, at-risk, or failing?
        backupHeadline() {
          const bs = this.backupStatus;
          const lb = bs && bs.last_backup;
          const sch = bs && bs.schedule;
          const enabled = sch && sch.enabled;
          const active = sch && sch.active;
          if (!lb) return { text: 'No backups yet', color: '#ffb74d' };
          if (!lb.success) return { text: 'Failing', color: '#ef9a9a' };
          if (!enabled) return { text: 'On demand only (not scheduled)', color: '#ffb74d' };
          // enabled but not loaded/running (e.g. stopped out-of-band, or failed to
          // start) — the timer won't fire, so "Active" would misreport. Say so.
          if (!active) return { text: 'Scheduled, but the timer is not running', color: '#ffb74d' };
          return { text: 'Active', color: '#81c784' };
        },
        // "every 6 hours · last 12:10 PM ✓ · next 6:10 PM" — the at-a-glance line.
        backupScheduleLine() {
          const bs = this.backupStatus;
          const sch = bs && bs.schedule;
          const lb = bs && bs.last_backup;
          const parts = [];
          if (sch && sch.enabled) {
            const key = sch.interval;
            if (key && key !== 'custom' && this._intervalLabels[key]) parts.push(this._intervalLabels[key]);
            else if (key === 'custom') parts.push('custom schedule');
            else parts.push('scheduled');
          } else {
            parts.push('not scheduled');
          }
          if (lb && lb.timestamp) {
            const when = new Date(lb.timestamp).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
            parts.push('last ' + when + (lb.success ? ' ✓' : ' ✗'));
          }
          if (sch && sch.enabled && sch.next_run) parts.push('next ' + sch.next_run);
          return parts.join(' · ');
        },
        // Off-site (Tier-2) health label + colour from the status/backend fields.
        tier2Health() {
          const t2 = this.backupStatus && this.backupStatus.destinations
            && this.backupStatus.destinations.tier2;
          if (!t2 || t2.backend === 'none' || !t2.backend)
            return { text: 'Not configured', color: 'var(--color-text-secondary)' };
          const s = t2.status;
          if (s === 'ok') return { text: 'Off-site copy confirmed', color: '#81c784' };
          if (s === 'partial') return { text: 'Partial — last off-site copy incomplete', color: '#ffb74d' };
          if (s === 'no_smbclient') return { text: 'smbclient not installed', color: '#ef9a9a' };
          if (s === 'not_configured') return { text: 'Not configured', color: 'var(--color-text-secondary)' };
          return { text: s || 'unknown', color: '#ffb74d' };
        },
        async triggerBackup() {
          try {
            const resp = await fetchApi("/api/genesis/backup/trigger", { method: "POST" });
            // Type=oneshot backup runs ~5 min; status won't reflect it immediately.
            if (resp && resp.ok) { alert("Backup started — a full run takes a few minutes."); setTimeout(() => this.fetchBackupStatus(), 8000); }
            else { alert("Trigger failed"); }
          } catch (e) { alert("Trigger failed: " + e.message); }
        },
        async fetchBackupConfig() {
          try {
            const resp = await fetchApi("/api/genesis/backup/config");
            if (resp && resp.ok) {
              const c = await resp.json();
              this.backupConfig = c;
              const f = this.backupConfigForm;
              f.repo = c.repo || '';
              f.tier2_backend = c.tier2_backend || 'none';
              f.local_path = c.local_path || '';
              f.nas = c.nas || '';
              f.nas_user = c.nas_user || '';
              // A "custom"/null server interval can't be a preset dropdown value;
              // default the control to 6h but DON'T mark dirty, so a plain Save
              // never rewrites a hand-edited schedule (scheduleDirty gates it).
              f.schedule_interval = (c.schedule_interval && c.schedule_interval !== 'custom')
                ? c.schedule_interval : '6h';
              f.schedule_enabled = c.schedule_enabled !== false;
              this.scheduleDirty = false;   // loaded state is not a user change
              // passphrase / nas_pass are write-only — never populated.
            }
          } catch (e) { console.warn("Backup config fetch failed:", e); }
        },
        async saveBackupConfig() {
          this.backupConfigSaving = true;
          this.backupConfigMsg = null;
          try {
            const f = this.backupConfigForm;
            const body = { tier2_backend: f.tier2_backend };
            // The cron job is a stateful side-effect — only touch it when the
            // user actually changed the schedule controls.
            if (this.scheduleDirty) {
              body.schedule_interval = f.schedule_interval;
              body.schedule_enabled = f.schedule_enabled;
            }
            // Only send repo if the user actually changed it — re-sending the
            // credential-stripped value would clobber any token in the stored URL.
            if (f.repo && f.repo !== (this.backupConfig?.repo || '')) body.repo = f.repo;
            if (f.local_path) body.local_path = f.local_path;
            if (f.nas) body.nas = f.nas;
            if (f.nas_user) body.nas_user = f.nas_user;
            if (f.nas_pass) body.nas_pass = f.nas_pass;
            if (f.passphrase) body.passphrase = f.passphrase;
            const resp = await fetchApi("/api/genesis/backup/config", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body),
            });
            const data = resp ? await resp.json() : null;
            if (resp && resp.ok) {
              this.backupConfigMsg = {
                ok: true,
                text: "Saved. Schedule changes apply now; destination/credential changes apply on the next run.",
                warnings: (data && data.warnings) || [],
              };
              f.nas_pass = ''; f.passphrase = '';   // clear write-only fields
              this.scheduleDirty = false;
              this.fetchBackupConfig();
              this.fetchBackupStatus();
            } else {
              const detail = data && (data.error +
                (data.details ? ': ' + data.details.join('; ') : ''));
              this.backupConfigMsg = { ok: false, text: detail || "Save failed", warnings: [] };
            }
          } catch (e) {
            this.backupConfigMsg = { ok: false, text: String(e), warnings: [] };
          } finally {
            this.backupConfigSaving = false;
          }
        },

        // ── Update methods ──────────────────────────────────────
        async fetchUpdateStatus() {
          try {
            const resp = await fetchApi("/api/genesis/updates/status");
            if (resp && resp.ok) { this.updateStatus = await resp.json(); }
          } catch (e) { console.warn("Update status failed:", e); }
        },
        async checkForUpdates() {
          this._checkingForUpdates = true;
          this._checkUpToDate = false;
          try {
            const resp = await fetchApi("/api/genesis/updates/check", { method: "POST" });
            if (resp && resp.ok) {
              const check = await resp.json();
              if (!this.updateStatus) await this.fetchUpdateStatus();
              if (check.commits_behind > 0) {
                this.updateStatus.update_available = {
                  commits_behind: check.commits_behind,
                  target_tag: check.target_tag,
                  summary: check.summary,
                  detected_at: new Date().toISOString(),
                };
              } else {
                this.updateStatus.update_available = null;
                this._checkUpToDate = true;
                setTimeout(() => { this._checkUpToDate = false; }, 4000);
              }
            } else {
              let msg = "Could not reach upstream";
              try { const body = await resp.json(); if (body.error) msg = body.error; } catch {}
              alert("Check failed: " + msg);
            }
          } catch (e) { alert("Server unreachable — is Genesis running?"); }
          this._checkingForUpdates = false;
        },
        async fetchUpdateProgress() {
          try {
            const resp = await fetchApi("/api/genesis/updates/progress");
            if (resp && resp.ok) { this.updateProgress = await resp.json(); }
          } catch (e) { /* server may be down during update */ }
        },
        _updatePhaseLabel(phase) {
          const labels = {
            fetching: "Fetching latest code...",
            merging: "Merging updates...",
            bootstrap: "Running bootstrap...",
            migrations: "Running migrations...",
            health_check: "Verifying system health...",
            done: "Update complete",
          };
          return labels[phase] || "Starting update...";
        },
        async applyUpdate() {
          const msg = "Apply Genesis update?\n\nA background session will:\n\u2022 Back up data\n\u2022 Merge latest code\n\u2022 Resolve trivial conflicts automatically\n\u2022 Run migrations & restart services\n\u2022 Verify health\n\nContinue?";
          if (!(await this.showConfirm("Apply Update", msg))) return;
          this._updating = true;
          try {
            await fetchApi("/api/genesis/updates/apply", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ supervised: true }),
            });
          } catch (e) { /* expected — server may restart */ }
          const start = Date.now();
          let seenInProgress = false;
          const poll = setInterval(async () => {
            try {
              await this.fetchUpdateProgress();
              const prog = this.updateProgress;
              if (!prog) return;
              if (prog.in_progress) seenInProgress = true;
              // Conflicts detected — stop polling, show resolve UI
              if (prog.escalation && (prog.escalation.includes("tier3_needed") || prog.escalation.includes("tier2_needed"))
                  && !prog.in_progress) {
                clearInterval(poll);
                this._updating = false;
                await this.fetchUpdateStatus();
                return;
              }
              // Failed state (not success, not in progress)
              if (prog.failed && !prog.in_progress) {
                clearInterval(poll);
                this._updating = false;
                await this.fetchUpdateStatus();
                return;
              }
              // Success
              if (!prog.in_progress && prog.summary && prog.summary.startsWith("success")) {
                clearInterval(poll);
                this._updating = false;
                await this.fetchUpdateStatus();
                await this.fetchHealth();
                return;
              }
              // Fallback: health ok + not in progress
              if (!prog.in_progress) {
                const resp = await fetchApi("/api/genesis/health");
                if (resp && resp.ok) {
                  clearInterval(poll);
                  this._updating = false;
                  await this.fetchUpdateStatus();
                  await this.fetchHealth();
                  return;
                }
              }
              // Silent-death detection: process ended without leaving any state
              if (!prog.in_progress && !prog.summary && !prog.escalation && !(prog.conflicts && prog.conflicts.length)) {
                if (seenInProgress || Date.now() - start > 30000) {
                  clearInterval(poll);
                  this._updating = false;
                  alert("Update process ended without completing.\nCheck server logs for status.");
                  await this.fetchUpdateStatus();
                  return;
                }
              }
            } catch (e) { /* server may be restarting, keep polling */ }
            // Backstop: if server is completely dead, stop after 30 min
            if (Date.now() - start > 1800000) {
              clearInterval(poll);
              this._updating = false;
              alert("Update polling timed out after 30 minutes.\nCheck server status manually.");
              return;
            }
          }, 5000);
        },
        async resolveConflicts() {
          if (!(await this.showConfirm("Resolve Conflicts", "Spawn an Opus session to resolve deep merge conflicts?\n\nThis may take a few minutes."))) return;
          this._resolvingConflicts = true;
          try {
            const resp = await fetchApi("/api/genesis/updates/resolve", { method: "POST" });
            if (resp && !resp.ok) {
              const data = await resp.json();
              alert("Failed: " + (data.error || "unknown error"));
              this._resolvingConflicts = false;
              return;
            }
          } catch (e) { /* server may restart */ }
          // Poll for completion
          const poll = setInterval(async () => {
            try {
              await this.fetchUpdateProgress();
              if (this.updateProgress && !this.updateProgress.in_progress) {
                clearInterval(poll);
                this._resolvingConflicts = false;
                await this.fetchUpdateStatus();
                await this.fetchHealth();
              }
            } catch (e) { /* keep polling */ }
          }, 5000);
          // Safety timeout — alert and refresh status so user isn't left with a blank UI
          setTimeout(async () => {
            clearInterval(poll);
            this._resolvingConflicts = false;
            alert("Conflict resolution did not complete after 15 minutes.\nCheck server logs for status.");
            await this.fetchUpdateStatus();
          }, 900000);
        },

        async dismissUpdate() {
          try {
            await fetchApi("/api/genesis/updates/dismiss", { method: "POST" });
            this.updateProgress = {};
            await this.fetchUpdateStatus();
          } catch (e) { console.warn("Dismiss failed:", e); }
        },

        // ── Settings hub fetches ─────────────────────────────────
        async fetchSettingsDomains() {
          try {
            const resp = await fetchApi("/api/genesis/settings");
            if (resp && resp.ok) { this.settingsDomains = await resp.json(); }
          } catch (e) { console.warn("Settings index failed:", e); }
          // Also fetch the system timezone
          try {
            const tzResp = await fetchApi("/api/genesis/settings/timezone");
            if (tzResp && tzResp.ok) { this.systemTimezone = (await tzResp.json()).timezone; }
          } catch (e) { /* ignore */ }
        },
        async fetchSettingsData(domain) {
          try {
            const resp = await fetchApi(`/api/genesis/settings/${domain}`);
            if (resp && resp.ok) {
              const d = await resp.json();
              this.settingsData[domain] = d.config;
            }
          } catch (e) { console.warn(`Settings fetch ${domain} failed:`, e); }
        },
        async saveSettings(domain) {
          this.settingsSaving = true;
          try {
            const resp = await fetchApi(`/api/genesis/settings/${domain}`, {
              method: "PUT",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(this.settingsData[domain]),
            });
            if (resp && resp.ok) {
              const d = await resp.json();
              this.settingsData[domain] = d.config;
              this.settingsEditing = null;
              if (domain === "autonomous_cli_policy") {
                this.fetchAutonomousCliPolicy();
              }
              if (d.needs_restart) { this.settingsRestartMessage = "Settings saved. Restart Genesis server to apply."; }
            } else {
              const err = await resp.json().catch(() => ({}));
              alert("Save failed: " + (err.details ? err.details.join(", ") : err.error || "Unknown error"));
            }
          } catch (e) { alert("Save failed: " + e.message); }
          this.settingsSaving = false;
        },
        startEditSettings(domain) {
          if (!this.settingsData[domain]) { this.fetchSettingsData(domain); }
          this.settingsEditing = domain;
        },
        viewSettings(domain) {
          if (this.settingsViewing === domain) { this.settingsViewing = null; return; }
          if (!this.settingsData[domain]) { this.fetchSettingsData(domain); }
          this.settingsViewing = domain;
        },

        // ── Provider Keys methods ────────────────────────────────
        async fetchSecrets() {
          try {
            const resp = await fetchApi("/api/genesis/secrets");
            if (resp && resp.ok) { this.secretsGroups = (await resp.json()).groups; }
          } catch (e) { console.warn("Secrets fetch failed:", e); }
        },
        toggleSecretEdit(keyName) {
          this.secretsEditing = {...this.secretsEditing, [keyName]: !this.secretsEditing[keyName]};
          if (!this.secretsEditing[keyName]) { delete this.secretsValues[keyName]; }
        },
        async saveSecret(keyName) {
          const val = (this.secretsValues[keyName] || '').trim();
          if (!val) { this.secretsMessage = {type: 'error', text: 'Value cannot be empty'}; return; }
          this.secretsSaving = true;
          try {
            const resp = await fetchApi("/api/genesis/secrets", {
              method: "PUT", headers: {"Content-Type": "application/json"},
              body: JSON.stringify({keys: {[keyName]: val}}),
            });
            const d = await resp.json();
            if (resp.ok) {
              this.secretsEditing = {...this.secretsEditing, [keyName]: false};
              delete this.secretsValues[keyName];
              this.secretsMessage = {type: 'restart', text: `${keyName} saved. Changes take effect after server restart.`};
              this.fetchSecrets();
            } else {
              this.secretsMessage = {type: 'error', text: d.error || 'Save failed'};
            }
          } catch (e) { this.secretsMessage = {type: 'error', text: e.message}; }
          this.secretsSaving = false;
        },
        // Map env var names → provider_type values from model_routing.yaml.
        // MAINTENANCE: update when adding new providers to config/model_routing.yaml.
        _KEY_TO_PROVIDER_TYPES: {
          API_KEY_GROQ: ['groq'],
          API_KEY_MISTRAL: ['mistral'],
          GOOGLE_API_KEY: ['google'],
          API_KEY_OPENROUTER: ['openrouter'],
          API_KEY_DEEPSEEK: ['deepseek'],
          API_KEY_QWEN: ['qwen'],
          API_KEY_ZENMUX: ['zenmux'],
          API_KEY_MINIMAX: ['minimax'],
          OPENAI_API_KEY: ['openai'],
          API_KEY_XAI: ['xai'],
          API_KEY_DEEPINFRA: ['deepinfra'],
          API_KEY_CEREBRAS: ['cerebras'],
          API_KEY_GITHUB: ['github'],
          API_KEY_SAMBANOVA: ['sambanova'],
        },
        secretHealthStatus(keyEntry) {
          if (keyEntry.status === 'not_set') return 'not_set';
          const provTypes = this._KEY_TO_PROVIDER_TYPES[keyEntry.key];
          if (!provTypes || !this.routingConfig) return keyEntry.status;
          const cbStates = this.routingConfig.cb_states || {};
          const providers = this.routingConfig.providers || {};
          // Find all routing providers that match this key's provider types
          const matchedProviders = Object.keys(providers).filter(pname => {
            const prov = providers[pname];
            return provTypes.includes(prov.type);
          });
          if (matchedProviders.length === 0) return keyEntry.status;
          const openCount = matchedProviders.filter(p => cbStates[p] === 'OPEN').length;
          if (openCount === matchedProviders.length) return 'error';
          if (openCount > 0) return 'degraded';
          return 'healthy';
        },
        secretStatusColor(status) {
          if (status === 'validated' || status === 'healthy') return '#4caf50';
          if (status === 'configured') return '#4caf50';
          if (status === 'degraded') return '#f0ad4e';
          if (status === 'error') return '#d9534f';
          return 'var(--color-text-secondary)';
        },

        // ── Auth methods ─────────────────────────────────────────
        async checkAuth() {
          try {
            const resp = await fetchApi("/api/genesis/auth/status");
            if (resp && resp.ok) {
              const d = await resp.json();
              this.authEnabled = d.enabled;
            }
          } catch (e) { /* auth check is best-effort */ }
        },
        async logout() {
          try {
            await fetchApi("/api/genesis/auth/logout", {
              method: "POST", credentials: "same-origin",
            });
          } catch (e) { /* ignore */ }
          window.location.href = "/genesis/login";
        },

        _SETTING_LABELS: {
          // Autonomous CLI Policy
          autonomous_cli_fallback_enabled: 'CLI Fallback Enabled',
          manual_approval_required: 'Manual Approval Required',
          approval_channel: 'Approval Channel',
          reask_interval_hours: 'Re-ask Interval (hours)',
          shared_export_enabled: 'Shared Export',
          // TTS
          provider: 'TTS Provider', voice_id: 'Voice ID', model: 'Model',
          stability: 'Stability', similarity_boost: 'Similarity Boost',
          style: 'Style Expressiveness', speed: 'Speed',
          use_speaker_boost: 'Speaker Boost', strip_markdown: 'Strip Markdown',
          max_chars: 'Max Characters', block_background_sessions: 'Block Background TTS',
          // Ego
          enabled: 'Enabled', cadence_minutes: 'Cadence (minutes)',
          activity_threshold_minutes: 'Activity Threshold (min)',
          max_interval_minutes: 'Max Interval (min)', backoff_multiplier: 'Backoff Multiplier',
          board_size: 'Board Size', batch_digest: 'Batch Digest',
          morning_report_hour: 'Morning Report Hour', morning_report_minute: 'Morning Report Minute',
          shadow_morning_report: 'Shadow Morning Report',
          consecutive_failure_limit: 'Failure Limit', failure_backoff_minutes: 'Failure Backoff (min)',
          // Inbox Monitor
          watch_path: 'Watch Path', response_dir: 'Response Dir Pattern',
          check_interval_seconds: 'Check Interval (sec)', batch_size: 'Batch Size',
          effort: 'Effort Level', timeout_s: 'Timeout (sec)',
          // Outreach
          start: 'Start Time', end: 'End Time',
          default: 'Default Channel', blocker: 'Blocker Channel',
          alert: 'Alert Channel', surplus: 'Surplus Channel', digest: 'Digest Channel',
          max_daily: 'Max Daily Messages', surplus_daily: 'Surplus Daily Limit',
          trigger_time: 'Trigger Time', timeout_hours: 'Timeout (hours)',
          poll_interval_minutes: 'Poll Interval (min)',
          // Surplus
          interval_minutes: 'Dispatch Interval (min)', task_expiry_hours: 'Task Expiry (hours)',
          max_iterations_per_cycle: 'Max Iterations/Cycle',
          brainstorm_check_hours: 'Brainstorm Check (hours)',
          code_index_hours: 'Code Index (hours)',
          recon_gather_hours: 'Recon Gather (hours)', maintenance_hours: 'Maintenance (hours)',
          analytical_hours: 'Analytical (hours)', follow_up_dispatch_minutes: 'Follow-up Dispatch (min)',
          memory_extraction_hours: 'Memory Extraction (hours)',
          // Resilience
          transition_count: 'Transition Count', window_seconds: 'Window (sec)',
          stabilization_seconds: 'Stabilization (sec)',
          confirmation_probes: 'Confirmation Probes', confirmation_interval_s: 'Confirmation Interval (sec)',
          drain_pace_s: 'Drain Pace (sec)', embedding_pace_per_min: 'Embedding Pace/min',
          queue_overflow_threshold: 'Queue Overflow Threshold',
          max_sessions_per_hour: 'Max Sessions/Hour', throttle_threshold_pct: 'Throttle Threshold (%)',
          // Confidence Gates
          min_confidence: 'Min Confidence', min_separability: 'Min Separability',
          shadow_mode: 'Shadow Mode',
          // Updates
          interval_hours: 'Check Interval (hours)',
          auto_apply: 'Auto Apply', allowed_impacts: 'Allowed Impacts',
          backup_before_update: 'Backup Before Update',
          // Channels
          telegram: 'Telegram',
          default_model: 'Default Model', default_effort: 'Default Effort',
        },
        _SETTING_DESCS: {
          // Autonomous CLI Policy
          autonomous_cli_fallback_enabled: 'Allow background sessions to use Claude Code CLI when API routing fails',
          manual_approval_required: 'Require Telegram approval before dispatching background CC sessions',
          approval_channel: 'Channel for approval requests (must be configured and reachable)',
          reask_interval_hours: 'Hours between re-asking for approval on the same task',
          shared_export_enabled: 'Export policy to Guardian shared mount for host-side enforcement',
          // TTS
          provider: 'Active TTS provider. Requires restart to switch.',
          voice_id: 'Voice ID string from the TTS provider (e.g., ElevenLabs voice ID)',
          model: 'LLM model for processing (fable = most capable, opus = high quality, haiku = fastest)',
          stability: '0.0\u20131.0: higher = more consistent voice',
          similarity_boost: '0.0\u20131.0: higher = closer to original voice',
          style: '0.0\u20131.0: higher = more expressive delivery',
          speed: '0.7\u20131.2: playback speed multiplier',
          use_speaker_boost: 'Apply TTS provider speaker boost enhancement',
          strip_markdown: 'Remove markdown formatting before synthesis',
          max_chars: 'Maximum text length sent to TTS',
          block_background_sessions: 'Prevent background sessions from triggering voice',
          // Ego
          enabled: 'Enable autonomous ego sessions (proposal pipeline)',
          cadence_minutes: 'Base interval between autonomous cycles',
          activity_threshold_minutes: 'Min time since last user activity before running',
          max_interval_minutes: 'Maximum backoff cap (prevents indefinite delays)',
          backoff_multiplier: 'How much to increase interval on idle cycles',
          board_size: 'Max active proposals on the bulletin board (ego tables excess)',
          batch_digest: 'Bundle ego proposals into periodic digests instead of individual messages',
          morning_report_hour: 'Hour (0\u201323) to send the daily morning report',
          morning_report_minute: 'Minute (0\u201359) for morning report delivery',
          shadow_morning_report: 'Send reports to a test topic for validation before going live',
          consecutive_failure_limit: 'Consecutive failures before circuit breaker trips',
          failure_backoff_minutes: 'Cooldown period after consecutive failures before retrying',
          // Inbox Monitor
          watch_path: 'Directory path monitored for incoming files',
          response_dir: 'Subdirectory pattern where responses are written (excluded from scanning)',
          check_interval_seconds: 'How often (in seconds) to scan the inbox directory for new files',
          batch_size: 'Maximum files to process per scan cycle (1\u201310)',
          effort: 'Processing depth: low (fast), medium (balanced), high (thorough), xhigh (deeper), or max (deepest)',
          timeout_s: 'Max seconds per inbox item processing before timeout',
          // Outreach
          start: 'Quiet hours start \u2014 no outreach before this time (e.g., 22:00)',
          end: 'Quiet hours end \u2014 outreach resumes after this time (e.g., 07:00)',
          default: 'Channel for general outreach (morning reports, digests)',
          blocker: 'Channel for blocking/critical alerts that need immediate attention',
          alert: 'Channel for non-blocking alerts and notifications',
          surplus: 'Channel for surplus compute outputs (brainstorms, enrichment)',
          digest: 'Channel for periodic summary digests',
          max_daily: 'Maximum outreach messages per day across all categories',
          surplus_daily: 'Maximum surplus compute messages per day',
          trigger_time: 'Daily time when digest/report delivery fires (e.g., 08:00)',
          timeout_hours: 'Max hours to wait for outreach delivery confirmation',
          poll_interval_minutes: 'How often (in minutes) to check for pending outreach items',
          // Surplus
          interval_minutes: 'How often the surplus dispatch loop runs',
          task_expiry_hours: 'Pending tasks older than this are discarded',
          max_iterations_per_cycle: 'Maximum tasks dispatched per surplus cycle',
          brainstorm_check_hours: 'Hours between brainstorm surplus runs',
          code_index_hours: 'Hours between code indexing runs',
          recon_gather_hours: 'Hours between recon gathering runs',
          maintenance_hours: 'Hours between maintenance task runs',
          analytical_hours: 'Hours between analytical runs (0 = disabled)',
          follow_up_dispatch_minutes: 'Minutes between follow-up dispatch checks',
          memory_extraction_hours: 'Hours between memory extraction runs',
          // Resilience
          transition_count: 'State transitions in window before flagging as flapping',
          window_seconds: 'Time window for flapping detection',
          stabilization_seconds: 'Seconds a service must stay stable after recovery',
          confirmation_probes: 'Health probes required to confirm recovery',
          confirmation_interval_s: 'Seconds between confirmation probes',
          drain_pace_s: 'Seconds between draining queued items',
          embedding_pace_per_min: 'Max embedding operations per minute during recovery',
          queue_overflow_threshold: 'Queue size that triggers overflow alert',
          max_sessions_per_hour: 'Max CC sessions per hour before throttling',
          throttle_threshold_pct: 'API usage percentage that triggers rate throttling',
          // Confidence Gates
          min_confidence: 'Minimum confidence score to accept (0.0\u20131.0)',
          min_separability: 'Minimum separability score for deep reflection (0.0\u20131.0)',
          shadow_mode: 'Log what would be filtered but do not enforce',
          // Updates
          interval_hours: 'Hours between checking for upstream updates',
          auto_apply: 'Automatically apply safe updates without approval',
          allowed_impacts: 'Impact levels eligible for auto-apply',
          backup_before_update: 'Run backup before applying any update',
          // Channels
          default_model: 'Model for new Telegram sessions (fable = most capable, opus = high quality, haiku = fastest)',
          default_effort: 'Effort level for new Telegram sessions (higher = more thorough)',
        },
        _SETTING_CHOICES: {
          provider: ['elevenlabs', 'fish_audio', 'cartesia'],
          model: ['fable', 'opus', 'sonnet', 'haiku'],
          effort: ['low', 'medium', 'high', 'xhigh', 'max'],
          approval_channel: ['telegram', 'email', 'dashboard'],
          default: ['telegram', 'email', 'dashboard'],
          blocker: ['telegram', 'email', 'dashboard'],
          alert: ['telegram', 'email', 'dashboard'],
          surplus: ['telegram', 'email', 'dashboard'],
          digest: ['telegram', 'email', 'dashboard'],
          default_model: ['fable', 'opus', 'sonnet', 'haiku'],
          default_effort: ['low', 'medium', 'high', 'xhigh', 'max'],
        },
        // Display order for settings domains — most important first
        _DOMAIN_ORDER: [
          'channels', 'autonomous_cli_policy', 'ego', 'outreach', 'tts',
          'inbox_monitor', 'surplus', 'resilience', 'confidence_gates',
          'updates', 'contribution', 'recon_schedules', 'recon_watchlist', 'recon_sources',
          'autonomy', 'autonomy_rules', 'guardian',
          'model_profiles', 'model_routing', 'content_sanitization',
        ],
        _DOMAIN_LABELS: {
          autonomous_cli_policy: 'Autonomous CLI Policy',
          ego: 'Autonomous Ego', outreach: 'Outreach Pipeline',
          tts: 'Text-to-Speech', inbox_monitor: 'Inbox Monitor',
          surplus: 'Surplus Compute', resilience: 'Resilience',
          confidence_gates: 'Confidence Gates', updates: 'Updates',
          recon_schedules: 'Recon Schedules', recon_watchlist: 'Recon Watchlist',
          recon_sources: 'Recon Sources',
          autonomy: 'Autonomy Levels', autonomy_rules: 'Autonomy Rules',
          guardian: 'Guardian', model_profiles: 'Model Profiles',
          model_routing: 'Model Routing',
          content_sanitization: 'Content Sanitization',
          channels: 'Channels',
          contribution: 'Contribution Offers',
        },
        _sortedDomains(domains) {
          const order = this._DOMAIN_ORDER;
          return [...domains].sort((a, b) => {
            const ai = order.indexOf(a.name), bi = order.indexOf(b.name);
            return (ai === -1 ? 999 : ai) - (bi === -1 ? 999 : bi);
          });
        },
        _settingLabel(key) {
          return this._SETTING_LABELS[key] || key.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
        },
        _settingDesc(key) {
          return this._SETTING_DESCS[key] || '';
        },

        // ── Color helpers for Knowledge & Memory tabs ──────────────
        _domainColor(d) {
          const m = {ai_research:'#60a5fa', professional:'#4ade80', genesis:'#a78bfa',
                     security:'#f87171', finance:'#fbbf24', general:'#94a3b8',
                     career:'#34d399', technology:'#38bdf8', science:'#c084fc'};
          return m[d] || '#6b7280';
        },
        _tierColor(t) {
          return {curated:'#fbbf24', validated:'#4ade80', raw:'#6b7280', promoted:'#60a5fa',
                  extraction_job:'#38bdf8', recon:'#f59e0b', manual:'#a78bfa',
                  ingestion:'#34d399', conversation:'#f472b6', unknown:'#6b7280'}[t] || '#6b7280';
        },
        _memoryTypeColor(t) {
          const m = {episodic_memory:'#60a5fa', semantic_memory:'#4ade80', procedural:'#f59e0b',
                     core_fact:'#a78bfa', episodic:'#60a5fa', semantic:'#4ade80'};
          return m[t] || '#6b7280';
        },

        async fetchRoutingConfig() {
          this.startFetch("routing");
          try {
            const resp = await fetchApi("/api/genesis/routing/config");
            if (resp && resp.ok) {
              this.routingConfig = await resp.json();
              this.finishFetch("routing");
            } else {
              this.failFetch("routing", "Routing config unavailable");
            }
          } catch (e) {
            console.warn("Routing config fetch failed:", e);
            this.failFetch("routing", "Routing config unavailable");
          }
        },

        async fetchApprovals() {
          this.startFetch("approvals");
          try {
            const resp = await fetchApi("/api/genesis/approvals");
            if (resp && resp.ok) {
              this.approvals = await resp.json();
              this.finishFetch("approvals");
            } else {
              this.failFetch("approvals", "Approval queue unavailable");
            }
          } catch (e) {
            console.warn("Approval fetch failed:", e);
            this.failFetch("approvals", "Approval queue unavailable");
          }
        },

        // New panel fetch methods
        async fetchCognitive() {
          this.startFetch("cognitive");
          try {
            const resp = await fetchApi("/api/genesis/cognitive");
            if (resp && resp.ok) {
              this.cognitiveState = await resp.json();
              this.finishFetch("cognitive");
            } else {
              this.failFetch("cognitive", "Cognitive state unavailable");
            }
          } catch (e) {
            console.warn("Cognitive fetch failed:", e);
            this.failFetch("cognitive", "Cognitive state unavailable");
          }
        },

        async fetchEssentialKnowledge() {
          try {
            const resp = await fetchApi("/api/genesis/essential-knowledge");
            if (resp && resp.ok) {
              this.essentialKnowledge = await resp.json();
            }
          } catch (e) {
            console.warn("Essential knowledge fetch failed:", e);
          }
        },

        async fetchAwarenessSignals() {
          this.startFetch("awarenessSignals");
          try {
            const resp = await fetchApi("/api/genesis/awareness/signals");
            if (resp && resp.ok) {
              this.awarenessSignals = await resp.json();
              this.finishFetch("awarenessSignals");
            } else {
              this.failFetch("awarenessSignals", "Awareness signals unavailable");
            }
          } catch (e) {
            console.warn("Awareness signals fetch failed:", e);
            this.failFetch("awarenessSignals", "Awareness signals unavailable");
          }
        },

        async fetchJobHealth() {
          this.startFetch("jobHealth");
          try {
            const resp = await fetchApi("/api/genesis/jobs");
            if (resp && resp.ok) {
              this.jobHealth = await resp.json();
              this.finishFetch("jobHealth");
            } else {
              this.failFetch("jobHealth", "Job health unavailable");
            }
          } catch (e) {
            console.warn("Job health fetch failed:", e);
            this.failFetch("jobHealth", "Job health unavailable");
          }
        },

        async fetchSchedulerData() {
          try {
            const [sysResp, userResp] = await Promise.all([
              fetchApi("/api/genesis/scheduler/system"),
              fetchApi("/api/genesis/scheduler/user"),
            ]);
            if (sysResp && sysResp.ok) this.schedulerData = await sysResp.json();
            if (userResp && userResp.ok) this.userJobs = await userResp.json();
          } catch (e) {
            // Non-critical
          }
        },

        async userJobAction(jobId, action) {
          try {
            const resp = await fetchApi(`/api/genesis/scheduler/user/${jobId}/control`, {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({ action }),
            });
            if (resp && resp.ok) {
              await this.fetchSchedulerData();
            }
          } catch (e) {
            console.warn("User job action failed:", e);
          }
        },

        async fetchEvalMetrics() {
          try {
            const resp = await fetchApi("/api/genesis/metrics/compounding");
            if (resp && resp.ok) {
              this.evalMetrics = await resp.json();
            }
          } catch (e) {
            console.warn("Eval metrics fetch failed:", e);
          }
          try {
            const resp = await fetchApi("/api/genesis/eval/subsystem-grades");
            if (resp && resp.ok) {
              this.subsystemGrades = await resp.json();
            }
          } catch (e) {
            console.warn("Subsystem grades fetch failed:", e);
          }
        },

        async fetchAutonomyConfig() {
          this.startFetch("autonomyConfig");
          try {
            const resp = await fetchApi("/api/genesis/autonomy/config");
            if (resp && resp.ok) {
              this.autonomyConfig = await resp.json();
              this.finishFetch("autonomyConfig");
            } else {
              this.failFetch("autonomyConfig", "Autonomy config unavailable");
            }
          } catch (e) {
            console.warn("Autonomy config fetch failed:", e);
            this.failFetch("autonomyConfig", "Autonomy config unavailable");
          }
        },

        async fetchAutonomousCliPolicy() {
          this.startFetch("autonomousCliPolicy");
          try {
            const resp = await fetchApi("/api/genesis/autonomous-cli-policy");
            if (resp && resp.ok) {
              this.autonomousCliPolicy = await resp.json();
              this.finishFetch("autonomousCliPolicy");
            } else {
              this.failFetch("autonomousCliPolicy", "Autonomous CLI policy unavailable");
            }
          } catch (e) {
            console.warn("Autonomous CLI policy fetch failed:", e);
            this.failFetch("autonomousCliPolicy", "Autonomous CLI policy unavailable");
          }
        },

        // Pause / kill switch
        async fetchPauseState() {
          try {
            const resp = await fetchApi("/api/genesis/pause");
            if (resp && resp.ok) {
              const data = await resp.json();
              this.pauseState.paused = data.paused;
              this.pauseState.reason = data.reason;
              this.pauseState.since = data.since;
            }
          } catch (e) { console.warn("Pause state fetch failed:", e); }
        },

        async togglePause() {
          const newState = !this.pauseState.paused;
          const action = newState ? "Pause" : "Resume";
          const pauseMsg = "Pause Genesis?\n\nThis will stop ALL background activity:\n• Reflections\n• Surplus tasks\n• Outreach\n• Inbox monitoring\n\nConversations still work but without background enrichment.\nYou must manually resume via this button or /pause off in Telegram.";
          const resumeMsg = "Resume Genesis?\n\nAll background activity will restart\n(reflections, surplus tasks, outreach, inbox monitoring).";
          if (!(await this.showConfirm(action + " Genesis", newState ? pauseMsg : resumeMsg))) return;
          this.pauseState.toggling = true;
          try {
            const resp = await fetchApi("/api/genesis/pause", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ paused: newState, reason: "Dashboard toggle" }),
            });
            if (resp && resp.ok) {
              await this.fetchPauseState();
            }
          } catch (e) { console.warn("Pause toggle failed:", e); }
          this.pauseState.toggling = false;
        },

        // Service restarts
        async restartBridge() {
          const label = this.health.services?.bridge?.service_label || "Genesis";
          if (!(await this.showConfirm("Restart", `Restart Genesis ${label}?`))) return;
          this._restartingBridge = true;
          try {
            const resp = await fetchApi("/api/genesis/restart/bridge", { method: "POST" });
            if (resp && resp.ok) {
              setTimeout(() => { this._restartingBridge = false; this.fetchHealth(); }, 5000);
            } else {
              // Restarting ourselves kills the connection — a network error
              // or 500 with empty message is expected, not a failure.
              const err = await resp?.json().catch(() => ({}));
              if (!err.message) {
                setTimeout(() => { this._restartingBridge = false; this.fetchHealth(); }, 5000);
              } else {
                alert(`${label} restart failed: ` + err.message);
                this._restartingBridge = false;
              }
            }
          } catch (e) {
            // Network error from the server restarting itself — expected.
            setTimeout(() => { this._restartingBridge = false; this.fetchHealth(); }, 5000);
          }
        },

        async restartHostFramework() {
          const name = this.health.services?.host_framework?.name || "host framework";
          if (!(await this.showConfirm("Restart", `Restart ${name}? The dashboard will reload.`))) return;
          this._restartingHF = true;
          try {
            fetchApi("/api/genesis/restart/host-framework", { method: "POST" });
          } catch (e) { /* server will die — expected */ }
          setTimeout(() => window.location.reload(), 8000);
        },

        // Module & Provider toggles
        async toggleModule(name) {
          try {
            const resp = await fetchApi(`/api/genesis/modules/${name}/toggle`, { method: "POST" });
            if (resp?.ok) {
              await this.fetchModules();
            } else {
              const err = await resp?.json().catch(() => ({}));
              alert("Toggle failed: " + (err.message || "unknown error"));
            }
          } catch (e) { alert("Toggle failed: " + e.message); }
        },

        async toggleProvider(name) {
          try {
            const resp = await fetchApi(`/api/genesis/providers/${name}/toggle`, { method: "POST" });
            if (resp?.ok) {
              await Promise.all([this.fetchProvidersDetail(), this.fetchRoutingConfig()]);
            } else {
              const err = await resp?.json().catch(() => ({}));
              alert("Toggle failed: " + (err.message || "unknown error"));
            }
          } catch (e) { alert("Toggle failed: " + e.message); }
        },

        // Actions

        syncBudgetEditor() {
          const current = this.budgets.find(b => b.budget_type === this.budgetEditor.budget_type);
          this.budgetEditor.limit_usd = current ? Number(current.limit_usd).toFixed(2) : "";
          this.budgetEditor.warning_pct = current ? String(Number(current.warning_pct)) : "0.8";
        },

        selectBudgetType(type) {
          this.budgetEditor.budget_type = type;
          this.budgetEditor.message = null;
          this.budgetEditor.error = null;
          this.syncBudgetEditor();
        },

        async saveBudget() {
          this.budgetEditor.saving = true;
          this.budgetEditor.message = null;
          this.budgetEditor.error = null;
          try {
            const resp = await fetchApi("/api/genesis/budgets", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                budget_type: this.budgetEditor.budget_type,
                limit_usd: Number(this.budgetEditor.limit_usd),
                warning_pct: Number(this.budgetEditor.warning_pct),
              }),
            });
            if (resp?.ok) {
              await this.fetchBudgets();
              this.budgetEditor.message = `Saved ${this.budgetEditor.budget_type} budget`;
            } else {
              this.budgetEditor.error = await this.readApiError(resp, "Budget save failed");
            }
          } catch (e) {
            console.error("Budget save failed:", e);
            this.budgetEditor.error = "Budget save failed";
          } finally {
            this.budgetEditor.saving = false;
          }
        },

        openRoutingEditor(siteId) {
          const config = this.routingConfig?.call_sites?.[siteId];
          if (!config) return;
          this.routingEditor.siteId = siteId;
          this.routingEditor.chainText = (config.chain || []).join(", ");
          this.routingEditor.default_paid = !!config.default_paid;
          this.routingEditor.never_pays = !!config.never_pays;
          this.routingEditor.message = null;
          this.routingEditor.error = null;
        },

        closeRoutingEditor() {
          this.routingEditor.siteId = null;
          this.routingEditor.chainText = "";
          this.routingEditor.default_paid = false;
          this.routingEditor.never_pays = false;
          this.routingEditor.message = null;
          this.routingEditor.error = null;
        },

        async saveRoutingEditor() {
          if (!this.routingEditor.siteId) return;
          this.routingEditor.saving = true;
          this.routingEditor.message = null;
          this.routingEditor.error = null;
          try {
            const chain = this.routingEditor.chainText
              .split(",")
              .map(value => value.trim())
              .filter(Boolean);
            const resp = await fetchApi(`/api/genesis/routing/config/${encodeURIComponent(this.routingEditor.siteId)}`, {
              method: "PUT",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                chain,
                default_paid: !!this.routingEditor.default_paid,
                never_pays: !!this.routingEditor.never_pays,
              }),
            });
            if (resp?.ok) {
              await Promise.all([this.fetchRoutingConfig(), this.fetchHealth(), this.fetchErrorSummary()]);
              this.routingEditor.message = `Saved routing for ${this.routingEditor.siteId}`;
            } else {
              this.routingEditor.error = await this.readApiError(resp, "Routing update failed");
            }
          } catch (e) {
            console.error("Routing update failed:", e);
            this.routingEditor.error = "Routing update failed";
          } finally {
            this.routingEditor.saving = false;
          }
        },

        async reloadRoutingConfig() {
          this.routingEditor.reloading = true;
          this.routingEditor.reloadMessage = null;
          this.routingEditor.reloadError = null;
          try {
            const resp = await fetchApi("/api/genesis/routing/reload", { method: "POST" });
            if (resp?.ok) {
              await Promise.all([this.fetchRoutingConfig(), this.fetchHealth(), this.fetchErrorSummary()]);
              this.routingEditor.reloadMessage = "Routing config reloaded";
            } else {
              this.routingEditor.reloadError = await this.readApiError(resp, "Routing reload failed");
            }
          } catch (e) {
            console.error("Routing reload failed:", e);
            this.routingEditor.reloadError = "Routing reload failed";
          } finally {
            this.routingEditor.reloading = false;
          }
        },

        async resolveApproval(requestId, decision, outreachMsgId = null) {
          const current = this.approvalActions[requestId] || {};
          this.approvalActions = {
            ...this.approvalActions,
            [requestId]: { ...current, saving: true, error: null },
          };
          try {
            const resp = await fetchApi(`/api/genesis/approvals/${encodeURIComponent(requestId)}/resolve`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ decision }),
            });
            if (resp?.ok) {
              this.approvals = this.approvals.filter(req => req.id !== requestId);
              this.approvalActions = {
                ...this.approvalActions,
                [requestId]: { saving: false, error: null },
              };
              this.finishFetch("approvals");
              // Post-resolve follow-ups must not flip the error state — the
              // approval itself already resolved successfully.
              try {
                if (outreachMsgId) {
                  const outcome = decision === "approved" ? "useful" : "not_useful";
                  await fetchApi(`/api/genesis/outreach/${encodeURIComponent(outreachMsgId)}/engage`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ outcome, response: decision === "approved" ? "approve" : "deny" }),
                  });
                }
                await Promise.all([
                  this.fetchOutreachMessages(),
                  this.fetchApprovals(),
                  this.fetchComms(),
                ]);
              } catch (e) {
                console.error("post-resolve refresh failed", e);
              }
            } else {
              const error = await this.readApiError(resp, "Approval update failed");
              this.approvalActions = {
                ...this.approvalActions,
                [requestId]: { saving: false, error },
              };
            }
          } catch (e) {
            this.approvalActions = {
              ...this.approvalActions,
              [requestId]: { saving: false, error: "Approval update failed" },
            };
          }
        },

        async clearDeferredItem(itemId) {
          this.queueReview.clearingIds = { ...this.queueReview.clearingIds, [itemId]: true };
          this.queueReview.message = null;
          this.queueReview.error = null;
          try {
            const resp = await fetchApi(`/api/genesis/deferred/${encodeURIComponent(itemId)}/clear`, { method: "DELETE" });
            if (resp?.ok) {
              // Brief delay to ensure DB commit propagates before re-fetch
              await new Promise(r => setTimeout(r, 200));
              await Promise.all([this.fetchHealth(), this.fetchErrorSummary()]);
              this.queueReview.message = "Discarded item cleared";
              setTimeout(() => { this.queueReview.message = null; }, 5000);
            } else {
              this.queueReview.error = await this.readApiError(resp, "Unable to clear discarded item");
            }
          } catch (e) {
            console.error("Clear discarded item failed:", e);
            this.queueReview.error = "Unable to clear discarded item";
          } finally {
            const { [itemId]: _, ...rest } = this.queueReview.clearingIds;
            this.queueReview.clearingIds = rest;
          }
        },

        async clearAllDiscardedItems() {
          this.queueReview.clearingAll = true;
          this.queueReview.message = null;
          this.queueReview.error = null;
          try {
            const resp = await fetchApi("/api/genesis/deferred/all/clear", { method: "DELETE" });
            if (resp?.ok) {
              const data = await resp.json();
              await Promise.all([this.fetchHealth(), this.fetchErrorSummary()]);
              this.queueReview.message = `Cleared ${data?.cleared ?? 0} discarded item${data?.cleared === 1 ? "" : "s"}`;
            } else {
              this.queueReview.error = await this.readApiError(resp, "Unable to clear discarded items");
            }
          } catch (e) {
            console.error("Clear all discarded items failed:", e);
            this.queueReview.error = "Unable to clear discarded items";
          } finally {
            this.queueReview.clearingAll = false;
          }
        },

        async openConfigFile(file) {
          const sameFile = this.configModal.loadedName === file.name;
          this.configModal.open = true;
          this.configModal.name = file.name;
          this.configModal.editable = file.editable !== false;
          this.configModal.deletable = !!file.deletable;
          this.configModal.dirty = false;
          this.configModal.saveMessage = null;
          this.configModal.saveError = null;
          if (!sameFile) {
            this.configModal.content = "";
            this.configModal.loadedName = "";
            this.resetModalFetch("configModal");
          }
          this.startModalFetch("configModal");
          try {
            const resp = await fetchApi(`/api/genesis/config-files/${file.name.split('/').map(encodeURIComponent).join('/')}`);
            if (resp?.ok) {
              const data = await resp.json();
              this.configModal.content = data.content || "(empty file)";
              this.configModal.loadedName = file.name;
              this.configModal.syntax = data.syntax || "markdown";
              this.finishModalFetch("configModal");
              requestAnimationFrame(() => this._initConfigEditor());
            } else {
              this.failModalFetch("configModal", "Config file unavailable");
            }
          } catch (e) {
            this.failModalFetch("configModal", "Config file unavailable");
          }
        },

        _initConfigEditor() {
          const el = document.getElementById("config-ace-editor");
          if (!el || !window.ace) return;
          if (this._configEditor) { this._configEditor.destroy(); this._configEditor = null; }
          const editor = window.ace.edit(el);
          editor.setTheme("ace/theme/twilight");
          const mode = this.configModal.syntax === "yaml" ? "ace/mode/yaml" : "ace/mode/markdown";
          editor.session.setMode(mode);
          editor.setValue(this.configModal.content, -1);
          editor.setReadOnly(!this.configModal.editable);
          editor.setOptions({ fontSize: "13px", showPrintMargin: false, wrap: true, tabSize: 2 });
          editor.on("change", () => {
            this.configModal.dirty = true;
            this.configModal.saveMessage = null;
            this.configModal.saveError = null;
          });
          this._configEditor = editor;
        },

        async saveConfigFile() {
          if (!this._configEditor || this.configModal.saving) return;
          this.configModal.saving = true;
          this.configModal.saveMessage = null;
          this.configModal.saveError = null;
          const content = this._configEditor.getValue();
          try {
            const resp = await fetchApi(
              `/api/genesis/config-files/${this.configModal.name.split('/').map(encodeURIComponent).join('/')}`,
              { method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({content}) }
            );
            if (resp?.ok) {
              this.configModal.dirty = false;
              this.configModal.content = content;
              this.configModal.saveMessage = "Saved";
            } else {
              const data = await resp.json().catch(() => ({}));
              this.configModal.saveError = data.error || "Save failed";
            }
          } catch (e) {
            this.configModal.saveError = "Save failed";
          } finally {
            this.configModal.saving = false;
          }
        },

        async deleteConfigFile() {
          if (!this.configModal.deletable) return;
          if (!(await this.showConfirm("Delete File", `Delete ${this.configModal.name}? This cannot be undone.`))) return;
          try {
            const resp = await fetchApi(
              `/api/genesis/config-files/${this.configModal.name.split('/').map(encodeURIComponent).join('/')}`,
              { method: "DELETE" }
            );
            if (resp?.ok) {
              this.configFiles = this.configFiles.filter(f => f.name !== this.configModal.name);
              this.closeConfigModal();
            } else {
              const data = await resp.json().catch(() => ({}));
              this.configModal.saveError = data.error || "Delete failed";
            }
          } catch (e) {
            this.configModal.saveError = "Delete failed";
          }
        },

        async closeConfigModal() {
          if (this.configModal.dirty && !(await this.showConfirm("Discard Changes", "You have unsaved changes. Discard?"))) return;
          if (this._configEditor) { this._configEditor.destroy(); this._configEditor = null; }
          this.configModal.open = false;
          this.configModal.dirty = false;
        },

        // Helpers
        severityColor(severity) {
          const map = { debug: "#666", info: "#ccc", warning: "#f0ad4e", error: "#d9534f", critical: "#ff2d2d" };
          return map[severity] || "#ccc";
        },

        async readApiError(resp, fallback) {
          if (!resp) return fallback;
          try {
            const data = await resp.json();
            return data?.error || data?.message || fallback;
          } catch {
            return fallback;
          }
        },

        startFetch(name) {
          const state = this.fetchState[name];
          if (!state) return;
          state.state = state.lastSuccess ? "refreshing" : "loading";
          state.error = null;
        },

        finishFetch(name) {
          const state = this.fetchState[name];
          if (!state) return;
          state.state = "healthy";
          state.lastSuccess = Date.now();
          state.error = null;
        },

        failFetch(name, message) {
          const state = this.fetchState[name];
          if (!state) return;
          state.state = state.lastSuccess ? "stale" : "error";
          state.error = message;
        },

        panelState(name) {
          const state = this.fetchState[name];
          return state ? state.state : "unknown";
        },

        panelStateColor(name) {
          return this.fetchStateColor(this.panelState(name));
        },

        panelStateLabel(name) {
          const state = this.fetchState[name];
          if (!state) return "unknown";
          return state.state;
        },

        panelStatusDetail(name) {
          return this.fetchStatusDetail(this.fetchState[name]);
        },

        resetModalFetch(name) {
          const state = this[name]?.fetch;
          if (!state) return;
          state.state = "idle";
          state.lastSuccess = null;
          state.error = null;
        },

        startModalFetch(name) {
          const state = this[name]?.fetch;
          if (!state) return;
          state.state = state.lastSuccess ? "refreshing" : "loading";
          state.error = null;
        },

        finishModalFetch(name) {
          const state = this[name]?.fetch;
          if (!state) return;
          state.state = "healthy";
          state.lastSuccess = Date.now();
          state.error = null;
        },

        failModalFetch(name, message) {
          const state = this[name]?.fetch;
          if (!state) return;
          state.state = state.lastSuccess ? "stale" : "error";
          state.error = message;
        },

        fetchStateColor(stateName) {
          const map = {
            healthy: "#4caf50",
            loading: "#2196F3",
            refreshing: "#2196F3",
            stale: "#f0ad4e",
            error: "#d9534f",
            idle: "#666",
            unknown: "#666",
          };
          return map[stateName] || "#666";
        },

        fetchStatusDetail(state) {
          if (!state) return "";
          if (state.state === "stale") {
            const parts = [];
            if (state.error) parts.push(state.error);
            if (state.lastSuccess) parts.push(`last ok ${this.relativeTime(state.lastSuccess)}`);
            return parts.join(" — ");
          }
          if (state.state === "healthy" && state.lastSuccess) {
            return `updated ${this.relativeTime(state.lastSuccess)}`;
          }
          if (state.state === "refreshing" && state.lastSuccess) {
            return `refreshing — last ok ${this.relativeTime(state.lastSuccess)}`;
          }
          return state.error || "";
        },

        modalStateColor(modal) {
          return this.fetchStateColor(modal?.fetch?.state || "unknown");
        },

        modalStateLabel(modal) {
          return modal?.fetch?.state || "unknown";
        },

        modalStatusDetail(modal) {
          return this.fetchStatusDetail(modal?.fetch);
        },

        semanticStateColor(state) {
          // Colors for status DOTS — classified through the same shared
          // chipState() the chips use, so the two surfaces can never disagree
          // about what a state means. Values match the token palette
          // (--ok/--warn/--err); off/idle/unknown keep the historical dot gray.
          const colors = { ok: "#4caf50", warn: "#f0ad4e", err: "#d9534f", stale: "#9e9e9e", off: "#888" };
          return colors[chipState(state)] || "#888";
        },

        statusReason(fetchName, semantic) {
          const fetchDetail = fetchName ? this.panelStatusDetail(fetchName) : "";
          if (fetchDetail && semantic?.reason) return `${semantic.reason} — ${fetchDetail}`;
          return semantic?.reason || fetchDetail || "";
        },

        infrastructureSemantic() {
          const infra = this.health.infrastructure;
          if (!infra) return { state: "unknown", reason: "infrastructure data unavailable" };
          const probes = Object.values(infra);
          if (!probes.length) return { state: "unknown", reason: "no infrastructure probes reported" };
          if (probes.some(probe => ["error", "down"].includes(probe?.status))) {
            return { state: "error", reason: "one or more infrastructure probes failed" };
          }
          if ((infra.ollama?.missing_models?.length || 0) > 0) {
            return { state: "degraded", reason: "configured Ollama models are missing" };
          }
          if (infra.cc_tmp?.cc_tier === "red" || infra.cc_tmp?.sys_tier === "red") {
            return { state: "error", reason: "temp directory critically full" };
          }
          if (infra.cc_tmp?.cc_tier === "orange" || infra.cc_tmp?.sys_tier === "orange") {
            return { state: "degraded", reason: "temp directory pressure detected" };
          }
          if (probes.some(probe => ["degraded", "fallback", "overdue"].includes(probe?.status))) {
            return { state: "degraded", reason: "one or more infrastructure probes are degraded" };
          }
          if (probes.some(probe => ["unknown", "unavailable"].includes(probe?.status))) {
            return { state: "unknown", reason: "some infrastructure probes have no current data" };
          }
          return { state: "healthy", reason: "infrastructure probes are healthy" };
        },

        ccSemantic() {
          const cc = this.health.cc_sessions;
          if (!cc) return { state: "unknown", reason: "CC session data unavailable" };
          const rt = cc.realtime_status;
          // A hard contingency outage (UNAVAILABLE, never auto-reconciled) outranks
          // fallback: don't paint a red outage yellow if is_fallback is stale.
          if (rt === "UNAVAILABLE") {
            return { state: "error", reason: "CC is unavailable — contingency mode active" };
          }
          // Fallback is more specific than the rate-limit that triggered it, so it
          // takes precedence over the RATE_LIMITED/THROTTLED branch below.
          if (cc.fallback?.is_fallback) {
            return { state: "fallback", reason: `running on ${cc.fallback.fallback || 'a roster peer'} (Claude ${(cc.fallback.reason || 'unavailable').replace(/_/g, ' ')})` };
          }
          if (rt === "RATE_LIMITED" || rt === "THROTTLED") {
            return { state: "degraded", reason: `CC is ${rt.toLowerCase().replace('_', '-')}` };
          }
          if (cc.background?.status === "error") {
            return { state: "error", reason: "background CC session health check failed" };
          }
          if ((cc.failed_24h || 0) > 0 || ["warning", "limited"].includes(cc.background?.status)) {
            return { state: "degraded", reason: "recent failed CC sessions detected" };
          }
          if (cc.background?.status === "unknown") {
            return { state: "unknown", reason: "background CC capacity is unknown" };
          }
          return { state: "healthy", reason: "CC sessions are operating normally" };
        },

        costSemantic() {
          const cost = this.health.cost;
          if (!cost) return { state: "unknown", reason: "cost data unavailable" };
          if (cost.budget_status === "ERROR") {
            return { state: "error", reason: "cost tracker returned an error" };
          }
          const cc = this.health.cc_sessions;
          if (cc && (cc.rate_limited_24h || 0) > 0) {
            return { state: "degraded", reason: cc.rate_limited_24h + " rate limit(s) in 24h" };
          }
          if (cost.monthly_usd == null) return { state: "unknown", reason: "cost tracking is unavailable" };
          if (cost.budget_monthly_limit && cost.forecast_monthly_usd > cost.budget_monthly_limit) {
            return { state: "degraded", reason: "forecast exceeds monthly budget" };
          }
          if (cost.budget_monthly_limit && cost.forecast_monthly_usd > cost.budget_monthly_limit * 0.8) {
            return { state: "degraded", reason: "forecast is approaching the monthly budget" };
          }
          return { state: "healthy", reason: "cost usage is within current limits" };
        },

        providerHealthSemantic() {
          // Provider health now derived from api_keys alerts
          const apiKeys = this.health.api_keys;
          if (!apiKeys) return { state: "unknown", reason: "provider data unavailable" };
          const alerts = apiKeys.alerts || [];
          const critical = alerts.filter(a => a.severity === "critical");
          if (critical.length > 0) {
            return { state: "error", reason: critical.map(a => a.message).join("; ") };
          }
          const warnings = alerts.filter(a => a.severity === "warning");
          if (warnings.length > 0) {
            return { state: "degraded", reason: warnings.length + " provider warning(s)" };
          }
          return { state: "healthy", reason: "all providers healthy" };
        },

        queuesSemantic() {
          const queues = this.health.queues;
          if (!queues) return { state: "unknown", reason: "queue data unavailable" };
          if ((queues.dead_letters || 0) > 0 || (queues.discarded_items?.length || 0) > 0
              || (queues.deferred_stuck || 0) > 0 || (queues.failed_embeddings || 0) > 0) {
            return { state: "error", reason: "stuck/failed items or dead letters require attention" };
          }
          if ((queues.deferred_work || 0) > 0 || (queues.deferred_processing || 0) > 0) {
            return { state: "degraded", reason: "work is backing up in deferred queues" };
          }
          const pe = queues.pending_embeddings || 0;
          if (pe > 2000) {
            return { state: "error", reason: `embedding queue critically backed up (${pe} pending)` };
          }
          if (pe > 500) {
            return { state: "degraded", reason: `embedding queue backed up (${pe} pending)` };
          }
          if (Array.isArray(queues.errors) && queues.errors.length > 0) {
            return { state: "unknown", reason: "some queue counters could not be collected" };
          }
          return { state: "healthy", reason: "queues are clear" };
        },

        surplusSemantic() {
          const surplus = this.health.surplus;
          if (!surplus || surplus.status === "unknown") return { state: "unknown", reason: "surplus scheduler data unavailable" };
          const failed = surplus.tasks_failed_24h || 0;
          const completed = surplus.tasks_completed_24h || 0;
          if (failed > 0 && completed > 0 && failed / (completed + failed) > 0.2) {
            return { state: "degraded", reason: `surplus failure rate ${Math.round(failed / (completed + failed) * 100)}% (${failed}/${completed + failed})` };
          }
          if (failed > 0 && completed === 0) {
            return { state: "degraded", reason: `${failed} surplus failures, no completions` };
          }
          return { state: "healthy", reason: `surplus scheduler is ${surplus.status}` };
        },

        egoSemantic() {
          const ego = this.egoStatus;
          if (!ego || ego.status === "not_bootstrapped") return { state: "unknown", reason: "ego data unavailable" };
          if (!ego.enabled) return { state: "unknown", reason: "ego disabled by config" };
          // Check both egos' cadence state via the per-ego data
          const ue = ego.egos?.user_ego;
          const ge = ego.egos?.genesis_ego;
          const ueCad = ue?.cadence;
          const geCad = ge?.cadence;
          // Circuit breaker on either ego
          if (ueCad?.consecutive_failures >= 3 || geCad?.consecutive_failures >= 3) {
            const which = (ueCad?.consecutive_failures >= 3 ? "CEO" : "") +
                          (geCad?.consecutive_failures >= 3 ? (ueCad?.consecutive_failures >= 3 ? " + COO" : "COO") : "");
            return { state: "error", reason: `circuit open (${which})` };
          }
          // Fallback to modal cadence if per-ego data not available yet
          const cadence = this.egoModal.cadence;
          if (!ue && cadence?.available) {
            if (cadence.consecutive_failures >= 3) return { state: "error", reason: `circuit open (${cadence.consecutive_failures} failures)` };
            if (cadence.is_paused) return { state: "degraded", reason: "ego paused" };
          }
          if (ueCad?.is_paused && geCad?.is_paused) {
            return { state: "degraded", reason: "both egos paused" };
          }
          if (ueCad?.is_paused || geCad?.is_paused) {
            const which = ueCad?.is_paused ? "CEO" : "COO";
            return { state: "degraded", reason: `${which} paused` };
          }
          // Proposals piling up
          if (ego.pending_proposals > 5) {
            return { state: "degraded", reason: `${ego.pending_proposals} proposals awaiting review` };
          }
          return { state: "healthy", reason: ego.focus_summary || "ego active" };
        },

        servicesSemantic() {
          const services = this.health.services;
          if (!services || services.status === "unknown") return { state: "unknown", reason: "service status unavailable" };
          const svcLabel = (services?.bridge?.service_label || "Genesis").toLowerCase();
          // Host framework health
          const hf = services?.host_framework;
          if (hf?.detected && (hf.status === "down" || hf.status === "error")) {
            return { state: "error", reason: `${hf.name} is ${hf.status}` };
          }
          if (services?.bridge?.active_state && services.bridge.active_state !== "active") {
            if (services.bridge.active_state === "unknown") {
              return { state: "unknown", reason: `${svcLabel} service state is unknown` };
            }
            return { state: "error", reason: `${svcLabel} service is not active` };
          }
          if (services?.watchdog_timer?.active_state && services.watchdog_timer.active_state !== "active") {
            if (services.watchdog_timer.active_state === "unknown") {
              return { state: "unknown", reason: "watchdog timer state is unknown" };
            }
            return { state: "error", reason: "watchdog timer is not active" };
          }
          if (hf?.detected && hf.status === "degraded") {
            return { state: "degraded", reason: `${hf.name} is degraded` };
          }
          if ((services?.watchdog?.consecutive_failures || 0) > 3) {
            const wd = services.watchdog;
            return { state: "degraded", reason: `watchdog: ${wd.consecutive_failures} repeated restarts (${wd.last_reason || 'unknown'})` };
          }
          // Sentinel — container-side guardian. Below liveness checks above so a
          // dead bridge/timer/host_framework still wins; an escalated Sentinel
          // paints the card red, an in-flight investigation paints it degraded.
          const sentinel = services?.sentinel;
          if (sentinel?.enabled) {
            if (sentinel.is_stale) {
              return { state: "degraded", reason: `sentinel stale — last heartbeat ${Math.round((sentinel.staleness_s || 0) / 60)}m ago` };
            }
            if (sentinel.current_state === "escalated") {
              return { state: "error", reason: "sentinel escalated — CC diagnosis failed" };
            }
            if (["investigating", "remediating", "awaiting_dispatch_approval", "awaiting_action_approval"].includes(sentinel.current_state)) {
              const label = sentinel.current_state.startsWith("awaiting") ? "awaiting approval" : sentinel.current_state;
              return { state: "degraded", reason: `sentinel ${label}` };
            }
          }
          return { state: "healthy", reason: `${svcLabel} and watchdog services are active` };
        },

        containerSemantic() {
          const mem = this.health.infrastructure?.container_memory;
          const disk = this.health.infrastructure?.disk;
          const cpu = this.health.infrastructure?.cpu;
          // "down" is the memory block's worst status; rank it with "error".
          const stateRank = { error: 3, down: 3, degraded: 2, unknown: 1, unavailable: 1, healthy: 0 };

          // Memory assessment (primary — OOM kills). Status already folds in
          // sustained memory PSI server-side; report PSI when it's the driver.
          let memState = "healthy", memReason = "container resources within safe limits";
          const memPct = (mem?.anon_pct ?? mem?.used_pct);
          const memFull60 = mem?.pressure?.full_avg60;
          if (!mem || ["unknown", "unavailable"].includes(mem.status)) {
            memState = "unknown"; memReason = "container memory data unavailable";
          } else if (mem.status === "down" || memPct > 85) {
            memState = "error";
            memReason = (memFull60 >= 30)
              ? "memory stalling work (PSI " + memFull60.toFixed(0) + "%)"
              : "memory in danger zone (" + memPct.toFixed(0) + "%)";
          } else if (mem.status === "degraded" || memPct > 75) {
            memState = "degraded";
            memReason = (memFull60 >= 10)
              ? "memory pressure elevated (PSI " + memFull60.toFixed(0) + "%)"
              : "memory usage elevated (" + memPct.toFixed(0) + "%)";
          }

          // Disk assessment (secondary — write failures)
          let diskState = "healthy", diskReason = "";
          if (disk && disk.free_pct != null) {
            const usedPct = 100 - disk.free_pct;
            if (usedPct > 90) {
              diskState = "error"; diskReason = "disk critically full (" + usedPct.toFixed(0) + "%)";
            } else if (usedPct > 85) {
              diskState = "degraded"; diskReason = "disk usage high (" + usedPct.toFixed(0) + "%)";
            }
          }

          // CPU assessment (utilization % from used_pct — NOT loadavg).
          let cpuState = "healthy", cpuReason = "";
          if (cpu && (cpu.status === "degraded" || cpu.status === "error")) {
            cpuState = cpu.status;
            const cpuFull60 = cpu.pressure?.full_avg60;
            cpuReason = "CPU " + cpu.status + " (" + (cpu.used_pct ?? 0).toFixed(0) + "% used"
              + ((cpuFull60 >= 1) ? ", stall " + cpuFull60.toFixed(0) + "%" : "") + ")";
          }

          // Worst-of — memory, disk, or CPU, whichever is worst.
          let worst = { state: memState, reason: memReason };
          for (const c of [{ state: diskState, reason: diskReason }, { state: cpuState, reason: cpuReason }]) {
            if ((stateRank[c.state] || 0) > (stateRank[worst.state] || 0)) worst = c;
          }
          return worst;
        },

        awarenessSemantic() {
          const awareness = this.health.awareness;
          if (!awareness) return { state: "unknown", reason: "awareness data unavailable" };
          if (awareness.status === "overdue") {
            return { state: "error", reason: "awareness loop is overdue (>6 min)" };
          }
          if (awareness.status === "no_ticks") {
            return { state: "error", reason: "awareness loop has never ticked — system may not be running" };
          }
          if (awareness.status === "unknown") {
            return { state: "unknown", reason: "awareness loop has no current tick data" };
          }
          if ((awareness.critical_bypasses_24h || 0) > 0) {
            return { state: "degraded", reason: "critical bypasses were recorded recently" };
          }
          return { state: "healthy", reason: "awareness loop is ticking normally" };
        },

        outreachSemantic() {
          const outreach = this.health.outreach_stats;
          if (!outreach || outreach.status === "unknown") {
            return { state: "unknown", reason: "outreach metrics unavailable" };
          }
          return { state: "healthy", reason: outreach.total > 0 ? "recent outreach activity recorded" : "no outreach sent in the last 7 days" };
        },

        conversationSemantic() {
          const conversation = this.health.conversation;
          if (!conversation || conversation.status === "unknown" || conversation.last_user_message_age_s == null) {
            return { state: "unknown", reason: "conversation activity is unavailable" };
          }
          return { state: "healthy", reason: "conversation activity is available" };
        },

        mcpSemantic() {
          const servers = this.health.mcp_servers;
          if (!servers) return { state: "unknown", reason: "MCP server data unavailable" };
          const values = Object.values(servers);
          if (!values.length) return { state: "unknown", reason: "no MCP servers reported" };
          if (values.some(info => info.status === "error")) {
            return { state: "error", reason: "one or more MCP servers are failing" };
          }
          if (values.some(info => info.status !== "up")) {
            return { state: "degraded", reason: "one or more MCP servers are only partially available" };
          }
          return { state: "healthy", reason: "MCP servers are available" };
        },

        apiKeysSemantic() {
          const apiKeys = this.health.api_keys;
          if (!apiKeys) return { state: "unknown", reason: "API key data unavailable" };
          const keys = apiKeys.providers || apiKeys;
          const vals = Object.values(keys);
          // Convention: red = a provider's API is not working (breaker open /
          // out of credits) → error chip; yellow = a key is missing/unconfigured
          // → degraded chip; otherwise healthy. (System-wide alarm is governed
          // separately by essential coverage, not by this card.)
          const red = vals.filter(info => info.key_health === "red").length;
          const yellow = vals.filter(info => info.key_health === "yellow").length;
          if (red > 0) {
            return { state: "error", reason: `${red} provider API(s) not working (e.g. out of credits)` };
          }
          if (yellow > 0) {
            return { state: "degraded", reason: `${yellow} provider key(s) missing or unconfigured` };
          }
          return { state: "healthy", reason: "provider keys are configured and working" };
        },

        overallHealthSemantic() {
          // Aggregate all semantic health signals into one overall status
          const checks = [
            this.infrastructureSemantic(),
            this.providerHealthSemantic(),
            this.servicesSemantic(),
            this.awarenessSemantic(),
            this.surplusSemantic(),
            this.queuesSemantic(),
            ...(this.egoStatus?.enabled ? [this.egoSemantic()] : []),
          ];
          const hasError = checks.some(c => c.state === "error");
          const hasDegraded = checks.some(c => c.state === "degraded" || c.state === "fallback");
          const hasUnknown = checks.some(c => c.state === "unknown");
          if (hasError) {
            const reasons = checks.filter(c => c.state === "error").map(c => c.reason);
            return { state: "error", reason: reasons.join("; ") };
          }
          if (hasDegraded) {
            const reasons = checks.filter(c => c.state === "degraded" || c.state === "fallback").map(c => c.reason);
            return { state: "degraded", reason: reasons.join("; ") };
          }
          if (hasUnknown) {
            return { state: "unknown", reason: "some subsystems have unknown status" };
          }
          return { state: "healthy", reason: "all core subsystems operational" };
        },

        routingSemantic() {
          const sites = this.health.call_sites || {};
          const routing = Object.values(sites).filter(s => s.routing !== false);
          if (!routing.length) return { state: "unknown", reason: "routing data unavailable" };
          if (routing.some(site => site.status === "down")) {
            return { state: "error", reason: "one or more call sites are down" };
          }
          if (this.fallbackCallSiteCount > 0) {
            return { state: "fallback", reason: "one or more call sites are running on fallback providers" };
          }
          if (routing.some(site => site.status === "degraded")) {
            return { state: "degraded", reason: "one or more call sites are degraded" };
          }
          if (routing.some(site => site.status === "warning")) {
            return { state: "degraded", reason: "recent routing failures detected on one or more call sites" };
          }
          return { state: "healthy", reason: "call sites are using their primary routes" };
        },

        probeStatusColor(probe) {
          // Color Rule: GREEN = primary/healthy, YELLOW = fallback/degraded, RED = error/down, GRAY = unknown
          const map = {
            healthy: "#4caf50", active: "#4caf50",     // GREEN: confirmed working
            degraded: "#f0ad4e", fallback: "#f0ad4e", warning: "#f0ad4e",  // YELLOW: working but not ideal
            idle: "#888", unknown: "#666",              // GRAY: no data or inactive
            down: "#d9534f", error: "#d9534f",         // RED: broken
            up: "#4caf50",                              // GREEN: MCP server up
            unavailable: "#666",                        // GRAY: not applicable
            local: "#2196F3", configured: "#4caf50",   // API key states
            missing: "#d9534f",                         // API key missing
          };
          if (typeof probe === "string") return map[probe] || "#666";
          if (probe?.status) return map[probe.status] || "#666";
          // Disk: derive status from free_pct
          if (probe?.free_pct != null) {
            if (probe.free_pct >= 20) return "#4caf50";
            if (probe.free_pct > 10) return "#f0ad4e";
            return "#d9534f";
          }
          // CC tmp: derive from cc_tier/sys_tier
          if (probe?.cc_tier != null || probe?.sys_tier != null) {
            const worst = [probe.cc_tier, probe.sys_tier].includes("red") ? "red"
              : [probe.cc_tier, probe.sys_tier].includes("orange") ? "orange" : "green";
            if (worst === "red") return "#d9534f";
            if (worst === "orange") return "#f0ad4e";
            return "#4caf50";
          }
          return "#666";
        },

        // Display label for infrastructure probe keys: the internal dict key stays
        // canonical (e.g. "ambient"); only the surfaced name is user-facing. The
        // ambient-capture edge bridge is shown as "Voice Bridge" on the card.
        // Infrastructure probes ordered failed-first so operators see problems at
        // the top of the card. Severity is derived from probeStatusColor so the
        // sort order always matches the status dots the user sees (RED > YELLOW >
        // GRAY > GREEN). Rebuilds an object rather than an array: infra keys are
        // non-integer strings, so insertion order is preserved and the existing
        // `(probe, name) in ...` template iterates in sorted order unchanged.
        get sortedInfrastructure() {
          const infra = this.health.infrastructure;
          if (!infra || typeof infra !== "object") return infra;
          const rankOf = (probe) => (
            { "#d9534f": 3, "#f0ad4e": 2, "#888": 1, "#666": 1 }[this.probeStatusColor(probe)] ?? 0
          );
          // Array.prototype.sort is stable in modern engines → within-rank order
          // stays as the backend emitted it.
          const sorted = Object.entries(infra).sort((a, b) => rankOf(b[1]) - rankOf(a[1]));
          const out = {};
          for (const [name, probe] of sorted) out[name] = probe;
          return out;
        },

        infraLabel(name) {
          // cc_slots renders in its own dedicated "Claude Code Sessions" section
          // (it's an array, not a probe) and is excluded from the probe grid; the
          // label here is defensive in case infraLabel is ever called for it.
          const labels = { ambient: "Voice Bridge", cc_slots: "Claude Code Sessions" };
          return labels[name] || name;
        },

        keyHealthColor(c) {
          // API-key health convention: green = configured + working,
          // yellow = key missing/unconfigured, red = present but NOT working
          // (circuit breaker open — incl. out of credits), gray = disabled.
          return ({ green: "#4caf50", yellow: "#f0ad4e", red: "#d9534f", gray: "#888" })[c] || "#666";
        },

        get groupedConfigFiles() {
          const groups = {};
          for (const f of this.configFiles) {
            if (!groups[f.category]) groups[f.category] = [];
            groups[f.category].push(f);
          }
          return groups;
        },

        sessionStatusColor(status) {
          const map = { active: "#2196F3", completed: "#4caf50", failed: "#d9534f", expired: "#999" };
          return map[status] || "#666";
        },

        sessionTypeLabel(type) {
          const map = { foreground: "FG", background_reflection: "Refl", background_task: "Task" };
          return map[type] || type;
        },

        formatDuration(ms) {
          if (!ms) return "-";
          const s = Math.round(ms / 1000);
          if (s < 60) return `${s}s`;
          return `${Math.floor(s / 60)}m ${s % 60}s`;
        },

        formatAgeSeconds(seconds) {
          // DURATION formatter (a quantity composed into prose: "45s old",
          // "oldest 3m") — NOT a timestamp age. The fmtAge contract's
          // "just now" bucket is for standalone ages and reads broken when
          // composed ("just now old"), so this keeps duration semantics.
          if (seconds == null) return "-";
          if (seconds < 60) return `${Math.round(seconds)}s`;
          if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
          if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
          return `${Math.round(seconds / 86400)}d`;
        },

        formatTime(isoStr) {
          if (!isoStr) return "-";
          try { return new Date(isoStr).toLocaleTimeString("en-US", { hour12: false }); } catch { return isoStr; }
        },

        formatDateTime(isoStr) {
          if (!isoStr) return "-";
          try {
            const d = new Date(isoStr);
            return d.toLocaleDateString("en-US", { month: "short", day: "numeric" })
              + " " + d.toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit" });
          } catch { return isoStr; }
        },

        relativeTime(value) {
          // Delegates to the shared fmtAge contract (spec §3.4):
          // just now (<90s) / Nm / Nh / Nd — no "ago" suffix, one format everywhere.
          return fmtAge(value);
        },

        // 5-state chip helpers over the shared status semantics (spec §3.3).
        // Layered onto the existing .status-chip pill: chip--* supplies the
        // canonical color + background tint; the glyph is color-blind support.
        semanticChipClass(state) {
          return `chip--${chipState(state)}`;
        },

        semanticChipGlyph(state) {
          return chipGlyph(chipState(state));
        },

        approvalActionState(requestId) {
          return this.approvalActions[requestId] || { saving: false, error: null };
        },

        approvalActionLabel(req) {
          return req?.context_data?.action_label || req?.description || req?.action_type || "Approval request";
        },

        approvalFallbackLabel(req) {
          const context = req?.context_data || {};
          const model = context.model || "claude -p";
          const effort = context.effort ? ` (${context.effort})` : "";
          return `${model}${effort}`;
        },

        approvalReasonLabel(req) {
          const context = req?.context_data || {};
          if (context.api_error) return context.api_error;
          if (context.api_call_site_id) return `API route ${context.api_call_site_id} exhausted`;
          return "CLI fallback requires manual approval";
        },

        groupQueueItems(items) {
          const groups = new Map();
          for (const item of (items || [])) {
            const key = [item.site || "unknown", item.reason || "unknown", item.type || "unknown"].join("::");
            if (!groups.has(key)) {
              groups.set(key, {
                key,
                site: item.site || "unknown",
                reason: item.reason || "unknown",
                type: item.type || "unknown",
                count: 0,
                maxAgeSeconds: 0,
                items: [],
              });
            }
            const group = groups.get(key);
            group.count += 1;
            group.maxAgeSeconds = Math.max(group.maxAgeSeconds, item.age_s || 0);
            group.items.push(item);
          }
          return Array.from(groups.values()).sort((a, b) => {
            if (b.count !== a.count) return b.count - a.count;
            if (b.maxAgeSeconds !== a.maxAgeSeconds) return b.maxAgeSeconds - a.maxAgeSeconds;
            return a.site.localeCompare(b.site);
          });
        },

        get deferredQueueGroups() {
          return this.groupQueueItems(this.health.queues?.deferred_items || []);
        },

        get discardedQueueGroups() {
          return this.groupQueueItems(this.health.queues?.discarded_items || []);
        },

        get routingProviderList() {
          return Object.keys(this.routingConfig?.providers || {});
        },

        providerCbState(providerName) {
          const sites = this.health.call_sites || {};
          for (const [, site] of Object.entries(sites)) {
            for (const p of (site.chain_health || [])) {
              if (p.provider === providerName) return p.state;
            }
          }
          return null;
        },

        providerCallSites(providerName) {
          const sites = this.health.call_sites || {};
          const result = [];
          for (const [siteId, site] of Object.entries(sites)) {
            if ((site.chain_health || []).some(p => p.provider === providerName)) {
              result.push(siteId);
            }
          }
          return result;
        },

        providerFallbackActive(providerName) {
          const sites = this.health.call_sites || {};
          const usingSites = [];
          for (const [siteId, site] of Object.entries(sites)) {
            if ((site.chain_health || []).some(p => p.provider === providerName)) {
              usingSites.push(site);
            }
          }
          if (usingSites.length === 0) return false;
          return usingSites.every(site =>
            (site.chain_health || []).some(p => p.provider !== providerName && p.state === 'closed')
          );
        },

        get subsystems() {
          return ["", "routing", "awareness", "surplus", "memory", "health",
                  "perception", "learning", "inbox", "reflection", "providers", "web", "outreach", "dashboard"];
        },

        get fallbackCallSites() {
          const sites = this.health.call_sites || {};
          return Object.entries(sites).filter(([, site]) => {
            const chain = site.chain_health || [];
            return site.active_provider && chain.length > 0 && site.active_provider !== chain[0].provider;
          }).map(([id]) => id);
        },
        get fallbackCallSiteCount() {
          return this.fallbackCallSites.length;
        },

        get pendingApprovalCount() {
          return this.approvals.length;
        },

        get badApiKeyCount() {
          const apiKeysData = this.health.api_keys || {};
          const keys = apiKeysData.providers || apiKeysData;
          const sites = this.health.call_sites || {};
          // "failed" keys always count. "missing" keys only count if they
          // are causing a specific call site to be degraded/down/on-fallback.
          const missingProviders = new Set(
            Object.entries(keys)
              .filter(([, info]) => info.status === "missing")
              .map(([name]) => name)
          );
          const missingCausingPain = new Set();
          if (missingProviders.size > 0) {
            for (const site of Object.values(sites)) {
              const chain = site.chain_health || [];
              const onFallback = site.active_provider && chain.length > 0 && site.active_provider !== chain[0].provider;
              const degraded = site.status === "degraded" || site.status === "down";
              if (onFallback || degraded) {
                for (const link of chain) {
                  if (missingProviders.has(link.provider)) {
                    missingCausingPain.add(link.provider);
                  }
                }
              }
            }
          }
          const failedCount = Object.values(keys).filter(info => info.status === "failed").length;
          return failedCount + missingCausingPain.size;
        },

        get attentionItems() {
          const items = [];
          const allAlerts = this.errorSummary.active_alerts || [];
          const criticalAlerts = allAlerts.filter(a => a.severity === "CRITICAL");
          const warningAlerts = allAlerts.filter(a => a.severity === "WARNING");
          const activeGroups = (this.errorSummary.groups || []).filter(g => g.still_active).length;
          if (criticalAlerts.length > 0) {
            items.push({ level: "critical", title: `${criticalAlerts.length} critical alert${criticalAlerts.length === 1 ? "" : "s"}`, detail: criticalAlerts.map(a => a.message).slice(0, 3).join("; "), href: "/genesis/errors" });
          }
          if (warningAlerts.length > 0) {
            items.push({ level: "warning", title: `${warningAlerts.length} system warning${warningAlerts.length === 1 ? "" : "s"}`, detail: `${warningAlerts.length} call site${warningAlerts.length === 1 ? "" : "s"} on fallback or degraded`, href: "/genesis/errors" });
          }
          if (activeGroups > 0) {
            items.push({ level: "warning", title: `${activeGroups} active error group${activeGroups === 1 ? "" : "s"}`, detail: "grouped warnings/errors across events, dead letters, or deferred work", href: "/genesis/errors" });
          }
          if ((this.health.queues?.dead_letters || 0) > 0) {
            items.push({ level: "critical", title: `${this.health.queues.dead_letters} dead letters`, detail: "requests exhausted all fallback providers; open the errors view to inspect", href: "/genesis/errors" });
          }
          if ((this.health.queues?.discarded_items?.length || 0) > 0) {
            items.push({ level: "warning", title: `${this.health.queues.discarded_items.length} discarded item${this.health.queues.discarded_items.length === 1 ? "" : "s"}`, detail: "work was dropped and can be reviewed or cleared below", href: "#queue-review", tab: "overview", anchor: "queue-review" });
          }
          // Fallback call sites are already captured in the warningAlerts
          // bucket above (severity=WARNING from health_alerts). The dedicated
          // card was removed to avoid duplication — "14 warnings" + "14 on
          // fallback" was confusing. Click the warnings card → /genesis/errors
          // for details.
          if (this.pendingApprovalCount > 0) {
            items.push({ level: "warning", title: `${this.pendingApprovalCount} pending approval${this.pendingApprovalCount === 1 ? "" : "s"}`, detail: "autonomous CLI fallbacks are waiting for operator action", href: "#chat", tab: "chat", onClick: () => { this.commsTab = 'approvals'; this.fetchApprovals(); } });
          }
          if (this.badApiKeyCount > 0) {
            items.push({ level: "critical", title: `${this.badApiKeyCount} provider key issue${this.badApiKeyCount === 1 ? "" : "s"}`, detail: "missing or failed provider credentials detected", href: null });
          }
          if (this.costSemantic().state === "degraded") {
            items.push({ level: "warning", title: "Budget pressure rising", detail: this.costSemantic().reason, href: "#budget-controls", tab: "config", anchor: "budget-controls" });
          }
          // Update availability
          if (this.updateStatus?.update_available) {
            const u = this.updateStatus.update_available;
            const label = u.target_tag || (u.commits_behind + " commit" + (u.commits_behind === 1 ? "" : "s") + " behind");
            items.push({ level: "info", title: `Update available (${label})`, detail: "go to Backup & Updates to apply", href: "#update-section", tab: "backup", anchor: "update-section" });
          }
          if (this.updateStatus?.last_update?.status === "rolled_back" || this.updateStatus?.last_update?.status === "failed") {
            items.push({ level: "warning", title: "Last update failed", detail: this.updateStatus.last_update.failure_reason || "check Backup & Updates tab", href: "#update-section", tab: "backup", anchor: "update-section" });
          }
          for (const [name, state] of Object.entries(this.fetchState)) {
            if (state.state === "stale" || state.state === "error") {
              items.push({
                level: state.state === "error" ? "critical" : "warning",
                title: `${name} panel ${state.state}`,
                detail: state.error || (state.lastSuccess ? `last ok ${this.relativeTime(state.lastSuccess)}` : "no successful fetch yet"),
                href: null,
              });
            }
          }
          return items.slice(0, 8);
        },

        attentionLevelColor(level) {
          const map = { critical: "#d9534f", warning: "#f0ad4e", info: "#2196F3" };
          return map[level] || "#666";
        },

        async fetchSurplusDetail() {
          this.startModalFetch("surplusModal");
          try {
            const resp = await fetchApi("/api/genesis/surplus/detail");
            if (resp?.ok) {
              this.surplusModal.data = await resp.json();
              this.finishModalFetch("surplusModal");
            } else {
              this.failModalFetch("surplusModal", "Surplus detail unavailable");
            }
          } catch {
            this.failModalFetch("surplusModal", "Surplus detail unavailable");
          }
        },

        async fetchEgoStatus() {
          try {
            const resp = await fetchApi("/api/genesis/ego/status");
            if (resp?.ok) { this.egoStatus = await resp.json(); }
          } catch { /* silent — card shows unknown */ }
        },

        async fetchEgoDetail() {
          this.startModalFetch("egoModal");
          try {
            const [statusR, cadenceR, cyclesR, proposalsR, followUpsR, vcrR] = await Promise.all([
              fetchApi("/api/genesis/ego/status"),
              fetchApi("/api/genesis/ego/cadence"),
              fetchApi("/api/genesis/ego/cycles"),
              fetchApi("/api/genesis/ego/proposals/all"),
              fetchApi("/api/genesis/ego/follow-ups"),
              fetchApi("/api/genesis/ego/vcr"),
            ]);
            if (statusR?.ok) this.egoStatus = await statusR.json();
            if (cadenceR?.ok) this.egoModal.cadence = await cadenceR.json();
            if (cyclesR?.ok) this.egoModal.cycles = await cyclesR.json();
            if (proposalsR?.ok) this.egoModal.proposals = await proposalsR.json();
            if (followUpsR?.ok) this.egoModal.followUps = await followUpsR.json();
            if (vcrR?.ok) this.egoModal.vcr = await vcrR.json();
            this.finishModalFetch("egoModal");
          } catch {
            this.failModalFetch("egoModal", "Ego detail unavailable");
          }
        },

        async resolveEgoProposal(id, status, response) {
          try {
            const resp = await fetchApi("/api/genesis/ego/proposals/" + id + "/resolve", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ status, response: response || "" }),
            });
            if (resp?.ok) {
              this.egoModal.rejectingId = null;
              this.egoModal.rejectReason = "";
              await this.fetchEgoDetail();
            }
          } catch (e) { console.warn("Proposal resolve failed:", e); }
        },

        async fetchOutreachMessages() {
          this.startModalFetch("outreachModal");
          try {
            const [msgResp, approvalResp] = await Promise.all([
              fetchApi("/api/genesis/outreach/messages?limit=20"),
              fetchApi("/api/genesis/approvals").catch(() => null),
            ]);
            if (msgResp?.ok) {
              this.outreachModal.messages = await msgResp.json();
              this.outreachModal.pendingApprovals = approvalResp?.ok ? await approvalResp.json() : [];
              this.finishModalFetch("outreachModal");
            } else {
              this.failModalFetch("outreachModal", "Outreach history unavailable");
            }
          } catch {
            this.failModalFetch("outreachModal", "Outreach history unavailable");
          }
        },

        // Find a pending approval that matches a blocker outreach message
        findMatchingApproval(msg) {
          if (msg.category !== 'blocker') return null;
          const approvals = this.outreachModal.pendingApprovals || [];
          // Match by signal_type → action_type mapping
          for (const a of approvals) {
            // CLI fallback approvals: signal_type contains 'cli_approval' or 'approval'
            if (msg.signal_type === 'cli_approval' && a.action_type === 'autonomous_cli_fallback') return a;
            // Generic: match topic substring
            if (a.description && msg.topic && a.description.includes(msg.topic.slice(0, 30))) return a;
          }
          return null;
        },

        async approveAllPending() {
          try {
            const resp = await fetchApi("/api/genesis/approvals/approve-all", {
              method: "POST", headers: {"Content-Type": "application/json"},
            });
            if (resp?.ok) {
              // Optimistic clear for instant visual feedback
              this.approvals = [];
              this.commsPendingApprovals = [];
              this.outreachModal.pendingApprovals = [];
              // Confirm from server
              await Promise.all([
                this.fetchOutreachMessages(),
                this.fetchApprovals(),
                this.fetchComms(),
              ]);
            }
          } catch (e) { console.error("approve-all failed", e); }
        },

        async engageOutreach(id, outcome, response) {
          try {
            const body = {outcome};
            if (response) body.response = response;
            await fetchApi("/api/genesis/outreach/" + id + "/engage", {
              method: "POST", headers: {"Content-Type": "application/json"},
              body: JSON.stringify(body)
            });
            await this.fetchOutreachMessages();
          } catch (e) { console.error("engage failed", e); }
        },

        async fetchMcpTools() {
          this.startModalFetch("mcpModal");
          try {
            const resp = await fetchApi("/api/genesis/mcp/tools");
            if (resp?.ok) {
              this.mcpModal.data = await resp.json();
              this.finishModalFetch("mcpModal");
            } else {
              this.failModalFetch("mcpModal", "MCP tool inventory unavailable");
            }
          } catch {
            this.failModalFetch("mcpModal", "MCP tool inventory unavailable");
          }
        },
      });
    });
