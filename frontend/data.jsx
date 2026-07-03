/* data.jsx – skills, sample sessions, demo data */

const SKILLS = [
  {
    id: "ior_period_pao_sberbank",
    title: "ИОР за период",
    subtitle: "Сводный отчёт по ПАО Сбербанк",
    desc: "Главный отчёт. Выгрузка инцидентов за указанный период с фильтром по ТБ, типу события, источнику. 67 атрибутов.",
    triggers: ["ИОР за 2025", "выгрузка за квартал", "все ИОР по СЗБ", "инциденты за период"],
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 20h18M7 20v-7M12 20V8M17 20v-11"/>
      </svg>
    ),
    placeholder: "Выгрузи ИОР за 2025 год по СЗБ"
  },
  {
    id: "report_period_specific_ior",
    title: "Досье инцидента",
    subtitle: "Полная информация по EVE-XXXXXXX",
    desc: "Атрибуты ИОР + все фин. последствия + все возмещения в одном отчёте. Декартово произведение N×M.",
    triggers: ["EVE-5092355", "что с инцидентом", "досье ИОР", "детали EVE-"],
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>
      </svg>
    ),
    placeholder: "Покажи всё про инцидент EVE-5092355"
  },
  {
    id: "vozmeshenie_ior",
    title: "Возмещения",
    subtitle: "Recovery по ИОР за период",
    desc: "Все операции возмещения по инцидентам – компенсации от сотрудников, страховые выплаты, восстановление средств.",
    triggers: ["возмещения", "recovery", "сколько вернули", "компенсация по ИОР"],
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 12h18M11 5l-7 7 7 7"/>
      </svg>
    ),
    placeholder: "Возмещения по ИОР за Q1 2025"
  },
  {
    id: "financial_consequences_ior",
    title: "Финансовые последствия",
    subtitle: "Детализация потерь по типам",
    desc: "Прямые / косвенные / нереализовавшиеся / третьих лиц / прибыль. Детализация по каждому инциденту.",
    triggers: ["финансовые последствия", "прямые потери", "сумма ущерба", "потери по ИОР"],
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>
      </svg>
    ),
    placeholder: "Финансовые последствия по ИОР за 2025"
  },
  {
    id: "ior_nonfinancial_consequences",
    title: "Нефинансовые последствия",
    subtitle: "Репутация, регулятор, клиенты",
    desc: "Влияние на репутацию, претензии регулятора, операционные нарушения без прямых потерь.",
    triggers: ["нефинансовые последствия", "репутация", "регуляторные", "non-financial"],
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>
      </svg>
    ),
    placeholder: "Нефинансовые последствия за 2025"
  },
  {
    id: "deleted_ior",
    title: "Удалённые ИОР",
    subtitle: "Журнал изменений статусов",
    desc: "Инциденты, у которых был статус «удалён» — для аудита изменений. Полный журнал переходов статусов.",
    triggers: ["удалённые ИОР", "удалили инцидент", "журнал статусов", "deleted_ior"],
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
        <line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/>
      </svg>
    ),
    placeholder: "Удалённые ИОР за 2024 год"
  }
];

const SKILL_BY_ID = Object.fromEntries(SKILLS.map(s => [s.id, s]));

/* ----- Helpers ----- */

const fmtRub = (n) => {
  if (n == null) return "-";
  return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 }).format(n) + " ₽";
};

const fmtNum = (n) => new Intl.NumberFormat('ru-RU').format(n);

const fmtMs = (ms) => {
  if (ms < 1000) return ms + " мс";
  return (ms / 1000).toFixed(1) + " с";
};

/* ----- Sample sessions (history) ----- */

