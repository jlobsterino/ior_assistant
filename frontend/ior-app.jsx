/* ior-app.jsx — ИОР-помощник: подключение к реальному backend через SSE/WebSocket
 *
 * ПРИМЕЧАНИЕ (при транскрибации с фото):
 * Этот файл восстановлен построчно по номерам строк, видимым на 20 фото экрана.
 * Некоторые фрагменты в правой части экрана могли быть обрезаны кадром —
 * такие места стоит перепроверить в оригинале. Компоненты TweaksPanel,
 * TweakSection, TweakToggle, TweakRadio, useTweaks, SkillsModal, UserMessage,
 * AssistantMessage и CreditCalcForm объявлены в других файлах проекта
 * (skills-modal.jsx, messages.jsx, credit-form.jsx — видны во вкладках на
 * скриншотах, но их код не попал в кадр) и здесь не восстановлены.
 */

const { useState, useEffect, useRef } = React;
const { SKILLS: FALLBACK_SKILLS, fmtMs } = window.IOR_DATA;

const TWEAK_DEFAULTS = window.__IOR_DEFAULTS;

function useApplyTweaks(t) {
  useEffect(() => {
    const html = document.documentElement;
    html.setAttribute('data-accent', t.accent);
    html.setAttribute('data-theme', t.dark ? 'dark' : 'light');
    html.setAttribute('data-density', t.density);
  }, [t.accent, t.dark, t.density]);
}

const nowTime = () => {
  const d = new Date();
  return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
};

/* —— API ——————————————————————————————————————————————
   Все URL — относительные к base path: пишем `api/...` без ведущего `/`.
   При работе за reverse-proxy (JupyterHub/DataLab) браузер видит
   /user/<u>/proxy/<port>/, и относительный fetch попадёт в корректный
   путь автоматически.
—————————————————————————————————————————————————————— */

// База от текущей страницы. Гарантируем trailing slash.
const __BASE = (() => {
  let p = window.location.pathname || '/';
  if (!p.endsWith('/')) p += '/';
  return p;
})();
window.__IOR_BASE = __BASE;

// Хелпер: api('/api/skills') → '/user/.../proxy/8000/api/skills'
const api = (path) => __BASE + path.replace(/^\//, '');
window.iorApi = api;

// Сохранённые отчёты (П5) — доступны из messages.jsx через window
window.iorSaveReport = async (name, spec, query) => {
  const r = await fetch(api('/api/reports'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, spec, query }),
  });
  return r.ok ? r.json() : null;
};

window.iorListReports = async () => {
  try {
    const r = await fetch(api('/api/reports'));
    return r.ok ? (await r.json()).items : [];
  } catch (e) {
    return [];
  }
};

// Обзор базы знаний (П7)
window.iorExploreSchema = async () => {
  try {
    const r = await fetch(api('/api/explore/schema'));
    return r.ok ? (await r.json()).tables : [];
  } catch (e) {
    return [];
  }
};

window.iorExploreValues = async (table, column, contains) => {
  let u = api(`/api/explore/values?table=${encodeURIComponent(table)}&column=${encodeURIComponent(column)}`);
  if (contains) u += `&contains=${encodeURIComponent(contains)}`;
  try {
    const r = await fetch(u);
    return r.ok ? await r.json() : null;
  } catch (e) {
    return null;
  }
};

