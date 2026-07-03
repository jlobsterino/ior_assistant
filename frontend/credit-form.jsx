/* credit-form.jsx – форма калькулятора потерь по кредиту.
   Параметров много и часть условные (факторы/суммы только для отдельных
   отклонений), поэтому структурированный ввод вместо свободного текста. */

function CreditCalcForm({ onClose }) {
  const api = window.IOR_API;
  const [meta, setMeta] = React.useState(null);
  const [loading, setLoading] = React.useState(false);
  const [result, setResult] = React.useState(null);
  const [error, setError] = React.useState(null);

  const [form, setForm] = React.useState({
    client_type: '1',            // 1=ФЛ
    id_credit: '',
    incident_date: '',
    risk_profile_code: 'DRP-10047',
    deviation_code: '',
    factor_codes: [],
    drp_10027_type: '',
    zalog_overact_amount: '',
    vivod_sredstv_pct: '',
    vivod_sredstv_amount: '',
  });

  React.useEffect(() => {
    fetch(api.creditMetaUrl())
      .then(r => r.json())
      .then(setMeta)
      .catch(e => setError('Не загрузил справочники: ' + (e.message || e)));
  }, []);

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }));

  // Допустимые отклонения по ЦПР + сегменту (как в расчётном скрипте)
  const allowedDeviations = React.useMemo(() => {
    if (!meta) return null;
    const ad = meta.allowed_deviations || {};
    const seg = { '2': 'КСБ', '3': 'ММБ' }[form.client_type] || 'ФЛ';
    const key2 = `${form.risk_profile_code}|${seg}`;
    if (ad[key2]) return ad[key2];
    if (ad[form.risk_profile_code]) return ad[form.risk_profile_code];
    return null; // null = все отклонения
  }, [meta, form.risk_profile_code, form.client_type]);

  const deviationOptions = React.useMemo(() => {
    if (!meta) return [];
    const dt = meta.deviation_type || {};
    const keys = allowedDeviations || Object.keys(dt);
    return keys.filter(k => dt[k]).map(k => [k, dt[k]]);
  }, [meta, allowedDeviations]);

  const isDRP10027 = form.risk_profile_code === 'DRP-10027';
  const isDRP10023 = form.risk_profile_code === 'DRP-10023';
  // суммы залога: завышение оценки (отклонение 35/40) либо неоформление
  const showZalogAmount = ['35', '40'].includes(form.deviation_code);
  const showFactors = !!meta && (form.client_type !== '1');

  const submit = async () => {
    setError(null);
    if (!form.id_credit.trim()) { setError('Укажите ID кредита'); return; }
    if (!form.deviation_code) { setError('Выберите отклонение'); return; }
    setLoading(true);
    setResult(null);
    try {
      const r = await fetch(api.creditCalcUrl(), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(form),
      });
      const d = await r.json();
      if (!r.ok || d.ok === false) {
        setError(d.detail || d.error || 'Ошибка расчёта');
      } else {
        setResult(d);
      }
    } catch (e) {
      setError('Ошибка сети: ' + (e.message || e));
    } finally {
      setLoading(false);
    }
  };

  const toggleFactor = (code) => {
    setForm(f => {
      const has = f.factor_codes.includes(code);
      const next = has ? f.factor_codes.filter(c => c !== code)
                       : (f.factor_codes.length < 4 ? [...f.factor_codes, code] : f.factor_codes);
      return { ...f, factor_codes: next };
    });
  };

  const fmtRub = (v) => {
    if (v == null || v === '') return '-';
    const n = Number(v);
    if (Number.isNaN(n)) return String(v);
    return n.toLocaleString('ru-RU', { maximumFractionDigits: 2 }) + ' ₽';
  };

  return (
    <div className="credit-overlay" onClick={onClose}>
      <div className="credit-modal" onClick={e => e.stopPropagation()}>
        <div className="credit-head">
          <div>
            <div className="credit-eyebrow">Калькулятор</div>
            <div className="credit-title">Потери по кредиту: невозможность взыскания</div>
          </div>
          <button className="credit-close" onClick={onClose} aria-label="Закрыть">X</button>
        </div>

        {!meta && !error && <div className="credit-loading">Загрузка...</div>}

        {meta && (
          <div className="credit-body">
            <div className="credit-grid">
              <label className="credit-field">
                <span>Тип клиента</span>
                <select value={form.client_type} onChange={e => set('client_type', e.target.value)}>
                  {Object.entries(meta.client_type).map(([k, v]) => (
                    <option key={k} value={k}>{v}</option>
                  ))}
                </select>
              </label>

              <label className="credit-field">
                <span>ID кредита / договора</span>
                <input value={form.id_credit} onChange={e => set('id_credit', e.target.value)}
                  placeholder="напр. 52802479370431" />
              </label>

              <label className="credit-field">
                <span>Дата обнаружения инцидента</span>
                <input type="date" value={form.incident_date}
                  onChange={e => set('incident_date', e.target.value)} />
              </label>

              <label className="credit-field credit-field-wide">
                <span>Цифровой профиль риска (ЦПР)</span>
                <select value={form.risk_profile_code}
                  onChange={e => { set('risk_profile_code', e.target.value); set('deviation_code', ''); }}>
                  {Object.entries(meta.digital_risk_profile).map(([k, v]) => (
                    <option key={k} value={k}>{k} — {v}</option>
                  ))}
                </select>
              </label>

              <label className="credit-field credit-field-wide">
                <span>Отклонение</span>
                <select value={form.deviation_code} onChange={e => set('deviation_code', e.target.value)}>
                  <option value="">-- выберите --</option>
                  {deviationOptions.map(([k, v]) => (
                    <option key={k} value={k}>{k}. {v}</option>
                  ))}
                </select>
              </label>

              {isDRP10027 && (
                <label className="credit-field">
                  <span>Вид события (DRP-10027)</span>
                  <select value={form.drp_10027_type} onChange={e => set('drp_10027_type', e.target.value)}>
                    <option value="">--</option>
                    {Object.entries(meta.DRP_10027_type).map(([k, v]) => (
                      <option key={k} value={k}>{v}</option>
                    ))}
                  </select>
                </label>
              )}

              {showZalogAmount && (
                <label className="credit-field">
                  <span>Сумма завышения залога, ₽</span>
                  <input value={form.zalog_overact_amount}
                    onChange={e => set('zalog_overact_amount', e.target.value)}
                    placeholder="число" />
                </label>
              )}

              {isDRP10023 && (
                <React.Fragment>
                  <label className="credit-field">
                    <span>% вывода средств</span>
                    <input value={form.vivod_sredstv_pct}
                      onChange={e => set('vivod_sredstv_pct', e.target.value)}
                      placeholder="напр. 30" />
                  </label>

                  <label className="credit-field">
                    <span>Сумма вывода средств, ₽</span>
                    <input value={form.vivod_sredstv_amount}
                      onChange={e => set('vivod_sredstv_amount', e.target.value)}
                      placeholder="число" />
                  </label>
                </React.Fragment>
              )}
            </div>

            {showFactors && (
              <div className="credit-factors">
                <div className="credit-factors-label">Факторы операционного риска (до 4):</div>
                {Object.entries(meta.factor_op).map(([k, v]) => (
                  <label key={k} className="credit-factor">
                    <input type="checkbox" checked={form.factor_codes.includes(k)}
                      onChange={() => toggleFactor(k)} />
                    <span>{v}</span>
                  </label>
                ))}
              </div>
            )}
          </div>
        )}

        {error && <div className="credit-error">{error}</div>}

        {result && (
          <div className="credit-result">
            <div className="credit-result-head">Результат расчёта</div>
            <div className="credit-result-grid">
              <div><span>Прямые потери</span><b>{fmtRub(result.losses?.['Прямые потери'])}</b></div>
              <div><span>Косвенные потери</span><b>{fmtRub(result.losses?.['Косвенные потери'])}</b></div>
              <div><span>Потенциальные потери</span><b>{fmtRub(result.losses?.['Потенциальные потери'])}</b></div>
            </div>
            {result.file_id && (
              <a className="credit-dl" href={api.fileUrl(result.file_id)}
                download={result.filename}>Скачать Excel</a>
            )}
          </div>
        )}

        <div className="credit-actions">
          <button className="credit-btn-secondary" onClick={onClose}>Закрыть</button>
          <button className="credit-btn-primary" onClick={submit} disabled={loading}>
            {loading ? 'Считаю...' : 'Рассчитать'}
          </button>
        </div>
      </div>
    </div>
  );
}

window.CreditCalcForm = CreditCalcForm;