const SAMPLE_SESSIONS = [
  // Newest at top - active fresh session
  {
    id: "s-fresh",
    title: "Новая сессия",
    group: "Сегодня",
    time: "сейчас",
    messages: []
  },

  // Period report - main flow
  {
    id: "s-period-szb",
    title: "ИОР за 2025 по СЗБ",
    group: "Сегодня",
    time: "12:14",
    messages: [
      { id: "m1", role: "user", time: "12:14", text: "Выгрузи ИОР за 2025 год по СЗБ" },
      {
        id: "m2",
        role: "assistant",
        time: "12:14",
        skill: SKILL_BY_ID["ior_period_pao_sberbank"],
        sseSteps: [
          { step: "routing", label: "Маршрутизация запроса", time: "+0.2c", status: "done" },
          { step: "selected", label: "Выбран навык: ИОР за период - confidence 0.94", time: "+0.4c", status: "done" },
          { step: "extracting", label: "Извлечение параметров: period_from=2025-01-01, period_to=2025-12-31, tb=СЗБ", time: "+1.1c", status: "done" },
          { step: "executing", label: "Запуск notebook • papermill execute", time: "+1.4c", status: "done" },
          { step: "formatting", label: "Форматирование ответа GigaChat-3-Ultra", time: "+4.0c", status: "done" }
        ],
        text: "Готова выгрузка по **ИОР за 2025 год по СЗБ**. В период попало **2 856 инцидентов**, из них с финансовыми последствиями — **385 млн ₽** (30%).\\n\\nДоминирующий тип события — **Операционные ошибки -> Технические сбои** (42% от выборки), главный процесс — **Кредитование ФЛ -> Выдача -> Авторизация платежа**. Сортировка по `incdnt_entry_dt`.",
        excel: {
          name: "ИОР за период по ПАО Сбербанк 2025-01-01 - 2025-12-31.xlsx",
          size: "4.2 МБ",
          rows: 2856,
          columns: 67,
          sample: [
            ["EVE-5092355", "14.03.2025", "СЗБ", "Технические сбои", "1 234 567 ₽", "Закрыт"],
            ["EVE-5102881", "21.03.2025", "СЗБ", "Технические сбои", "892 400 ₽", "Закрыт"],
            ["EVE-5142019", "02.04.2025", "СЗБ", "Ошибка ввода данных", "156 200 ₽", "Возмещение"],
            ["EVE-5198724", "18.04.2025", "СЗБ", "Внешнее мошенничество", "3 421 000 ₽", "На проверке"],
            ["EVE-5234012", "07.05.2025", "СЗБ", "Технические сбои", "78 950 ₽", "Закрыт"]
          ]
        },
        stats: {
          rows: 2856,
          sum_total_loss: 127856432,
          recovery: 38356929,
          duration_ms: 4320,
          top_tb: { label: "СЗБ", value: 2856 },
          top_type: { label: "Технические сбои", value: 1196, pct: 42 },
          top_process: { label: "Кредитование ФЛ", value: 1832, pct: 64 },
          breakdown_type: [
            { label: "Технические сбои", value: 1196 },
            { label: "Ошибка ввода данных", value: 624 },
            { label: "Внешнее мошенничество", value: 312 },
            { label: "Внутреннее мошенничество", value: 198 },
            { label: "Нарушение процесса", value: 526 }
          ],
          breakdown_month: [
            { label: "01", value: 198 }, { label: "02", value: 214 }, { label: "03", value: 287 },
            { label: "04", value: 312 }, { label: "05", value: 256 }, { label: "06", value: 244 },
            { label: "07", value: 198 }, { label: "08", value: 188 }, { label: "09", value: 234 },
            { label: "10", value: 268 }, { label: "11", value: 252 }, { label: "12", value: 205 }
          ]
        },
        followups: [
          { label: "Топ-10 ИОР по сумме", prompt: "Покажи топ-10 ИОР за 2025 по СЗБ по сумме потерь" },
          { label: "Только тех. сбои", prompt: "Отфильтруй выгрузку – только технические сбои" },
          { label: "Сравни с 2024", prompt: "Сравни статистику ИОР по СЗБ 2025 vs 2024" }
        ]
      }
    ]
  },

  // EVE dossier
  {
    id: "s-eve-dossier",
    title: "EVE-5092355 – что произошло?",
    group: "Сегодня",
    time: "11:42",
    messages: [
      { id: "m1", role: "user", time: "11:42", text: "Покажи всё про инцидент EVE-5092355" },
      {
        id: "m2",
        role: "assistant",
        time: "11:42",
        skill: SKILL_BY_ID["report_period_specific_ior"],
        sseSteps: [
          { step: "routing", label: "Маршрутизация запроса", time: "+0.1c", status: "done" },
          { step: "selected", label: "Выбран навык: Досье инцидента - confidence 0.98", time: "+0.3c", status: "done" },
          { step: "extracting", label: "Извлечение параметров: incdnt_sid=EVE-5092355", time: "+0.8c", status: "done" },
          { step: "executing", label: "Запуск notebook • 1 ИОР × 2 фин.посл. × 1 возм. = 2 строки", time: "+1.1c", status: "done" },
          { step: "formatting", label: "Форматирование ответа GigaChat-3-Ultra", time: "+2.4c", status: "done" }
        ],
        text: "Полное досье ниже. Инцидент связан с **двойным списанием** по 14 кредитным договорам ФЛ из-за сбоя в системе авторизации платежей. Сбой возник во внешней системе. Возмещение получено полностью.",
        dossier: {
          sid: "EVE-5092355",
          title: "Двойное списание по кредитным договорам ФЛ из-за сбоя авторизации",
          status: "Закрыт",
          entry_dt: "14.03.2025",
          detection_dt: "12.03.2025",
          start_dt: "12.03.2025",
          autoreg: true,
          risk_profile: "Профиль 8 – Информационная безопасность",
          type: "Операционные ошибки -> Технические сбои",
          source: "Система мониторинга -> Автоматическая регистрация",
          tb: "СЗБ",
          func_block: "Розничный бизнес -> Кредитование ФЛ",
          process: "Кредитование ФЛ -> Выдача -> Авторизация платежа",
          client_type: "Физическое лицо",
          summary: "В период 13:42-14:08 МСК 12.03.2025 произошёл сбой в системе авторизации платежей. Из-за повторной отправки транзакций возникло двойное списание. Инцидент закрыт после возмещения клиентам.",
          amounts: {
            direct: 1234567,
            indirect: 432100,
            unrealized: 0,
            third_party: 0,
            gain: 0,
            recovery: 1100000
          },
          fin_impacts: [
            { type: "Прямая потеря", kind: "Кредитная", amount: 1234567 },
            { type: "Косвенная", kind: "Некредитная", amount: 432100 }
          ],
          recoveries: [
            { type: "Восстановление по тех. сбою", date: "28.03.2025", amount: 1100000 }
          ],
          flags: { ib: true, is: true, behavior: false, model: false },
          timeline: [
            { label: "Обнаружен", date: "12.03.2025", state: "done" },
            { label: "Зарегистрирован", date: "14.03.2025", state: "done" },
            { label: "На проверке", date: "18.03.2025", state: "done" },
            { label: "Возмещение", date: "28.03.2025", state: "done" },
            { label: "Закрыт", date: "02.04.2025", state: "current" }
          ],
          links: { agr_num: "1234567890", appl_num: "ETSM-2025-019874" }
        },
        followups: [
          { label: "Нефин. последствия по EVE-5092355", prompt: "Покажи нефинансовые последствия по EVE-5092355" },
          { label: "Виновный сотрудник", prompt: "Кто работал с EVE-5092355?" },
          { label: "Журнал статусов", prompt: "Полный журнал изменения статусов по EVE-5092355" },
          { label: "Похожие ИОР", prompt: "Найди похожие ИОР по тех. сбоям в авторизации" }
        ]
      }
    ]
  },

  // Clarification flow
  {
    id: "s-clarif",
    title: "Возмещения за Q1 2025",
    group: "Сегодня",
    time: "10:08",
    messages: [
      { id: "m1", role: "user", time: "10:08", text: "Покажи возмещения за Q1" },
      {
        id: "m2",
        role: "assistant",
        time: "10:08",
        skill: SKILL_BY_ID["vozmeshenie_ior"],
        sseSteps: [
          { step: "routing", label: "Маршрутизация запроса", time: "+0.2c", status: "done" },
          { step: "selected", label: "Выбран навык: Возмещения - confidence 0.91", time: "+0.4c", status: "done" },
          { step: "extracting", label: "Извлечение параметров: period_from=2025-01-01, period_to=2025-03-31, tb=?", time: "+0.9c", status: "done" },
          { step: "clarifying", label: "Нужно уточнение от пользователя", time: "+1.0c", status: "active" }
        ],
        clarification: {
          question: "За какой ТБ выгрузить возмещения? Если по всему банку – нажмите «без фильтра».",
          options: ["СЗБ", "ЮЗБ", "СРБ", "Московский", "Без фильтра"]
        }
      },
      { id: "m3", role: "user", time: "10:08", text: "СЗБ" },
      {
        id: "m4",
        role: "assistant",
        time: "10:09",
        skill: SKILL_BY_ID["vozmeshenie_ior"],
        sseSteps: [
          { step: "routing", label: "Продолжаю предыдущий запрос", time: "+0.0c", status: "done" },
          { step: "selected", label: "Параметры собраны: period_from=2025-01-01, period_to=2025-03-31, tb=СЗБ", time: "+0.2c", status: "done" },
          { step: "executing", label: "Запуск notebook • 312 операций возмещения", time: "+0.5c", status: "done" },
          { step: "formatting", label: "Форматирование ответа GigaChat-3-Ultra", time: "+2.8c", status: "done" }
        ],
        text: "За **Q1 2025 по СЗБ** найдено **312 операций возмещения** на общую сумму **8.42 млн ₽**, относящихся к **198 инцидентам**. Из них:\\n\\n**Восстановление по тех. сбою** — 98 операций, 3.84 млн ₽\\n**Страховое возмещение** — 42 операции, 2.15 млн ₽\\n**Компенсация сотрудником** — 172 операции, 2.43 млн ₽.\\n\\nПодробный реестр прикреплен ниже.",
        excel: {
          name: "Возмещения по ИОР 2025-01-01 - 2025-03-31.xlsx",
          size: "412 КБ",
          rows: 312,
          columns: 28,
          sample: [
            ["EVE-5092355", "28.03.2025", "СЗБ", "Восст. техн. сбой", "1 100 000 ₽", "Закрыт"],
            ["EVE-5102881", "12.03.2025", "СЗБ", "Комп. сотрудника", "892 400 ₽", "Закрыт"],
            ["EVE-5034512", "08.02.2025", "СЗБ", "Страховое", "421 000 ₽", "Закрыт"],
            ["EVE-5012084", "21.01.2025", "СЗБ", "Комп. сотрудника", "78 950 ₽", "Закрыт"]
          ]
        },
        stats: {
          rows: 312,
          sum_total_loss: 8420000,
          duration_ms: 2810,
          top_tb: { label: "СЗБ", value: 312 },
          top_type: { label: "Восст. техн. сбой", value: 98, pct: 31 },
          top_process: { label: "Кредитование ФЛ", value: 218, pct: 70 }
        },
        followups: [
          { label: "Расширить до полугодия", prompt: "Возмещения по СЗБ за H1 2025" },
          { label: "По кому именно", prompt: "ФИО виновных по этим возмещениям" }
        ]
      }
    ]
  },

  // Generic past sessions
  { id: "s-hist-1", title: "Финансовые последствия Q2 2025", group: "Эта неделя", time: "вт", messages: [] },
  { id: "s-hist-2", title: "Удалённые ИОР за 2024", group: "Эта неделя", time: "пн", messages: [] },
  { id: "s-hist-3", title: "Нефин. последствия по ЮЗБ", group: "Прошлая неделя", time: "пн", messages: [] },
  { id: "s-hist-4", title: "EVE-6967014 – поведенческий риск", group: "Прошлая неделя", time: "ср", messages: [] },
  { id: "s-hist-5", title: "Топ-50 ИОР по сумме", group: "Прошлая неделя", time: "пн", messages: [] }
];

window.IOR_DATA = { SKILLS, SKILL_BY_ID, SAMPLE_SESSIONS, fmtRub, fmtNum, fmtMs };