const API = {
  async listSkills() {
    const r = await fetch(api('/api/skills'));
    return r.ok ? r.json() : { skills: [] };
  },

  async listSessions() {
    const r = await fetch(api('/api/sessions'));
    return r.ok ? r.json() : { sessions: [] };
  },

  async getSession(id) {
    const r = await fetch(api(`/api/sessions/${id}`));
    return r.ok ? r.json() : null;
  },

  async createSession() {
    const r = await fetch(api('/api/chat/sessions'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    return r.json();
  },

  async deleteSession(id) {
    return fetch(api(`/api/sessions/${id}`), { method: 'DELETE' });
  },

  /**
   * Стримит ответ на сообщение через WebSocket (default для prod / DataLab).
   *
   * Почему WebSocket, а не SSE: корп. WAF Сбера буферит HTTP-responses
   * целиком (см. backend/api/routes/chat.py). SSE/chunked-transfer не
   * стримит. WebSocket — единственный transport, который проходит.
   *
   * Возвращает async-iterator из объектов {event, data} — формат,
   * совместимый со старым parseSSE, чтобы потребитель в send() не менялся.
   */
  streamChat(sessionId, message, mode = 'agent') {
    const wsUrl = new URL(api('/api/chat/ws'), window.location.href)
      .toString()
      .replace(/^http/, 'ws');
    const ws = new WebSocket(wsUrl);

    const buffer = [];
    let waker = null;
    let closed = false;
    let error = null;

    const wake = () => { if (waker) { const w = waker; waker = null; w(); } };

    ws.onopen = () => {
      ws.send(JSON.stringify({ message, session_id: sessionId, mode }));
    };
    ws.onmessage = (e) => {
      try {
        const obj = JSON.parse(e.data);
        // ping-frame'ы от backend (idle keepalive) — пропускаем
        if (obj && obj.event === 'ping') return;
        buffer.push(obj);
        wake();
      } catch (err) {
        console.error('WS message parse error', err, e.data);
      }
    };
    ws.onerror = () => {
      error = new Error('WebSocket connection error');
      wake();
    };
    ws.onclose = () => { closed = true; wake(); };

    return {
      [Symbol.asyncIterator]() { return this; },
      async next() {
        while (true) {
          if (error) throw error;
          if (buffer.length) return { done: false, value: buffer.shift() };
          if (closed) return { done: true, value: undefined };
          await new Promise(r => { waker = r; });
        }
      },
      async return() {
        try { ws.close(); } catch {}
        return { done: true, value: undefined };
      },
      /** Phase 4.1: шлём WS-frame {cancel:true} — бэк прервёт текущий Spark job */
      cancel() {
        try {
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ cancel: true }));
          }
        } catch (e) {
          console.warn('cancel send failed', e);
        }
      },
    };
  },

  fileUrl(fileId) {
    return api(`/api/files/${fileId}`);
  },
  creditMetaUrl() {
    return api('/api/credit/meta');
  },
  creditCalcUrl() {
    return api('/api/credit/calculate');
  },
};

// Также пробрасываем в window для других модулей (messages.jsx)
window.IOR_API = API;

/* —— Восстановление сообщений из history (meta) —————————————————— */

function restoreUserMessage(m) {
  return {
    id: m.id,
    role: 'user',
    time: m.created_at
      ? new Date(m.created_at).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })
      : nowTime(),
    text: m.content,
  };
}

function restoreAssistantMessage(m, skillById) {
  const meta = m.meta || {};
  const skill = meta.skill_id
    ? (skillById[meta.skill_id] || { id: meta.skill_id, title: meta.skill_title || meta.skill_id })
    : null;

  let excel = null;
  if (meta.file_id) {
    excel = meta.excel
      ? { ...meta.excel, file_id: meta.file_id }
      : { file_id: meta.file_id, name: 'Отчёт.xlsx', rows: 0, columns: 0, size: '-', sample: [] };
  }

  return {
    id: m.id,
    role: 'assistant',
    time: m.created_at
      ? new Date(m.created_at).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })
      : nowTime(),
    text: m.content || '',
    skill,
    sseSteps: meta.sseSteps || null,
    excel,
    stats: meta.stats || null,
    result: meta.result || null,
    dossier: meta.dossier || null,
    followups: meta.followups || null,
    clarification: meta.clarification
      ? { question: m.content, options: meta.suggested_options || [] }
      : null,
  };
}

/* —— Главное приложение —————————————————————————————— */

