import { useId, type ReactNode } from "react";

export function SettingsAccordion({ title, open, onToggle, children }: {
  title: string; open: boolean; onToggle: () => void; children: ReactNode;
}) {
  const id = useId();
  return <section className="accordion">
    <h4 className="accordion-title">
      <button type="button" id={`${id}-header`} className="accordion-header" aria-expanded={open}
        aria-controls={`${id}-panel`} onClick={onToggle}>
        <span>{title}</span>
        <span className="accordion-chevron" aria-hidden="true">{open ? "▾" : "▸"}</span>
      </button>
    </h4>
    <div id={`${id}-panel`} role="region" aria-labelledby={`${id}-header`} hidden={!open}
      className="accordion-panel form-stack">{children}</div>
  </section>;
}
