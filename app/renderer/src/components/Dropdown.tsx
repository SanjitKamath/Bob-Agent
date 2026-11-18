import { useEffect, useRef, useState } from "react";

export interface Choice<T extends string> {
  value: T;
  label: string;
}

/**
 * A drop-down drawn in the page. A native <select> opens a separate macOS menu, which does not
 * open reliably from a menu bar window; this list is part of the page, so it always does.
 */
export function Dropdown<T extends string>({
  value,
  choices,
  onChange,
  label,
}: {
  value: T;
  choices: Choice<T>[];
  onChange: (value: T) => void;
  label: string;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const current = choices.find((choice) => choice.value === value) ?? choices[0];

  useEffect(() => {
    if (!open) return;
    const away = (event: MouseEvent): void => {
      if (root.current && !root.current.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent): void => {
      if (event.key === "Escape") {
        event.stopPropagation();
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", escape, true);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", escape, true);
    };
  }, [open]);

  return (
    <div className="dropdown" ref={root}>
      <button
        type="button"
        className="dropdown-button"
        aria-label={label}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <span>{current.label}</span>
        <span aria-hidden="true">▾</span>
      </button>
      {open && (
        <ul className="dropdown-list" role="listbox" aria-label={label}>
          {choices.map((choice) => (
            <li
              key={choice.value}
              role="option"
              aria-selected={choice.value === value}
              className={choice.value === value ? "selected" : ""}
              onClick={() => {
                onChange(choice.value);
                setOpen(false);
              }}
            >
              {choice.label}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