function App() {
  const [tweaks, setTweak] = useTweaks(TWEAK_DEFAULTS);
  useApplyTweaks(tweaks);

  const [skills, setSkills] = useState(FALLBACK_SKILLS);
  const skillById = React.useMemo(() => {
    const m = {};
    for (const s of skills) m[s.id || s.skill_id] = s;
    return m;
  }, [skills]);

  const [sessions, setSessions] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [composer, setComposer] = useState('');
  const [skillsOpen, setSkillsOpen] = useState(false);
  const [streamingMsg, setStreamingMsg] = useState(null);
  const [sidebarSearch, setSidebarSearch] = useState('');
  const [toast, setToast] = useState(null);
  const [mode, setMode] = useState('pipeline');
  const [creditOpen, setCreditOpen] = useState(false);

  const composerRef = useRef(null);
  const chatRef = useRef(null);
  const streamRef = useRef(null);

  /* —— Init: skills + sessions ————————————————————— */
  useEffect(() => {
    (async () => {
      try {
        const data = await API.listSkills();
        if (data.skills && data.skills.length) {
          setSkills(data.skills.map(s => ({ ...s, id: s.id || s.skill_id })));
        }
      } catch (e) { /* fallback */ }
      await refreshSessions();
    })();
  }, []);

  /* —— Auto-resize composer ————————————————————— */
  useEffect(() => {
    if (composerRef.current) {
      composerRef.current.style.height = 'auto';
      composerRef.current.style.height = Math.min(180, composerRef.current.scrollHeight) + 'px';
    }
  }, [composer]);

  /* —— Auto-scroll chat ————————————————————— */
  useEffect(() => {
    if (chatRef.current) chatRef.current.scrollTop = chatRef.current.scrollHeight;
  }, [messages.length, streamingMsg]);

  const showToast = (text) => { setToast(text); setTimeout(() => setToast(null), 2400); };

  async function refreshSessions() {
    const data = await API.listSessions();
    setSessions(data.sessions || []);
  }

  async function loadSession(id) {
    setActiveId(id);
    setStreamingMsg(null);
    streamRef.current = null;
    const data = await API.getSession(id);
    if (!data) { setMessages([]); return; }
    const restored = (data.messages || []).map(m =>
      m.role === 'user' ? restoreUserMessage(m) : restoreAssistantMessage(m, skillById)
    );
    setMessages(restored);
  }

  function newSession() {
    setMessages([]);
    setActiveId(null);
    setStreamingMsg(null);
    streamRef.current = null;
    setComposer('');
    composerRef.current?.focus();
  }

  /* —— Send → SSE/WS ————————————————————— */
  async function send(text) {
    text = (text || composer).trim();
    if (!text || streamingMsg) return;

    let sid = activeId;
    if (!sid) {
      const sess = await API.createSession();
      sid = sess.session_id;
      setActiveId(sid);
    }

    const userMsg = { id: 'u-' + Date.now(), role: 'user', time: nowTime(), text };
    setMessages(prev => [...prev, userMsg]);
    setComposer('');

    const initial = {
      id: 'a-' + Date.now(),
      role: 'assistant',
      time: nowTime(),
      skill: null,
      sseSteps: [],
      text: '',
      excel: null,
      stats: null,
      dossier: null,
      followups: null,
      clarification: null,
      plan: null,        // Agent v2: план шагов с per-step статусом
      warnings: null,
    };
    streamRef.current = initial;
    setStreamingMsg(initial);

    try {
      const stream = API.streamChat(sid, text, mode);
      streamRef.current.stream = stream;  // для кнопки Cancel
      for await (const ev of stream) {
        const cur = streamRef.current;
        if (!cur) break;
        applyEventToStream(cur, ev, skillById);
        setStreamingMsg({ ...cur });
      }
    } catch (e) {
      console.error('SSE error', e);
      const cur = streamRef.current;
      if (cur) {
        cur.text = (cur.text || '') + '\n\n⚠️ Ошибка соединения: ' + (e.message || e);
        setStreamingMsg({ ...cur });
      }
    } finally {
      const final = streamRef.current;
      streamRef.current = null;
      if (final) {
        delete final.stream;
        setMessages(prev => [...prev, final]);
      }
      setStreamingMsg(null);
      refreshSessions();
    }
  }

  /* — Phase 4.1: Cancel — шлём WS-frame {cancel:true} — */
  const handleCancel = () => {
    const cur = streamRef.current;
    if (cur && cur.stream && typeof cur.stream.cancel === 'function') {
      cur.stream.cancel();
    }
  };

  const handleClarifyAnswer = (option) => send(option);
  const handleFollowup = (prompt) => send(prompt);

  /* — Sidebar grouping ————————————————————— */
  const sessionGroups = React.useMemo(() => {
    const filtered = sessions.filter(s =>
      !sidebarSearch || (s.title || '').toLowerCase().includes(sidebarSearch.toLowerCase())
    );
    const grouped = {};
    for (const s of filtered) {
      const g = s.group || 'Сегодня';
      (grouped[g] = grouped[g] || []).push(s);
    }
    return grouped;
  }, [sessions, sidebarSearch]);

  /* — Topbar ————————————————————— */
  const lastMsg = messages.length > 0 ? messages[messages.length - 1] : null;
  const currentSkill = streamingMsg?.skill || lastMsg?.skill;
  const currentDuration = !streamingMsg && lastMsg?.stats?.duration_ms;
  const activeSession = sessions.find(s => s.id === activeId);
  const titleForTopbar = activeSession?.title || (activeId ? 'Сессия' : 'Новая сессия');
  const showWelcome = !activeId || (messages.length === 0 && !streamingMsg);

  const accentOptions = [
    { value: 'emerald', label: 'Изумруд',  sw: '#1F6F52' },
    { value: 'indigo',  label: 'Индиго',   sw: '#2C4FA8' },
    { value: 'amber',   label: 'Амбер',    sw: '#A2570E' },
    { value: 'magenta', label: 'Маджента', sw: '#9B2C6B' },
  ];

  return (
    <React.Fragment>
      {/* SIDEBAR */}
      <aside className="sidebar">
        <div className="sidebar-header">
          <div className="logo">
            <div>
              <div className="logo-title">Disrupt-tester</div>
              <div className="logo-version"></div>
            </div>
          </div>
        </div>

        <button className="btn-new-chat" onClick={newSession}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
            <path d="M12 5v14M5 12h14" />
          </svg>
          <span>Новая сессия</span>
          <span className="kbd-inline">⌘N</span>
        </button>

        <div className="sidebar-section sidebar-section-history">
          {sessions.length > 5 && (
            <input
              className="sessions-search"
              type="text"
              placeholder="Поиск по сессиям…"
              value={sidebarSearch}
              onChange={e => setSidebarSearch(e.target.value)}
            />
          )}
          <div className="sessions-list">
            {!activeId && (
              <React.Fragment>
                <div className="session-group">Активная</div>
                <div className="session-item active">
                  <span className="session-item-title">Новая сессия</span>
                  <span className="session-item-time">сейчас</span>
                </div>
              </React.Fragment>
            )}
            {Object.entries(sessionGroups).map(([group, items]) => (
              <React.Fragment key={group}>
                <div className="session-group">{group}</div>
                {items.map(s => (
                  <div
                    key={s.id}
                    className={`session-item ${activeId === s.id ? 'active' : ''}`}
                    onClick={() => loadSession(s.id)}
                  >
                    <span className="session-item-title">{s.title || 'Новая сессия'}</span>
                    <span className="session-item-time">{s.time || ''}</span>
                  </div>
                ))}
              </React.Fragment>
            ))}
          </div>
        </div>

        <div className="sidebar-bottom">
          <button className="btn-sidebar-bottom" onClick={() => setSkillsOpen(true)}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
              <rect x="3" y="3" width="7" height="7" rx="1"/>
              <rect x="14" y="3" width="7" height="7" rx="1"/>
              <rect x="3" y="14" width="7" height="7" rx="1"/>
              <rect x="14" y="14" width="7" height="7" rx="1"/>
            </svg>
            <span>Навыки</span>
            <span className="skill-count">{skills.length}</span>
          </button>
          <button className="btn-sidebar-bottom" onClick={() => setCreditOpen(true)}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
              <rect x="2" y="5" width="20" height="14" rx="2"/>
              <path d="M2 10h20M6 15h4"/>
            </svg>
            <span>Калькулятор потерь</span>
          </button>
          <button className="btn-sidebar-bottom" onClick={() => setTweak('dark', !tweaks.dark)}>
            {tweaks.dark ? (
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
                <circle cx="12" cy="12" r="4"/>
                <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
              </svg>
            ) : (
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
              </svg>
            )}
            <span>{tweaks.dark ? 'Светлая тема' : 'Тёмная тема'}</span>
          </button>
        </div>
      </aside>

      {/* MAIN */}
      <main className="main">
        <header className="topbar">
          <span className="topbar-eyebrow">Сессия</span>
          <span className="topbar-divider"></span>
          <h1 className="session-title">{titleForTopbar}</h1>
          <div className="topbar-right">
            {currentSkill && <span className="skill-tag">{currentSkill.id || currentSkill.skill_id}</span>}
            {currentDuration && <span className="duration-num">{fmtMs(currentDuration)}</span>}
          </div>
        </header>

        <div className="chat-area" ref={chatRef}>
          {showWelcome ? (
            <WelcomeScreen
              skills={skills}
              onPick={(t) => { setComposer(t); composerRef.current?.focus(); }}
            />
          ) : (
            <div className="messages">
              {messages.map(m =>
                m.role === 'user'
                  ? <UserMessage key={m.id} msg={m} />
                  : <AssistantMessage key={m.id} msg={m} tweaks={tweaks}
                                       onClarifyAnswer={handleClarifyAnswer}
                                       onFollowupPick={handleFollowup} />
              )}
              {streamingMsg && (
                <AssistantMessage msg={streamingMsg} tweaks={tweaks}
                                   onClarifyAnswer={handleClarifyAnswer}
                                   onFollowupPick={handleFollowup} />
              )}
            </div>
          )}
        </div>

        <div className="composer-wrap">
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '6px' }}>
            <span style={{ fontSize: '12px', color: '#888' }}>Режим:</span>
            <button
              onClick={() => setMode(mode === 'pipeline' ? 'ior_pipeline' : 'pipeline')}
              style={{
                padding: '4px 12px',
                borderRadius: '16px',
                border: '1px solid #ccc',
                background: mode === 'pipeline' ? '#1F6F52' : '#1a4f8a',
                color: 'white',
                cursor: 'pointer',
                fontSize: '12px',
              }}
            >
              {mode === 'pipeline' ? 'Поиск обращений' : 'ИОР-помощник'}
            </button>
          </div>
          <div className="composer">
            <textarea
              ref={composerRef}
              value={composer}
              onChange={e => setComposer(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
              }}
              placeholder="Опишите задачу — выгрузка, расчёт, поиск по EVE-…"
              rows={1}
            />
            {streamingMsg ? (
              <button className="btn-send" onClick={handleCancel}
                      aria-label="Остановить"
                      title="Остановить — прервать текущий Spark/LLM job"
                      style={{ background: '#fff', color: '#b00020', border: '1px solid #b00020' }}>
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"
                     strokeLinejoin="round">
                  <rect x="6" y="6" width="12" height="12" rx="1"/>
                </svg>
              </button>
            ) : (
              <button className="btn-send" onClick={() => send()}
                      disabled={!composer.trim()}
                      aria-label="Отправить">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M5 12h14M13 6l6 6-6 6"/>
                </svg>
              </button>
            )}
          </div>
          <div className="composer-hint">
            <span><kbd>Enter</kbd><span className="sep">отправить</span><kbd>Shift</kbd>+<kbd>Enter</kbd><span className="sep">перенос строки</span></span>
            <span>GigaChat-3-Ultra · Skill Registry · SSE</span>
          </div>
        </div>
      </main>

      <SkillsModal open={skillsOpen} onClose={() => setSkillsOpen(false)}
                   onPick={(text) => { setComposer(text); composerRef.current?.focus(); }}
                   skills={skills} />

      {creditOpen && window.CreditCalcForm &&
        <window.CreditCalcForm onClose={() => setCreditOpen(false)} />}

      {toast && <div className="toast">{toast}</div>}

      <TweaksPanel title="Tweaks · ИОР">
        <TweakSection label="Подпись" />
        <div className="twk-row">
          <div className="twk-lbl"><span>Акцент</span><span className="twk-val">{accentOptions.find(o => o.value === tweaks.accent)?.label}</span></div>
          <div style={{ display: 'flex', gap: 6, marginTop: 4 }}>
            {accentOptions.map(o => (
              <button
                key={o.value}
                type="button"
                onClick={() => setTweak('accent', o.value)}
                title={o.label}
                style={{
                  flex: 1, height: 24, borderRadius: 4,
                  background: o.sw,
                  border: tweaks.accent === o.value ? '2px solid rgba(255,255,255,0.95)' : '2px solid transparent',
                  boxShadow: tweaks.accent === o.value ? '0 0 1px rgba(0,0,0,0.45)' : 'none',
                  cursor: 'pointer', padding: 0,
                }}
              />
            ))}
          </div>
        </div>
        <TweakToggle label="Тёмная тема" value={tweaks.dark} onChange={(v) => setTweak('dark', v)} />
        <TweakRadio label="Плотность" value={tweaks.density}
                    options={['compact', 'regular', 'spacious']}
                    onChange={(v) => setTweak('density', v)} />

        <TweakSection label="Карточки ответа" />
        <TweakRadio label="Статистика" value={tweaks.statsStyle}
                    options={['compact', 'cards', 'chart']}
                    onChange={(v) => setTweak('statsStyle', v)} />
        <TweakRadio label="Excel" value={tweaks.excelStyle}
                    options={['row', 'preview']}
                    onChange={(v) => setTweak('excelStyle', v)} />
        <TweakRadio label="SSE-стрим" value={tweaks.sseStyle}
                    options={['timeline', 'quiet', 'hidden']}
                    onChange={(v) => setTweak('sseStyle', v)} />
      </TweaksPanel>
    </React.Fragment>
  );
}

