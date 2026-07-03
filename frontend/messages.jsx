/* messages.jsx – редизайн 2026: единый статус-трейс, карточка-артефакт Excel,
   иерархия результата, досье. Вся логика событий и проводка к backend сохранены. */

const { fmtRub, fmtNum, fmtMs } = window.IOR_DATA;

/* —— Tiny markdown —— */
function renderMarkdown(text) {
  if (!text) return null;
  const lines = text.split('\n');
  const blocks = [];
  let bullets = null;
  for (const line of lines) {
    const trimmed = line.trim();
    if (line.startsWith('• ') || line.startsWith('- ') || line.startsWith('* ')) {
      if (!bullets) bullets = [];
      bullets.push(line.slice(2));
    } else {
      if (bullets) { blocks.push({ type: 'ul', items: bullets }); bullets = null; }
      if (trimmed === '') {
        blocks.push({ type: 'br' });
      } else if (line.startsWith('### ')) {
        blocks.push({ type: 'h3', text: line.slice(4) });
      } else if (line.startsWith('## ')) {
        blocks.push({ type: 'h2', text: line.slice(3) });
      } else if (line.startsWith('# ')) {
        blocks.push({ type: 'h1', text: line.slice(2) });
      } else {
        blocks.push({ type: 'p', text: line });
      }
    }
  }
  if (bullets) blocks.push({ type: 'ul', items: bullets });

  const inline = (s) => {
    const parts = [];
    const re = /(\*\*[^*]+\*\*|`[^`]+`|!\[[^\]]*\]\([^)]+\))/g;
    let last = 0, m;
    while ((m = re.exec(s)) !== null) {
      if (m.index > last) parts.push(s.slice(last, m.index));
      const tok = m[0];
      if (tok.startsWith('**')) {
        parts.push(<strong key={parts.length}>{tok.slice(2, -2)}</strong>);
      } else if (tok.startsWith('`')) {
        parts.push(<code key={parts.length}>{tok.slice(1, -1)}</code>);
      } else if (tok.startsWith('![')) {
        const match = /!\[([^\]]*)\]\(([^)]+)\)/.exec(tok);
        if (match) {
          const alt = match[1];
          let url = match[2];
          if (url.startsWith('/api/') || url.startsWith('api/')) {
            const base = window.__IOR_BASE || '';
            const cleanPath = url.replace(/^\//, '');
            url = base + cleanPath;
          }
          parts.push(
            <div key={parts.length} className="md-image-container" style={{ margin: '12px 0' }}>
              <img 
                src={url} 
                alt={alt} 
                className="md-image" 
                style={{ 
                  maxWidth: '100%', 
                  borderRadius: '6px', 
                  border: '1px solid var(--border-color, #e0e0e0)',
                  boxShadow: '0 2px 8px rgba(0,0,0,0.06)' 
                }} 
              />
            </div>
          );
        }
      }
      last = m.index + tok.length;
    }
    if (last < s.length) parts.push(s.slice(last));
    return parts;
  };

  return blocks.map((b, i) => {
    if (b.type === 'br') return <br key={i} />;
    if (b.type === 'ul') return <ul key={i}>{b.items.map((it, j) => <li key={j}>{inline(it)}</li>)}</ul>;
    if (b.type === 'h3') return <h3 key={i} style={{ margin: '16px 0 8px 0', color: 'var(--text-header, #f8fafc)', fontSize: '1.15em', fontWeight: 'bold' }}>{inline(b.text)}</h3>;
    if (b.type === 'h2') return <h2 key={i} style={{ margin: '20px 0 10px 0', color: 'var(--text-header, #f8fafc)', fontSize: '1.3em', fontWeight: 'bold' }}>{inline(b.text)}</h2>;
    if (b.type === 'h1') return <h1 key={i} style={{ margin: '24px 0 12px 0', color: 'var(--text-header, #f8fafc)', fontSize: '1.5em', fontWeight: 'bold' }}>{inline(b.text)}</h1>;
    return <p key={i}>{inline(b.text)}</p>;
  });
}

/* —— User message —— */
function UserMessage({ msg }) {
  return (
    <div className="msg user">
      <div className="msg-bubble">{msg.text}</div>
    </div>
  );
}

/* ============================================================
   STATUS TRACE — единый сворачиваемый блок статуса агента.
   Кормится тремя источниками (activity / plan / status-SSE), но
   рисуется одинаково. Пока работает — раскрыт, прогресс-бар, shimmer.
   Завершён — сворачивается в строку «Готово · N шагов · T».
   ============================================================ */

function stepKind(raw) {
  const s = (raw || '').toLowerCase();
  if (s === 'done' || s === 'ok' || s === 'success' || s === 'skipped') return 'done';
  if (s === 'active' || s === 'running' || s === 'healing') return 'active';
  if (s === 'failed' || s === 'error') return 'failed';
  return 'pending';
}

function TraceMarker({ kind }) {
  if (kind === 'done') return (
    <span className="nstat-ic nstat-ic-done">
      <svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3.5 8.5l3 3 6-7" />
      </svg>
    </span>
  );
  if (kind === 'active') return <span className="nstat-ic nstat-ic-active" aria-hidden="true"></span>;
  if (kind === 'failed') return (
    <span className="nstat-ic nstat-ic-fail">
      <svg viewBox="0 0 16 16" width="10" height="10" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
        <path d="M3 3l10 10M13 3L3 13" />
      </svg>
    </span>
  );
  return <span className="nstat-ic nstat-ic-pending"><span className="nstat-dot" /></span>;
}

