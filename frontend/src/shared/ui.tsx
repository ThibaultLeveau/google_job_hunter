import { useId, type ComponentPropsWithRef, type ReactNode } from 'react';

type ButtonProps = ComponentPropsWithRef<'button'> & {
  tone?: 'primary' | 'secondary' | 'danger';
  loading?: boolean;
  loadingLabel?: string;
};

export function Button({
  tone = 'primary', loading = false, loadingLabel = 'Working…',
  disabled, type = 'button', className = '', children, ...props
}: ButtonProps) {
  return (
    <button {...props} type={type} className={`ui-button ${className}`} data-tone={tone}
      disabled={disabled || loading} aria-busy={loading || undefined}>
      {loading && <span className="ui-spinner" aria-hidden="true" />}
      {loading ? loadingLabel : children}
    </button>
  );
}

type InputProps = Omit<ComponentPropsWithRef<'input'>, 'type' | 'children'> & {
  type?: 'text' | 'email' | 'password' | 'search' | 'tel' | 'url' | 'number';
};

export function Input({ className = '', ...props }: InputProps) {
  return <input {...props} className={`ui-input ${className}`} />;
}

type FieldProps = InputProps & {
  label: string;
  description?: string;
  error?: string;
};

export function Field({ label, description, error, id, required, ...props }: FieldProps) {
  const generatedId = useId();
  const inputId = id ?? generatedId;
  const describedBy = [
    props['aria-describedby'], description && `${inputId}-hint`, error && `${inputId}-error`,
  ].filter(Boolean).join(' ') || undefined;
  return (
    <div className="ui-field">
      <label htmlFor={inputId}>
        {label}{required && <span className="ui-field-hint" aria-hidden="true">Required</span>}
      </label>
      <Input {...props} id={inputId} required={required}
        aria-invalid={error ? true : props['aria-invalid']} aria-describedby={describedBy} />
      {description && <p id={`${inputId}-hint`} className="ui-field-hint">{description}</p>}
      {error && <p id={`${inputId}-error`} className="ui-field-error" role="alert">Error: {error}</p>}
    </div>
  );
}

type Tone = 'neutral' | 'accent' | 'danger';

export function Badge({ children, tone = 'neutral' }: { children: string; tone?: Tone }) {
  return <span className="ui-badge" data-tone={tone}>{children}</span>;
}

const stateLabels = {
  loading: 'Loading', empty: 'Nothing here yet', error: 'Needs attention',
  unsupported: 'Unsupported', unknown: 'Unknown', stale: 'Out of date',
};

type StatePanelProps = {
  state: keyof typeof stateLabels;
  title: string;
  description: string;
  action?: ReactNode;
};

export function StatePanel({ state, title, description, action }: StatePanelProps) {
  const titleId = useId();
  return (
    <section className="ui-state" aria-labelledby={titleId}>
      <div role={state === 'error' ? 'alert' : 'status'} aria-atomic="true">
        <Badge tone={state === 'error' ? 'danger' : 'neutral'}>{stateLabels[state]}</Badge>
        <h2 id={titleId}>{title}</h2>
        <p>{description}</p>
      </div>
      {action && <div className="ui-state-action">{action}</div>}
    </section>
  );
}
