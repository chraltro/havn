import React from "react";

/*
 * The app's primary navigation: one icon per destination down the left edge,
 * with secondary items (agent, settings) pinned to the bottom. Under 760px it
 * becomes a bottom bar holding the primary destinations only.
 */

const ICONS = {
  home: <path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z" />,
  code: <path d="M8 6l-6 6 6 6M16 6l6 6-6 6" />,
  data: (
    <>
      <ellipse cx="12" cy="5.5" rx="8" ry="3" />
      <path d="M4 5.5v13c0 1.7 3.6 3 8 3s8-1.3 8-3v-13M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3" />
    </>
  ),
  pulse: <path d="M3 12h4l3-8 4 16 3-8h4" />,
  ship: (
    <>
      <circle cx="6" cy="6" r="2.5" />
      <circle cx="6" cy="18" r="2.5" />
      <circle cx="18" cy="12" r="2.5" />
      <path d="M6 8.5v7M8.3 6.8c5 .5 7.4 2.3 7.7 3.4" />
    </>
  ),
  agent: <path d="M12 3l2.2 4.8L19 9l-3.5 3.6.9 5.4-4.4-2.5-4.4 2.5.9-5.4L5 9l4.8-1.2z" />,
  gear: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.8 1.2V21a2 2 0 1 1-4 0v-.1A1.7 1.7 0 0 0 7 19.4l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.7 1.7 0 0 0 3 14H3a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 4.6 7l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.7 1.7 0 0 0 10 3V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 2.8 1.2l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1A1.7 1.7 0 0 0 21 10h.1a2 2 0 1 1 0 4z" />
    </>
  ),
};

function Icon({ name }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {ICONS[name]}
    </svg>
  );
}

const CSS = `
.havn-rail {
  width: 64px; flex-shrink: 0; display: flex; flex-direction: column; align-items: center;
  gap: 2px; padding: 10px 0; background: var(--havn-bg-tertiary);
  border-right: 1px solid var(--havn-border); z-index: 20;
}
.havn-rail-logo { background: none; border: none; padding: 0; margin-bottom: 12px; cursor: pointer; line-height: 0; }
.havn-rail-item {
  width: 54px; padding: 7px 0 5px; border-radius: var(--havn-radius); border: none; background: none;
  display: flex; flex-direction: column; align-items: center; gap: 3px; cursor: pointer;
  color: var(--havn-text-secondary); font: 500 10.5px var(--havn-font); position: relative;
}
.havn-rail-item svg { width: 18px; height: 18px; }
.havn-rail-item:hover { background: var(--havn-bg-secondary); color: var(--havn-text); }
.havn-rail-item:focus-visible { outline: 2px solid var(--havn-accent); outline-offset: 1px; }
.havn-rail-item[aria-current="page"], .havn-rail-item[aria-pressed="true"] {
  color: var(--havn-accent); background: color-mix(in srgb, var(--havn-accent) 13%, transparent);
}
.havn-rail-badge {
  position: absolute; top: 2px; right: 8px; min-width: 16px; height: 16px; padding: 0 4px;
  border-radius: 8px; background: var(--havn-red); color: #fff; font: 600 9.5px/16px var(--havn-font);
}
.havn-rail-spacer { flex: 1; }
@media (max-width: 760px) {
  .havn-rail {
    position: fixed; left: 0; right: 0; bottom: 0; width: auto; height: 58px;
    flex-direction: row; justify-content: space-around; padding: 0 4px;
    border-right: none; border-top: 1px solid var(--havn-border);
  }
  .havn-rail-logo, .havn-rail-spacer, .havn-rail-item[data-secondary="true"] { display: none; }
  .havn-rail-item { width: auto; flex: 1; }
  /* The shell makes room for the bottom bar; the file sidebar and the
     breadcrumb give way to the page on a phone. */
  .havn-shell { padding-bottom: 58px; }
  .havn-sidebar, .havn-crumb, .havn-omnibox-kbd, .havn-userinfo { display: none !important; }
  .havn-ship { grid-template-columns: 1fr !important; height: auto !important; overflow: auto; }
  .havn-ship > * { border-left: none !important; border-right: none !important; border-bottom: 1px solid var(--havn-border); }
  .havn-layers { grid-template-columns: 1fr 1fr !important; }
}
`;

export default function NavRail({ sections, activeSection, onNavigate, agentOpen, onToggleAgent, badges = {} }) {
  const primary = sections.filter((s) => !s.secondary);
  const secondary = sections.filter((s) => s.secondary);
  return (
    <nav className="havn-rail" aria-label="Main navigation" data-havn-guide="tabs">
      <style>{CSS}</style>
      <button className="havn-rail-logo" onClick={() => onNavigate(primary[0].id)} aria-label="havn home" title="Home">
        <img src="/logo.svg" alt="" width="28" height="28" />
      </button>
      {primary.map((s) => (
        <RailItem key={s.id} section={s} index={sections.indexOf(s)} active={activeSection === s.id}
                  badge={badges[s.id]} onClick={() => onNavigate(s.id)} />
      ))}
      <div className="havn-rail-spacer" />
      <button
        className="havn-rail-item"
        data-secondary="true"
        onClick={onToggleAgent}
        aria-pressed={!!agentOpen}
        title="Toggle agent sidebar"
      >
        <Icon name="agent" />
        Agent
      </button>
      {secondary.map((s) => (
        <RailItem key={s.id} section={s} index={sections.indexOf(s)} active={activeSection === s.id}
                  secondary onClick={() => onNavigate(s.id)} />
      ))}
    </nav>
  );
}

function RailItem({ section, index, active, badge, secondary, onClick }) {
  return (
    <button
      className="havn-rail-item"
      data-secondary={secondary ? "true" : undefined}
      data-havn-tab=""
      data-havn-active={active ? "true" : "false"}
      aria-current={active ? "page" : undefined}
      title={`${section.label} (Alt+${index + 1})`}
      onClick={onClick}
    >
      <Icon name={section.icon} />
      {section.label}
      {badge > 0 && (
        <span className="havn-rail-badge" aria-label={`${badge} need attention`}>{badge > 99 ? "99+" : badge}</span>
      )}
    </button>
  );
}
