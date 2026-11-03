import { cloneElement, useId, type ReactElement, type ReactNode } from "react";

type FieldControlProps = {
  id?: string;
  required?: boolean;
  "aria-describedby"?: string;
  "aria-invalid"?: boolean | "true" | "false";
};

export type FieldProps = {
  label: ReactNode;
  control: ReactElement<FieldControlProps>;
  hint?: ReactNode;
  error?: ReactNode;
  required?: boolean;
  className?: string;
};

function Field({ label, control, hint, error, required, className }: FieldProps) {
  const generatedId = useId();
  const controlId = control.props.id ?? `${generatedId}-control`;
  const hintId = hint ? `${generatedId}-hint` : undefined;
  const errorId = error ? `${generatedId}-error` : undefined;
  const describedBy = [control.props["aria-describedby"], hintId, errorId].filter(Boolean).join(" ") || undefined;
  const classes = ["ui-field", className].filter(Boolean).join(" ");
  const isRequired = required ?? Boolean(control.props.required);

  const linkedControl = cloneElement(control, {
    id: controlId,
    required: isRequired,
    "aria-describedby": describedBy,
    "aria-invalid": error ? true : control.props["aria-invalid"]
  });

  return (
    <div className={classes}>
      <label className="ui-field__label" htmlFor={controlId}>
        {label}
        {isRequired && <span aria-hidden="true"> *</span>}
      </label>
      {linkedControl}
      {hint && <span id={hintId} className="ui-field__hint">{hint}</span>}
      {error && <span id={errorId} className="ui-field__error">{error}</span>}
    </div>
  );
}

export default Field;
