
_INDEX_HTML = """<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Retrace UI</title>
  <link rel=\"stylesheet\" href=\"https://cdn.jsdelivr.net/npm/rrweb-player@latest/dist/style.css\" />
  <style>
    :root { --bg:#0f172a; --panel:#111827; --panel2:#0b1220; --line:#1f2937; --text:#e5e7eb; --muted:#9ca3af; --acc:#22d3ee; }
    body { margin:0; font-family: ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto; background:var(--bg); color:var(--text); }
    .app-shell { display:grid; grid-template-columns: 212px minmax(320px, 380px) minmax(0, 1fr); height:100vh; }
    .nav { border-right:1px solid var(--line); background:#08111f; padding:14px 12px; overflow:auto; }
    .brand { font-size:15px; font-weight:700; margin-bottom:14px; }
    .nav-btn { display:block; width:100%; text-align:left; margin:4px 0; background:transparent; color:var(--text); border:1px solid transparent; border-radius:8px; padding:9px 10px; cursor:pointer; font-size:13px; }
    .nav-btn:hover { background:#111a2b; }
    .nav-btn.active { background:#162033; border-color:#244158; color:#cffafe; }
    .rail { border-right:1px solid var(--line); overflow:auto; background:var(--panel2); }
    .main { overflow:auto; padding:16px; }
    .hdr { padding:12px 14px; border-bottom:1px solid var(--line); position:sticky; top:0; background:var(--panel2); z-index:2; }
    .view { display:none; }
    .view.active { display:block; }
    .finding { padding:10px 12px; border-bottom:1px solid #182235; cursor:pointer; }
    .finding:hover { background:#111a2b; }
    .finding.active { background:#162033; border-left:3px solid var(--acc); }
    .issue-row { padding:10px 12px; border-bottom:1px solid #182235; cursor:pointer; }
    .issue-row:hover { background:#111a2b; }
    .issue-row:focus { outline:2px solid var(--acc); outline-offset:-2px; }
    .issue-row.active { background:#162033; border-left:3px solid var(--acc); }
    .sev { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing: .08em; }
    .title { font-size:14px; line-height:1.35; margin-top:4px; }
    .view-head { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; margin-bottom:12px; }
    .view-head h2 { margin:0; font-size:19px; letter-spacing:0; }
    .actions { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
    .metric-grid { display:grid; grid-template-columns: repeat(4, minmax(130px, 1fr)); gap:12px; margin-bottom:14px; }
    .metric { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; }
    .metric strong { display:block; font-size:24px; margin-bottom:4px; }
    .detail-grid { display:grid; grid-template-columns: minmax(0, 1.35fr) minmax(280px, .65fr); gap:14px; align-items:start; }
    .grid { display:grid; grid-template-columns: 1fr 1fr; gap:14px; }
    .card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; }
    .card h3 { margin:0 0 8px 0; font-size:13px; color:#93c5fd; text-transform:uppercase; letter-spacing:.08em; }
    .lbl { font-size:12px; color:var(--muted); margin-top:8px; }
    input { width:100%; background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:8px; }
    textarea { width:100%; min-height:120px; resize:vertical; background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:8px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }
    ul { margin:0; padding-left:18px; }
    li { margin: 6px 0; font-size:13px; }
    pre { white-space:pre-wrap; font-size:12px; background:#0b1220; border:1px solid #1f2937; padding:10px; border-radius:8px; max-height:360px; overflow:auto; }
    .meta a { color:#67e8f9; text-decoration:none; }
    .meta a:hover { text-decoration:underline; }
    .rr { background:#0b1220; border:1px solid #1f2937; border-radius:10px; padding:8px; }
    .empty { color:var(--muted); font-size:13px; }
    .btn { background:#0b1220; color:#e5e7eb; border:1px solid #374151; border-radius:8px; padding:6px 8px; cursor:pointer; font-size:12px; }
    .timeline { border:1px solid #1f2937; border-radius:8px; overflow:hidden; }
    .timeline-row { display:grid; grid-template-columns:88px 130px 1fr; gap:10px; padding:9px 10px; border-top:1px solid #1f2937; font-size:13px; }
    .timeline-row:first-child { border-top:0; }
    .timeline-row.detector { background:#172033; border-left:3px solid #f59e0b; }
    .timeline-kind { color:#93c5fd; text-transform:uppercase; font-size:11px; letter-spacing:.08em; }
    .timeline-summary { color:var(--muted); margin-top:2px; overflow-wrap:anywhere; }
    .workflow-strip { display:grid; grid-template-columns: repeat(5, minmax(110px, 1fr)); gap:8px; margin:10px 0 12px 0; }
    .workflow-step { border:1px solid #26364f; border-radius:8px; padding:9px; background:#0b1220; min-height:58px; }
    .workflow-step.complete { border-color:#14532d; background:#0d1f19; }
    .workflow-step.current { border-color:#0e7490; background:#102235; }
    .workflow-step.blocked { border-color:#4b5563; color:#9ca3af; }
    .workflow-step strong { display:block; font-size:12px; margin-bottom:3px; }
    .workflow-step span { display:block; font-size:12px; color:var(--muted); }
    .workflow-action { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:12px; }
    .readiness-panel { border:1px solid #26364f; border-radius:8px; padding:10px; background:#0b1220; margin:10px 0 12px 0; }
    .readiness-panel .row { display:flex; justify-content:space-between; gap:10px; align-items:center; }
    .recommendation-list { margin-top:8px; }
    .recommendation-list button { margin-right:6px; margin-top:4px; }
    .suite-row { border-top:1px solid #1f2937; padding:10px 0; }
    .suite-row:first-child { border-top:0; padding-top:0; }
    .draft-editor { margin-top:12px; }
    .draft-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
    .inventory-grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:12px; }
    .inventory-row { border-top:1px solid #1f2937; padding:9px 0; overflow-wrap:anywhere; }
    .inventory-row:first-child { border-top:0; padding-top:0; }
    .ok { color:#86efac; } .bad { color:#fca5a5; }
    @media (max-width: 980px) {
      .app-shell { grid-template-columns: 1fr; height:auto; min-height:100vh; }
      .nav { position:sticky; top:0; z-index:3; border-right:0; border-bottom:1px solid var(--line); }
      .nav-btn { display:inline-block; width:auto; margin-right:4px; }
      .rail { border-right:0; border-bottom:1px solid var(--line); max-height:42vh; }
      .main { padding:12px; }
      .metric-grid, .detail-grid, .grid, .workflow-strip, .draft-grid, .inventory-grid { grid-template-columns: 1fr; }
      .timeline-row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class=\"app-shell\">
    <nav class=\"nav\">
      <div class=\"brand\">Retrace QA</div>
      <button class=\"nav-btn active\" type=\"button\" data-view=\"dashboard\">Dashboard</button>
      <button class=\"nav-btn\" type=\"button\" data-view=\"issues\">Issues</button>
      <button class=\"nav-btn\" type=\"button\" data-view=\"qa\">QA Incidents</button>
      <button class=\"nav-btn\" type=\"button\" data-view=\"replays\">Replays</button>
      <button class=\"nav-btn\" type=\"button\" data-view=\"findings\">Findings</button>
      <button class=\"nav-btn\" type=\"button\" data-view=\"tests\">Tests</button>
      <button class=\"nav-btn\" type=\"button\" data-view=\"runs\">Runs</button>
      <button class=\"nav-btn\" type=\"button\" data-view=\"settings\">Settings</button>
    </nav>
    <aside class=\"rail\">
      <div class=\"hdr\"><strong id=\"railTitle\">Issues</strong><div class=\"empty\" id=\"reportMeta\"></div></div>
      <div id=\"issueWorkflowList\"></div>
      <div id=\"findings\" style=\"display:none\"></div>
    </aside>
    <main class=\"main\" id=\"detail\">
      <section class=\"view active\" id=\"view-dashboard\"><div id=\"dashboardView\"></div></section>
      <section class=\"view\" id=\"view-issues\">
        <div class=\"view-head\">
          <div><h2>Issue Detail</h2><div class=\"empty\">Replay-backed failures are the primary workflow surface.</div></div>
          <div class=\"actions\">
            <button class=\"btn\" id=\"importPostHogReplaysBtn\" type=\"button\">Import PostHog Replays</button>
            <button class=\"btn\" id=\"processReplayJobsBtn\" type=\"button\">Process Queued Replays</button>
            <button class=\"btn\" id=\"verifyResolvedBtn\" type=\"button\">Verify Resolved Issues</button>
          </div>
        </div>
        <label class=\"empty\"><input id=\"replayAiAnalysis\" type=\"checkbox\" /> AI replay analysis</label>
        <div class=\"empty\" id=\"replayProcessStatus\"></div>
        <div class=\"empty\" id=\"verifyResolvedStatus\"></div>
        <div id=\"replayIssueDetail\"><div class=\"empty\">Select a replay-backed issue.</div></div>
      </section>
      <section class=\"view\" id=\"view-replays\">
        <div class=\"view-head\"><div><h2>Replays</h2><div class=\"empty\">Recent captured sessions and playback.</div></div></div>
        <div id=\"replaySessionsPanel\"></div>
        <div style=\"height:10px\"></div>
        <div class=\"rr\"><div id=\"firstPartyReplay\"><div class=\"empty\">Select a first-party replay session.</div></div></div>
      </section>
      <section class=\"view\" id=\"view-qa\">
        <div class=\"view-head\">
          <div><h2>QA Incidents</h2><div class=\"empty\">Unified queue across replay, UI test, API test, error monitor, and PR review.</div></div>
          <div class=\"actions\">
            <button class=\"btn\" id=\"qaRefreshBtn\" type=\"button\">Refresh</button>
            <select class=\"btn\" id=\"qaSourceFilter\" title=\"Filter by source kind\">
              <option value=\"\">All sources</option>
              <option value=\"replay\">replay</option>
              <option value=\"ui_test\">ui_test</option>
              <option value=\"api_test\">api_test</option>
              <option value=\"error_monitor\">error_monitor</option>
              <option value=\"manual\">manual</option>
            </select>
          </div>
        </div>
        <div id=\"qaList\"><div class=\"empty\">Loading…</div></div>
        <div id=\"qaDetail\" style=\"margin-top:18px\"></div>
      </section>
      <section class=\"view\" id=\"view-tests\"><div id=\"tester\"></div></section>
      <section class=\"view\" id=\"view-runs\"><div id=\"runsView\"></div></section>
      <section class=\"view\" id=\"view-settings\"><div class=\"card\" id=\"onboarding\"></div></section>
      <section class=\"view\" id=\"view-findings\"><div id=\"findingDetail\"><div class=\"empty\">Select a finding.</div></div></section>
      <div id=\"replayDashboard\" style=\"display:none\"></div>
    </main>
  </div>
  <script src=\"https://cdn.jsdelivr.net/npm/rrweb-player@latest/dist/index.js\"></script>
  <script>
    let findings = [];
    let active = null;
    let replayState = { issues: [], sessions: [], activeIssueId: null };
    const LLM_DEFAULTS = {
      openai_compatible: { base_url: 'http://localhost:8080/v1', model: 'llama-3.1-8b-instruct' },
      openai: { base_url: 'https://api.openai.com/v1', model: 'gpt-4o-mini' },
      anthropic: { base_url: 'https://api.anthropic.com/v1', model: 'claude-3-5-sonnet-latest' },
      openrouter: { base_url: 'https://openrouter.ai/api/v1', model: 'openai/gpt-4o-mini' },
    };
    const CLOUD_PROVIDERS = new Set(['openai', 'anthropic', 'openrouter']);
    const CUSTOM_MODEL = '__custom__';

    function esc(s){ return String(s || \"\").replace(/[&<>\"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c])); }
    function byId(id){ return document.getElementById(id); }

    function copyText(s){ navigator.clipboard.writeText(String(s || \"\")); }
    function copyPrompt(key){ if(active?.prompts?.[key]) copyText(active.prompts[key]); }
    function safeExternalUrl(raw){
      try {
        const url = new URL(String(raw || ''), window.location.origin);
        return (url.protocol === 'http:' || url.protocol === 'https:') ? url.href : '';
      } catch(_err) {
        return '';
      }
    }
    function safeHashUrl(raw, allowedPrefix){
      const value = String(raw || '');
      return value.startsWith(allowedPrefix) ? value : '';
    }

    function switchView(view){
      document.querySelectorAll('.view').forEach(el => el.classList.toggle('active', el.id === `view-${view}`));
      document.querySelectorAll('.nav-btn').forEach(el => el.classList.toggle('active', el.dataset.view === view));
      const title = byId('railTitle');
      if(title) title.textContent = view === 'findings' ? 'Report Findings' : (view === 'qa' ? 'QA Incidents' : 'Issues');
      if(byId('issueWorkflowList')) byId('issueWorkflowList').style.display = view === 'findings' ? 'none' : '';
      if(byId('findings')) byId('findings').style.display = view === 'findings' ? '' : 'none';
      if(view === 'qa'){ loadQaIncidents(); }
    }
    document.querySelectorAll('.nav-btn').forEach(el => el.addEventListener('click', () => switchView(el.dataset.view)));

    function escapeHtml(s){
      return String(s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }
    // Whitelist URLs to `http(s):` so a tainted `fix_pr_url` can't smuggle
    // `javascript:` into an anchor href.
    function safeExternalUrl(raw){
      const s = String(raw || '').trim();
      if(!s) return '';
      try {
        const u = new URL(s);
        return (u.protocol === 'http:' || u.protocol === 'https:') ? u.href : '';
      } catch(e) { return ''; }
    }
    // Reduce class-name tokens to a safe alphabet so `tag-${src}` can't
    // break out of the attribute.
    function safeToken(raw){
      return String(raw || '').replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 64);
    }
    async function loadQaIncidents(){
      const list = byId('qaList');
      const detail = byId('qaDetail');
      if(!list){ return; }
      list.innerHTML = '<div class="empty">Loading…</div>';
      if(detail) detail.innerHTML = '';
      const filter = (byId('qaSourceFilter') || {}).value || '';
      try {
        const url = filter ? `/api/qa-incidents?source=${encodeURIComponent(filter)}` : '/api/qa-incidents';
        const res = await fetch(url);
        const data = await res.json();
        const incidents = data.incidents || [];
        if(!incidents.length){
          list.innerHTML = '<div class="empty">No QA incidents yet. Try <code>retrace demo all</code> to seed every pillar.</div>';
          return;
        }
        const rows = incidents.map(inc => {
          const publicId = escapeHtml(inc.public_id || '');
          const title = escapeHtml(inc.title || '');
          const sev = escapeHtml(inc.severity || '-');
          const status = escapeHtml(inc.status || '-');
          const srcRaw = inc.primary_source_kind || '-';
          const srcClass = safeToken(srcRaw);
          const srcText = escapeHtml(srcRaw);
          const fixUrl = safeExternalUrl(inc.fix_pr_url);
          const fix = fixUrl
            ? ` · <a href="${escapeHtml(fixUrl)}" target="_blank" rel="noopener noreferrer">PR</a>`
            : '';
          const affected = Number(inc.affected_users) || 0;
          return `<div class="card" style="margin-bottom:8px"><div class="hdr"><strong>${publicId}</strong> &nbsp; <span class="tag tag-${srcClass}">${srcText}</span> &nbsp; <span>${title}</span></div><div class="empty">${sev} · ${status} · ${affected} user(s)${fix} · <a href="javascript:void(0)" data-qa-show="${publicId}">details</a></div></div>`;
        }).join('');
        list.innerHTML = rows;
        list.querySelectorAll('[data-qa-show]').forEach(el => {
          el.addEventListener('click', () => showQaIncident(el.dataset.qaShow));
        });
      } catch (err) {
        list.innerHTML = `<div class="empty">Failed to load QA incidents: ${escapeHtml(String(err))}</div>`;
      }
    }
    async function showQaIncident(publicId){
      const detail = byId('qaDetail');
      if(!detail) return;
      detail.innerHTML = '<div class="empty">Loading…</div>';
      try {
        const res = await fetch(`/api/qa-incidents/${encodeURIComponent(publicId)}`);
        if(!res.ok){
          detail.innerHTML = `<div class="empty">Not found.</div>`;
          return;
        }
        const data = await res.json();
        const inc = data.incident || {};
        const repro = (() => { try { return JSON.parse(inc.reproduction_json || '[]'); } catch(e) { return []; } })();
        const evidence = (() => { try { return JSON.parse(inc.evidence_json || '{}'); } catch(e) { return {}; } })();
        const steps = repro.map(s => `<li><strong>${escapeHtml(s.action || '?')}</strong> — ${escapeHtml(s.description || '')}</li>`).join('');
        const fixUrl = safeExternalUrl(inc.fix_pr_url);
        const fixUrlEsc = escapeHtml(fixUrl);
        detail.innerHTML = `
          <div class="card">
            <h3>${escapeHtml(inc.public_id || '')}  ${escapeHtml(inc.title || '')}</h3>
            <div class="empty">severity ${escapeHtml(inc.severity || '-')} · confidence ${escapeHtml(inc.confidence || '-')} · status ${escapeHtml(inc.status || '-')} · source ${escapeHtml(inc.primary_source_kind || '-')}</div>
            ${inc.summary ? `<p>${escapeHtml(inc.summary)}</p>` : ''}
            ${inc.suspected_cause ? `<p><em>Suspected cause:</em> ${escapeHtml(inc.suspected_cause)}</p>` : ''}
            ${steps ? `<h4>Reproduction</h4><ol>${steps}</ol>` : ''}
            ${evidence.top_stack_frame ? `<p><em>Top stack frame:</em> <code>${escapeHtml(evidence.top_stack_frame)}</code></p>` : ''}
            ${fixUrl ? `<p><strong>Fix PR:</strong> <a href="${fixUrlEsc}" target="_blank" rel="noopener noreferrer">${fixUrlEsc}</a></p>` : ''}
          </div>
        `;
      } catch (err) {
        detail.innerHTML = `<div class="empty">Failed: ${escapeHtml(String(err))}</div>`;
      }
    }
    document.addEventListener('click', (e) => {
      if(e.target && e.target.id === 'qaRefreshBtn'){ loadQaIncidents(); }
    });
    document.addEventListener('change', (e) => {
      if(e.target && e.target.id === 'qaSourceFilter'){ loadQaIncidents(); }
    });
    window.addEventListener('hashchange', () => applyReplayHash(replayState.issues, replayState.sessions));

    function statusClass(value){
      const v = String(value || '').toLowerCase();
      if(v.includes('pass') || v === 'resolved' || v === 'verified' || v === 'covered_passing') return 'ok';
      if(v.includes('fail') || v.includes('regressed') || v === 'unresolved' || v === 'covered_failing') return 'bad';
      return '';
    }

    function openReplayIssue(issueId){
      const issue = replayState.issues.find(i => i.public_id === issueId);
      if(!issue){ return; }
      const nextHash = `#issue=${encodeURIComponent(issue.public_id)}`;
      if(window.location.hash !== nextHash){
        window.location.hash = nextHash;
        return;
      }
      renderReplayIssueDetail(issue);
      switchView('issues');
    }

    function bindReplayIssueRows(root = document){
      root.querySelectorAll('[data-replay-issue]').forEach(el => {
        el.addEventListener('click', () => openReplayIssue(el.dataset.replayIssue));
        el.addEventListener('keydown', ev => {
          if(ev.key !== 'Enter' && ev.key !== ' ' && ev.key !== 'Spacebar') return;
          ev.preventDefault();
          openReplayIssue(el.dataset.replayIssue);
        });
      });
    }

    async function refreshTesterAndReplay(issueId = '', processStatus = ''){
      await Promise.all([loadTesterPanel(), loadReplayDashboard(processStatus)]);
      const targetId = issueId || replayState.activeIssueId;
      const refreshed = replayState.issues.find(i => i.public_id === targetId);
      if(refreshed) renderReplayIssueDetail(refreshed);
    }

    function llmKeyLabel(provider){
      if(provider === 'openai') return 'OpenAI API Key';
      if(provider === 'anthropic') return 'Anthropic API Key';
      if(provider === 'openrouter') return 'OpenRouter API Key';
      return 'LLM API Key (optional for local)';
    }

    function syncProviderUI(applyDefaults=false){
      const provider = byId('llmProvider').value || 'openai_compatible';
      const keyLbl = byId('llmKeyLabel');
      const keyReq = byId('llmKeyRequired');
      if(keyLbl) keyLbl.textContent = llmKeyLabel(provider);
      if(keyReq) keyReq.textContent = CLOUD_PROVIDERS.has(provider) ? 'required' : 'optional';
      if(applyDefaults){
        const d = LLM_DEFAULTS[provider] || LLM_DEFAULTS.openai_compatible;
        if(byId('llmBaseUrl')) byId('llmBaseUrl').value = d.base_url;
        if(byId('llmModel')) byId('llmModel').value = d.model;
      }
    }

    async function fetchModels(ev){
      ev.preventDefault();
      const provider = byId('llmProvider').value || 'openai_compatible';
      const body = {
        provider,
        base_url: byId('llmBaseUrl').value,
        api_key: byId('llmApiKey').value,
      };
      const status = byId('llmModelStatus');
      status.textContent = 'Loading models...';
      const res = await fetch('/api/llm/models', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
      const data = await res.json();
      if(!res.ok || !data.ok){
        status.textContent = data.error || 'Model discovery failed';
        return;
      }
      const models = data.models || [];
      const picker = byId('llmModelPicker');
      if(!models.length){
        status.textContent = 'No models returned. You can still type one manually.';
        picker.style.display = 'none';
        return;
      }
      status.textContent = `Loaded ${models.length} model(s).`;
      picker.innerHTML = models.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join('') + `<option value="${CUSTOM_MODEL}">Custom...</option>`;
      picker.style.display = 'block';
      const cur = byId('llmModel').value;
      const hasCur = models.includes(cur);
      picker.value = hasCur ? cur : models[0];
      byId('llmModel').value = hasCur ? cur : models[0];
    }

    function onModelPick(){
      const picker = byId('llmModelPicker');
      if(!picker) return;
      if(picker.value === CUSTOM_MODEL){
        return;
      }
      byId('llmModel').value = picker.value;
    }

    async function saveSettings(ev){
      ev.preventDefault();
      const body = {
        posthog_host: byId('phHost').value,
        posthog_project_id: byId('phProject').value,
        posthog_api_key: byId('phKey').value,
        llm_provider: byId('llmProvider').value,
        llm_base_url: byId('llmBaseUrl').value,
        llm_model: byId('llmModel').value,
        llm_api_key: byId('llmApiKey').value,
        tester_app_url: byId('testerAppUrl').value,
        tester_start_command: byId('testerStartCommand').value,
        tester_harness_command: byId('testerHarnessCommand').value,
        tester_max_retries: byId('testerMaxRetries').value,
        tester_auth_required: byId('testerAuthRequired').value,
        tester_auth_mode: byId('testerAuthMode').value,
        tester_auth_login_url: byId('testerAuthLoginUrl').value,
        tester_auth_username: byId('testerAuthUsername').value,
        tester_auth_password: byId('testerAuthPassword').value,
        tester_auth_jwt: byId('testerAuthJwt').value,
        tester_auth_headers: byId('testerAuthHeaders').value,
      };
      const res = await fetch('/api/settings', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
      const data = await res.json();
      if(!res.ok){ alert(data.error || 'Save failed'); return; }
      await loadOnboarding();
      await loadTesterPanel();
      await bootFindings();
    }

    async function connectGithubRepo(ev){
      ev.preventDefault();
      const status = byId('repoConnectStatus');
      if(status) status.textContent = 'Saving...';
      const res = await fetch('/api/github/repos', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          repo: byId('repoFullName').value,
          branch: byId('repoDefaultBranch').value,
          local_path: byId('repoLocalPath').value,
        }),
      });
      const data = await res.json();
      if(!res.ok || !data.ok){
        if(status) status.textContent = data.error || 'Repo save failed';
        return;
      }
      if(status) status.textContent = 'Saved.';
      await loadOnboarding();
    }

    async function createSdkKey(ev){
      ev.preventDefault();
      const status = byId('sdkKeyStatus');
      const result = byId('sdkKeyResult');
      if(status) status.textContent = 'Creating...';
      if(result) result.innerHTML = '';
      const res = await fetch('/api/sdk-keys', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          project: byId('sdkProjectName').value,
          environment: byId('sdkEnvironmentName').value,
          name: byId('sdkKeyName').value,
        }),
      });
      const data = await res.json();
      if(!res.ok || !data.ok){
        if(status) status.textContent = data.error || 'SDK key creation failed';
        return;
      }
      if(status) status.textContent = `Created ${data.id} ending in ${data.last4}.`;
      renderSdkKeyResult(data);
    }

    function renderSdkKeyResult(data){
      const root = byId('sdkKeyResult');
      if(!root){ return; }
      const ingestUrl = data.ingest_url || 'http://127.0.0.1:8788/api/sdk/replay';
      const installSnippet = 'npm install @retrace/browser';
      const initSnippet = `import { init } from "@retrace/browser";

const retrace = init({
  apiKey: "${data.key}",
  ingestUrl: "${ingestUrl}",
  privacy: {
    maskAllInputs: true,
    blockSelector: "[data-retrace-block]",
    maskTextSelector: "[data-retrace-mask]",
  },
});`;
      root.innerHTML = `
        <div class="lbl">Browser SDK Key (shown once)</div>
        <pre>${esc(data.key)}</pre>
        <button class="btn" id="copySdkKeyBtn" type="button">Copy Key</button>
        <div class="lbl">Install</div>
        <pre>${esc(installSnippet)}</pre>
        <button class="btn" id="copySdkInstallBtn" type="button">Copy Install</button>
        <div class="lbl">Initialize Capture</div>
        <pre>${esc(initSnippet)}</pre>
        <button class="btn" id="copySdkInitBtn" type="button">Copy Init</button>
        <button class="btn" id="sendSdkSmokeReplayBtn" type="button">Send Test Replay</button>
        <span class="empty" id="sdkSmokeReplayStatus"></span>
        <div class="empty" style="margin-top:8px">Project: <code>${esc(data.project_id)}</code> · Environment: <code>${esc(data.environment_id)}</code></div>
      `;
      byId('copySdkKeyBtn')?.addEventListener('click', () => copyText(data.key));
      byId('copySdkInstallBtn')?.addEventListener('click', () => copyText(installSnippet));
      byId('copySdkInitBtn')?.addEventListener('click', () => copyText(initSnippet));
      byId('sendSdkSmokeReplayBtn')?.addEventListener('click', () => sendSdkSmokeReplay(data.key, ingestUrl));
    }

    async function sendSdkSmokeReplay(apiKey, ingestUrl){
      const status = byId('sdkSmokeReplayStatus');
      if(status) status.textContent = 'Sending...';
      const now = Date.now();
      const sessionId = `ui-smoke-${now}-${Math.random().toString(36).slice(2)}`;
      const payload = {
        sessionId,
        sequence: 0,
        flushType: 'final',
        distinctId: 'retrace-ui-smoke',
        metadata: {
          source: 'retrace-ui',
          smoke_test: true,
        },
        events: [
          {
            type: 4,
            timestamp: now,
            data: { href: window.location.href },
          },
          {
            type: 6,
            timestamp: now + 1,
            data: {
              plugin: 'retrace/console@1',
              payload: {
                level: 'error',
                payload: ['Retrace UI smoke replay'],
                url: window.location.href,
              },
            },
          },
        ],
      };
      try {
        const res = await fetch(ingestUrl, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Retrace-Key': apiKey,
          },
          body: JSON.stringify(payload),
        });
        let data = {};
        try {
          data = await res.json();
        } catch(_err) {
          data = {};
        }
        if(!res.ok || data.accepted !== true){
          if(status) status.textContent = `Failed: ${data.error || data.message || 'ingest rejected'}`;
          return;
        }
        if(status) status.textContent = `Accepted ${data.event_count || payload.events.length} event(s) for ${sessionId}.`;
        await loadReplayDashboard('Test replay accepted. Process queued replays to create a replay-backed issue.');
      } catch(err) {
        if(status) status.textContent = `Failed: ${err?.message || err}`;
      }
    }

    async function loadOnboarding(){
      const [sRes, cRes, rRes, readyRes] = await Promise.all([
        fetch('/api/settings'),
        fetch('/api/system-checks'),
        fetch('/api/github/repos'),
        fetch('/api/onboarding/readiness'),
      ]);
      const settings = await sRes.json();
      const checks = await cRes.json();
      const repoData = await rRes.json();
      const readiness = await readyRes.json();
      const repos = repoData.repos || [];
      const gh = checks.gh || {};
      const ph = checks.posthog || {};
      const llm = checks.llm || {};
      const replayApi = checks.replay_api || {};
      const llmProvider = settings.llm_provider || 'openai_compatible';
      const llmProviderLabel = llmProvider === 'openai' ? 'OpenAI'
        : llmProvider === 'anthropic' ? 'Anthropic'
        : llmProvider === 'openrouter' ? 'OpenRouter'
        : 'OpenAI-compatible';
      const repoRows = repos.map(r => `
        <li><code>${esc(r.repo_full_name)}</code> · provider=<code>${esc(r.provider || 'github')}</code> · branch=<code>${esc(r.default_branch || 'main')}</code>${r.local_path ? ` · path=<code>${esc(r.local_path)}</code>` : ''}</li>
      `).join('');
      const readinessRows = (readiness.steps || []).map(step => `
        <li>
          <span class="${step.status === 'complete' ? 'ok' : (step.status === 'blocked' ? 'bad' : '')}">${esc(step.status)}</span>
          · <strong>${esc(step.label)}</strong>
          <br><span class="empty">${esc(step.detail || '')}</span>
          <br><span class="empty">Next: ${esc(step.action || '')}</span>
        </li>
      `).join('');
      byId('onboarding').innerHTML = `
        <h3>Onboarding & Settings</h3>
        <div class="readiness-panel">
          <div class="row">
            <div><strong>Hosted Readiness</strong><div class="empty">Capture, process, test, monitor, and repair loop setup.</div></div>
            <code class="${readiness.ready ? 'ok' : ''}">${esc(readiness.complete || 0)}/${esc(readiness.total || 0)}</code>
          </div>
          ${readinessRows ? `<ul>${readinessRows}</ul>` : '<div class="empty">Readiness checks unavailable.</div>'}
        </div>
        <form id=\"settingsForm\">
          <div class=\"lbl\">PostHog Host</div>
          <input id=\"phHost\" value=\"${esc(settings.posthog_host)}\" />
          <div class=\"lbl\">PostHog Project ID</div>
          <input id=\"phProject\" value=\"${esc(settings.posthog_project_id)}\" />
          <div class=\"lbl\">PostHog Personal API Key</div>
          <input id=\"phKey\" value=\"\" placeholder=\"${settings.posthog_api_key_present ? 'Configured (leave blank to keep current)' : 'Enter PostHog key (phx_...)'}\" />
          <div class=\"lbl\">LLM Provider</div>
          <select id=\"llmProvider\" style=\"width:100%; background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:8px;\">
            <option value=\"openai_compatible\" ${llmProvider === 'openai_compatible' ? 'selected' : ''}>OpenAI-compatible (local/custom)</option>
            <option value=\"openai\" ${llmProvider === 'openai' ? 'selected' : ''}>OpenAI API</option>
            <option value=\"anthropic\" ${llmProvider === 'anthropic' ? 'selected' : ''}>Anthropic API</option>
            <option value=\"openrouter\" ${llmProvider === 'openrouter' ? 'selected' : ''}>OpenRouter API</option>
          </select>
          <div class=\"lbl\">LLM Base URL</div>
          <input id=\"llmBaseUrl\" value=\"${esc(settings.llm_base_url)}\" />
          <div class=\"lbl\">LLM Model</div>
          <input id=\"llmModel\" value=\"${esc(settings.llm_model)}\" />
          <div style=\"margin-top:6px\"><button class=\"btn\" type=\"button\" id=\"fetchModelsBtn\">Fetch Models</button> <span class=\"empty\" id=\"llmModelStatus\"></span></div>
          <select id=\"llmModelPicker\" style=\"display:none; margin-top:8px; width:100%; background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:8px;\"></select>
          <div class=\"lbl\" id=\"llmKeyLabel\">LLM API Key</div>
          <div class=\"empty\">Key: <span id=\"llmKeyRequired\">optional</span></div>
          <input id=\"llmApiKey\" value=\"\" placeholder=\"${settings.llm_api_key_present ? 'Configured (leave blank to keep current)' : 'Enter LLM API key'}\" />
          <div class=\"lbl\">Tester App URL</div>
          <input id=\"testerAppUrl\" value=\"${esc(settings.tester_app_url || 'http://127.0.0.1:3000')}\" />
          <div class=\"lbl\">Tester Start Command</div>
          <input id=\"testerStartCommand\" value=\"${esc(settings.tester_start_command || '')}\" placeholder=\"npm run dev\" />
          <div class=\"lbl\">Tester Harness Command Template</div>
          <input id=\"testerHarnessCommand\" value=\"${esc(settings.tester_harness_command || 'browser-harness run --url {app_url} --task {prompt_q} --output {run_dir_q}')}\" />
          <div class=\"lbl\">Tester Retry Count</div>
          <input id=\"testerMaxRetries\" type=\"number\" min=\"0\" value=\"${esc(settings.tester_max_retries || 1)}\" />
          <div class=\"lbl\">Tester Auth Required?</div>
          <select id=\"testerAuthRequired\" style=\"width:100%; background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:8px;\">
            <option value=\"false\" ${settings.tester_auth_required ? '' : 'selected'}>No</option>
            <option value=\"true\" ${settings.tester_auth_required ? 'selected' : ''}>Yes</option>
          </select>
          <div class=\"lbl\">Tester Auth Mode</div>
          <select id=\"testerAuthMode\" style=\"width:100%; background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:8px;\">
            <option value=\"none\" ${settings.tester_auth_mode === 'none' ? 'selected' : ''}>None</option>
            <option value=\"form\" ${settings.tester_auth_mode === 'form' ? 'selected' : ''}>Form login</option>
            <option value=\"jwt\" ${settings.tester_auth_mode === 'jwt' ? 'selected' : ''}>JWT bearer</option>
            <option value=\"headers\" ${settings.tester_auth_mode === 'headers' ? 'selected' : ''}>Custom headers</option>
          </select>
          <div class=\"lbl\">Tester Auth Login URL</div>
          <input id=\"testerAuthLoginUrl\" value=\"${esc(settings.tester_auth_login_url || '')}\" placeholder=\"http://127.0.0.1:3000/login\" />
          <div class=\"lbl\">Tester Auth Username</div>
          <input id=\"testerAuthUsername\" value=\"${esc(settings.tester_auth_username || '')}\" />
          <div class=\"lbl\">Tester Auth Password</div>
          <input id=\"testerAuthPassword\" value=\"\" placeholder=\"${settings.tester_auth_password_present ? 'Configured (leave blank to keep current)' : 'Optional test password'}\" />
          <div class=\"lbl\">Tester Auth JWT</div>
          <input id=\"testerAuthJwt\" value=\"\" placeholder=\"${settings.tester_auth_jwt_present ? 'Configured (leave blank to keep current)' : 'Optional bearer token'}\" />
          <div class=\"lbl\">Tester Auth Headers (JSON)</div>
          <input id=\"testerAuthHeaders\" value=\"\" placeholder=\"${settings.tester_auth_headers_present ? 'Configured (leave blank to keep current)' : '{\\\"x-test\\\":\\\"value\\\"}'}\" />
          <div style=\"margin-top:10px\"><button class=\"btn\" type=\"submit\">Save Settings</button></div>
        </form>
        <div style=\"margin-top:10px\" class=\"empty\">GitHub CLI: <span class=\"${gh.installed?'ok':'bad'}\">${gh.installed?'installed':'missing'}</span> · auth: <span class=\"${gh.authed?'ok':'bad'}\">${gh.authed?'ok':'not authed'}</span></div>
        <div class=\"lbl\">Connected Code Repository</div>
        ${repoRows ? `<ul>${repoRows}</ul>` : '<div class=\"empty\">No connected repos yet.</div>'}
        <form id=\"repoConnectForm\" style=\"margin-top:8px\">
          <div class=\"grid\">
            <div>
              <div class=\"lbl\">Repo Label</div>
              <input id=\"repoFullName\" value=\"${esc(repos[0]?.provider === 'local' ? '' : (repos[0]?.repo_full_name || ''))}\" placeholder=\"owner/name or leave blank for local path\" />
            </div>
            <div>
              <div class=\"lbl\">Branch</div>
              <input id=\"repoDefaultBranch\" value=\"${esc(repos[0]?.default_branch || 'main')}\" />
            </div>
          </div>
          <div class=\"lbl\">Local Checkout Path</div>
          <input id=\"repoLocalPath\" value=\"${esc(repos[0]?.local_path || '')}\" placeholder=\"/path/to/repo\" />
          <div style=\"margin-top:8px\"><button class=\"btn\" type=\"submit\">Connect Repo</button> <span class=\"empty\" id=\"repoConnectStatus\"></span></div>
        </form>
        <div class=\"lbl\" style=\"margin-top:12px\">Browser Replay Capture Key</div>
        <form id=\"sdkKeyForm\" style=\"margin-top:8px\">
          <div class=\"grid\">
            <div>
              <div class=\"lbl\">Project</div>
              <input id=\"sdkProjectName\" value=\"Default\" />
            </div>
            <div>
              <div class=\"lbl\">Environment</div>
              <input id=\"sdkEnvironmentName\" value=\"production\" />
            </div>
          </div>
          <div class=\"lbl\">Key Name</div>
          <input id=\"sdkKeyName\" value=\"Browser SDK\" />
          <div style=\"margin-top:8px\"><button class=\"btn\" type=\"submit\">Create SDK Key</button> <span class=\"empty\" id=\"sdkKeyStatus\"></span></div>
        </form>
        <div id=\"sdkKeyResult\"></div>
        <div class=\"empty\">PostHog check: <span class=\"${ph.reachable===true?'ok':(ph.reachable===false?'bad':'')}\">${ph.reachable===true?'reachable':(ph.reachable===false?'unreachable':'not configured')}</span> ${esc(ph.detail || '')}</div>
        <div class=\"empty\">LLM check (${esc(llmProviderLabel)}): <span class=\"${llm.reachable===true?'ok':(llm.reachable===false?'bad':'')}\">${llm.reachable===true?'reachable':(llm.reachable===false?'unreachable':'not configured')}</span> ${esc(llm.detail || '')}</div>
        <div class=\"empty\">Replay ingest API: <span class=\"${replayApi.reachable===true?'ok':'bad'}\">${replayApi.reachable===true?'reachable':'unreachable'}</span> at <code>${esc(replayApi.url || 'http://127.0.0.1:8788')}</code> ${esc(replayApi.detail || '')}</div>
        ${replayApi.reachable !== true ? `<div class=\"empty\">Run in terminal: <code>${esc(replayApi.commands?.serve || 'retrace api serve')}</code> <button class=\"btn\" id=\"copyReplayServeBtn\" data-copy-text=\"${esc(replayApi.commands?.serve || 'retrace api serve')}\">Copy</button></div>` : ''}
        ${!gh.installed ? `<div class=\"empty\">Run in terminal: <code>${esc(gh.commands?.install || 'brew install gh')}</code> <button class=\"btn\" id=\"copyGhInstallBtn\" data-copy-text=\"${esc(gh.commands?.install || 'brew install gh')}\">Copy</button></div>` : ''}
        ${gh.installed && !gh.authed ? `<div class=\"empty\">Run in terminal: <code>${esc(gh.commands?.login || 'gh auth login')}</code> <button class=\"btn\" id=\"copyGhLoginBtn\" data-copy-text=\"${esc(gh.commands?.login || 'gh auth login')}\">Copy</button></div>` : ''}
      `;
      byId('llmProvider').addEventListener('change', () => syncProviderUI(true));
      byId('fetchModelsBtn').addEventListener('click', fetchModels);
      byId('llmModelPicker').addEventListener('change', onModelPick);
      syncProviderUI(false);
      byId('settingsForm').addEventListener('submit', saveSettings);
      byId('repoConnectForm').addEventListener('submit', connectGithubRepo);
      byId('sdkKeyForm').addEventListener('submit', createSdkKey);
      byId('copyReplayServeBtn')?.addEventListener('click', ev => copyText(ev.currentTarget.dataset.copyText));
      byId('copyGhInstallBtn')?.addEventListener('click', ev => copyText(ev.currentTarget.dataset.copyText));
      byId('copyGhLoginBtn')?.addEventListener('click', ev => copyText(ev.currentTarget.dataset.copyText));
    }

    async function createTesterSpec(ev){
      ev.preventDefault();
      const body = {
        name: byId('testerName').value,
        mode: byId('testerMode').value,
        prompt: byId('testerPrompt').value,
        app_url: byId('testerSpecAppUrl').value,
      };
      const res = await fetch('/api/tester/specs', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if(!res.ok || !data.ok){ alert(data.error || 'Failed to create tester spec'); return; }
      byId('testerPrompt').value = '';
      await loadTesterPanel();
    }

    async function runTesterSpec(){
      const specId = byId('testerSpecSelect').value;
      if(!specId){ return; }
      byId('testerRunStatus').textContent = 'Running...';
      const res = await fetch('/api/tester/run', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          spec_id: specId,
          retries: Number(byId('testerMaxRetries')?.value || 1),
        }),
      });
      const data = await res.json();
      if(!res.ok || !data.ok){
        const msg = data?.result?.error || data.error || 'Run failed';
        byId('testerRunStatus').textContent = `Failed: ${msg}`;
        await refreshTesterAndReplay();
        return;
      }
      byId('testerRunStatus').textContent = `OK run ${data.result.run_id} (${data.result.status || 'passed'})`;
      await refreshTesterAndReplay();
    }

    async function generateReplayIssueSpec(issue){
      if(!issue){ return; }
      const status = byId('replaySpecStatus');
      if(status) status.textContent = 'Generating...';
      const res = await fetch('/api/replay-issue/spec', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          issue_id: issue.public_id || issue.id,
          project_id: issue.project_id,
          environment_id: issue.environment_id,
          app_url: byId('testerSpecAppUrl')?.value || '',
        }),
      });
      const data = await res.json();
      if(!res.ok || !data.ok){
        if(status) status.textContent = `Failed: ${data.error || 'could not generate spec'}`;
        return;
      }
      if(status) status.textContent = `Created ${data.spec.spec_id} (${data.confidence} confidence)`;
      await refreshTesterAndReplay(issue.public_id);
    }

    function selectedReplayIssueIds(){
      const checked = [...document.querySelectorAll('[data-issue-select]:checked')].map(el => el.value).filter(Boolean);
      if(checked.length) return checked;
      const list = byId('replayIssueList');
      if(!list) return [];
      return [...list.querySelectorAll('[data-replay-issue]')]
        .filter(row => row.style.display !== 'none')
        .map(row => row.dataset.replayIssue)
        .filter(Boolean);
    }

    async function generateGroupedReplayIssueSpecs(){
      const status = byId('groupReplaySpecStatus');
      const issueIds = selectedReplayIssueIds();
      if(status) status.textContent = issueIds.length ? `Generating ${issueIds.length} spec(s)...` : 'Generating specs...';
      const res = await fetch('/api/replay-issues/specs', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          issue_ids: issueIds,
          status: byId('issueStatusFilter')?.value || '',
          project_id: replayState.issues[0]?.project_id || '',
          environment_id: replayState.issues[0]?.environment_id || '',
          app_url: byId('testerSpecAppUrl')?.value || '',
          missing_only: true,
          limit: Math.min(issueIds.length || 25, 100),
        }),
      });
      const data = await res.json();
      const failures = (data.failed || []).map(item => `${item.issue_public_id}: ${item.error}`).join('; ');
      if(!res.ok){
        if(status) status.textContent = failures || data.error || 'Grouped spec generation failed';
        await refreshTesterAndReplay(replayState.activeIssueId);
        return;
      }
      if(status) {
        status.textContent = failures
          ? `Generated ${data.generated || 0}; skipped ${(data.skipped || []).length}; failed ${(data.failed || []).length}: ${failures}`
          : `Generated ${data.generated || 0}; skipped ${(data.skipped || []).length}.`;
      }
      await refreshTesterAndReplay(replayState.activeIssueId);
    }

    async function generateReplayIssueApiSpec(issue){
      if(!issue){ return; }
      const status = byId('replayApiSpecStatus');
      if(status) status.textContent = 'Generating...';
      const res = await fetch('/api/replay-issue/api-spec', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          issue_id: issue.public_id || issue.id,
          project_id: issue.project_id,
          environment_id: issue.environment_id,
          app_url: byId('testerSpecAppUrl')?.value || '',
        }),
      });
      const data = await res.json();
      if(!res.ok || !data.ok){
        if(status) status.textContent = `Failed: ${data.error || 'could not generate API spec'}`;
        return;
      }
      if(status) status.textContent = `Created ${data.spec.spec_id} (${data.spec.method} ${data.spec.url})`;
      await refreshTesterAndReplay(issue.public_id);
    }

    async function runReplayIssueApiSpec(specId, issueId){
      const status = byId('replayApiSpecStatus');
      if(status) status.textContent = `Running ${specId}...`;
      const res = await fetch('/api/replay-issue/api-run', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({spec_id: specId}),
      });
      const data = await res.json();
      if(!res.ok || !data.ok){
        const msg = data?.result?.error || data.error || 'API run failed';
        if(status) status.textContent = `Failed: ${msg}`;
        await refreshTesterAndReplay(issueId || replayState.activeIssueId);
        return;
      }
      if(status) status.textContent = `API passed: ${data.result.run_id}`;
      await refreshTesterAndReplay(issueId || replayState.activeIssueId);
    }

    async function generateReplayIssueFixPrompts(issue){
      if(!issue){ return; }
      const status = byId('replayFixPromptStatus');
      if(status) status.textContent = 'Generating...';
      const res = await fetch('/api/replay-issue/fix-prompts', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          issue_id: issue.public_id || issue.id,
          project_id: issue.project_id,
          environment_id: issue.environment_id,
        }),
      });
      const data = await res.json();
      if(!res.ok || !data.ok){
        if(status) status.textContent = `Failed: ${data.error || 'could not generate prompts'}`;
        return;
      }
      if(status) status.textContent = `Wrote ${data.generated || 0} prompt set(s) for ${data.repo || 'repo'}`;
      await refreshTesterAndReplay(issue.public_id);
      renderReplayFixSuggestions(data);
    }

    async function transitionReplayIssue(issue, statusValue){
      if(!issue){ return; }
      const status = byId('replayLifecycleStatus');
      if(status) status.textContent = 'Saving...';
      const res = await fetch('/api/replay-issue/status', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          issue_id: issue.public_id || issue.id,
          project_id: issue.project_id,
          environment_id: issue.environment_id,
          status: statusValue,
        }),
      });
      const data = await res.json();
      if(!res.ok || !data.ok){
        if(status) status.textContent = `Failed: ${data.error || 'could not update issue'}`;
        return;
      }
      const updated = data.issue || issue;
      Object.assign(issue, updated);
      if(status) status.textContent = `Status: ${updated.status}`;
      await loadReplayDashboard(`Updated ${updated.public_id || issue.public_id} to ${updated.status}.`);
      const refreshed = replayState.issues.find(i => i.public_id === (updated.public_id || issue.public_id));
      renderReplayIssueDetail(refreshed || issue);
    }

    function renderReplayFixSuggestions(data){
      const root = byId('replayFixPrompts');
      if(!root){ return; }
      const cands = (data.candidates || []).map(c =>
        `<li><code>${esc(c.file_path)}</code>${c.symbol ? ` · <code>${esc(c.symbol)}</code>` : ''} (score=${esc(c.score)})<br><span class="empty">${esc(c.rationale)}</span></li>`
      ).join('');
      const codex = data.prompts?.codex || '';
      const claude = data.prompts?.claude_code || '';
      root.innerHTML = `
        <div class="grid">
          <div class="card"><h3>Likely Culprits</h3>${cands ? `<ul>${cands}</ul>` : '<div class="empty">No code candidates found. Connect a repo with a local path for file matching.</div>'}</div>
          <div class="card"><h3>Artifacts</h3>
            <div class="empty">Repo: <code>${esc(data.repo || '')}</code></div>
            <div class="empty">Output: <code>${esc(data.out_dir || '')}</code></div>
            <div class="empty">JSON: <code>${esc(data.artifact_json || '')}</code></div>
          </div>
        </div>
        <div style="height:12px"></div>
        <div class="grid">
          <div class="card"><h3>Codex Prompt <button class="btn" id="copyReplayCodexPrompt" type="button">Copy</button></h3><pre>${esc(codex)}</pre></div>
          <div class="card"><h3>Claude Prompt <button class="btn" id="copyReplayClaudePrompt" type="button">Copy</button></h3><pre>${esc(claude)}</pre></div>
        </div>
      `;
      byId('copyReplayCodexPrompt')?.addEventListener('click', () => copyText(codex));
      byId('copyReplayClaudePrompt')?.addEventListener('click', () => copyText(claude));
    }

    async function loadTesterPanel(){
      const [specRes, runsRes, settingsRes, suitesRes, apiSpecRes] = await Promise.all([
        fetch('/api/tester/specs'),
        fetch('/api/tester/runs'),
        fetch('/api/settings'),
        fetch('/api/api-suites'),
        fetch('/api/api-specs'),
      ]);
      const specData = await specRes.json();
      const runData = await runsRes.json();
      const settings = await settingsRes.json();
      const suiteData = await suitesRes.json();
      const apiSpecData = await apiSpecRes.json();
      const specs = specData.specs || [];
      const runs = runData.runs || [];
      const apiSuites = suiteData.suites || [];
      const apiSpecs = apiSpecData.specs || [];
      const specOptions = specs.map(s =>
        `<option value="${esc(s.spec_id)}">${esc(s.name)} (${esc(s.mode)})</option>`
      ).join('');
      const uiSpecRows = specs.map(s => {
        const fixtures = s.fixtures || {};
        const status = fixtures.draft_status || 'accepted';
        const linkedIssue = fixtures.issue_public_id || '';
        return `
          <div class="inventory-row">
            <button class="btn" type="button" data-select-ui-spec="${esc(s.spec_id)}">Select</button>
            <code>${esc(s.spec_id)}</code> · ${esc(s.name || '')}
            <br><span class="empty">status=<code>${esc(status)}</code> · engine=<code>${esc(s.execution_engine || '')}</code> · steps=<code>${esc((s.exact_steps || []).length)}</code> · assertions=<code>${esc((s.assertions || []).length)}</code>${linkedIssue ? ` · issue=<code>${esc(linkedIssue)}</code>` : ''}</span>
          </div>
        `;
      }).join('');
      const apiSpecRows = apiSpecs.map(s => `
        <div class="inventory-row">
          <button class="btn" type="button" data-run-api-management-spec="${esc(s.spec_id)}">Run</button>
          <code>${esc(s.spec_id)}</code> · <code>${esc(s.method)}</code> ${esc(s.openapi_path || s.url || '')}
          <br><span class="empty">expected=<code>${esc(s.expected_status)}</code> · source=<code>${esc(s.source || 'manual')}</code> · requests=<code>${esc(s.request_count)}</code> · assertions=<code>${esc((s.json_assertion_count || 0) + (s.schema_assertion_count || 0))}</code>${s.issue_public_id ? ` · issue=<code>${esc(s.issue_public_id)}</code>` : ''}${s.operation_id ? ` · op=<code>${esc(s.operation_id)}</code>` : ''}</span>
        </div>
      `).join('');
      const draftSpecs = specs.filter(s => (s.fixtures || {}).draft_status === 'draft');
      const draftOptions = draftSpecs.map(s =>
        `<option value="${esc(s.spec_id)}">${esc(s.name)} · ${esc(s.spec_id)}</option>`
      ).join('');
      const suiteRows = apiSuites.map(s => {
        const summary = s.import_summary || {};
        const warnings = s.quality_warning_count || 0;
        const operations = (s.operations || []).slice(0, 5).map(op => `<li><code>${esc(op.method)}</code> ${esc(op.path || op.url || '')}${op.operation_id ? ` · ${esc(op.operation_id)}` : ''}</li>`).join('');
        return `
          <div class="suite-row">
            <div><button class="btn" type="button" data-run-api-suite="${esc(s.suite_id)}">Run Suite</button> <strong>${esc(s.name || s.suite_id)}</strong> <code>${esc(s.suite_id)}</code></div>
            <div class="empty">source=<code>${esc(s.source)}</code> · specs=<code>${esc(s.spec_count)}</code> · operations=<code>${esc(s.operation_count)}</code> · skipped=<code>${esc(s.skipped_count)}</code> · warnings=<code class="${warnings ? 'bad' : 'ok'}">${esc(warnings)}</code></div>
            <div class="empty">coverage=<code>${esc(summary.coverage_percent ?? 0)}%</code>${s.auth_profile ? ` · auth=<code>${esc(s.auth_profile)}</code>` : ''}${s.env_profile ? ` · env=<code>${esc(s.env_profile)}</code>` : ''}</div>
            ${operations ? `<ul>${operations}</ul>` : ''}
          </div>
        `;
      }).join('');
      const runRows = runs.map(r =>
        `<li><code>${esc(r.run_id || '')}</code> · ${r.ok ? '<span class="ok">ok</span>' : '<span class="bad">fail</span>'} · <code>${esc(r.status || '')}</code> · attempts=<code>${esc(r.attempts || 1)}</code>${r.failure_classification ? ` · class=<code>${esc(r.failure_classification)}</code>` : ''}${r.flake_reason ? ` · flake=<code>${esc(r.flake_reason)}</code>` : ''} · <code>${esc(r.spec_id || '')}</code><br><span class="empty">${esc(r.run_dir || '')}</span></li>`
      ).join('');
      byId('tester').innerHTML = `
        <div class="view-head"><div><h2>Tests</h2><div class="empty">Create local specs, run saved checks, and verify linked failures.</div></div></div>
        <div class="detail-grid">
          <div class="card">
            <h3>Local UI Tester</h3>
            <form id="testerCreateForm">
              <div class="lbl">Test Name</div>
              <input id="testerName" value="" placeholder="Checkout happy path" />
              <div class="lbl">Mode</div>
              <select id="testerMode" style="width:100%; background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:8px;">
                <option value="describe">Describe Test</option>
                <option value="explore_suite">AI Explore Full Suite</option>
              </select>
              <div class="lbl">Prompt / Task</div>
              <input id="testerPrompt" value="" placeholder="Describe a specific test. Leave blank for suite exploration mode." />
              <div class="lbl">App URL (override)</div>
              <input id="testerSpecAppUrl" value="${esc(settings.tester_app_url || 'http://127.0.0.1:3000')}" />
              <div style="margin-top:10px"><button class="btn" type="submit">Save Test Spec</button></div>
            </form>
            <div class="lbl" style="margin-top:12px">Run Saved Spec</div>
            <select id="testerSpecSelect" style="width:100%; background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:8px;">
              ${specOptions || '<option value="">No specs yet</option>'}
            </select>
            <div style="margin-top:8px"><button class="btn" id="runTesterBtn" type="button">Run Selected Test</button> <span class="empty" id="testerRunStatus"></span></div>
          </div>
          <div class="card">
            <h3>Linked Failures</h3>
            <div id="linkedFailureTests"><div class="empty">Loading linked failures...</div></div>
          </div>
        </div>
        <div class="card draft-editor">
          <h3>Generated Draft Review</h3>
          ${draftSpecs.length ? `
            <div class="draft-grid">
              <div>
                <div class="lbl">Draft Spec</div>
                <select id="draftSpecSelect" style="width:100%; background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:8px;">${draftOptions}</select>
                <div class="lbl">Name</div>
                <input id="draftName" value="" />
                <div class="lbl">Prompt</div>
                <textarea id="draftPrompt"></textarea>
                <div class="lbl">App URL</div>
                <input id="draftAppUrl" value="" />
                <div class="lbl">Review Note</div>
                <input id="draftReviewNote" value="" placeholder="What changed or what you verified" />
                <div style="margin-top:8px">
                  <button class="btn" id="saveDraftSpecBtn" type="button">Save Draft</button>
                  <button class="btn" id="acceptDraftSpecBtn" type="button">Accept Draft</button>
                  <button class="btn" id="runAcceptedDraftBtn" type="button">Run Accepted</button>
                  <span class="empty" id="draftEditStatus"></span>
                </div>
              </div>
              <div>
                <div class="lbl">Steps JSON</div>
                <textarea id="draftStepsJson"></textarea>
                <div class="lbl">Assertions JSON</div>
                <textarea id="draftAssertionsJson"></textarea>
                <div class="empty" id="draftReviewSummary"></div>
              </div>
            </div>
          ` : '<div class="empty">No generated drafts waiting for review.</div>'}
        </div>
        <div style="height:12px"></div>
        <div class="inventory-grid">
          <div class="card">
            <h3>UI Spec Inventory</h3>
            ${uiSpecRows || '<div class="empty">No UI specs yet.</div>'}
          </div>
          <div class="card">
            <h3>API Spec Inventory</h3>
            <div class="empty" id="apiManagementRunStatus"></div>
            ${apiSpecRows || '<div class="empty">No API specs yet.</div>'}
          </div>
        </div>
        <div style="height:12px"></div>
        <div class="card">
          <h3>API Suites</h3>
          <div class="empty" id="apiSuiteRunStatus"></div>
          <div id="apiSuiteRunMatrix"></div>
          ${suiteRows || '<div class="empty">No API suites yet. Import an OpenAPI document with <code>retrace tester api-import-openapi</code>.</div>'}
        </div>
      `;
      byId('runsView').innerHTML = `
        <div class="view-head"><div><h2>Runs</h2><div class="empty">Recent local tester results.</div></div></div>
        <div class="card">${runRows ? `<ul>${runRows}</ul>` : '<div class="empty">No runs yet.</div>'}</div>
      `;
      byId('testerCreateForm').addEventListener('submit', createTesterSpec);
      byId('runTesterBtn').addEventListener('click', runTesterSpec);
      document.querySelectorAll('[data-select-ui-spec]').forEach(el => {
        el.addEventListener('click', () => {
          const select = byId('testerSpecSelect');
          if(select) select.value = el.dataset.selectUiSpec;
        });
      });
      document.querySelectorAll('[data-run-api-management-spec]').forEach(el => {
        el.addEventListener('click', () => runManagedApiSpec(el.dataset.runApiManagementSpec));
      });
      document.querySelectorAll('[data-run-api-suite]').forEach(el => {
        el.addEventListener('click', () => runManagedApiSuite(el.dataset.runApiSuite));
      });
      if(draftSpecs.length){
        window.retraceDraftSpecs = draftSpecs;
        byId('draftSpecSelect')?.addEventListener('change', renderSelectedDraftEditor);
        byId('saveDraftSpecBtn')?.addEventListener('click', () => saveDraftSpec(false));
        byId('acceptDraftSpecBtn')?.addEventListener('click', () => saveDraftSpec(true));
        byId('runAcceptedDraftBtn')?.addEventListener('click', runAcceptedDraftSpec);
        renderSelectedDraftEditor();
      }
      renderLinkedFailureTests();
    }

    async function runManagedApiSpec(specId){
      const status = byId('apiManagementRunStatus');
      if(status) status.textContent = `Running ${specId}...`;
      const res = await fetch('/api/api-spec/run', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({spec_id: specId}),
      });
      const data = await res.json();
      if(!res.ok || !data.ok){
        const msg = data?.result?.error || data.error || 'API run failed';
        if(status) status.textContent = `Failed: ${msg}`;
        return;
      }
      if(status) status.textContent = `API passed: ${data.result.run_id}`;
      await loadTesterPanel();
    }

    async function runManagedApiSuite(suiteId){
      const status = byId('apiSuiteRunStatus');
      const matrix = byId('apiSuiteRunMatrix');
      if(status) status.textContent = `Running suite ${suiteId}...`;
      if(matrix) matrix.innerHTML = '';
      const res = await fetch('/api/api-suite/run', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({suite_id: suiteId}),
      });
      const data = await res.json();
      const rows = (data.results || []).map(item => `
        <li>
          <code>${esc(item.spec_id)}</code> · <span class="${item.ok ? 'ok' : 'bad'}">${esc(item.status || (item.ok ? 'passed' : 'failed'))}</span>
          ${item.status_code ? ` · status=<code>${esc(item.status_code)}</code>` : ''}
          ${item.run_id ? ` · run=<code>${esc(item.run_id)}</code>` : ''}
          ${item.error ? `<br><span class="bad">${esc(item.error)}</span>` : ''}
        </li>
      `).join('');
      if(status) status.textContent = `${data.name || suiteId}: ${data.passed || 0}/${data.total || 0} passed`;
      if(matrix) matrix.innerHTML = rows ? `<ul>${rows}</ul>` : '<div class="empty">No suite results.</div>';
      if(!res.ok || !data.ok){
        return;
      }
    }

    function selectedDraftSpec(){
      const id = byId('draftSpecSelect')?.value || '';
      return (window.retraceDraftSpecs || []).find(s => s.spec_id === id) || null;
    }

    function renderSelectedDraftEditor(){
      const spec = selectedDraftSpec();
      if(!spec){ return; }
      byId('draftName').value = spec.name || '';
      byId('draftPrompt').value = spec.prompt || '';
      byId('draftAppUrl').value = spec.app_url || '';
      byId('draftStepsJson').value = JSON.stringify(spec.exact_steps || [], null, 2);
      byId('draftAssertionsJson').value = JSON.stringify(spec.assertions || [], null, 2);
      const fixtures = spec.fixtures || {};
      const generation = fixtures.generation || {};
      const review = generation.review || {};
      const notes = fixtures.review_notes || [];
      byId('draftReviewSummary').innerHTML = `
        draft=<code>${esc(fixtures.draft_status || '')}</code> · steps=<code>${esc((spec.exact_steps || []).length)}</code> · assertions=<code>${esc((spec.assertions || []).length)}</code>
        ${review.summary ? `<br>${esc(review.summary)}` : ''}
        ${notes.length ? `<br>Notes: ${notes.map(item => `<code>${esc(item)}</code>`).join(' ')}` : ''}
      `;
      byId('draftEditStatus').textContent = '';
    }

    function parseDraftJson(id, label){
      try {
        const value = JSON.parse(byId(id).value || '[]');
        if(!Array.isArray(value) || value.some(item => !item || typeof item !== 'object' || Array.isArray(item))){
          throw new Error(`${label} must be a JSON list of objects`);
        }
        return value;
      } catch(err) {
        throw new Error(`${label}: ${err.message || err}`);
      }
    }

    async function saveDraftSpec(accept=false){
      const spec = selectedDraftSpec();
      const status = byId('draftEditStatus');
      if(!spec || !status){ return; }
      let steps, assertions;
      try {
        steps = parseDraftJson('draftStepsJson', 'Steps');
        assertions = parseDraftJson('draftAssertionsJson', 'Assertions');
      } catch(err) {
        status.textContent = err.message || String(err);
        return;
      }
      status.textContent = accept ? 'Accepting...' : 'Saving...';
      const res = await fetch('/api/tester/draft', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          spec_id: spec.spec_id,
          name: byId('draftName').value,
          prompt: byId('draftPrompt').value,
          app_url: byId('draftAppUrl').value,
          steps,
          assertions,
          review_note: byId('draftReviewNote').value,
          accept,
        }),
      });
      const data = await res.json();
      if(!res.ok || !data.ok){
        status.textContent = data.error || 'Draft update failed';
        return;
      }
      status.textContent = accept
        ? `Accepted ${data.spec.spec_id}`
        : `Saved ${data.changed_fields.join(', ') || 'metadata'}`;
      await loadTesterPanel();
      if(accept){
        const select = byId('testerSpecSelect');
        if(select) select.value = data.spec.spec_id;
      }
    }

    async function runAcceptedDraftSpec(){
      const spec = selectedDraftSpec();
      if(!spec){ return; }
      await saveDraftSpec(true);
      const select = byId('testerSpecSelect');
      if(select) select.value = spec.spec_id;
      await runTesterSpec();
    }

    async function processReplayJobs(){
      const status = byId('replayProcessStatus');
      status.textContent = 'Processing...';
      const ai = !!byId('replayAiAnalysis')?.checked;
      const res = await fetch('/api/replays/process', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({limit: 25, ai})});
      const data = await res.json();
      if(!res.ok || !data.ok){
        status.textContent = data.error || 'Processing failed';
        return;
      }
      await loadReplayDashboard(`Processed ${data.result.jobs_processed} job(s), updated ${data.result.issues_created_or_updated} issue(s)${ai ? ' with AI analysis' : ''}.`);
    }

    async function importPostHogReplays(){
      const status = byId('replayProcessStatus');
      status.textContent = 'Importing PostHog replays...';
      const ai = !!byId('replayAiAnalysis')?.checked;
      const res = await fetch('/api/replays/import-posthog', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({since_hours: 24, max_sessions: 50, process: true, ai}),
      });
      const data = await res.json();
      if(!res.ok || !data.ok){
        status.textContent = data.error || 'PostHog import failed';
        return;
      }
      const processed = data.processed || {};
      await loadReplayDashboard(`Imported ${data.imported_sessions.length} PostHog replay(s); processed ${processed.jobs_processed || 0} job(s), updated ${processed.issues_created_or_updated || 0} issue(s).`);
    }

    async function verifyResolvedReplayIssues(){
      const status = byId('verifyResolvedStatus');
      if(status) status.textContent = 'Verifying...';
      const res = await fetch('/api/replay-issues/verify-resolved', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({limit: 10}),
      });
      const data = await res.json();
      if(!res.ok || !data.ok){
        if(status) status.textContent = data.error || 'Verification failed';
        return;
      }
      const verified = (data.verified || []).length;
      const regressed = (data.regressed || []).length;
      const planned = (data.plan || []).length;
      const message = `Verified ${verified}/${planned} resolved issue(s); regressed=${regressed}.`;
      await refreshTesterAndReplay('', message);
      if(byId('verifyResolvedStatus')) byId('verifyResolvedStatus').textContent = message;
    }

    async function loadReplayDashboard(processStatus = ''){
      const res = await fetch('/api/replay-dashboard');
      const data = await res.json();
      const issues = data.issues || [];
      const sessions = data.sessions || [];
      replayState.issues = issues;
      replayState.sessions = sessions;
      const issueOptions = [...new Set(issues.map(i => i.status || 'unknown'))].sort()
        .map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
      const sessionOptions = [...new Set(sessions.map(s => s.status || 'unknown'))].sort()
        .map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
      const issueRows = issues.map(i => `
          <div class="issue-row ${replayState.activeIssueId === i.public_id ? 'active' : ''}" role="button" tabindex="0" data-issue-status="${esc(i.status)}" data-replay-issue="${esc(i.public_id)}">
            <input type="checkbox" data-issue-select value="${esc(i.public_id)}" aria-label="Select ${esc(i.public_id)}" style="width:auto; margin-right:6px" />
            <div class="sev">${esc(i.status)} · ${esc(i.severity)} · ${esc(i.confidence || 'medium')} confidence</div>
            <div class="title">${esc(i.title || 'Untitled issue')}</div>
          <div class="empty">${esc(i.public_id)} · sessions=${esc(i.affected_count)} · users=${esc(i.affected_users || 0)} · evidence=${esc((i.timeline || []).length)} · tests=${esc((i.test_links || []).length)} · ${esc(i.analysis_status || 'fallback')}</div>
          </div>`).join('');
      const sessionRows = sessions.map(s => `
        <li data-session-status="${esc(s.status)}"><button class="btn" type="button" data-replay-session="${esc(s.stable_id)}">Play</button>
          <a href="${esc(s.share_url)}"><code>${esc(s.public_id)}</code></a> · ${esc(s.stable_id)}<br>
          <span class="empty">${esc(s.status)} · events=${esc(s.event_count)} · ${esc(s.last_seen_at)} · ${esc(JSON.stringify(s.preview || {}))}</span>
        </li>`).join('');
      const unresolved = issues.filter(i => !['resolved', 'verified', 'ignored'].includes(i.status)).length;
      const covered = issues.filter(i => (i.test_links || []).length > 0).length;
      const high = issues.filter(i => i.severity === 'high' || i.priority === 'high').length;
      byId('issueWorkflowList').innerHTML = `
        <div class="hdr">
          <select id="issueStatusFilter" style="width:100%; background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:8px;">
            <option value="">All statuses</option>${issueOptions}
          </select>
          <button class="btn" id="generateGroupedReplaySpecsBtn" type="button" style="margin-top:8px">Generate Tests For Group</button>
          <div class="empty" id="groupReplaySpecStatus"></div>
        </div>
        <div id="replayIssueList">${issueRows || '<div class="empty" style="padding:12px">No replay issues yet.</div>'}</div>
      `;
      byId('dashboardView').innerHTML = `
        <div class="view-head">
          <div><h2>Dashboard</h2><div class="empty">Local-first QA workflow across issues, replays, generated tests, and repair prompts.</div></div>
          <div class="actions"><button class="btn" type="button" data-view-jump="issues">Open Issue Detail</button></div>
        </div>
        <div class="metric-grid">
          <div class="metric"><strong>${esc(issues.length)}</strong><span class="empty">total issues</span></div>
          <div class="metric"><strong>${esc(unresolved)}</strong><span class="empty">active issues</span></div>
          <div class="metric"><strong>${esc(covered)}</strong><span class="empty">with linked tests</span></div>
          <div class="metric"><strong>${esc(high)}</strong><span class="empty">high priority/severity</span></div>
        </div>
        <div class="card"><h3>Next Issues</h3>${issues.slice(0, 6).map(i => `<div class="issue-row" role="button" tabindex="0" data-replay-issue="${esc(i.public_id)}"><div class="sev">${esc(i.status)} · ${esc(i.severity)}</div><div class="title">${esc(i.title || 'Untitled issue')}</div><div class="empty">${esc(i.public_id)} · timeline=${esc((i.timeline || []).length)} · tests=${esc((i.test_links || []).length)}</div></div>`).join('') || '<div class="empty">No issues yet.</div>'}</div>
      `;
      byId('replaySessionsPanel').innerHTML = `
        <div class="card">
          <h3>Recent Sessions</h3>
          <select id="sessionStatusFilter" style="width:100%; background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:8px;">
            <option value="">All statuses</option>${sessionOptions}
          </select>
          ${sessionRows ? `<ul id="replaySessionList">${sessionRows}</ul>` : '<div class="empty">No first-party replay sessions yet.</div>'}
        </div>
      `;
      if(byId('replayProcessStatus')) byId('replayProcessStatus').textContent = processStatus;
      byId('importPostHogReplaysBtn').onclick = importPostHogReplays;
      byId('processReplayJobsBtn').onclick = processReplayJobs;
      byId('verifyResolvedBtn').onclick = verifyResolvedReplayIssues;
      document.querySelectorAll('[data-replay-session]').forEach(el => {
        el.onclick = () => loadFirstPartyReplay(el.dataset.replaySession);
      });
      bindReplayIssueRows();
      document.querySelectorAll('[data-issue-select]').forEach(el => {
        el.addEventListener('click', ev => ev.stopPropagation());
        el.addEventListener('keydown', ev => ev.stopPropagation());
      });
      byId('generateGroupedReplaySpecsBtn')?.addEventListener('click', generateGroupedReplayIssueSpecs);
      document.querySelectorAll('[data-view-jump]').forEach(el => el.addEventListener('click', () => switchView(el.dataset.viewJump)));
      byId('issueStatusFilter')?.addEventListener('change', ev => filterReplayRows('replayIssueList', 'issueStatus', ev.target.value));
      byId('sessionStatusFilter')?.addEventListener('change', ev => filterReplayRows('replaySessionList', 'sessionStatus', ev.target.value));
      applyReplayHash(issues, sessions);
      if(!replayState.activeIssueId && issues[0]){
        renderReplayIssueDetail(issues[0]);
      }
      renderLinkedFailureTests();
    }

    function filterReplayRows(listId, dataKey, value){
      const list = byId(listId);
      if(!list) return;
      list.querySelectorAll('li, .issue-row').forEach(row => {
        row.style.display = !value || row.dataset[dataKey] === value ? '' : 'none';
      });
    }

    function renderIssueTimeline(issue){
      const timeline = issue.timeline || [];
      const types = [...new Set(timeline.map(ev => ev.type || 'evidence'))].sort();
      const options = types.map(t => `<option value="${esc(t)}">${esc(t)}</option>`).join('');
      const rows = timeline.map(ev => {
        const reasons = ev.reason_codes || [];
        const reasonText = reasons.length ? ` Reasons: ${reasons.map(code => `<code>${esc(code)}</code>`).join(', ')}` : '';
        const confidenceText = ev.confidence ? ` Confidence: <code>${esc(ev.confidence)}</code>` : '';
        return `
          <div class="timeline-row ${ev.detector_hit ? 'detector' : ''}" data-timeline-type="${esc(ev.type || '')}">
            <div><code>${esc(ev.occurred_at_ms || 0)}ms</code></div>
            <div class="timeline-kind">${esc(ev.kind || ev.type || 'evidence')}</div>
            <div>
              <strong>${esc(ev.title || ev.type || 'Evidence')}</strong>
              <div class="timeline-summary">${esc(ev.summary || '')}</div>
              ${ev.detector ? `<div class="empty">Detector: <code>${esc(ev.detector)}</code>${confidenceText}${reasonText}</div>` : ''}
            </div>
          </div>`;
      }).join('');
      return `
        <div class="lbl">Timeline</div>
        <div style="display:flex; gap:8px; align-items:center; margin:6px 0 8px 0">
          <select id="timelineTypeFilter" style="background:#0b1220; border:1px solid #374151; color:#e5e7eb; border-radius:8px; padding:7px;">
            <option value="">All event types</option>${options}
          </select>
          <button class="btn" id="copyEvidenceBundleBtn" type="button">Copy Evidence Bundle</button>
          <span class="empty" id="copyEvidenceBundleStatus"></span>
        </div>
        ${rows ? `<div class="timeline" id="issueTimeline">${rows}</div>` : '<div class="empty">No timeline evidence captured yet.</div>'}
      `;
    }

    function filterIssueTimeline(value){
      byId('issueTimeline')?.querySelectorAll('.timeline-row').forEach(row => {
        row.style.display = !value || row.dataset.timelineType === value ? '' : 'none';
      });
    }

    function copyEvidenceBundle(issue){
      copyText(JSON.stringify({
        public_id: issue.public_id,
        title: issue.title,
        status: issue.status,
        severity: issue.severity,
        summary: issue.summary,
        likely_cause: issue.likely_cause,
        reproduction_steps: issue.reproduction_steps || [],
        timeline: issue.timeline || [],
        evidence: issue.evidence || {},
      }, null, 2));
      const status = byId('copyEvidenceBundleStatus');
      if(status) status.textContent = 'Copied';
    }

    function renderTestLinks(issue){
      const links = issue.test_links || [];
      const rows = links.map(link => `
        <li>
          <code>${esc(link.spec_id)}</code>${link.spec_name ? ` · ${esc(link.spec_name)}` : ''}
          <br><span class="empty">coverage=<code class="${statusClass(link.coverage_state)}">${esc(link.coverage_state)}</code>${link.latest_run_status ? ` · latest=<code class="${statusClass(link.latest_run_status)}">${esc(link.latest_run_status)}</code>` : ''}${link.latest_run_classification ? ` · class=<code>${esc(link.latest_run_classification)}</code>` : ''}</span>
          ${link.spec_path ? `<br><span class="empty">${esc(link.spec_path)}</span>` : ''}
        </li>
      `).join('');
      return rows ? `<ul>${rows}</ul>` : '<div class="empty">No linked regression tests yet.</div>';
    }

    function renderApiRegressionPanel(issue){
      const calls = issue.api_calls || [];
      const apiLinks = (issue.test_links || []).filter(link => (link.source || '').includes('api'));
      const callRows = calls.map(call => `
        <li>
          <code>${esc(call.method || 'GET')}</code> ${esc(call.url || '')}
          <br><span class="empty">status=<code>${esc(call.status || '')}</code> · detector=<code>${esc(call.detector || '')}</code>${call.confidence ? ` · ${esc(call.confidence)} confidence` : ''}</span>
        </li>
      `).join('');
      const linkRows = apiLinks.map(link => `
        <li>
          <button class="btn" type="button" data-run-api-spec="${esc(link.spec_id)}" data-issue-id="${esc(issue.public_id)}">Run</button>
          <code>${esc(link.spec_id)}</code>${link.spec_name ? ` · ${esc(link.spec_name)}` : ''}
          <br><span class="empty">coverage=<code class="${statusClass(link.coverage_state)}">${esc(link.coverage_state)}</code>${link.latest_run_status ? ` · latest=<code class="${statusClass(link.latest_run_status)}">${esc(link.latest_run_status)}</code>` : ''}</span>
          ${link.spec_path ? `<br><span class="empty">${esc(link.spec_path)}</span>` : ''}
        </li>
      `).join('');
      return `
        <div class="lbl">Triggered API Calls</div>
        ${callRows ? `<ul>${callRows}</ul>` : '<div class="empty">No failed API call evidence on this issue.</div>'}
        <div style="height:8px"></div>
        <button class="btn" id="generateReplayApiSpecBtn" type="button" ${calls.length ? '' : 'disabled'}>Generate API Regression</button>
        <span class="empty" id="replayApiSpecStatus"></span>
        <div style="height:8px"></div>
        ${linkRows ? `<ul>${linkRows}</ul>` : '<div class="empty">No linked API regression tests yet.</div>'}
      `;
    }

    function renderIssueWorkflow(issue){
      const workflow = issue.workflow || {};
      const stages = workflow.stage_states || {};
      const counts = workflow.counts || {};
      const stageLabels = [
        ['evidence', 'Evidence', `${counts.timeline || 0} item(s)`],
        ['reproduction', 'Reproduce', `${counts.replays || 0} replay(s)`],
        ['test', 'Test', `${counts.tests || 0} linked`],
        ['repair', 'Repair', `${counts.repair_tasks || 0} task(s)`],
        ['verification', 'Verify', workflow.coverage_state || 'not_covered'],
      ];
      const action = workflow.primary_action || 'none';
      const button = action !== 'none'
        ? `<button class="btn" type="button" data-workflow-action="${esc(action)}">${esc(workflow.primary_label || 'Continue')}</button>`
        : '';
      return `
        <div class="workflow-strip">
          ${stageLabels.map(([key, label, detail]) => `<div class="workflow-step ${esc(stages[key] || 'current')}"><strong>${esc(label)}</strong><span>${esc(detail)}</span></div>`).join('')}
        </div>
        <div class="workflow-action">
          ${button}
          <span class="empty">Next: ${esc(workflow.primary_label || 'Review issue')}</span>
        </div>
      `;
    }

    function renderEvidenceStitching(issue){
      const stitching = issue.evidence_stitching || {};
      const stages = stitching.stages || [];
      const stageRows = stages.map(stage => `
        <div class="workflow-step ${stage.status === 'complete' ? 'complete' : (stage.status === 'missing' ? 'blocked' : 'current')}">
          <strong>${esc(stage.label)}</strong>
          <span>${esc(stage.detail || '')}</span>
        </div>
      `).join('');
      const traceRows = (stitching.trace_ids || []).map(id => `<code>${esc(id)}</code>`).join(' ');
      const apiRows = (stitching.api_regression_spec_ids || []).filter(Boolean).map(id => `<code>${esc(id)}</code>`).join(' ');
      const frameRows = (stitching.source_map_frames || []).map(frame => `
        <li>
          <code>${esc(frame.filename || 'frame')}</code> · ${frame.source_mapped ? '<span class="ok">mapped</span>' : '<span class="bad">unmapped</span>'}
          ${frame.reason ? ` · <span class="empty">${esc(frame.reason)}</span>` : ''}
        </li>
      `).join('');
      return `
        <div class="workflow-strip">${stageRows}</div>
        <div class="empty">Trace IDs: ${traceRows || 'none captured'}</div>
        <div class="empty">API regression specs: ${apiRows || 'none linked'}</div>
        ${frameRows ? `<ul>${frameRows}</ul>` : '<div class="empty">No source-map frame diagnostics in this issue timeline.</div>'}
      `;
    }

    function renderIssueReadiness(issue){
      const workflow = issue.workflow || {};
      const blockers = workflow.blockers || [];
      const actions = workflow.recommended_actions || [];
      const blockerRows = blockers.map(item => `<li>${esc(item)}</li>`).join('');
      const actionRows = actions.map(item => `
        <div>
          <button class="btn" type="button" data-workflow-action="${esc(item.action)}">${esc(item.label || item.action)}</button>
          <span class="empty">${esc(item.reason || '')}</span>
        </div>
      `).join('');
      return `
        <div class="readiness-panel">
          <div class="row">
            <div><strong>QA Loop Status</strong><div class="empty">Capture → test → repair → verify across replay, UI, and API evidence.</div></div>
            <code class="${workflow.readiness === 'verified' ? 'ok' : (blockers.length ? 'bad' : '')}">${esc(workflow.readiness || 'unknown')}</code>
          </div>
          ${blockerRows ? `<div class="lbl">Blockers</div><ul>${blockerRows}</ul>` : '<div class="empty" style="margin-top:8px">No blocking evidence gaps detected.</div>'}
          ${actionRows ? `<div class="lbl">Recommended Actions</div><div class="recommendation-list">${actionRows}</div>` : ''}
        </div>
      `;
    }

    function handleIssueWorkflowAction(issue, action){
      if(action === 'generate_replay_spec') return generateReplayIssueSpec(issue);
      if(action === 'generate_api_regression') return generateReplayIssueApiSpec(issue);
      if(action === 'generate_repair') return generateReplayIssueFixPrompts(issue);
      if(action === 'verify_resolved') return verifyResolvedReplayIssues();
      if(action === 'review_timeline'){
        byId('issueTimeline')?.scrollIntoView({behavior:'smooth', block:'start'});
        return;
      }
      if(action === 'run_tests'){
        switchView('tests');
        return;
      }
    }

    function renderRepairTask(issue){
      const task = issue.repair_task || null;
      if(!task){
        return '<div class="empty">No repair task yet. Generate fix prompts to package evidence, likely files, validation commands, and agent-ready prompts.</div>';
      }
      const files = (task.likely_files || []).map(file => `<li><code>${esc(file)}</code></li>`).join('');
      const commands = (task.validation_commands || []).map(command => `<li><code>${esc(command)}</code></li>`).join('');
      const artifacts = (task.prompt_artifacts || []).map(artifact => {
        const label = artifact.label || artifact.path || artifact.type || 'artifact';
        return `<li>${esc(label)}</li>`;
      }).join('');
      const prUrl = safeExternalUrl(task.pr_url);
      return `
        <div class="empty"><code>${esc(task.public_id || task.id)}</code> · ${esc(task.status || 'open')} · ${esc(task.title || 'Repair task')}</div>
        ${files ? `<div class="lbl">Likely Files</div><ul>${files}</ul>` : ''}
        ${commands ? `<div class="lbl">Validation Commands</div><ul>${commands}</ul>` : ''}
        ${artifacts ? `<div class="lbl">Prompt Artifacts</div><ul>${artifacts}</ul>` : ''}
        ${task.risk_notes ? `<div class="lbl">Risk Notes</div><div>${esc(task.risk_notes)}</div>` : ''}
        ${prUrl ? `<div class="lbl">PR</div><a href="${esc(prUrl)}" target="_blank" rel="noopener noreferrer">${esc(prUrl)}</a>` : ''}
      `;
    }

    function renderExternalLinks(issue){
      const links = [];
      const externalTicketUrl = safeExternalUrl(issue.external_ticket_url);
      if(externalTicketUrl) links.push(`<a href="${esc(externalTicketUrl)}" target="_blank" rel="noopener noreferrer">${esc(issue.external_ticket_id || 'External ticket')}</a>`);
      const issueUrl = safeHashUrl(issue.share_url, '#issue=');
      if(issueUrl) links.push(`<a href="${esc(issueUrl)}">Issue permalink</a>`);
      for(const session of issue.sessions || []){
        const replay = replayState.sessions.find(s => s.stable_id === session.session_id || s.public_id === session.public_id) || {};
        const replayId = replay.public_id || session.public_id || session.session_id;
        const playerId = replay.stable_id || session.stable_id || session.session_id;
        if(replayId) links.push(`<a href="#replay=${encodeURIComponent(replayId)}" data-replay-session="${esc(playerId)}">Replay ${esc(replayId)}</a>`);
      }
      return links.length ? `<ul>${links.map(link => `<li>${link}</li>`).join('')}</ul>` : `<div class="empty">${esc(issue.external_ticket_state || 'No external links yet.')}</div>`;
    }

    function renderLinkedFailureTests(){
      const root = byId('linkedFailureTests');
      if(!root){ return; }
      const rows = [];
      for(const issue of replayState.issues || []){
        for(const link of issue.test_links || []){
          rows.push(`
            <li>
              <button class="btn" type="button" data-replay-issue="${esc(issue.public_id)}">Open</button>
              <code>${esc(link.spec_id)}</code> · <span class="${statusClass(link.coverage_state)}">${esc(link.coverage_state)}</span>
              <br><span class="empty">${esc(issue.public_id)} · ${esc(issue.title || 'Replay issue')}${link.latest_run_status ? ` · latest=${esc(link.latest_run_status)}` : ''}</span>
            </li>
          `);
        }
      }
      root.innerHTML = rows.length ? `<ul>${rows.join('')}</ul>` : '<div class="empty">No linked failures yet. Generate a regression spec from an issue to create coverage.</div>';
      bindReplayIssueRows(root);
    }

    function renderReplayIssueDetail(issue){
      const root = byId('replayIssueDetail');
      if(!root || !issue){ return; }
      replayState.activeIssueId = issue.public_id;
      document.querySelectorAll('[data-replay-issue]').forEach(el => {
        el.classList.toggle('active', el.dataset.replayIssue === issue.public_id);
      });
      const steps = (issue.reproduction_steps || []).map(s => `<li>${esc(s)}</li>`).join('');
      const sessions = (issue.sessions || []).map(s => {
        const replay = replayState.sessions.find(session => session.stable_id === s.session_id || session.public_id === s.public_id) || {};
        const playerId = replay.stable_id || s.stable_id || s.session_id;
        const replayId = replay.public_id || s.public_id || s.session_id;
        return `<li><button class="btn" type="button" data-replay-session="${esc(playerId)}">Play</button> <a href="#replay=${esc(replayId)}"><code>${esc(replayId)}</code></a> · ${esc(s.role)}</li>`;
      }).join('');
      root.innerHTML = `
        <div class="view-head">
          <div>
            <h2>${esc(issue.public_id)} · ${esc(issue.title || 'Replay issue')}</h2>
            <div class="empty">${esc(issue.status)} · ${esc(issue.severity)} · ${esc(issue.confidence || 'medium')} confidence · affected=${esc(issue.affected_count)} · users=${esc(issue.affected_users)}</div>
          </div>
          <div class="actions">
            <button class="btn" id="resolveReplayIssueBtn" type="button">Mark Resolved</button>
            <button class="btn" id="unresolveReplayIssueBtn" type="button">Mark Unresolved</button>
            <button class="btn" id="ignoreReplayIssueBtn" type="button">Ignore Fingerprint</button>
          </div>
        </div>
        <div class="empty" id="replayLifecycleStatus"></div>
        ${renderIssueWorkflow(issue)}
        ${renderIssueReadiness(issue)}
        <div class="detail-grid">
          <div>
            <div class="card">
              <h3>Failure Narrative</h3>
              <div class="lbl">Analysis</div><div>${esc(issue.analysis_status || 'fallback')}${issue.analysis_model ? ` · ${esc(issue.analysis_model)}` : ''}${issue.analysis_error ? ` · ${esc(issue.analysis_error)}` : ''}</div>
              <div class="lbl">Summary</div><div>${esc(issue.summary || '')}</div>
              <div class="lbl">Likely Cause</div><div>${esc(issue.likely_cause || '')}</div>
              <div class="lbl">Reproduction Steps</div>${steps ? `<ul>${steps}</ul>` : '<div class="empty">No steps generated yet.</div>'}
            </div>
            <div style="height:12px"></div>
            <div class="card">${renderIssueTimeline(issue)}</div>
            <div style="height:12px"></div>
            <div class="card"><h3>Evidence Stitching</h3>${renderEvidenceStitching(issue)}</div>
            <div style="height:12px"></div>
            <div class="card">
              <h3>Repair Task</h3>
              <button class="btn" id="generateReplayFixPromptsBtn" type="button">Generate Fix Prompts</button>
              <span class="empty" id="replayFixPromptStatus"></span>
              <div style="height:10px"></div>
              ${renderRepairTask(issue)}
              <div id="replayFixPrompts"></div>
            </div>
          </div>
          <div>
            <div class="card"><h3>Replay</h3>${sessions ? `<ul>${sessions}</ul>` : '<div class="empty">No linked sessions.</div>'}</div>
            <div style="height:12px"></div>
            <div class="card">
              <h3>Generated Test</h3>
              <button class="btn" id="generateReplaySpecBtn" type="button">Generate Regression Spec</button>
              <span class="empty" id="replaySpecStatus"></span>
              <div style="height:8px"></div>
              ${renderTestLinks(issue)}
            </div>
            <div style="height:12px"></div>
            <div class="card"><h3>API Regression</h3>${renderApiRegressionPanel(issue)}</div>
            <div style="height:12px"></div>
            <div class="card"><h3>External Links</h3>${renderExternalLinks(issue)}</div>
            <div style="height:12px"></div>
            <div class="card"><h3>Signals</h3><pre>${esc(JSON.stringify(issue.signal_summary || {}, null, 2))}</pre></div>
          </div>
        </div>
      `;
      root.querySelectorAll('[data-replay-session]').forEach(el => {
        el.addEventListener('click', () => loadFirstPartyReplay(el.dataset.replaySession));
      });
      byId('resolveReplayIssueBtn')?.addEventListener('click', () => transitionReplayIssue(issue, 'resolved'));
      byId('unresolveReplayIssueBtn')?.addEventListener('click', () => transitionReplayIssue(issue, 'unresolved'));
      byId('ignoreReplayIssueBtn')?.addEventListener('click', () => transitionReplayIssue(issue, 'ignored'));
      byId('generateReplaySpecBtn')?.addEventListener('click', () => generateReplayIssueSpec(issue));
      byId('generateReplayApiSpecBtn')?.addEventListener('click', () => generateReplayIssueApiSpec(issue));
      byId('generateReplayFixPromptsBtn')?.addEventListener('click', () => generateReplayIssueFixPrompts(issue));
      root.querySelectorAll('[data-workflow-action]').forEach(el => {
        el.addEventListener('click', () => handleIssueWorkflowAction(issue, el.dataset.workflowAction));
      });
      root.querySelectorAll('[data-run-api-spec]').forEach(el => {
        el.addEventListener('click', () => runReplayIssueApiSpec(el.dataset.runApiSpec, el.dataset.issueId));
      });
      byId('timelineTypeFilter')?.addEventListener('change', ev => filterIssueTimeline(ev.target.value));
      byId('copyEvidenceBundleBtn')?.addEventListener('click', () => copyEvidenceBundle(issue));
    }

    function applyReplayHash(issues, sessions){
      const hash = new URLSearchParams(window.location.hash.replace(/^#/, ''));
      const issueId = hash.get('issue');
      const replayId = hash.get('replay');
      if(issueId){
        renderReplayIssueDetail(issues.find(i => i.public_id === issueId));
        switchView('issues');
      }
      if(replayId){
        const session = sessions.find(s => s.public_id === replayId);
        if(session) loadFirstPartyReplay(session.stable_id);
        switchView('replays');
      }
    }

    async function loadFirstPartyReplay(sessionId){
      const root = byId('firstPartyReplay');
      root.innerHTML = '<div class="empty">Loading replay...</div>';
      const res = await fetch(`/api/replay-session/${encodeURIComponent(sessionId)}/events`);
      const data = await res.json();
      if(!res.ok || !data.events || !data.events.length){
        root.innerHTML = `<div class="empty">${esc(data.error || 'No events found.')}</div>`;
        return;
      }
      root.innerHTML = '';
      new rrwebPlayer({ target: root, props: { events: data.events, width: 980, height: 560, autoPlay: false }});
    }

    function renderList() {
      const root = byId('findings');
      root.innerHTML = findings.map(f => `
        <div class=\"finding ${active && active.id===f.id?'active':''}\" data-id=\"${f.id}\">
          <div class=\"sev\">${esc(f.severity)} · ${esc(f.category)}</div>
          <div class=\"title\">${esc(f.title)}</div>
        </div>`).join('');
      root.querySelectorAll('.finding').forEach(el => {
        el.addEventListener('click', () => {
          active = findings.find(f => f.id === el.dataset.id);
          renderList();
          renderDetail();
        });
      });
    }

    async function loadReplay(sessionId){
      const rr = byId('rr');
      rr.innerHTML = '<div class=\"empty\">Loading replay...</div>';
      try {
        const res = await fetch(`/api/session/${sessionId}/events`);
        const data = await res.json();
        rr.innerHTML = '';
        if(!data.events || !data.events.length){ rr.innerHTML='<div class=\"empty\">No events found.</div>'; return; }
        new rrwebPlayer({ target: rr, props: { events: data.events, width: 980, height: 560, autoPlay: false }});
      } catch(_e){
        rr.innerHTML = '<div class=\"empty\">Replay failed to load.</div>';
      }
    }

    function renderDetail(){
      const root = byId('findingDetail');
      if(!active){ root.innerHTML = '<div class=\"empty\">Select a finding.</div>'; return; }
      const cands = (active.candidates||[]).map(c => `<li><code>${esc(c.file_path)}</code> (score=${c.score})<br><span class=\"empty\">${esc(c.rationale)}</span></li>`).join('');
      const codex = active.prompts?.codex || '';
      const claude = active.prompts?.claude_code || '';
      const errIssues = (active.error_issue_ids||[]).join(', ') || '—';
      const traceIds = (active.trace_ids||[]).join(', ') || '—';
      const issueCount = (active.error_issue_ids||[]).filter(Boolean).length;
      const traceCount = (active.trace_ids||[]).filter(Boolean).length;
      const hasStack = Boolean((active.top_stack_frame || '').trim());
      const hasErrorLink = Boolean(active.error_tracking_url);
      const hasLogsLink = Boolean(active.logs_url);
      const hasCorrelation = issueCount > 0 || traceCount > 0 || hasStack || hasErrorLink || hasLogsLink;
      const errWindow = (active.first_error_ts_ms || active.last_error_ts_ms)
        ? `${active.first_error_ts_ms} → ${active.last_error_ts_ms}`
        : '—';
      const regressionState = active.regression_state || 'new';
      const regressionCount = active.regression_occurrence_count || 1;
      const correlationStatusHtml = hasCorrelation
        ? `<div class=\"empty\">Live correlation data found.</div>
           <div class=\"empty\">Issues: <code>${issueCount}</code> · Traces: <code>${traceCount}</code> · Stack frame: <code>${hasStack ? 'yes' : 'no'}</code></div>
           <div class=\"empty\" style=\"margin-top:4px\">Links: ${hasErrorLink ? 'Error Tracking' : '—'} ${hasLogsLink ? 'Logs' : ''}</div>`
        : `<div class=\"empty\">No correlated error/log/trace evidence yet for this finding.</div>`;
      root.innerHTML = `
        <div class=\"meta card\"><h3>${esc(active.title)}</h3><div class=\"empty\">${esc(active.severity)} · ${esc(active.category)}</div><div style=\"margin-top:8px\"><a href=\"${esc(active.session_url)}\" target=\"_blank\">Open PostHog replay</a></div></div>
        <div style=\"height:10px\"></div>
        <div class=\"rr\"><div id=\"rr\"></div></div>
        <div style=\"height:12px\"></div>
        <div class=\"grid\">
          <div class=\"card\"><h3>Likely Culprits</h3>${cands ? `<ul>${cands}</ul>` : '<div class=\"empty\">No candidates generated.</div>'}</div>
          <div class=\"card\"><h3>Evidence</h3><pre>${esc(active.evidence_text)}</pre></div>
        </div>
        <div style=\"height:12px\"></div>
        <div class=\"grid\">
          <div class=\"card\">
            <h3>Observability Links</h3>
            <div class=\"empty\">Distinct ID: <code>${esc(active.distinct_id || '—')}</code></div>
            <div class=\"empty\">Error issues: <code>${esc(errIssues)}</code></div>
            <div class=\"empty\">Trace IDs: <code>${esc(traceIds)}</code></div>
            <div class=\"empty\">Top stack frame: <code>${esc(active.top_stack_frame || '—')}</code></div>
            <div class=\"empty\">Error window (ms): <code>${esc(errWindow)}</code></div>
            <div class=\"empty\">Regression: <code>${esc(regressionState)}</code> · seen <code>${esc(regressionCount)}</code> time(s)</div>
            <div style=\"margin-top:8px\">${active.error_tracking_url ? `<a href=\"${esc(active.error_tracking_url)}\" target=\"_blank\">Open Error Tracking</a>` : '<span class=\"empty\">Error Tracking link unavailable</span>'}</div>
            <div style=\"margin-top:4px\">${active.logs_url ? `<a href=\"${esc(active.logs_url)}\" target=\"_blank\">Open Logs</a>` : '<span class=\"empty\">Logs link unavailable</span>'}</div>
          </div>
          <div class=\"card\"><h3>Correlation Status</h3>${correlationStatusHtml}</div>
        </div>
        <div style=\"height:12px\"></div>
        <div class=\"grid\">
          <div class=\"card\"><h3>Codex Prompt <button class=\"btn\" id=\"copyFindingCodexPrompt\" type=\"button\">Copy</button></h3><pre>${esc(codex)}</pre></div>
          <div class=\"card\"><h3>Claude Prompt <button class=\"btn\" id=\"copyFindingClaudePrompt\" type=\"button\">Copy</button></h3><pre>${esc(claude)}</pre></div>
        </div>`;
      byId('copyFindingCodexPrompt')?.addEventListener('click', () => copyPrompt('codex'));
      byId('copyFindingClaudePrompt')?.addEventListener('click', () => copyPrompt('claude_code'));
      loadReplay(active.session_id);
    }

    async function bootFindings(){
      const res = await fetch('/api/findings');
      const data = await res.json();
      findings = data.findings || [];
      byId('reportMeta').textContent = data.report_path || 'No report found';
      active = findings[0] || null;
      renderList();
      renderDetail();
    }

    async function boot(){
      await loadOnboarding();
      await loadTesterPanel();
      await loadReplayDashboard();
      await bootFindings();
    }
    boot();
  </script>
</body>
</html>
"""


