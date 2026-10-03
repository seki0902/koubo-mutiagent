// agent.js - Task 8.1 chat shell: send a message, show both sides of the
// conversation, and keep one session_id across turns.
//
// The Agent stream is consumed here and nowhere else in this page.  Tasks 8.2
// and 8.4 add topic cards and the working-state indicator on top of the same
// event handler; neither of them re-reads the assistant text to guess state.

(function () {
  'use strict';

  var SESSION_KEY = 'koubo_agent_session_id';
  var ENDPOINT = '/api/agent/chat/stream';

  var log = document.getElementById('agentLog');
  var form = document.getElementById('agentForm');
  var input = document.getElementById('agentInput');
  var sendButton = document.getElementById('btnSend');
  var sessionLabel = document.getElementById('agentSessionLabel');
  var hint = document.getElementById('agentHint');
  var newSessionButton = document.getElementById('btnNewSession');
  var sideBody = document.getElementById('agentSideBody');
  var refreshPanelButton = document.getElementById('btnRefreshPanel');
  var activityStrip = document.getElementById('agentActivity');

  // ─── working state (Task 8.4) ──────────────────────────────────────────
  //
  // Five fixed slots, updated only by agent_status / tool_started /
  // tool_finished events.  The assistant text is never parsed for state: a
  // sentence that happens to contain the word 审核 must not start an
  // animation.

  var AGENT_SLOTS = [
    { key: 'topic', label: '选题' },
    { key: 'research', label: '核验' },
    { key: 'script', label: '写稿' },
    { key: 'review', label: '审核' },
    { key: 'generation', label: '数字人' }
  ];

  var agentSlots = {};

  function buildActivityStrip() {
    if (!activityStrip) return;
    activityStrip.innerHTML = '';
    AGENT_SLOTS.forEach(function (slot) {
      var node = document.createElement('span');
      node.className = 'agent-activity-idle';
      node.innerHTML =
        '<span class="agent-activity-dot"></span>' +
        '<span class="agent-activity-label"></span>' +
        '<span class="agent-activity-tool"></span>';
      node.querySelector('.agent-activity-label').textContent = slot.label;
      activityStrip.appendChild(node);
      agentSlots[slot.key] = {
        node: node,
        tool: node.querySelector('.agent-activity-tool')
      };
    });
  }

  function setActivity(agent, status, message, tool) {
    var slot = agentSlots[agent];
    if (!slot) return;
    slot.node.className =
      status === 'working' ? 'agent-activity-working'
      : status === 'error' ? 'agent-activity-error'
      : 'agent-activity-done';
    slot.node.title = message || '';
    if (tool !== undefined) slot.tool.textContent = tool || '';
  }

  function resetActivity() {
    Object.keys(agentSlots).forEach(function (key) {
      agentSlots[key].node.className = 'agent-activity-idle';
      agentSlots[key].node.title = '';
      agentSlots[key].tool.textContent = '';
    });
  }

  // One streamed turn is rendered at a time; the button stays disabled until
  // the turn settles, so a second POST cannot interleave with the first.
  var streaming = false;

  function newID(prefix) {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      return prefix + window.crypto.randomUUID().replace(/-/g, '');
    }
    return prefix + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
  }

  function loadSessionID() {
    var existing = '';
    try { existing = window.localStorage.getItem(SESSION_KEY) || ''; } catch (e) { existing = ''; }
    if (existing.trim()) return existing.trim();
    var created = newID('sess_');
    try { window.localStorage.setItem(SESSION_KEY, created); } catch (e) { /* private mode */ }
    return created;
  }

  var sessionID = loadSessionID();

  function showSession() {
    sessionLabel.textContent = sessionID;
    sessionLabel.title = '当前会话 ID：' + sessionID;
  }

  function setHint(text, isError) {
    hint.textContent = text;
    hint.className = isError ? 'agent-hint agent-hint-error' : 'agent-hint';
  }

  function scrollToBottom() {
    log.scrollTop = log.scrollHeight;
  }

  function addBubble(role, text) {
    var row = document.createElement('div');
    row.className = 'agent-row agent-row-' + role;
    var bubble = document.createElement('div');
    bubble.className = 'agent-bubble agent-bubble-' + role;
    // textContent, never innerHTML: assistant text is model output.
    bubble.textContent = text || '';
    row.appendChild(bubble);
    log.appendChild(row);
    scrollToBottom();
    return bubble;
  }

  function appendText(bubble, text) {
    if (!text) return;
    bubble.textContent += text;
    scrollToBottom();
  }

  function renderTranscript(entries) {
    // A refresh opens an empty log, and the conversation lives in the thread on
    // the server: read it back instead of losing it with the page. Only fills a
    // log that is still empty, so it can never duplicate a live exchange.
    if (!Array.isArray(entries) || !entries.length) return;
    if (log.dataset.transcriptRendered === '1' || log.children.length > 0) return;
    entries.forEach(function (entry) {
      addBubble(entry.role === 'user' ? 'user' : 'assistant', entry.text);
    });
    log.dataset.transcriptRendered = '1';
  }

  function setBusy(busy) {
    streaming = busy;
    sendButton.disabled = busy;
    sendButton.textContent = busy ? '生成中…' : '发送';
  }

  function autoGrow() {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 160) + 'px';
  }

  function requestIDFor() {
    return newID('req_');
  }

  // ─── event handling ────────────────────────────────────────────────────
  //
  // Handled event types are the Task 6.1 contract, by wire name.  Everything
  // else is ignored rather than treated as an error: an unknown frame from a
  // newer service must not break a running turn.

  // ─── topic cards (Task 8.2) ────────────────────────────────────────────
  //
  // The cards are rendered from the event payload, never from the assistant
  // text and never by renumbering anything here: display_index and topic_id
  // are the ones the business store committed.  Selecting a card therefore
  // sends the same "第 N 个" reference a user would type, so both routes land
  // on the same persisted display_index.

  function renderCards(view, payload) {
    var cards = payload.candidates || [];
    view.batchId = payload.candidate_batch_id || view.batchId;
    if (!cards.length) return;

    var wrap = document.createElement('div');
    wrap.className = 'agent-cards';
    if (payload.summary) {
      var summary = document.createElement('div');
      summary.className = 'agent-cards-summary';
      summary.textContent = payload.summary;
      wrap.appendChild(summary);
    }

    cards.forEach(function (card) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'agent-card';
      button.setAttribute('data-topic-id', card.topic_id || '');
      button.setAttribute('data-candidate-batch-id', payload.candidate_batch_id || '');
      button.setAttribute('data-display-index', String(card.display_index));

      var index = document.createElement('span');
      index.className = 'agent-card-index';
      index.textContent = String(card.display_index);
      button.appendChild(index);

      var title = document.createElement('span');
      title.className = 'agent-card-title';
      title.textContent = card.title || '';
      button.appendChild(title);

      button.addEventListener('click', function () {
        // Off while a turn runs, and off once a newer batch has been shown, so
        // a click can never resolve against a batch the user is not looking at.
        if (streaming || view.batchId !== payload.candidate_batch_id) return;
        send('第' + card.display_index + '个');
      });

      wrap.appendChild(button);
    });

    log.appendChild(wrap);
    scrollToBottom();
  }

  //: Shown in the assistant bubble while the turn works. A topic turn spends
  //: most of its time inside the search capability before any text exists, and
  //: an empty bubble during that minute reads as "nothing happened" — which is
  //: how a working turn got refreshed away.
  var WAITING_TEXT = '正在处理…（选题要联网搜索，通常 1 分钟左右，请不要刷新）';

  function renderThinking(view, text) {
    // The model's reasoning, shown as progress rather than as the answer: it is
    // what arrives while the turn is still searching, and a collapsed block
    // keeps it out of the transcript the user reads.
    if (!view.thinking) {
      var block = document.createElement('div');
      block.className = 'agent-thinking';
      var head = document.createElement('div');
      head.className = 'agent-thinking-head';
      head.textContent = '思考过程（点击折叠/展开）';
      var body = document.createElement('div');
      body.className = 'agent-thinking-body';
      block.appendChild(head);
      block.appendChild(body);
      head.addEventListener('click', function () {
        block.classList.toggle('agent-thinking-collapsed');
      });
      log.appendChild(block);
      view.thinking = body;
      clearWaiting(view);
    }
    view.thinking.textContent += text;
    scrollToBottom();
  }

  function clearWaiting(view) {
    if (!view.waitingText) return;
    view.waitingText = false;
    view.assistant.textContent = '';
    view.assistant.classList.remove('agent-bubble-waiting');
  }

  function setWaitingText(view, message) {
    if (!view.waitingText || !message) return;
    view.assistant.textContent = message + '…';
  }

  function handleEvent(event, view) {
    var payload = event.payload || {};
    switch (event.type) {
      case 'message_chunk':
        clearWaiting(view);
        appendText(view.assistant, payload.text || '');
        break;
      case 'message_thinking':
        renderThinking(view, payload.text || '');
        break;
      case 'message_completed':
        // The closing text is authoritative: it is the same turn's full
        // assistant message, so a lost chunk cannot leave the bubble short.
        clearWaiting(view);
        if (typeof payload.text === 'string' && payload.text) {
          view.assistant.textContent = payload.text;
        }
        view.sawText = true;
        break;
      case 'error':
        clearWaiting(view);
        view.failed = true;
        addBubble('error', (payload.error_code || 'ERROR') + '：' + (payload.message || ''));
        break;
      case 'waiting_user':
        view.waiting = true;
        break;
      case 'agent_finished':
        view.finished = true;
        break;
      case 'agent_status':
        setActivity(payload.agent, payload.status, payload.message);
        setWaitingText(view, payload.message);
        break;
      case 'tool_started':
        setActivity(payload.agent, 'working', payload.message, payload.tool);
        setWaitingText(view, payload.message || '正在调用工具');
        break;
      case 'tool_finished':
        setActivity(payload.agent, 'finished', payload.message, '');
        break;
      case 'candidate_batch_updated':
        view.state.candidateBatchId = payload.candidate_batch_id || view.state.candidateBatchId;
        renderCards(view, payload);
        break;
      case 'draft_updated':
        view.state.draftId = payload.draft_id || view.state.draftId;
        view.state.draftVersion = payload.draft_version || view.state.draftVersion;
        break;
      case 'review_updated':
        view.state.reviewId = payload.review_id || view.state.reviewId;
        view.state.reviewStatus = payload.status || view.state.reviewStatus;
        break;
      case 'generation_started':
        view.state.draftId = payload.draft_id || view.state.draftId;
        view.state.draftVersion = payload.draft_version || view.state.draftVersion;
        break;
      case 'generation_completed':
        view.state.generationId = payload.generation_id || view.state.generationId;
        view.state.generationStatus = payload.status || view.state.generationStatus;
        view.state.providerTaskId = payload.provider_task_id || view.state.providerTaskId;
        break;
      default:
        break;
    }
  }

  // ─── submitted video tasks ─────────────────────────────────────────────
  //
  // One read, never a poll loop: the list is fetched when the user clicks 刷新
  // and once after a turn that could have submitted something. The Agent's
  // submissions live in this app's own task list, not in the tool's.

  var tasksBody = document.getElementById('agentTasksBody');
  var refreshTasksButton = document.getElementById('btnRefreshTasks');

  function renderTasks(tasks) {
    tasksBody.innerHTML = '';
    if (!Array.isArray(tasks) || !tasks.length) {
      tasksBody.appendChild(element('div', 'agent-side-empty', '这个应用还没有提交过视频任务'));
      return;
    }
    tasks.slice(0, 5).forEach(function (task) {
      var block = element('div', 'agent-side-block');
      block.appendChild(element('div', 'agent-side-label', String(task.task_id || '')));
      var status = String(task.status || 'unknown');
      block.appendChild(badge(status, status));
      if (task.video_url) {
        var link = document.createElement('a');
        link.className = 'agent-task-link';
        link.href = String(task.video_url);
        link.target = '_blank';
        link.textContent = '打开成片';
        block.appendChild(link);
      }
      if (task.error) {
        block.appendChild(element('div', 'agent-side-error', String(task.error)));
      }
      tasksBody.appendChild(block);
    });
  }

  async function refreshTasks() {
    try {
      var response = await fetch('/api/tasks', { headers: { 'Accept': 'application/json' } });
      if (!response.ok) {
        tasksBody.innerHTML = '';
        tasksBody.appendChild(element('div', 'agent-side-error', '读取任务失败：HTTP ' + response.status));
        return;
      }
      var payload = await response.json();
      renderTasks(payload && payload.tasks);
    } catch (e) {
      tasksBody.innerHTML = '';
      tasksBody.appendChild(element('div', 'agent-side-error', '读取任务失败：' + e.message));
    }
  }

  refreshTasksButton.addEventListener('click', refreshTasks);

  // ─── model settings ────────────────────────────────────────────────────
  //
  // There is deliberately no model form on this page. The settings page owns the
  // shared model section, and the Agent service reads that section for its own
  // roles, so a second form here would only let the two disagree.

  // ─── right panel (Task 8.3) ────────────────────────────────────────────
  //
  // The panel is rendered from the backend snapshot only.  Nothing on this
  // page accumulates panel state across turns, which is what makes a refresh
  // show the same thing: the values never lived in the browser to begin with.

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function block(label, valueNode) {
    var wrap = element('div', 'agent-side-block');
    wrap.appendChild(element('div', 'agent-side-label', label));
    wrap.appendChild(valueNode);
    return wrap;
  }

  function textBlock(label, text, className) {
    return block(label, element('div', className || 'agent-side-value', text));
  }

  function badge(text, status) {
    return element('span', 'agent-side-badge ' + (status || ''), text);
  }

  function listBlock(label, items) {
    var list = element('ul', 'agent-side-list');
    items.forEach(function (item) { list.appendChild(element('li', '', item)); });
    return block(label, list);
  }

  function renderPanel(snapshot) {
    sideBody.innerHTML = '';

    var topic = snapshot.topic;
    if (topic) {
      var topicValue = element('div', 'agent-side-value');
      topicValue.appendChild(element('div', '', topic.title));
      topicValue.appendChild(
        element('div', 'agent-side-meta', '第 ' + topic.display_index + ' 个 · ' + topic.topic_id)
      );
      sideBody.appendChild(block('当前选题', topicValue));
      if (topic.content_angle) {
        sideBody.appendChild(textBlock('切入角度', topic.content_angle));
      }
    } else {
      sideBody.appendChild(textBlock('当前选题', '尚未确认选题'));
    }

    var draft = snapshot.draft;
    if (draft && draft.current_version) {
      sideBody.appendChild(
        block(
          '当前稿件版本',
          element(
            'div',
            'agent-side-value',
            'v' + draft.current_version + '（共 ' + draft.version_count + ' 版）'
          )
        )
      );
      sideBody.appendChild(
        textBlock('稿件正文', draft.body || '（空）', 'agent-side-body-text')
      );
    } else {
      sideBody.appendChild(textBlock('当前稿件版本', '尚无稿件'));
    }

    var review = snapshot.review;
    if (review) {
      var reviewValue = element('div', 'agent-side-value');
      reviewValue.appendChild(badge(review.status + '（v' + review.draft_version + '）', review.status));
      if (review.summary) {
        reviewValue.appendChild(element('div', 'agent-side-meta', review.summary));
      }
      sideBody.appendChild(block('审核状态', reviewValue));
      if (review.must_fix && review.must_fix.length) {
        sideBody.appendChild(listBlock('必须修改', review.must_fix));
      }
      if (review.risk_items && review.risk_items.length) {
        sideBody.appendChild(listBlock('风险项', review.risk_items));
      }
    } else {
      sideBody.appendChild(textBlock('审核状态', '尚未审核'));
    }

    var generation = snapshot.generation;
    if (generation) {
      // The provider view wins when it is present, because a render can finish
      // long after the submission was recorded.
      var provider = generation.provider || null;
      var shownStatus = provider ? provider.status : (generation.status || 'pending');
      var generationValue = element('div', 'agent-side-value');
      generationValue.appendChild(badge(shownStatus, shownStatus));
      generationValue.appendChild(
        element('div', 'agent-side-meta', 'v' + generation.draft_version + ' · ' + generation.generation_id)
      );
      if (provider) {
        generationValue.appendChild(
          element(
            'div',
            'agent-side-meta',
            '进度 ' + (provider.progress || 0) + '% · 本地状态 ' + (provider.raw_status || '—')
          )
        );
        if (provider.error) {
          generationValue.appendChild(element('div', 'agent-side-error', provider.error));
        }
      }
      if (generation.provider_task_id) {
        generationValue.appendChild(
          element('div', 'agent-side-meta', 'provider: ' + generation.provider_task_id)
        );
      }
      sideBody.appendChild(block('生成状态', generationValue));

      if (provider && provider.video_url) {
        var videoWrap = element('div', 'agent-side-value');
        var link = document.createElement('a');
        link.className = 'agent-side-video-link';
        link.href = provider.video_url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.textContent = '打开视频结果';
        videoWrap.appendChild(link);
        sideBody.appendChild(block('视频结果', videoWrap));
        var video = document.createElement('video');
        video.className = 'agent-side-video';
        video.controls = true;
        video.preload = 'metadata';
        video.src = provider.video_url;
        sideBody.appendChild(video);
      } else if (provider && provider.status === 'completed') {
        sideBody.appendChild(textBlock('视频结果', 'provider 已完成，但未返回视频地址'));
      }

      var history = generation.history || [];
      if (history.length > 1) {
        var historyWrap = element('div', 'agent-side-history');
        history.forEach(function (record) {
          var row = element('div', 'agent-side-history-row');
          row.appendChild(badge(record.status, record.status));
          row.appendChild(element('span', '', 'v' + record.draft_version + ' · ' + record.generation_id));
          historyWrap.appendChild(row);
        });
        sideBody.appendChild(block('历史提交', historyWrap));
      }
    } else {
      sideBody.appendChild(textBlock('生成状态', '尚未提交数字人'));
    }
  }

  async function refreshPanel(options) {
    var askProvider = !!(options && options.provider);
    var url = '/api/agent/session/' + encodeURIComponent(sessionID) + (askProvider ? '?refresh=1' : '');
    try {
      var response = await fetch(url, {
        headers: { 'Accept': 'application/json' }
      });
      if (response.status === 404) {
        // A session that has not had a turn yet has no task row: an empty panel
        // is the correct answer, not an error.
        sideBody.innerHTML = '';
        sideBody.appendChild(element('div', 'agent-side-empty', '这个会话还没有任务，先发一条消息。'));
        return;
      }
      if (!response.ok) {
        var detail = '';
        try {
          var parsed = JSON.parse(await response.text());
          detail = parsed.message || parsed.error_code || '';
        } catch (e) { detail = ''; }
        sideBody.innerHTML = '';
        sideBody.appendChild(
          element('div', 'agent-side-error', '读取当前任务失败：' + (detail || ('HTTP ' + response.status)))
        );
        return;
      }
      var snapshot = await response.json();
      renderPanel(snapshot);
      renderTranscript(snapshot.transcript);
    } catch (e) {
      sideBody.innerHTML = '';
      sideBody.appendChild(element('div', 'agent-side-error', '读取当前任务失败：' + e.message));
    }
  }

  // ─── SSE parsing ───────────────────────────────────────────────────────

  function framesFrom(buffer) {
    var frames = [];
    var separator = /\r?\n\r?\n/;
    var match = separator.exec(buffer);
    while (match) {
      var raw = buffer.slice(0, match.index);
      buffer = buffer.slice(match.index + match[0].length);
      var joined = raw.split(/\r?\n/).map(function (line) {
        return line.charAt(0) === ':' ? '' : line.replace(/^data:\s?/, '');
      }).join('\n').trim();
      if (joined) frames.push(joined);
      match = separator.exec(buffer);
    }
    return { frames: frames, rest: buffer };
  }

  function closeView(view) {
    clearWaiting(view);
    if (!view.sawText && !view.failed && !view.assistant.textContent) {
      // A turn that produced nothing at all is reported rather than left as a
      // silently empty bubble.
      addBubble('error', 'INTERNAL_ERROR：本轮没有返回任何内容');
    } else if (!view.sawText && view.assistant.textContent) {
      // Partial text that never got its closing frame: keep it visible, but
      // say that it is unfinished rather than passing it off as complete.
      view.assistant.textContent += ' …（本轮未正常结束）';
    }
  }

  async function send(message) {
    addBubble('user', message);
    var assistant = addBubble('assistant', WAITING_TEXT);
    assistant.classList.add('agent-bubble-waiting');
    resetActivity();
    var view = {
      assistant: assistant,
      waitingText: true,
      sawText: false,
      failed: false,
      waiting: false,
      finished: false,
      batchId: '',
      state: {}
    };

    setBusy(true);
    setHint('Agent 正在处理…', false);

    var response;
    try {
      response = await fetch(ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
        body: JSON.stringify({
          session_id: sessionID,
          message: message,
          request_id: requestIDFor()
        })
      });
    } catch (e) {
      setBusy(false);
      addBubble('error', 'CAPABILITY_UNAVAILABLE：无法连接本地服务（' + e.message + '）');
      setHint('连接失败', true);
      return;
    }

    if (!response.ok) {
      var detail = '';
      try { detail = await response.text(); } catch (e) { detail = ''; }
      var message2 = 'HTTP ' + response.status;
      try {
        var parsed = JSON.parse(detail);
        message2 = (parsed.error_code || message2) + '：' + (parsed.message || '');
      } catch (e) {
        if (detail.trim()) message2 = detail.trim().slice(0, 300);
      }
      addBubble('error', message2);
      closeView(view);
      setBusy(false);
      setHint('本轮失败', true);
      return;
    }

    var reader = response.body.getReader();
    var decoder = new TextDecoder('utf-8');
    var buffer = '';

    try {
      while (true) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        var parsedFrame = framesFrom(buffer);
        buffer = parsedFrame.rest;
        parsedFrame.frames.forEach(function (frame) {
          var event;
          try { event = JSON.parse(frame); } catch (e) { return; }
          handleEvent(event, view);
        });
      }
    } catch (e) {
      view.failed = true;
      addBubble('error', 'STREAM_INTERRUPTED：' + e.message);
    }

    closeView(view);
    setBusy(false);
    setHint(view.failed ? '本轮失败' : '已就绪', view.failed);
    // The panel is re-read rather than patched from this turn's events, so the
    // live view and a refreshed page always come from the same source.
    await refreshPanel();
    // A turn may just have submitted a video: one read, not a poll loop.
    refreshTasks();
  }

  function submit() {
    if (streaming) return;
    var message = input.value.trim();
    if (!message) {
      setHint('请先输入内容', true);
      return;
    }
    input.value = '';
    autoGrow();
    send(message);
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    submit();
  });

  input.addEventListener('keydown', function (event) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  });

  input.addEventListener('input', autoGrow);

  newSessionButton.addEventListener('click', function () {
    if (streaming) return;
    sessionID = newID('sess_');
    try { window.localStorage.setItem(SESSION_KEY, sessionID); } catch (e) { /* private mode */ }
    log.innerHTML = '';
    showSession();
    setHint('已开始新会话', false);
    refreshPanel();
    input.focus();
  });

  refreshPanelButton.addEventListener('click', function () {
    // The explicit refresh is the one action that asks the provider, so a slow
    // or unreachable provider is something the user chose to wait for.
    refreshPanel({ provider: true });
  });

  buildActivityStrip();
  showSession();
  setHint('已就绪', false);
  refreshPanel();
  input.focus();
  autoGrow();
})();