/* —— SSE event apply —————————————————————————————————— */

function applyEventToStream(cur, ev, skillById) {
  switch (ev.event) {
    case 'status': {
      if (ev.data.steps) {
        cur.sseSteps = ev.data.steps;
      } else if (ev.data.step) {
        cur.sseSteps = [...(cur.sseSteps || []), {
          step: ev.data.step,
          label: ev.data.label || ev.data.text || ev.data.step,
          time: ev.data.time || '',
          status: ev.data.status || 'active',
        }];
      }
      break;
    }
    case 'skill': {
      cur.skill = skillById[ev.data.skill_id] || {
        id: ev.data.skill_id,
        title: ev.data.title || ev.data.skill_id,
      };
      break;
    }
    case 'file': {
      cur.clarification = null;
      // Финальная карточка — переопределяет любой preparing-state
      cur.excel = {
        file_id: ev.data.file_id,
        name: ev.data.name,
        size: ev.data.size || '-',
        rows: ev.data.rows || 0,
        columns: ev.data.columns || 0,
        sample: ev.data.sample || [],
        sample_headers: ev.data.sample_headers || [],
        status: ev.data.status || 'ready',
        has_csv: !!ev.data.has_csv,
      };
      break;
    }
    /* — Phase 3: предварительная карточка пока xlsx ещё пишется — */
    case 'file_pending': {
      cur.excel = {
        ...(cur.excel || {}),
        status: 'preparing',
        total_rows: ev.data.total_rows,
        bytes_written: 0,
      };
      break;
    }
    case 'file_progress': {
      if (cur.excel) {
        cur.excel = {
          ...cur.excel,
          bytes_written: ev.data.bytes_written || 0,
          name: ev.data.name || cur.excel.name,
        };
      }
      break;
    }
    /* — Phase 1.3: warning для больших выгрузок — */
    case 'warning': {
      cur.warnings = [...(cur.warnings || []), {
        level: ev.data.level || 'info',
        message: ev.data.message || '',
      }];
      break;
    }
    /* —— Agent v2: PER Loop events —————————————————————————— */
    case 'plan': {
      // План показывается ПЕРЕД исполнением — preview блок
      cur.plan = {
        rationale: ev.data.rationale || '',
        steps: ev.data.steps || [],
        expected_duration_sec: ev.data.expected_duration_sec || 0,
        // step results будут наполняться ниже по step_done/step_failed
        results: {},
      };
      break;
    }
    case 'step_started': {
      if (cur.plan) {
        cur.plan.results[ev.data.step_id] = {
          status: 'running',
          tool: ev.data.tool,
          started_at: Date.now(),    // для live-таймера в UI
        };
      }
      break;
    }
    case 'step_done': {
      if (cur.plan && cur.plan.results) {
        cur.plan.results[ev.data.step_id] = {
          status: 'done',
          tool: ev.data.tool,
          summary: ev.data.summary,
          duration_ms: ev.data.duration_ms,
        };
      }
      break;
    }
    case 'step_failed': {
      if (cur.plan && cur.plan.results) {
        cur.plan.results[ev.data.step_id] = {
          status: 'failed',
          tool: ev.data.tool,
          error: ev.data.error,
        };
      }
      break;
    }
    case 'step_healed': {
      if (cur.plan && cur.plan.results) {
        const prev = cur.plan.results[ev.data.step_id] || {};
        cur.plan.results[ev.data.step_id] = {
          ...prev,
          status: 'healing',
          heal_note: ev.data.reasoning,
        };
      }
      break;
    }
    case 'step_skipped': {
      if (cur.plan && cur.plan.results) {
        cur.plan.results[ev.data.step_id] = {
          status: 'skipped',
          tool: cur.plan.results[ev.data.step_id]?.tool,
          reasoning: ev.data.reasoning,
        };
      }
      break;
    }
    case 'reflecting': {
      // Информационное событие — не сохраняем в state, только в timeline
      // (timeline уже обновляется через status events от агента)
      break;
    }
    case 'replanned': {
      // План был перестроен — фиксируем визуально
      if (cur.plan) {
        cur.plan.replanned = true;
        cur.plan.replan_note = ev.data.reasoning;
      }
      break;
    }
    case 'plan_started':
    case 'plan_done': {
      // Информационные — timeline обновляется через status
      break;
    }
    /* — Phase 4.1: подтверждение отмены от бэка — */
    case 'cancelled': {
      cur.cancelled = true;
      cur.text = (cur.text || '') + '\n\n⛔ Отменено пользователем';
      cur.sseSteps = (cur.sseSteps || []).map(s => ({ ...s, status: 'done' }));
      break;
    }
    case 'metadata': {
      cur.stats = ev.data.stats || cur.stats;
      break;
    }
    case 'dossier': {
      cur.dossier = ev.data;
      break;
    }
    case 'followups': {
      cur.followups = ev.data.items || [];
      break;
    }
    /* — result-пакет: методология / воронка / превью / числа — */
    case 'result': {
      cur.result = ev.data || null;
      break;
    }
    case 'clarification': {
      cur.clarification = {
        question: ev.data.question,
        options: ev.data.options || [],
      };
      cur.sseSteps = (cur.sseSteps || []).map(s => ({ ...s, status: 'done' }));
      break;
    }
    /* — Премиальная лента статусов агента — */
    case 'activity': {
      cur.clarification = null;
      const a = ev.data || {};
      const list = cur.activities ? cur.activities.slice() : [];
      const idx = list.findIndex(x => x.id === a.id);
      const item = { id: a.id, kind: a.kind, title: a.title,
                      detail: a.detail, status: a.status };
      if (idx >= 0) {
        const prev = list[idx];
        // живой таймер: отметка времени, когда шаг стал active
        item._startedAt = a.status === 'active'
          ? (prev.status === 'active' ? prev._startedAt : Date.now())
          : undefined;
        list[idx] = { ...prev, ...item };
      } else {
        if (a.status === 'active') item._startedAt = Date.now();
        list.push(item);
      }
      cur.activities = list;
      break;
    }
    case 'token': {
      cur.text = (cur.text || '') + (ev.data.text || '');
      break;
    }
    case 'error': {
      cur.text = (cur.text || '') + '\n\n⚠️ ' + (ev.data.message || 'Ошибка');
      cur.sseSteps = (cur.sseSteps || []).map(s => ({ ...s, status: 'done' }));
      cur.activities = (cur.activities || []).map(
        a => a.status === 'active' ? { ...a, status: 'failed' } : a);
      break;
    }
    case 'done': {
      cur.sseSteps = (cur.sseSteps || []).map(s => ({ ...s, status: 'done' }));
      // гасим shimmer: всё активное → завершено
      cur.activities = (cur.activities || []).map(
        a => a.status === 'active' ? { ...a, status: 'done' } : a);
      break;
    }
  }
}

