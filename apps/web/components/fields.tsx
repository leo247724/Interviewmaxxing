"use client";

import type { HTMLAttributes, InputHTMLAttributes } from "react";

export function describedBy(id: string, hint?: string | null, error?: string | null) {
  const ids = [hint ? `${id}-hint` : null, error ? `${id}-error` : null].filter(Boolean);
  return ids.length ? ids.join(" ") : undefined;
}

export function RequirementTag({ required }: { required?: boolean }) {
  return required ? <span className="tag tag--required">Required</span> : <span className="tag">Optional</span>;
}

export function FieldMessages({ id, hint, error }: { id: string; hint?: string | null; error?: string | null }) {
  return (
    <>
      {hint && (
        <p id={`${id}-hint`} className="field__hint">
          {hint}
        </p>
      )}
      {error && (
        <p id={`${id}-error`} className="field__error">
          {error}
        </p>
      )}
    </>
  );
}

interface TextFieldProps {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  error?: string;
  hint?: string;
  required?: boolean;
  type?: InputHTMLAttributes<HTMLInputElement>["type"];
  inputMode?: HTMLAttributes<HTMLInputElement>["inputMode"];
  autoComplete?: string;
  spellCheck?: boolean;
  placeholder?: string;
  variant?: "hero";
}

export function TextField(props: TextFieldProps) {
  const { id, error, hint } = props;
  return (
    <div className={`field${props.variant === "hero" ? " field--hero" : ""}${error ? " is-invalid" : ""}`}>
      <label htmlFor={id} className="field__label">
        {props.label} <RequirementTag required={props.required} />
      </label>
      <input
        id={id}
        name={id}
        className="input"
        type={props.type ?? "text"}
        inputMode={props.inputMode}
        autoComplete={props.autoComplete}
        spellCheck={props.spellCheck}
        placeholder={props.placeholder}
        value={props.value}
        required={props.required}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(id, hint, error)}
        onChange={(event) => props.onChange(event.target.value)}
      />
      <FieldMessages id={id} hint={hint} error={error} />
    </div>
  );
}
