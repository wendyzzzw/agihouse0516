(function () {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const params = new URLSearchParams(window.location.search);
  let pollTimer = null;
  let replayTimer = null;

  async function api(path, options = {}) {
    const response = await fetch(path, options);
    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || `${response.status} ${response.statusText}`);
    }
    return response.json();
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function fmtMoney(value) {
    const n = Number(value);
    if (!Number.isFinite(n) || n <= 0) return "-";
    return `$${Math.round(n).toLocaleString()}`;
  }

  function fmtSignedMoney(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "-";
    const sign = n < 0 ? "-" : "";
    return `${sign}$${Math.abs(Math.round(n)).toLocaleString()}`;
  }

  function fmtNumber(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n.toLocaleString() : "-";
  }

  function fmtPct(value) {
    const n = Number(value);
    return Number.isFinite(n) ? `${Math.round(n)}%` : "-";
  }

  function fmtDate(seconds) {
    const n = Number(seconds);
    if (!Number.isFinite(n)) return "-";
    return new Date(n * 1000).toLocaleString();
  }

  function labelize(value) {
    return String(value || "")
      .replace(/_/g, " ")
      .replace(/\b\w/g, char => char.toUpperCase());
  }

  function roleFor(id, playersById) {
    return playersById[id]?.role || (String(id).startsWith("seller") ? "seller" : "buyer");
  }

  function actor(id, playersById) {
    const role = roleFor(id, playersById);
    return `<span class="actor ${escapeHtml(role || "unknown")}">${escapeHtml(id || "system")}</span>`;
  }

  function statusBadge(status) {
    const normalized = String(status || "unknown").toLowerCase();
    return `<span class="badge ${escapeHtml(normalized)}">${escapeHtml(normalized)}</span>`;
  }

  function setText(selector, text) {
    const node = $(selector);
    if (node) node.textContent = text;
  }

  function showError(selector, error) {
    const node = $(selector);
    if (node) node.textContent = `Error: ${error.message || error}`;
  }

  function renderNav(active) {
    const nav = $(".navlinks");
    if (!nav) return;
    const links = [
      ["runs", "Past Runs", "runs.html"],
      ["create", "Create Simulation", "create.html"],
    ];
    nav.innerHTML = links.map(([key, label, href]) => (
      `<a class="${active === key ? "active" : ""}" href="${href}">${label}</a>`
    )).join("");
  }

  async function initRunsPage() {
    renderNav("runs");
    const list = $("#run-list");
    const status = $("#runs-status");
    try {
      const [payload, comparison] = await Promise.all([
        api("/api/runs"),
        api("/api/analysis/compare").catch(error => ({ error })),
      ]);
      const runs = payload.runs || [];
      status.textContent = runs.length ? `${runs.length} file-backed run${runs.length === 1 ? "" : "s"}` : "No live runs yet";
      renderComparison(comparison);
      if (!runs.length) {
        list.innerHTML = `<div class="empty">No past live runs yet. Create a simulation to write the first run under runs/live.</div>`;
        return;
      }
      list.innerHTML = runs.map(renderRunRow).join("");
    } catch (error) {
      showError("#runs-status", error);
      list.innerHTML = `<div class="empty">The backend is not reachable. Start FastAPI and reload this page.</div>`;
    }
  }

  function renderComparison(comparison) {
    const root = $("#comparison");
    if (!root) return;
    if (comparison?.error) {
      $("#comparison-status").textContent = `Comparison unavailable: ${comparison.error.message || comparison.error}`;
      root.innerHTML = `<div class="empty">Run analysis is unavailable.</div>`;
      return;
    }
    const rows = comparison?.scenario_comparison || [];
    const learnings = comparison?.overall_learnings || [];
    $("#comparison-status").textContent = rows.length ? `${rows.length} scenario group${rows.length === 1 ? "" : "s"}` : "No completed analysis yet";
    if (!rows.length) {
      root.innerHTML = `<div class="empty">Create or open a run to generate analysis.</div>`;
      return;
    }
    root.innerHTML = `
      <div class="comparison-grid">
        ${learnings.slice(0, 3).map((learning, index) => `
          <div class="comparison-card">
            <strong>Learning ${index + 1}</strong>
            <p>${escapeHtml(learning)}</p>
          </div>
        `).join("")}
      </div>
      <div class="heatmap-wrap comparison-table">
        <table class="table">
          <thead>
            <tr>
              <th>Scenario</th>
              <th>Setup</th>
              <th>Avg Price</th>
              <th>Spread</th>
              <th>Purchase</th>
              <th>Surplus</th>
              <th>Seller Rev</th>
              <th>Messages</th>
              <th>Power</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map(row => `
              <tr>
                <td>${escapeHtml(row.scenario_name)}</td>
                <td>${escapeHtml(labelize(row.setup_type))}</td>
                <td>${fmtMoney(row.avg_price)}</td>
                <td>${fmtMoney(row.avg_price_spread)}</td>
                <td>${fmtPct(row.avg_purchase_rate_pct)}</td>
                <td>${fmtSignedMoney(row.avg_buyer_surplus)}</td>
                <td>${fmtMoney(row.avg_seller_revenue)}</td>
                <td>${fmtNumber(row.avg_messages)}</td>
                <td>${escapeHtml(labelize(row.dominant_advantage))}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
      ${renderArchetypeComparison(comparison.archetype_comparison || [])}
    `;
  }

  function renderArchetypeComparison(rows) {
    const buyerRows = rows.filter(row => row.role === "buyer").slice(0, 5);
    const sellerRows = rows.filter(row => row.role === "seller").slice(0, 5);
    if (!buyerRows.length && !sellerRows.length) return "";
    return `
      <div class="comparison-grid" style="margin-top: 12px;">
        <div class="comparison-card">
          <strong>Buyer Archetype Signals</strong>
          <p>${buyerRows.map(row => `${escapeHtml(row.label)}: ${fmtPct(row.avg_purchase_rate_pct)} bought, ${fmtSignedMoney(row.avg_surplus)} surplus`).join("<br>")}</p>
        </div>
        <div class="comparison-card">
          <strong>Seller Archetype Signals</strong>
          <p>${sellerRows.map(row => `${escapeHtml(row.label)}: ${fmtMoney(row.avg_revenue)} revenue, ${fmtNumber(row.avg_messages)} msg touches`).join("<br>")}</p>
        </div>
        <div class="comparison-card">
          <strong>How To Read This</strong>
          <p>Compare setup first, then outcome: topology changes who can coordinate, while archetypes explain which agents converted that structure into trades or revenue.</p>
        </div>
      </div>
    `;
  }

  function renderRunRow(run) {
    const summary = run.result_summary || {};
    const scenario = labelize(run.scenario_id);
    const turn = `${fmtNumber(run.current_turn)} / ${fmtNumber(run.max_rounds)}`;
    return `
      <a class="run-row" href="run.html?run_id=${encodeURIComponent(run.run_id)}">
        <div class="run-main">
          <strong>${escapeHtml(scenario)}</strong>
          <span>${escapeHtml(run.run_id)}</span>
        </div>
        <div>${statusBadge(run.status)}</div>
        <div>
          <div>${escapeHtml(turn)}</div>
          <div class="run-cell-label">turns</div>
        </div>
        <div>
          <div>${escapeHtml(run.llm_provider || "rule")}</div>
          <div class="run-cell-label">${escapeHtml(run.model || "")}</div>
        </div>
        <div>
          <div>${fmtNumber(run.message_count || summary.total_messages)}</div>
          <div class="run-cell-label">messages</div>
        </div>
        <div>
          <div>${fmtMoney(summary.avg_price)}</div>
          <div class="run-cell-label">${fmtPct(summary.avg_satisfaction)} satisfaction</div>
        </div>
      </a>
    `;
  }

  async function initCreatePage() {
    renderNav("create");
    const select = $("#scenario_id");
    const status = $("#create-status");
    try {
      const payload = await api("/api/scenarios");
      select.innerHTML = (payload.scenarios || []).map(scenario => (
        `<option value="${escapeHtml(scenario.id)}">${escapeHtml(labelize(scenario.id))}</option>`
      )).join("");
      const summaryById = Object.fromEntries((payload.scenarios || []).map(s => [s.id, s.summary || ""]));
      const updateSummary = () => {
        setText("#scenario-summary", summaryById[select.value] || "");
      };
      select.addEventListener("change", updateSummary);
      updateSummary();
    } catch (error) {
      showError("#create-status", error);
    }

    $("#create-form").addEventListener("submit", async event => {
      event.preventDefault();
      const form = new FormData(event.currentTarget);
      const maxRoundsRaw = String(form.get("max_rounds") || "").trim();
      const payload = {
        scenario_id: String(form.get("scenario_id") || "open_bazaar"),
        seed: Number(form.get("seed") || 42),
        max_rounds: maxRoundsRaw ? Number(maxRoundsRaw) : null,
        llm_provider: String(form.get("llm_provider") || "rule"),
        model: String(form.get("model") || "gpt-4.1-mini"),
        speed_ms: Number(form.get("speed_ms") || 500),
      };
      status.textContent = "Creating run...";
      $("#create-submit").disabled = true;
      try {
        const created = await api("/api/runs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        window.location.href = `run.html?run_id=${encodeURIComponent(created.run_id)}`;
      } catch (error) {
        showError("#create-status", error);
        $("#create-submit").disabled = false;
      }
    });
  }

  async function initRunPage() {
    renderNav("runs");
    const runId = params.get("run_id");
    if (!runId) {
      $("#run-root").innerHTML = `<div class="empty">Missing run_id. Open a run from the past runs page.</div>`;
      return;
    }
    $("#context-link").href = `context.html?run_id=${encodeURIComponent(runId)}`;
    $("#refresh-run").addEventListener("click", () => loadRunPage(runId));
    $("#recap-recompute").addEventListener("click", () => recomputeAnalysis(runId));
    $("#message-filter").addEventListener("change", () => updateRunReplay({ fetchSnapshot: false }));
    $("#replay-turn").addEventListener("input", event => {
      window.__replayTurn = Number(event.currentTarget.value || 0);
      window.__followLatestTurn = false;
      updateRunReplay({ fetchSnapshot: true });
    });
    $("#replay-play").addEventListener("click", () => toggleReplay());
    $("#replay-latest").addEventListener("click", () => {
      stopReplay();
      window.__followLatestTurn = true;
      window.__replayTurn = window.__replayMaxTurn || 0;
      updateRunReplay({ fetchSnapshot: true });
    });
    await loadRunPage(runId);
  }

  async function recomputeAnalysis(runId) {
    const button = $("#recap-recompute");
    button.disabled = true;
    $("#recap-status").textContent = "Recomputing recap...";
    try {
      const analysis = await api(`/api/runs/${encodeURIComponent(runId)}/analysis/recompute`, { method: "POST" });
      if (window.__runPageData) window.__runPageData.analysis = analysis;
      renderRecap(analysis);
    } catch (error) {
      showError("#recap-status", error);
    } finally {
      button.disabled = false;
    }
  }

  async function loadRunPage(runId, fetchData = true) {
    try {
      let data = window.__runPageData;
      if (fetchData || !data) {
        const [meta, snapshot, messages, context, analysis] = await Promise.all([
          api(`/api/runs/${encodeURIComponent(runId)}`),
          api(`/api/runs/${encodeURIComponent(runId)}/snapshot`),
          api(`/api/runs/${encodeURIComponent(runId)}/messages`),
          api(`/api/runs/${encodeURIComponent(runId)}/context`),
          api(`/api/runs/${encodeURIComponent(runId)}/analysis`).catch(error => ({ error })),
        ]);
        const previousSnapshots = data?.snapshotsByTurn || {};
        data = { runId, meta, snapshot, messages: messages.messages || [], context, analysis, snapshotsByTurn: previousSnapshots };
        data.snapshotsByTurn[Number(snapshot.current_turn || 0)] = snapshot;
        window.__runPageData = data;
      }

      renderRunPage(data.meta, data.snapshot, data.messages, data.context);
      if (["queued", "running"].includes(String(data.meta.status).toLowerCase())) {
        clearTimeout(pollTimer);
        pollTimer = setTimeout(() => loadRunPage(runId), 1200);
      } else {
        clearTimeout(pollTimer);
      }
    } catch (error) {
      showError("#run-status", error);
    }
  }

  function renderRunPage(meta, snapshot, messages, context) {
    const title = `${labelize(meta.scenario_id)} / ${meta.run_id}`;
    $("#run-title").textContent = title;
    $("#run-subtitle").textContent = meta.summary || "Live file-backed simulation run";
    $("#run-status").innerHTML = `${statusBadge(meta.status)} <span class="muted">Created ${escapeHtml(fmtDate(meta.created_at))}; provider ${escapeHtml(meta.llm_provider)} / ${escapeHtml(meta.model)}</span>`;
    $("#context-link").href = `context.html?run_id=${encodeURIComponent(meta.run_id)}`;

    const maxTurn = Number(meta.current_turn ?? snapshot.current_turn ?? 0);
    const previousMax = Number(window.__replayMaxTurn ?? -1);
    window.__replayMaxTurn = maxTurn;
    if (window.__replayTurn === undefined || window.__replayTurn === null || window.__followLatestTurn || window.__replayTurn >= previousMax) {
      window.__replayTurn = maxTurn;
      window.__followLatestTurn = true;
    }
    window.__replayTurn = Math.max(0, Math.min(Number(window.__replayTurn || 0), maxTurn));

    renderRecap(window.__runPageData?.analysis);
    renderMessageFilter(mergePlayers(context, snapshot));
    renderHeatmap(messages, mergePlayers(context, snapshot));
    updateRunReplay({ fetchSnapshot: false });
  }

  function renderRecap(analysis) {
    const root = $("#recap");
    if (!root) return;
    if (!analysis || analysis.error) {
      $("#recap-status").textContent = analysis?.error ? `Recap unavailable: ${analysis.error.message || analysis.error}` : "No recap yet";
      root.innerHTML = `<div class="empty">Recap analysis is unavailable for this run.</div>`;
      return;
    }
    const recap = analysis.recap || {};
    const outcomes = analysis.outcomes || {};
    const power = analysis.buyer_seller_power || {};
    const communication = analysis.communication || {};
    $("#recap-status").textContent = `Generated at turn ${fmtNumber(analysis.current_turn)}`;
    root.innerHTML = `
      <p class="recap-headline">${escapeHtml(recap.headline || "No headline generated.")}</p>
      <div class="recap-grid">
        <div class="recap-card">
          <strong>What This Tested</strong>
          <p>${escapeHtml(recap.setup || analysis.setup?.what_it_tests || "")}</p>
        </div>
        <div class="recap-card">
          <strong>Buyer vs Seller Power</strong>
          <p>${escapeHtml(labelize(power.advantage))}: ${escapeHtml(power.explanation || "")}</p>
        </div>
        <div class="recap-card">
          <strong>Outcome Snapshot</strong>
          <p>${fmtNumber(outcomes.transaction_count)} trades, ${fmtPct(outcomes.purchase_rate_pct)} buyer participation, ${fmtMoney(outcomes.seller_revenue)} seller revenue, ${fmtNumber(communication.total_messages)} messages.</p>
        </div>
      </div>
      <div class="recap-grid" style="margin-top: 12px;">
        <div class="recap-card">
          <strong>What Happened</strong>
          <ul class="recap-list">${(recap.what_happened || []).map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
        </div>
        <div class="recap-card">
          <strong>Notable Dynamics</strong>
          <ul class="recap-list">${(recap.notable_dynamics || []).map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
        </div>
        <div class="recap-card">
          <strong>Takeaway</strong>
          <p>${escapeHtml(recap.takeaway || "")}</p>
        </div>
      </div>
      <div class="recap-card" style="margin-top: 12px;">
        <strong>Evidence</strong>
        <div class="evidence-list">
          ${(recap.evidence || analysis.evidence || []).map(item => `
            <div class="evidence-item">
              <div class="message-route">
                <span>${escapeHtml(item.label || item.kind || "Evidence")}</span>
                ${item.turn !== undefined ? `<span>t=${fmtNumber(item.turn)}</span>` : ""}
                ${item.from ? `<span>${escapeHtml(item.from)}</span>` : ""}
                ${item.to ? `<span>-&gt; ${escapeHtml(item.to)}</span>` : ""}
              </div>
              <div class="message-content">${escapeHtml(item.detail || "")}</div>
            </div>
          `).join("")}
        </div>
      </div>
    `;
  }

  function mergePlayers(context, snapshot) {
    const contextPlayers = context?.players || [];
    const snapshotPlayers = Object.values(snapshot?.players || {});
    const byId = {};
    contextPlayers.forEach(player => {
      byId[player.id] = { ...player };
    });
    snapshotPlayers.forEach(player => {
      byId[player.id] = {
        ...(byId[player.id] || {}),
        ...player,
        status: {
          ...(byId[player.id]?.status || {}),
          ...player,
        },
      };
    });
    return Object.values(byId).sort((a, b) => {
      if (a.role !== b.role) return a.role === "seller" ? 1 : -1;
      return String(a.id).localeCompare(String(b.id), undefined, { numeric: true });
    });
  }

  async function updateRunReplay({ fetchSnapshot = true } = {}) {
    const data = window.__runPageData;
    if (!data) return;
    const maxTurn = Number(window.__replayMaxTurn || data.meta.current_turn || 0);
    const turn = Math.max(0, Math.min(Number(window.__replayTurn || 0), maxTurn));
    window.__replayTurn = turn;

    const slider = $("#replay-turn");
    slider.max = String(maxTurn);
    slider.value = String(turn);
    $("#replay-turn-label").textContent = `Turn ${turn}`;

    let snapshot = data.snapshotsByTurn?.[turn] || (turn === Number(data.snapshot.current_turn || 0) ? data.snapshot : null);
    if (!snapshot && fetchSnapshot) {
      try {
        snapshot = await api(`/api/runs/${encodeURIComponent(data.runId)}/snapshot?turn=${encodeURIComponent(turn)}`);
        data.snapshotsByTurn[turn] = snapshot;
      } catch (error) {
        snapshot = data.snapshot;
        showError("#replay-status", error);
      }
    }
    snapshot = snapshot || data.snapshot;

    const players = mergePlayers(data.context, snapshot);
    const playersById = Object.fromEntries(players.map(player => [player.id, player]));
    const turnMessages = data.messages.filter(message => Number(message.turn || 0) === turn);
    const turnEvents = (snapshot.events || []).filter(event => Number(event.turn || 0) === turn);

    renderMetrics(snapshot.summary || {});
    renderPlayers(players);
    renderMessages(data.runId, data.messages, playersById, turn);
    renderSellers(snapshot.sellers || {});
    renderEvents(turnEvents, playersById, turn);
    renderTopology(snapshot, playersById, turnMessages);
    renderPriceChart(snapshot);
    renderReplaySummary(turn, maxTurn, turnMessages, turnEvents);
  }

  function renderMetrics(summary) {
    $("#metrics").innerHTML = [
      ["Avg price", fmtMoney(summary.avg_price)],
      ["Bought", fmtNumber(summary.n_bought)],
      ["Missed", fmtNumber(summary.n_missed)],
      ["Satisfaction", fmtPct(summary.avg_satisfaction)],
      ["Messages", fmtNumber(summary.total_messages)],
    ].map(([label, value]) => (
      `<div class="metric"><span>${label}</span><strong>${value}</strong></div>`
    )).join("");
  }

  function renderReplaySummary(turn, maxTurn, messages, events) {
    $("#replay-status").textContent = `Turn ${turn} of ${maxTurn}`;
    $("#replay-turn-summary").innerHTML = [
      `${fmtNumber(messages.length)} directed message${messages.length === 1 ? "" : "s"} this turn`,
      `${fmtNumber(events.length)} event${events.length === 1 ? "" : "s"} this turn`,
      turn === maxTurn ? "at latest stored turn" : "replaying historical turn",
    ].map(value => `<span class="badge">${escapeHtml(value)}</span>`).join("");
  }

  function toggleReplay() {
    if (replayTimer) {
      stopReplay();
      return;
    }
    const maxTurn = Number(window.__replayMaxTurn || 0);
    if (Number(window.__replayTurn || 0) >= maxTurn) {
      window.__replayTurn = 0;
      window.__followLatestTurn = false;
      updateRunReplay({ fetchSnapshot: true });
    }
    $("#replay-play").textContent = "Pause";
    replayTimer = setInterval(() => {
      const max = Number(window.__replayMaxTurn || 0);
      const next = Number(window.__replayTurn || 0) + 1;
      if (next > max) {
        stopReplay();
        return;
      }
      window.__replayTurn = next;
      window.__followLatestTurn = false;
      updateRunReplay({ fetchSnapshot: true });
    }, 900);
  }

  function stopReplay() {
    if (replayTimer) clearInterval(replayTimer);
    replayTimer = null;
    const button = $("#replay-play");
    if (button) button.textContent = "Play";
  }

  function renderMessageFilter(players) {
    const select = $("#message-filter");
    const previous = select.value || "all";
    select.innerHTML = `<option value="all">All messages</option>` + players.map(player => (
      `<option value="${escapeHtml(player.id)}">${escapeHtml(player.id)}</option>`
    )).join("");
    select.value = [...select.options].some(option => option.value === previous) ? previous : "all";
  }

  function renderPlayers(players) {
    const root = $("#players");
    if (!players.length) {
      root.innerHTML = `<div class="empty">No players found in this run.</div>`;
      return;
    }
    root.innerHTML = players.map(player => {
      const status = player.status || player;
      const prompt = player.system_prompt || player.archetype_description || "";
      const detailsHref = `context.html?run_id=${encodeURIComponent(params.get("run_id"))}&agent_id=${encodeURIComponent(player.id)}`;
      const inventory = status.inventory !== undefined && status.inventory !== null ? `inventory ${fmtNumber(status.inventory)}` : "";
      const budget = status.budget !== undefined && status.budget !== null ? `budget ${fmtMoney(status.budget)}` : "";
      const price = status.current_price !== undefined && status.current_price !== null ? `price ${fmtMoney(status.current_price)}` : "";
      const bought = status.purchase_price ? `bought ${fmtMoney(status.purchase_price)}` : "";
      return `
        <div class="player-row">
          <div>
            <div class="player-main">
              <span class="agent-id">${escapeHtml(player.id)}</span>
              <span class="badge ${escapeHtml(player.role)}">${escapeHtml(player.role)}</span>
              <span class="prompt-chip" tabindex="0">
                ${escapeHtml(labelize(player.archetype))}
                <span class="prompt-tooltip">
                  <div class="tooltip-title">Archetype and system prompt</div>
                  ${escapeHtml(prompt)}
                </span>
              </span>
            </div>
            <div class="player-meta">
              <span>${escapeHtml(budget || inventory || "state active")}</span>
              <span>${escapeHtml(price || bought || "")}</span>
              <span>${fmtNumber((status.messages_sent ?? player.messages_sent) || 0)} sent</span>
              <span>${fmtNumber((status.messages_received ?? player.messages_received) || 0)} received</span>
              <span>${fmtNumber((player.neighbors || []).length)} contacts</span>
            </div>
          </div>
          <a class="button" href="${detailsHref}">Context</a>
        </div>
      `;
    }).join("");
  }

  function renderMessages(runId, messages, playersById, turn) {
    const filter = $("#message-filter").value || "all";
    const rows = messages
      .filter(message => Number(message.turn || 0) === turn)
      .filter(message => filter === "all" || message.sender === filter || message.recipient === filter)
      .sort((a, b) => Number(a.turn || 0) - Number(b.turn || 0));
    $("#message-count").textContent = `${rows.length} shown`;
    if (!rows.length) {
      $("#messages").innerHTML = `<div class="empty">No messages match this filter at turn ${fmtNumber(turn)}.</div>`;
      return;
    }
    $("#messages").innerHTML = rows.map((message, index) => `
      <div class="message-row">
        <div class="message-route">
          <span>t=${fmtNumber(message.turn)}</span>
          ${actor(message.sender, playersById)}
          <span>-&gt;</span>
          ${actor(message.recipient, playersById)}
          <span class="badge">${escapeHtml(message.action || "MESSAGE")}</span>
        </div>
        <div class="message-content">${escapeHtml(message.content)}</div>
        <div class="message-actions">
          <button type="button" class="inspect-message" data-message-index="${index}">Inspect I/O</button>
        </div>
      </div>
    `).join("");
    $$(".inspect-message").forEach(button => {
      button.addEventListener("click", () => {
        const message = rows[Number(button.dataset.messageIndex || 0)];
        inspectMessageIO(runId, message, playersById);
      });
    });
  }

  async function inspectMessageIO(runId, message, playersById) {
    const root = $("#message-inspector");
    root.className = "";
    root.innerHTML = `<div class="status-line">Loading trace for ${escapeHtml(message.sender)} at turn ${fmtNumber(message.turn)}...</div>`;
    try {
      const trace = await api(`/api/runs/${encodeURIComponent(runId)}/debug/agents/${encodeURIComponent(message.sender)}/turns/${encodeURIComponent(message.turn)}`);
      renderMessageInspector(root, message, trace, playersById);
      root.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (error) {
      root.className = "empty";
      root.textContent = `No trace found for ${message.sender} at turn ${message.turn}: ${error.message || error}`;
    }
  }

  function renderMessageInspector(root, message, trace, playersById) {
    const input = trace.llm_input || {};
    const systemPrompt = input.system_prompt || trace.system_prompt || "";
    const userPrompt = input.user_prompt || trace.user_prompt || "";
    const actionSchema = input.action_schema || trace.action_schema || {};
    root.innerHTML = `
      <div class="stack">
        <div class="message-route">
          <span>Message produced by</span>
          ${actor(message.sender, playersById)}
          <span>at t=${fmtNumber(message.turn)}</span>
          <span>-&gt;</span>
          ${actor(message.recipient, playersById)}
          <span class="badge">${escapeHtml(message.action || "MESSAGE")}</span>
        </div>
        <div class="message-content">${escapeHtml(message.content)}</div>
        ${trace.adapter_error ? `<div class="event-row warn"><div class="message-content">Adapter error: ${escapeHtml(trace.adapter_error)}. This action came from fallback logic.</div></div>` : ""}
        <div class="inspector-grid">
          <div>
            <div class="panel-title">Input: System Prompt</div>
            <pre class="prompt-block">${escapeHtml(systemPrompt)}</pre>
          </div>
          <div>
            <div class="panel-title">Input: User Prompt / Visible History</div>
            <pre class="prompt-block">${escapeHtml(userPrompt)}</pre>
          </div>
          <div>
            <div class="panel-title">Input: Action Schema</div>
            <pre class="prompt-block">${escapeHtml(JSON.stringify(actionSchema, null, 2))}</pre>
          </div>
          <div>
            <div class="panel-title">Input: Local View JSON</div>
            <pre class="prompt-block">${escapeHtml(JSON.stringify(trace.local_view || {}, null, 2))}</pre>
          </div>
          <div>
            <div class="panel-title">Output: Raw LLM / Adapter Decision</div>
            <pre class="prompt-block">${escapeHtml(JSON.stringify(trace.raw_decision || {}, null, 2))}</pre>
          </div>
          <div>
            <div class="panel-title">Output: Sanitized Executed Action</div>
            <pre class="prompt-block">${escapeHtml(JSON.stringify(trace.parsed_action || {}, null, 2))}</pre>
          </div>
        </div>
      </div>
    `;
  }

  function renderSellers(sellers) {
    const rows = Object.entries(sellers);
    if (!rows.length) {
      $("#sellers").innerHTML = `<div class="empty">No sellers in this snapshot.</div>`;
      return;
    }
    $("#sellers").innerHTML = `
      <table class="table">
        <thead><tr><th>Seller</th><th>Archetype</th><th>Price</th><th>Inventory</th><th>Revenue</th></tr></thead>
        <tbody>
          ${rows.map(([id, seller]) => `
            <tr>
              <td>${escapeHtml(id)}</td>
              <td>${escapeHtml(labelize(seller.archetype))}</td>
              <td>${fmtMoney(seller.current_price || seller.final_price)}</td>
              <td>${fmtNumber(seller.final_inventory)} / ${fmtNumber(seller.initial_inventory)}</td>
              <td>${fmtMoney(seller.revenue)}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    `;
  }

  function renderEvents(events, playersById, turn) {
    if (!events.length) {
      $("#events").innerHTML = `<div class="empty">No events logged at turn ${fmtNumber(turn)}.</div>`;
      return;
    }
    $("#events").innerHTML = events.slice().reverse().map(event => {
      const cls = String(event.cls || "");
      const style = cls.includes("buy") ? "buy" : cls.includes("probe") ? "probe" : cls.includes("lie") ? "warn" : "";
      return `
        <div class="event-row ${style}">
          <div class="message-route">
            <span>t=${fmtNumber(event.turn)}</span>
            ${actor(event.from, playersById)}
            ${event.to ? `<span>-&gt;</span>${actor(event.to, playersById)}` : ""}
          </div>
          <div class="message-content">${escapeHtml(event.msg)}</div>
        </div>
      `;
    }).join("");
  }

  function renderTopology(snapshot, playersById, activeMessages = []) {
    const players = Object.values(snapshot.players || playersById || {});
    const matrix = snapshot.comm_matrix || {};
    const ids = players.map(player => player.id);
    if (!ids.length) {
      $("#topology").innerHTML = `<div class="empty">No topology available.</div>`;
      return;
    }
    const width = 720;
    const height = 300;
    const cx = width / 2;
    const cy = height / 2;
    const radius = Math.min(width, height) * 0.36;
    const positions = {};
    ids.forEach((id, index) => {
      const angle = -Math.PI / 2 + (Math.PI * 2 * index) / ids.length;
      positions[id] = {
        x: cx + Math.cos(angle) * radius,
        y: cy + Math.sin(angle) * radius,
      };
    });
    const edges = [];
    ids.forEach((a, i) => {
      ids.slice(i + 1).forEach(b => {
        if (matrix[a]?.[b]) edges.push([a, b]);
      });
    });
    const directed = activeMessages.filter(message => positions[message.sender] && positions[message.recipient]);
    const pairCounts = {};
    directed.forEach(message => {
      const key = `${message.sender}->${message.recipient}`;
      pairCounts[key] = (pairCounts[key] || 0) + 1;
    });
    const pairSeen = {};
    const arrowPaths = directed.map(message => {
      const key = `${message.sender}->${message.recipient}`;
      const index = pairSeen[key] || 0;
      pairSeen[key] = index + 1;
      return arrowPath(positions[message.sender], positions[message.recipient], index, pairCounts[key]);
    });
    $("#topology").innerHTML = `
      <svg class="viz" viewBox="0 0 ${width} ${height}" role="img" aria-label="Communication graph">
        <defs>
          <marker id="arrow-head" markerWidth="7" markerHeight="7" refX="6" refY="2.5" orient="auto" markerUnits="userSpaceOnUse">
            <path d="M0,0 L0,5 L6,2.5 z" fill="#e5b454"></path>
          </marker>
        </defs>
        ${edges.map(([a, b]) => `
          <line x1="${positions[a].x}" y1="${positions[a].y}" x2="${positions[b].x}" y2="${positions[b].y}" stroke="#38424b" stroke-width="1.2" />
        `).join("")}
        ${arrowPaths.map(path => `
          <path d="${path}" fill="none" stroke="#e5b454" stroke-width="1.6" stroke-linecap="round" marker-end="url(#arrow-head)" opacity="0.78" />
        `).join("")}
        ${ids.map(id => {
          const p = positions[id];
          const role = roleFor(id, playersById);
          const color = role === "seller" ? "#f08a68" : "#6aa7ff";
          return `
            <circle cx="${p.x}" cy="${p.y}" r="12" fill="${color}" stroke="#0d1012" stroke-width="3" />
            <text x="${p.x}" y="${p.y + 29}" text-anchor="middle" fill="#dce3e7" font-size="11">${escapeHtml(id)}</text>
          `;
        }).join("")}
      </svg>
    `;
  }

  function arrowPath(from, to, index, count) {
    const dx = to.x - from.x;
    const dy = to.y - from.y;
    const len = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
    const ux = dx / len;
    const uy = dy / len;
    const nx = -uy;
    const ny = ux;
    const offset = (index - (count - 1) / 2) * 6;
    const start = {
      x: from.x + ux * 15 + nx * offset,
      y: from.y + uy * 15 + ny * offset,
    };
    const end = {
      x: to.x - ux * 16 + nx * offset,
      y: to.y - uy * 16 + ny * offset,
    };
    const curve = Math.min(22, len * 0.10) + Math.abs(offset);
    const mid = {
      x: (start.x + end.x) / 2 + nx * curve,
      y: (start.y + end.y) / 2 + ny * curve,
    };
    return `M${start.x.toFixed(1)},${start.y.toFixed(1)} Q${mid.x.toFixed(1)},${mid.y.toFixed(1)} ${end.x.toFixed(1)},${end.y.toFixed(1)}`;
  }

  function renderHeatmap(messages, players) {
    const root = $("#heatmap");
    if (!root) return;
    const ids = players.map(player => player.id);
    const counts = {};
    let max = 0;
    messages.forEach(message => {
      const sender = message.sender;
      const recipient = message.recipient;
      if (!sender || !recipient) return;
      counts[sender] = counts[sender] || {};
      counts[sender][recipient] = (counts[sender][recipient] || 0) + 1;
      max = Math.max(max, counts[sender][recipient]);
    });
    $("#heatmap-status").textContent = `${fmtNumber(messages.length)} total directed messages`;
    if (!ids.length || !messages.length) {
      root.innerHTML = `<div class="empty">No communication to summarize yet.</div>`;
      return;
    }
    root.innerHTML = `
      <div class="heatmap-wrap">
        <table class="heatmap-table">
          <thead>
            <tr>
              <th>Sender \\ Recipient</th>
              ${ids.map(id => `<th>${escapeHtml(id)}</th>`).join("")}
            </tr>
          </thead>
          <tbody>
            ${ids.map(sender => `
              <tr>
                <td>${escapeHtml(sender)}</td>
                ${ids.map(recipient => {
                  const value = counts[sender]?.[recipient] || 0;
                  const alpha = value ? 0.18 + 0.72 * value / Math.max(max, 1) : 0;
                  const bg = value ? `background: rgba(45, 212, 191, ${alpha.toFixed(2)});` : "";
                  return `<td class="heatmap-cell" style="${bg}" title="${escapeHtml(sender)} -> ${escapeHtml(recipient)}: ${fmtNumber(value)}">${value || ""}</td>`;
                }).join("")}
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    `;
  }

  function renderPriceChart(snapshot) {
    const points = snapshot.prices_over_time || [];
    const sellers = Object.keys(snapshot.sellers || {});
    if (!points.length || !sellers.length) {
      $("#price-chart").innerHTML = `<div class="empty">No price history yet.</div>`;
      return;
    }
    const keys = sellers.map((_, index) => String.fromCharCode(97 + index));
    const values = points.flatMap(point => keys.map(key => Number(point[key])).filter(Number.isFinite));
    const min = Math.min(...values);
    const max = Math.max(...values);
    const width = 720;
    const height = 260;
    const pad = 38;
    const x = index => pad + (points.length === 1 ? 0 : index * (width - pad * 2) / (points.length - 1));
    const y = value => {
      if (max === min) return height / 2;
      return height - pad - (value - min) * (height - pad * 2) / (max - min);
    };
    const colors = ["#2dd4bf", "#6aa7ff", "#e5b454", "#ee7777", "#72d391"];
    const lines = keys.map((key, index) => {
      const coords = points
        .map((point, i) => [x(i), y(Number(point[key]))])
        .filter(([, py]) => Number.isFinite(py))
        .map(pair => pair.join(","))
        .join(" ");
      return `<polyline points="${coords}" fill="none" stroke="${colors[index % colors.length]}" stroke-width="2.5" />`;
    }).join("");
    const legend = sellers.map((seller, index) => `
      <g transform="translate(${pad + index * 135}, 18)">
        <rect width="10" height="10" fill="${colors[index % colors.length]}" rx="2" />
        <text x="16" y="10" fill="#cdd5da" font-size="11">${escapeHtml(seller)}</text>
      </g>
    `).join("");
    $("#price-chart").innerHTML = `
      <svg class="viz" viewBox="0 0 ${width} ${height}" role="img" aria-label="Seller price history">
        ${legend}
        <line x1="${pad}" y1="${height - pad}" x2="${width - pad}" y2="${height - pad}" stroke="#343b43" />
        <line x1="${pad}" y1="${pad}" x2="${pad}" y2="${height - pad}" stroke="#343b43" />
        <text x="6" y="${y(max) + 4}" fill="#8f9aa3" font-size="11">${fmtMoney(max)}</text>
        <text x="6" y="${y(min) + 4}" fill="#8f9aa3" font-size="11">${fmtMoney(min)}</text>
        ${lines}
      </svg>
    `;
  }

  async function initContextPage() {
    renderNav("runs");
    const runId = params.get("run_id");
    if (!runId) {
      $("#context-root").innerHTML = `<div class="empty">Missing run_id. Open a run from the past runs page.</div>`;
      return;
    }
    $("#back-run").href = `run.html?run_id=${encodeURIComponent(runId)}`;
    try {
      const context = await api(`/api/runs/${encodeURIComponent(runId)}/context`);
      renderContextPage(context, params.get("agent_id"));
    } catch (error) {
      showError("#context-status", error);
    }
  }

  function renderContextPage(context, requestedAgentId) {
    const run = context.run || {};
    const players = context.players || [];
    let selected = players.find(player => player.id === requestedAgentId) || players[0];
    $("#context-title").textContent = `${labelize(run.scenario_id)} / Context`;
    $("#context-subtitle").textContent = `${run.run_id} at turn ${fmtNumber(context.turn)}. Select an agent to inspect the prompt and local view it receives.`;
    $("#context-status").innerHTML = `${statusBadge(run.status)} <span class="muted">${escapeHtml(run.llm_provider)} / ${escapeHtml(run.model)}</span>`;

    const renderSelected = player => {
      selected = player;
      history.replaceState(null, "", `context.html?run_id=${encodeURIComponent(run.run_id)}&agent_id=${encodeURIComponent(player.id)}`);
      $$(".agent-nav button").forEach(button => button.classList.toggle("active", button.dataset.agentId === player.id));
      $("#selected-agent").innerHTML = `
        <div class="panel-head">
          <div>
            <div class="panel-title">${escapeHtml(player.id)}</div>
            <div class="muted">${escapeHtml(player.role)} / ${escapeHtml(labelize(player.archetype))}</div>
          </div>
          <span class="badge ${escapeHtml(player.role)}">${escapeHtml(player.role)}</span>
        </div>
        <div class="panel-body stack">
          <div class="split">
            <div>
              <div class="panel-title">Goal</div>
              <pre class="prompt-block">${escapeHtml(JSON.stringify(player.goal || {}, null, 2))}</pre>
            </div>
            <div>
              <div class="panel-title">Constraints and Contacts</div>
              <pre class="prompt-block">${escapeHtml(JSON.stringify({
                constraints: player.constraints || {},
                actions: player.actions || [],
                neighbors: player.neighbors || [],
              }, null, 2))}</pre>
            </div>
          </div>
          <div>
            <div class="panel-title">System Prompt</div>
            <pre class="prompt-block">${escapeHtml(player.system_prompt)}</pre>
          </div>
          <div>
            <div class="panel-title">User Prompt</div>
            <pre class="prompt-block">${escapeHtml(player.user_prompt)}</pre>
          </div>
          <div>
            <div class="panel-title">Local View JSON</div>
            <pre class="prompt-block">${escapeHtml(JSON.stringify(player.local_view || {}, null, 2))}</pre>
          </div>
          <div class="panel">
            <div class="panel-head">
              <div class="panel-title">Decision Trace</div>
              <div class="actions">
                <input id="trace-turn" type="number" min="1" max="${escapeHtml(run.current_turn || context.turn || 1)}" value="${escapeHtml(run.current_turn || context.turn || 1)}" style="width: 92px;">
                <button id="load-trace" type="button">Load Trace</button>
              </div>
            </div>
            <div class="panel-body">
              <pre id="trace-output" class="prompt-block">Select a turn with an agent decision trace.</pre>
            </div>
          </div>
        </div>
      `;
      $("#load-trace").addEventListener("click", () => loadTrace(run.run_id, player.id));
    };

    $("#agent-nav").innerHTML = players.map(player => `
      <button type="button" data-agent-id="${escapeHtml(player.id)}" class="${selected && selected.id === player.id ? "active" : ""}">
        <span>${escapeHtml(player.id)}</span>
        <span class="badge ${escapeHtml(player.role)}" style="margin-left:auto;">${escapeHtml(player.role)}</span>
      </button>
    `).join("");
    $$("#agent-nav button").forEach(button => {
      button.addEventListener("click", () => {
        const player = players.find(candidate => candidate.id === button.dataset.agentId);
        if (player) renderSelected(player);
      });
    });

    if (selected) renderSelected(selected);
  }

  async function loadTrace(runId, agentId) {
    const turn = Number($("#trace-turn").value || 1);
    const output = $("#trace-output");
    output.textContent = "Loading trace...";
    try {
      const trace = await api(`/api/runs/${encodeURIComponent(runId)}/debug/agents/${encodeURIComponent(agentId)}/turns/${encodeURIComponent(turn)}`);
      output.textContent = JSON.stringify(trace, null, 2);
    } catch (error) {
      output.textContent = `No trace for ${agentId} at turn ${turn}: ${error.message || error}`;
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    const page = document.body.dataset.page;
    if (page === "runs") initRunsPage();
    if (page === "create") initCreatePage();
    if (page === "run") initRunPage();
    if (page === "context") initContextPage();
  });
})();