/* steps: [{label, human, kind, time, error}] */
function StatusTrace({ steps, running, headLabel, headTime, progress, debug }) {
  const [open, setOpen] = React.useState(!!running);
  const [dbg, setDbg] = React.useState(false);
  React.useEffect(() => { setOpen(!!running); }, [running]);
  // живой тик для таймеров активных шагов
  const [, force] = React.useState(0);
  React.useEffect(() => {
    if (!running) return undefined;
    const t = setInterval(() => force(x => x + 1), 1000);
    return () => clearInterval(t);
  }, [running]);

  if (!steps || !steps.length) return null;
  const isOpen = running ? true : open;

  return (
    <div className={`nstat ${running ? 'is-running' : 'is-done'}`}>
      <button className="nstat-head" onClick={() => !running && setOpen(o => !o)}
              style={{ cursor: running ? 'default' : 'pointer' }}>
        {running
          ? <span className="nstat-live" />
          : <span className="nstat-head-ic">
              <svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
                <path d="M3.5 8.5l3 3 6-7" />
              </svg>
            </span>}
        <span className="nstat-head-label">{headLabel}</span>
        <span className="nstat-spacer" />
        {headTime && <span className="nstat-head-time">{headTime}</span>}
        {!running && (
          <svg className="nstat-caret" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor"
               strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
               style={{ transform: isOpen ? 'rotate(180deg)' : 'none' }}>
            <path d="M6 9l6 6 6-6" />
          </svg>
        )}
      </button>

      {running && (
        <div className="nstat-prog"><div className="nstat-prog-fill" style={{ width: progress || '0%' }} /></div>
      )}

      {isOpen && (
        <div className="nstat-body">
          {steps.map((s, i) => {
            const last = i === steps.length - 1;
            const k = s.kind;
            return (
              <div key={i} className={`nstat-step nstat-step-${k}`}>
                <div className="nstat-rail">
                  <TraceMarker kind={k} />
                  {!last && <span className="nstat-line" />}
                </div>
                <div className="nstat-step-body" style={{ paddingBottom: last ? 0 : '14px' }}>
                  <div className="nstat-step-head">
                    <span className={`nstat-label ${k === 'active' ? 'is-shimmer' : ''}`}>{s.label}</span>
                    <span className="nstat-spacer" />
                    {s.time && <span className="nstat-step-time">{s.time}</span>}
                  </div>
                  {s.error
                    ? <div className="nstat-err">{String(s.error).slice(0, 220)}</div>
                    : (s.human && <div className="nstat-human">{s.human}</div>)}
                </div>
              </div>
            );
          })}

          {debug && debug.length > 0 && (
            <div className="nstat-debug-wrap">
              <button className="nstat-debug-toggle" onClick={() => setDbg(d => !d)}>
                <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M9 6l6 6-6 6" />
                </svg>
                Технические детали
              </button>
              {dbg && (
                <div className="nstat-debug">
                  {debug.map((d, i) => <div key={i}>{d}</div>)}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* Чистит техжаргон из SSE-подписей (сырьё уезжает в «Технические детали») */
function cleanSseLabel(s) {
  if (!s) return s;
  let t = String(s);
  t = t.replace(/\s*\.\s*confidence\s*[\d.]+/i, '');
  t = t.replace(/запуск\s+notebook\s*\.\s*papermill\s+execute/i, 'Запрос к базе знаний');
  t = t.replace(/papermill\s+execute/i, 'выполнение');
  t = t.replace(/форматирование\s+ответа\s+GigaChat[\w.-]*/i, 'Формирование ответа');
  t = t.replace(/\s*\.?\s*GigaChat[\w.-]*/i, '');
  t = t.replace(/\s*\.?\s*Spark[\w.-]*/i, '');
  t = t.replace(/:\s*[A-Za-zА-Яа-яЁё_]+\s*=.*/i, '');
  return t.trim() || String(s);
}

/* —— SSE-status источник —— */
function SseBlock({ steps, mode }) {
  if (!steps || steps.length === 0) return null;
  if (mode === 'hidden') return null;
  const running = steps.some(s => stepKind(s.status) === 'active') || !steps.every(s => stepKind(s.status) === 'done');
  const tsteps = steps.map(s => ({ label: cleanSseLabel(s.label), kind: stepKind(s.status), time: s.time || '' }));
  const debug = steps.map(s => s.label).filter(Boolean);
  const headLabel = running ? 'Агент работает' : `Готово · ${steps.length} ${stepsWord(steps.length)}`;
  const lastTime = steps.length ? (steps[steps.length - 1].time || '') : '';
  return <StatusTrace steps={tsteps} running={running} headLabel={headLabel} headTime={running ? '' : lastTime} progress="100%" debug={debug} />;
}

/* —— Plan-источник (Agent v2) —— */
function PlanBlock({ plan }) {
  const [, setTick] = React.useState(0);
  React.useEffect(() => {
    if (!plan || !plan.results) return undefined;
    const hasRunning = plan.steps.some(s => (plan.results || {})[s.id]?.status === 'running');
    if (!hasRunning) return undefined;
    const id = setInterval(() => setTick(t => t + 1), 1000);
    return () => clearInterval(id);
  }, [plan?.steps, plan?.results]);

  if (!plan || !plan.steps || !plan.steps.length) return null;

  const total = plan.steps.length;
  const results = plan.results || {};
  const doneCount = plan.steps.filter(s => results[s.id]?.status === 'done').length;
  const failedCount = plan.steps.filter(s => results[s.id]?.status === 'failed').length;
  const totalDurMs = plan.steps.reduce((a, s) => a + (results[s.id]?.duration_ms || 0), 0);
  const overall = failedCount > 0 ? 'failed' : doneCount === total ? 'done' : doneCount === 0 ? 'pending' : 'running';
  const running = overall === 'running' || overall === 'pending';

  const tsteps = plan.steps.map(step => {
    const r = results[step.id] || { status: 'pending' };
    const k = stepKind(r.status);
    let time = '';
    if (r.status === 'running' && r.started_at) time = fmtElapsed(Date.now() - r.started_at);
    else if (r.duration_ms != null && r.duration_ms > 0) time = r.duration_ms < 1000 ? `${r.duration_ms} мс` : `${(r.duration_ms / 1000).toFixed(1)} c`;
    let human = humanizeSummary(r.summary, step.tool);
    if (r.status === 'running' && step.tool === 'query' && r.started_at && (Date.now() - r.started_at) > 3000) human = 'Готовим ответ…';
    if (r.heal_note) human = (human ? human + '. ' : '') + 'исправлено автоматически';
    if (r.status === 'skipped' && r.reasoning) human = 'Пропущено: ' + r.reasoning;
    return { label: TOOL_LABELS[step.tool] || step.tool, human, kind: k, time, error: r.status === 'failed' ? r.error : null };
  });

  const headLabel = {
    pending: 'Готов к запуску',
    running: `Выполняется · ${doneCount} из ${total}`,
    done: `Готово · ${total} ${stepsWord(total)}`,
    failed: 'Ошибка выполнения',
  }[overall];
  const headTime = (!running && totalDurMs > 0) ? `${(totalDurMs / 1000).toFixed(1)} c` : '';
  const progress = total ? Math.round((doneCount / total) * 100) + '%' : '0%';
  const debug = plan.steps.map(s => {
    const r = results[s.id]; if (!r || !r.summary) return null; return `${s.tool}: ${r.summary}`;
  }).filter(Boolean);

  return (
    <React.Fragment>
      {plan.rationale && <div className="nstat-rationale">{plan.rationale}</div>}
      <StatusTrace steps={tsteps} running={running} headLabel={headLabel} headTime={headTime} progress={progress} debug={debug} />
      {plan.replanned && <div className="nstat-replan">План перестроен · {plan.replan_note}</div>}
    </React.Fragment>
  );
}

/* —— Activity-источник (ReAct-лента) —— */
function ActivityStream({ activities }) {
  const [, force] = React.useState(0);
  const anyActive = (activities || []).some(a => a.status === 'active');
  React.useEffect(() => {
    if (!anyActive) return undefined;
    const t = setInterval(() => force(x => x + 1), 1000);
    return () => clearInterval(t);
  }, [anyActive]);
  if (!activities || !activities.length) return null;
  const rows = activities.filter(a => !(a.kind === 'thinking' && a.status === 'done'));
  if (!rows.length) return null;

  const elapsed = (a) => {
    if (a.status !== 'active' || !a._startedAt) return '';
    const s = Math.round((Date.now() - a._startedAt) / 1000);
    return s >= 2 ? `${s} c` : '';
  };
  const tsteps = rows.map(a => ({
    label: a.title,
    human: a.kind !== 'thinking' ? (a.detail || '') : '',
    kind: stepKind(a.status),
    time: elapsed(a),
  }));
  const headLabel = anyActive ? 'Агент работает' : `Готово · ${rows.length} ${stepsWord(rows.length)}`;
  return <StatusTrace steps={tsteps} running={anyActive} headLabel={headLabel} headTime="" progress="100%" />;
}

/* —— Stats — герой-число + метрики + бары —— */
function StatsBlock({ stats, style }) {
  if (!stats) return null;

  // герой — главная финансовая величина, иначе число строк
  let hero, metrics = [];
  const rowsItem = { label: 'Инцидентов', value: fmtNum(stats.rows), sub: 'строк в выгрузке' };
  if (stats.sum_total_loss != null) {
    hero = { label: 'Сумма потерь', value: fmtRub(stats.sum_total_loss), sub: 'прямые + косвенные' };
    metrics.push(rowsItem);
    if (stats.recovery != null) metrics.push({ label: 'Возмещение', value: fmtRub(stats.recovery), sub: 'stats.sum_total_loss' });
  } else {
    hero = { label: 'Строк в выгрузке', value: fmtNum(stats.rows), sub: stats.duration_ms ? `за ${fmtMs(stats.duration_ms)}` : null };
  }
  if (stats.top_tb) metrics.push({ label: 'Топ ТБ', value: stats.top_tb.label, sub: `${fmtNum(stats.top_tb.value)} ИОР` });
  if (stats.top_type && metrics.length < 4) metrics.push({ label: 'Топ тип', value: stats.top_type.label, sub: `${stats.top_type.value}` });
  if (stats.top_process && metrics.length < 4) metrics.push({ label: 'Топ процесс', value: stats.top_process.label, sub: `${stats.top_process.value}` });

  const bars = stats.breakdown_type || null;
  const barMax = bars ? Math.max(...bars.map(b => b.value)) : 1;

  return (
    <div className="nres">
      <div className="nres-hero">
        <div className="nres-hero-main">
          <div className="nres-hero-label">{hero.label}</div>
          <div className="nres-hero-val">{hero.value}</div>
          {hero.sub && <div className="nres-hero-sub">{hero.sub}</div>}
        </div>
        <div className="nres-metrics">
          {metrics.map((m, i) => (
            <div className="nres-metric" key={i}>
              <div className="nres-metric-label">{m.label}</div>
              <div className={m.small ? 'nres-metric-val small' : 'nres-metric-val'}>{m.value}</div>
              {m.sub && <div className="nres-metric-sub">{m.sub}</div>}
            </div>
          ))}
        </div>
      </div>

      {bars && bars.length > 0 && (
        <div className="nres-bars">
          <div className="nres-bars-title">Распределение по типам события</div>
          {bars.slice(0, 6).map((b, i) => (
            <div className="nres-bar" key={i}>
              <span className="nres-bar-label" title={b.label}>{b.label}</span>
              <span className="nres-bar-track">
                <span className={`nres-bar-fill ${b.value === barMax ? 'peak' : ''}`} style={{ width: `${Math.round((b.value / barMax) * 100)}%` }} />
              </span>
              <span className="nres-bar-val">{fmtNum(b.value)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* —— Excel — карточка-артефакт —— */
function _excelHref(excel) {
  if (!excel || !excel.file_id) return null;
  return (window.IOR_API && window.IOR_API.fileUrl) ? window.IOR_API.fileUrl(excel.file_id) : `api/files/${excel.file_id}`;
}
function _csvHref(excel) {
  if (!excel || !excel.file_id || !excel.has_csv) return null;
  const base = (window.IOR_API && window.IOR_API.fileUrl) ? window.IOR_API.fileUrl(excel.file_id) : `api/files/${excel.file_id}`;
  return base + '/csv';
}
function _formatBytes(n) {
  if (!n) return '—';
  if (n < 1024) return n + ' Б';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' КБ';
  return (n / 1024 / 1024).toFixed(1) + ' МБ';
}

function ExcelPending({ excel }) {
  const total = excel.total_rows;
  const written = excel.bytes_written || 0;
  return (
    <div className="nxl is-pending">
      <div className="nxl-head">
        <div className="nxl-icon nxl-icon-muted">
          <span className="nxl-spinner" />
        </div>
        <div className="nxl-info">
          <div className="nxl-eyebrow">Готовится · XLSX</div>
          <div className="nxl-name">{excel.name || 'Формирование отчёта…'}</div>
        </div>
      </div>
      <div className="nxl-facts">
        {total ? <span className="nxl-fact"><span className="k">Строк</span><span className="v">≈ {fmtNum(total)}</span></span> : null}
        <span className="nxl-fact"><span className="k">Статус</span><span className="v">{written > 0 ? `записано ${_formatBytes(written)}` : 'идёт запись…'}</span></span>
      </div>
    </div>
  );
}

function ExcelAttachment({ excel, style }) {
  if (!excel) return null;
  if (excel.status === 'preparing') return <ExcelPending excel={excel} />;
  if (excel.status === 'failed') {
    return (
      <div className="nxl is-failed">
        <div className="nxl-head">
          <div className="nxl-icon nxl-icon-fail">
            <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
              <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
              <path d="M12 9v4M12 17h.01" />
            </svg>
          </div>
          <div className="nxl-info">
            <div className="nxl-eyebrow nxl-eyebrow-fail">Ошибка</div>
            <div className="nxl-name">Не удалось создать файл</div>
          </div>
        </div>
        <div className="nxl-suberr">{excel.error || 'неизвестная ошибка'}</div>
      </div>
    );
  }

  const href = _excelHref(excel);
  const csvHref = _csvHref(excel);
  const dlProps = href ? { href, target: '_blank', rel: 'noopener', download: excel.name || true } : {};

  const facts = [];
  if (excel.rows != null) facts.push({ k: 'Строк', v: fmtNum(excel.rows) });
  if (excel.columns) facts.push({ k: 'Столбцов', v: String(excel.columns) });
  if (excel.size && excel.size !== '—') facts.push({ k: 'Размер', v: excel.size });

  const sample = excel.sample && excel.sample.length > 0 ? excel.sample : null;
  let headers = null, maxCols = 0;
  if (sample) {
    maxCols = Math.min(6, (sample[0] || []).length);
    headers = (excel.sample_headers && excel.sample_headers.length > 0)
      ? excel.sample_headers.slice(0, maxCols)
      : Array.from({ length: maxCols }, (_, i) => `Поле ${i + 1}`);
  }
  const isEve = (v) => typeof v === 'string' && /^EVE-/.test(v);
  const isNum = (v) => typeof v === 'string' && /\d[\d\u00A0]*(Р|%)?$/.test(v) && /\d/.test(v) &&
    (v.includes('Р') || /^\s*[\d\u00A0]+\s*$/.test(v));

  return (
    <div className="nxl">
      <div className="nxl-head">
        <div className="nxl-icon">
          <svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
            <path d="M14 3v5h5" />
            <path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8" strokeWidth="1.5" />
            <path d="M9 13h2.5L9 18M14.5 13h2.5L14 18M14.5 13h-2.5" />
          </svg>
        </div>
        <div className="nxl-info">
          <div className="nxl-eyebrow">Готовый отчёт · XLSX</div>
          <div className="nxl-name" title={excel.name}>{excel.name}</div>
        </div>
        {href
          ? <a className="nxl-dl" {...dlProps}>
              <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3" />
              </svg>
              Скачать
            </a>
          : <span className="nxl-dl nxl-dl-disabled">Файл</span>}
      </div>

      {facts.length > 0 && (
        <div className="nxl-facts">
          {facts.map((f, i) => <span className="nxl-fact" key={i}><span className="k">{f.k}</span><span className="v">{f.v}</span></span>)}
        </div>
      )}

      {sample && (
        <div className="nxl-table-wrap">
          <table className="nxl-table">
            <thead><tr>{headers.map((h, i) => <th key={i}>{h}</th>)}</tr></thead>
            <tbody>
              {sample.slice(0, 5).map((r, ri) => (
                <tr key={ri}>
                  {r.slice(0, maxCols).map((cell, ci) => {
                    const v = cell == null || cell === '' ? '—' : cell;
                    const cls = isEve(v) ? 'eve' : (isNum(v) ? 'num' : '');
                    return <td key={ci} className={cls}>{v}</td>;
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {(excel.rows > 5 || csvHref) && (
        <div className="nxl-foot">
          <span>{excel.rows > 5 ? `+ ещё ${fmtNum(excel.rows - 5)} строк в файле` : ''}</span>
          {csvHref && <a className="nxl-csv" href={csvHref} target="_blank" rel="noopener" download>Скачать как CSV</a>}
        </div>
      )}
    </div>
  );
}

/* —— EVE Dossier —— */
function Dossier({ d }) {
  if (!d) return null;
  const netto = d.amounts.direct + d.amounts.indirect + d.amounts.recovery;
  const money = [
    { label: 'Прямые потери', value: fmtRub(d.amounts.direct), sub: 'кредитные' },
    { label: 'Косвенные', value: fmtRub(d.amounts.indirect), sub: 'некредитные' },
    { label: 'Возмещение', value: fmtRub(d.amounts.recovery), sub: 'recovery' },
    { label: 'Нетто', value: (netto > 0 ? '-' : '') + fmtRub(Math.abs(netto)), sub: 'потери - возмещ.', hero: true },
  ];
  const flags = [
    { label: 'ИБ', on: !!d.flags.ib },
    { label: 'ИС', on: !!d.flags.is },
    { label: 'Поведение', on: !!d.flags.behavior },
    { label: 'Модель', on: !!d.flags.model },
  ];
  const cells = [
    { label: 'ЦПР', value: d.risk_profile },
    { label: 'Тип события', value: d.type },
    { label: 'Источник', value: d.source },
    { label: 'Тип клиента', value: d.client_type },
    { label: 'ТБ · функциональный блок', value: `${d.tb} · ${d.func_block}` },
    { label: 'Процесс', value: d.process },
    { label: 'Описание', value: d.summary, wide: true },
    { label: 'Кредитный договор', value: d.links.agr_num, mono: true },
    { label: 'Заявка', value: d.links.appl_num, mono: true },
  ];

  return (
    <div className="ndos">
      <div className="ndos-head">
        <div className="ndos-head-main">
          <div className="ndos-sid">{d.sid}</div>
          <div className="ndos-title">{d.title}</div>
        </div>
        <span className="ndos-status"><span className="ndos-status-dot" />{d.status}</span>
      </div>

      <div className="ndos-timeline">
        {d.timeline.map((t, i) => {
          const cur = t.state === 'current';
          return (
            <div className="ndos-tl-step" key={i}>
              <div className="ndos-tl-rail">
                <span className="ndos-tl-line" style={{ background: i === 0 ? 'transparent' : 'var(--accent)' }} />
                <span className={`ndos-tl-dot ${cur ? 'cur' : ''}`} />
                <span className="ndos-tl-line" style={{ background: i === d.timeline.length - 1 ? 'transparent' : 'var(--accent)' }} />
              </div>
              <div className={`ndos-tl-label ${cur ? 'cur' : ''}`}>{t.label}</div>
              <div className="ndos-tl-date">{t.date}</div>
            </div>
          );
        })}
      </div>

      <div className="ndos-grid">
        {cells.map((c, i) => (
          <div className={`ndos-cell ${c.wide ? 'wide' : ''}`} key={i}>
            <div className="ndos-cell-label">{c.label}</div>
            <div className={`ndos-cell-val ${c.mono ? 'mono' : ''}`}>{c.value}</div>
          </div>
        ))}
      </div>

      <div className="ndos-money">
        {money.map((m, i) => (
          <div className={`ndos-mo ${m.hero ? 'hero' : ''}`} key={i}>
            <div className="ndos-mo-label">{m.label}</div>
            <div className="ndos-mo-val">{m.value}</div>
            <div className="ndos-mo-sub">{m.sub}</div>
          </div>
        ))}
      </div>

      {d.fin_impacts && d.fin_impacts.length > 0 && (
        <div className="ndos-list">
          <div className="ndos-list-label"><span>Финансовые последствия</span><span className="ndos-list-count">{d.fin_impacts.length}</span></div>
          {d.fin_impacts.map((f, i) => (
            <div className="ndos-row" key={i}>
              <div><span className="ndos-row-type">{f.type}</span><span className="ndos-row-kind">{f.kind}</span></div>
              <div className="ndos-row-amt">{fmtRub(f.amount)}</div>
            </div>
          ))}
        </div>
      )}

      {d.recoveries && d.recoveries.length > 0 && (
        <div className="ndos-list">
          <div className="ndos-list-label"><span>Возмещения</span><span className="ndos-list-count">{d.recoveries.length}</span></div>
          {d.recoveries.map((r, i) => (
            <div className="ndos-row" key={i}>
              <div><span className="ndos-row-type">{r.type}</span><span className="ndos-row-kind">{r.date}</span></div>
              <div className="ndos-row-amt">{fmtRub(r.amount)}</div>
            </div>
          ))}
        </div>
      )}

      <div className="ndos-flags">
        <span className="ndos-flags-label">Риск-флаги</span>
        {flags.map((f, i) => (
          <span className={`ndos-flag ${f.on ? 'on' : ''}`} key={i}>
            {f.on && (
              <svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                <path d="M3.5 8l3 3 6-7" />
              </svg>
            )}
            {f.label}
          </span>
        ))}
      </div>
    </div>
  );
}

/* —— Clarification —— */
function Clarification({ c, onAnswer }) {
  if (!c) return null;
  return (
    <div className="nclar">
      <div className="nclar-q">
        <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="9.5" />
          <path d="M9.2 9a2.8 2.8 0 0 1 5.4 1c0 1.8-2.6 2.5-2.6 2.5M12 17h.01" />
        </svg>
        <span>{c.question}</span>
      </div>
      <div className="nclar-opts">
        {c.options.map((o, i) => <button key={i} className="nclar-opt" onClick={() => onAnswer && onAnswer(o)}>{o}</button>)}
      </div>
    </div>
  );
}

/* —— Follow-ups —— */
function Followups({ items, onPick }) {
  if (!items || items.length === 0) return null;
  return (
    <div className="nfollow">
      <div className="nfollow-label">Продолжить</div>
      <div className="nfollow-row">
        {items.map((f, i) => (
          <button key={i} className="nfollow-chip" onClick={() => onPick && onPick(f.prompt)}>
            <span>{f.label}</span>
            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M5 12h14M13 6l6 6-6 6" />
            </svg>
          </button>
        ))}
      </div>
    </div>
  );
}

/* —— Warning banner (лимиты по размеру выгрузки) —— */
function WarningBanner({ warnings }) {
  if (!warnings || !warnings.length) return null;
  const high = warnings.some(w => w.level === 'high');
  return (
    <div className={`nwarn ${high ? 'is-high' : ''}`}>
      {warnings.map((w, i) => (
        <div key={i} className="nwarn-row">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
            <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
            <path d="M12 9v4M12 17h.01" />
          </svg>
          <span>{w.message}</span>
        </div>
      ))}
    </div>
  );
}

/* —— Tool labels / humanize (для PlanBlock) —— */
const TOOL_LABELS = {
  query: 'Запрос к базе знаний', filter_df: 'Фильтрация', top_n: 'Отбор лидеров', group_by: 'Группировка',
  join_dfs: 'Объединение таблиц', export_excel: 'Формирование Excel', export_csv: 'Формирование CSV',
  get_ior_details: 'Сбор досье ИОР', run_preset: 'Готовый отчёт', window_rank: 'Ранжирование в группах', derive_column: 'Вычисление поля',
  search_values: 'Поиск значений в данных', probe: 'Проверка наличия данных', run_query_spec: 'Сборка выгрузки', run_query: 'Запрос среза данных',
  describe_schema: 'Изучение структуры',
};

function humanizeSummary(summary, tool) {
  if (!summary) return null;
  const s = String(summary);
  const fmt = (n) => Number(n).toLocaleString('ru-RU').replace(/,/g, ' ');
  let m;
  if ((m = s.match(/df_\d+:\s*(\d+)\s*rows?\s*[×x]\s*(\d+)\s*cols?/))) return `${fmt(m[1])} строк по ${fmt(m[2])} ${fieldsWord(m[2])}`;
  if ((m = s.match(/df_\d+:\s*(\d+)\s*топ-\d+\s+из\s+(\d+)/i))) return `${m[1]} крупнейших · из ${fmt(m[2])}`;
  if ((m = s.match(/df_\d+:\s*(\d+)\s*rows?\s*\(was\s+(\d+)\)/i))) return `Осталось ${fmt(m[1])} ${rowsWord(m[1])} · из ${fmt(m[2])}`;
  if ((m = s.match(/df_\d+:\s*(\d+)\s*групп/))) return `${fmt(m[1])} ${groupsWord(m[1])}`;
  if ((m = s.match(/df_\d+:\s*(\d+)\s*rows?\s*\(left=(\d+),\s*right=(\d+)\)/i))) return `Объединено ${fmt(m[1])} ${rowsWord(m[1])}`;
  if ((m = s.match(/file_\d+:.*?\((\d+)\s*строк\w*\)/i))) return `Excel готов · ${fmt(m[1])} ${rowsWord(m[1])}`;
  if ((m = s.match(/file_\d+:.*?csv/i))) return `CSV готов`;
  if ((m = s.match(/досье\s+(\S+):\s*(\d+)\s*возмещ/i))) return `Досье ${m[1]} собрано`;
  if ((m = s.match(/preset\s+\S+:\s*(\d+)\s*строк/i))) return `Отчёт готов · ${fmt(m[1])} ${rowsWord(m[1])}`;
  if (/^probe:\s*0\s/i.test(s)) return 'Пусто — фильтр уточняется';
  if ((m = s.match(/^probe:\s*(≥?)(\d+)\s*строк/i))) return `Найдено ${m[1]}${fmt(m[2])} ${rowsWord(m[2])}`;
  if (/→/.test(s) && /строк/i.test(s) && /'/.test(s)) return 'Сверено с реальными значениями';
  if ((m = s.match(/:\s*(\d+)\s*значени\w+/))) return 'В данных не найдено — нужна иная формулировка';
  if ((m = s.match(/^схема\s+(\d+)\s+значени\w+/i))) return `${fmt(m[1])} реальных значений`;
  return s.replace(/^(df_\d+|file_\d+):\s*/, '');
}

function rowsWord(n) {
  const v = Number(n) % 100, k = v % 10;
  if (v > 10 && v < 20) return 'строк';
  if (k === 1) return 'строка';
  if (k >= 2 && k <= 4) return 'строки';
  return 'строк';
}
function fieldsWord(n) {
  const v = Number(n) % 100, k = v % 10;
  if (v > 10 && v < 20) return 'полям';
  if (k === 1) return 'полю';
  return 'полям';
}
function groupsWord(n) {
  const v = Number(n) % 100, k = v % 10;
  if (v > 10 && v < 20) return 'групп';
  if (k === 1) return 'группа';
  if (k >= 2 && k <= 4) return 'группы';
  return 'групп';
}
function stepsWord(n) {
  const v = Number(n) % 100, k = v % 10;
  if (v > 10 && v < 20) return 'шагов';
  if (k === 1) return 'шаг';
  if (k >= 2 && k <= 4) return 'шага';
  return 'шагов';
}

function fmtElapsed(ms) {
  if (!ms || ms < 0) return '0 c';
  const totalSec = Math.floor(ms / 1000);
  if (totalSec < 60) return `${totalSec} c`;
  const m = Math.floor(totalSec / 60), s = totalSec % 60;
  if (m < 60) return `${m}:${String(s).padStart(2, '0')}`;
  const h = Math.floor(m / 60);
  return `${h}:${String(m % 60).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

/* —— result-пакет: методология / результат (П1·П2·П3·П4) —— */
function refineSuggestions(result) {
  const out = [];
  const c = result.conditions || [];
  if (c.some(x => x.kind === 'period')) out.push('Повтори за другой период');
  if (result.summary && result.summary.is_aggregate) { out.push('Покажи топ-10'); out.push('Отсортируй по другой метрике'); }
  c.filter(x => x.kind === 'filter').slice(0, 1).forEach(x => out.push(`Убери фильтр «${x.detail}»`));
  out.push('Выгрузи в CSV');
  return out.slice(0, 4);
}

function ResultBlock({ result, onFollowup }) {
  if (!result) return null;
  const { summary, bars, preview, conditions, funnel, methodology, warnings, spec } = result;
  const send = (t) => onFollowup && onFollowup(t);
  const hl = (summary && summary.highlights) || [];
  const [saved, setSaved] = React.useState(false);
  const [methodOpen, setMethodOpen] = React.useState(false);
  const saveReport = async () => {
    if (!window.iorSaveReport || !spec) return;
    const def = (result.query || '').slice(0, 60) || 'Отчёт';
    const name = window.prompt('Название отчёта:', def);
    if (!name) return;
    const res = await window.iorSaveReport(name, spec, result.query || '');
    if (res) { setSaved(true); setTimeout(() => setSaved(false), 2500); }
  };

  return (
    <div className="nres">
      {hl.length > 0 && (
        <div className="nres-hero">
          <div className="nres-hero-main">
            <div className="nres-hero-label">{hl[0].label}</div>
            <div className="nres-hero-val">{hl[0].value}</div>
            {hl[0].sub && <div className="nres-hero-sub" title={hl[0].sub}>{hl[0].sub}</div>}
          </div>
          {hl.length > 1 && (
            <div className="nres-metrics">
              {hl.slice(1, 4).map((h, i) => (
                <div className="nres-metric" key={i}>
                  <div className="nres-metric-label">{h.label}</div>
                  <div className="nres-metric-val">{h.value}</div>
                  {h.sub && <div className="nres-metric-sub" title={h.sub}>{h.sub}</div>}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {bars && bars.items && bars.items.length > 0 && (
        <div className="nres-bars">
          {bars.items.map((it, i) => (
            <div className="nres-bar" key={i}>
              <span className="nres-bar-label" title={it.label}>{it.label}</span>
              <span className="nres-bar-track"><span className="nres-bar-fill" style={{ width: `${Math.round(it.pct * 100)}%` }} /></span>
              <span className="nres-bar-val">{it.value}</span>
            </div>
          ))}
        </div>
      )}

      {preview && preview.rows && preview.rows.length > 0 && (
        <div className="nxl-table-wrap" style={{ borderRadius: '12px', border: '1px solid var(--line-1)' }}>
          <table className="nxl-table">
            <thead><tr>{preview.headers.map((h, i) => <th key={i}>{h}</th>)}</tr></thead>
            <tbody>{preview.rows.map((r, ri) => <tr key={ri}>{r.map((c, ci) => <td key={ci}>{c}</td>)}</tr>)}</tbody>
          </table>
          {preview.truncated && <div className="nxl-foot"><span>и ещё {fmtNum(preview.total - preview.rows.length)} в файле</span></div>}
        </div>
      )}

      {warnings && warnings.length > 0 && (
        <div className="nres-warn">
          {warnings.map((w, i) => (
            <div key={i} className="nres-warn-row">
              <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round">
                <circle cx="12" cy="12" r="9.5" />
                <path d="M12 8v5M12 16h.01" />
              </svg>
              <span>{w}</span>
            </div>
          ))}
        </div>
      )}

      <div className="nres-method">
        <button className="nres-method-head" onClick={() => setMethodOpen(o => !o)}>
          <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
               style={{ transform: methodOpen ? 'rotate(90deg)' : 'none' }}>
            <path d="M9 6l6 6-6 6" />
          </svg>
          <span className="nres-method-title">Как это посчитано</span>
          {funnel && funnel.length > 1 && <span className="nres-funnel-inline">{funnel.map(f => fmtNum(f.rows)).join(' → ')}</span>}
        </button>
        {methodOpen && (
          <div className="nres-method-body">
            {conditions && conditions.length > 0 && (
              <div className="nres-chips">
                {conditions.map((c, i) => (
                  <button key={i} className={`nres-chip ${c.editable ? 'editable' : ''}`}
                          onClick={c.editable ? () => send(`Измени условие: ${c.label}${c.detail ? ` (${c.detail})` : ''}`) : undefined}
                          title={c.editable ? 'Нажмите, чтобы изменить' : ''}>
                    <span className="nres-chip-key">{c.label}</span>
                    {c.detail && <span className="nres-chip-val">{c.detail}</span>}
                  </button>
                ))}
              </div>
            )}
            {methodology && <div className="nres-method-text">{methodology}</div>}
            {spec && (
              <details className="nres-spec"><summary>Показать спецификацию</summary><pre>{JSON.stringify(spec, null, 2)}</pre></details>
            )}
          </div>
        )}
      </div>

      <div className="nres-refine">
        <span className="nres-refine-label">Доработать</span>
        {refineSuggestions(result).map((t, i) => <button key={i} className="nres-refine-chip" onClick={() => send(t)}>{t}</button>)}
        {spec && (
          <button className="nres-refine-chip nres-save" onClick={saveReport}>{saved ? '✓ Сохранено' : 'Сохранить отчёт'}</button>
        )}
      </div>
    </div>
  );
}

/* —— Assistant wrapper —— */
function AssistantMessage({ msg, tweaks, onClarifyAnswer, onFollowupPick }) {
  const hasActivity = msg.activities && msg.activities.length;
  const skillName = msg.skill ? (msg.skill.title || msg.skill.id || msg.skill.skill_id) : null;
  const statusHidden = tweaks && tweaks.sseStyle === 'hidden';
  return (
    <div className="msg assistant">
      <div className="am-head">
        <span className="am-mark">
          <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 3v18M3 12h18" />
          </svg>
        </span>
        <span className="am-name">Ассистент</span>
        {skillName && <span className="am-skill">{skillName}</span>}
        <span className="am-spacer" />
        <span className="am-time">{msg.time}</span>
      </div>

      <div className="msg-body">
        {!statusHidden && (hasActivity
          ? <ActivityStream activities={msg.activities} />
          : (!msg.plan && <SseBlock steps={msg.sseSteps} mode={tweaks && tweaks.sseStyle} />))}
        {!statusHidden && <PlanBlock plan={msg.plan} />}

        <WarningBanner warnings={msg.warnings} />

        {msg.text && <div className="am-text">{renderMarkdown(msg.text)}</div>}

        {msg.result && <ResultBlock result={msg.result} onFollowup={onFollowupPick} />}
        {msg.clarification && <Clarification c={msg.clarification} onAnswer={onClarifyAnswer} />}
        {msg.dossier && <Dossier d={msg.dossier} />}
        {msg.excel && !msg.result && <ExcelAttachment excel={msg.excel} />}
        {msg.stats && <StatsBlock stats={msg.stats} style={tweaks && tweaks.statsStyle} />}
        {msg.excel && msg.result && <ExcelAttachment excel={msg.excel} style="row" />}

        <Followups items={msg.followups} onPick={onFollowupPick} />
      </div>
    </div>
  );
}

Object.assign(window, {
  UserMessage, AssistantMessage,
  SseBlock, StatsBlock, ExcelAttachment, Dossier, Clarification, Followups,
  PlanBlock, WarningBanner, ActivityStream, ResultBlock, StatusTrace,
  renderMarkdown,
});