/* —— ExplorerPanel — обзор базы знаний (П7) —————————————————— */

const fmtNum2 = (n) => (n == null ? '' : Number(n).toLocaleString('ru-RU').replace(/,/g, ' '));

function ExplorerPanel({ onClose }) {
  const [tables, setTables] = useState([]);
  const [openTable, setOpenTable] = useState(null);
  const [col, setCol] = useState(null);
  const [values, setValues] = useState(null);
  const [q, setQ] = useState('');
  React.useEffect(() => { window.iorExploreSchema().then(setTables).catch(() => {}); }, []);

  const openCol = async (table, column) => {
    setCol({ table, column }); setValues(null); setQ('');
    setValues(await window.iorExploreValues(table, column));
  };
  const shown = (values && values.values)
    ? values.values.filter(v => !q || v.value.toLowerCase().includes(q.toLowerCase()))
    : [];

  return (
    <div className="explorer-overlay" onClick={onClose}>
      <div className="explorer" onClick={e => e.stopPropagation()}>
        <div className="explorer-head">
          <span className="explorer-title">Обзор базы знаний</span>
          <button className="explorer-close" onClick={onClose} aria-label="Закрыть">
            <i className="ti ti-x" aria-hidden="true"></i>
          </button>
        </div>
        <div className="explorer-body">
          <div className="explorer-tables">
            {tables.map(t => (
              <div key={t.table} className="explorer-table">
                <button className="explorer-table-head"
                        onClick={() => setOpenTable(openTable === t.table ? null : t.table)}>
                  <i className={`ti ti-chevron-${openTable === t.table ? 'down' : 'right'}`} aria-hidden="true"></i>
                  <span className="et-title">{t.title}</span>
                  <span className="et-rows">{fmtNum2(t.rows)}</span>
                </button>
                {openTable === t.table && (
                  <div className="explorer-cols">
                    {t.columns.map(c => (
                      <button key={c.name}
                              className={`explorer-col ${c.has_values ? 'clickable' : ''} ${col && col.table === t.table ? 'active' : ''}`}
                              onClick={c.has_values ? () => openCol(t.table, c.name) : undefined}>
                        <span className="ec-name">{c.name}</span>
                        {c.filled_pct != null && <span className="ec-filled" title="заполнено">{Math.round(c.filled_pct)}%</span>}
                        {c.has_values && <i className="ti ti-list-search ec-vicon" aria-hidden="true"></i>}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
          <div className="explorer-values">
            {!col && <div className="explorer-hint">Выберите колонку со справочником (значок поиска) — покажем значения</div>}
            {col && (
              <React.Fragment>
                <div className="explorer-values-head">
                  <span className="evh-name">{col.column}</span>
                  {values && <span className="evh-total">{fmtNum2(values.total)} значений</span>}
                </div>
                <input className="explorer-search" placeholder="фильтр значений…"
                       value={q} onChange={e => setQ(e.target.value)} />
                {!values && <div className="explorer-hint">Загрузка…</div>}
                {values && (
                  <div className="explorer-vallist">
                    {shown.map((v, i) => (
                      <div key={i} className="explorer-val">
                        <span className="ev-val">{v.value}</span>
                        {v.count != null && <span className="ev-count">{fmtNum2(v.count)}</span>}
                      </div>
                    ))}
                    {shown.length === 0 && <div className="explorer-hint">Ничего не найдено.</div>}
                  </div>
                )}
              </React.Fragment>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

/* —— WelcomeScreen — динамический список skills с backend —— */

function WelcomeScreen({ skills, onPick }) {
  const [reports, setReports] = useState([]);
  const [explorerOpen, setExplorerOpen] = useState(false);
  React.useEffect(() => {
    if (window.iorListReports) window.iorListReports().then(setReports).catch(() => {});
  }, []);
  return (
    <div className="welcome">
      {explorerOpen && <ExplorerPanel onClose={() => setExplorerOpen(false)} />}
      <div className="welcome-eyebrow">Audit Intelligence · ИОР</div>
      <h1 className="welcome-title">Что нужно <em>сегодня?</em></h1>
      <p className="welcome-subtitle">
        Выгрузки из БЗ ИОР, статистика по инцидентам, поиск по EVE-…,
        расчёты последствий по СМ 4467. Опишите задачу — подберу нужный отчёт и пришлю Excel.
      </p>
      <button className="welcome-explore" onClick={() => setExplorerOpen(true)}>
        <i className="ti ti-database-search" aria-hidden="true"></i>
        Что есть в базе знаний?
      </button>

      <div className="welcome-skills-label">
        <span>Навыки</span>
        <span className="ws-count">{skills.length} активных</span>
      </div>

      <div className="welcome-cards">
        {skills.map((s, i) => (
          <button key={s.id || s.skill_id} className="welcome-card"
                  onClick={() => onPick(s.placeholder || (s.examples && s.examples[0]) || s.title)}>
            <span className="wc-num">{String(i + 1).padStart(2, '0')}</span>
            <div className="wc-body">
              <div className="wc-title">{s.title}</div>
              <div className="wc-desc">{s.subtitle || s.desc || s.description || ''}</div>
              <div className="wc-trigger">{s.placeholder || (s.examples && s.examples[0]) || ''}</div>
            </div>
          </button>
        ))}
      </div>

      {reports.length > 0 && (
        <React.Fragment>
          <div className="welcome-skills-label" style={{ marginTop: 22 }}>
            <span>Мои отчёты</span>
            <span className="ws-count">{reports.length}</span>
          </div>
          <div className="welcome-reports">
            {reports.map(r => (
              <button key={r.id} className="welcome-report"
                      onClick={() => onPick(r.query || r.name)}
                      title={r.query || ''}>
                <i className="ti ti-bookmark" aria-hidden="true"></i>
                <span className="wr-name">{r.name}</span>
                <i className="ti ti-arrow-right wr-go" aria-hidden="true"></i>
              </button>
            ))}
          </div>
        </React.Fragment>
      )}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
