/* skills-modal.jsx – flat list */

function SkillsModal({ open, onClose, onPick, skills }) {
  if (!open) return null;
  const items = skills && skills.length ? skills : (window.IOR_DATA && window.IOR_DATA.SKILLS) || [];

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div>
            <h3>Навыки</h3>
            <div className="modal-sub">{items.length} активных • авто-обнаружение из knowledge_base/scripts/*.md</div>
          </div>
          <button className="btn-icon" onClick={onClose} aria-label="Закрыть">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <path d="M18 6 6 18M6 6l12 12"/>
            </svg>
          </button>
        </div>
        <div className="modal-body">
          {items.map((s, i) => {
            const sid = s.id || s.skill_id;
            const triggers = s.triggers || [];
            const placeholder = s.placeholder || (s.examples && s.examples[0]) || s.title;
            return (
              <div key={sid} className="skill-row" onClick={() => { onPick(placeholder); onClose(); }}>
                <span className="skill-num">{String(i + 1).padStart(2, '0')}</span>
                <div className="skill-info">
                  <div className="skill-row-head">
                    <span className="skill-row-title">{s.title}</span>
                    <span className="skill-row-id">{sid}</span>
                  </div>
                  <div className="skill-row-desc">{s.desc || s.description || s.subtitle || ''}</div>
                  <div className="skill-row-triggers">
                    {triggers.slice(0, 4).map((t, j) => (
                      <span key={j} className="trigger-tag">{t}</span>
                    ))}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

window.SkillsModal = SkillsModal;