/** The five inputs, the filing chooser, and the three actions. */
import * as React from "react";
import { FORMS, TABLES, type FieldErrors, type Form, type FormValues } from "../domain.js";
import type { FilingSummary } from "../domain.js";
import type { Profile } from "../../../src/protocol.js";

interface Props {
  values: FormValues;
  errors: FieldErrors;
  filings: FilingSummary[];
  selectedFilingId: string | null;
  busy: boolean;
  canExtract: boolean;
  onChange: (name: keyof FormValues, value: string) => void;
  onSelectFiling: (id: string) => void;
  onFind: () => void;
  onExtract: () => void;
  onCancel: () => void;
}

function Field(props: {
  id: keyof FormValues;
  label: string;
  error?: string;
  hint?: React.ReactNode;
  children: React.ReactNode;
}) {
  const describedBy = [props.error ? `${props.id}-error` : null, props.hint ? `${props.id}-hint` : null]
    .filter(Boolean)
    .join(" ");
  return (
    <div className="field">
      <label htmlFor={props.id}>{props.label}</label>
      {React.isValidElement(props.children)
        ? React.cloneElement(props.children as React.ReactElement<any>, {
            "aria-invalid": props.error ? true : undefined,
            "aria-describedby": describedBy || undefined,
          })
        : props.children}
      {props.hint ? (
        <p className="hint" id={`${props.id}-hint`}>
          {props.hint}
        </p>
      ) : null}
      {props.error ? (
        <p className="error" id={`${props.id}-error`} role="alert">
          {props.error}
        </p>
      ) : null}
    </div>
  );
}

export function FilingForm(props: Props) {
  const { values, errors } = props;
  const set = (name: keyof FormValues) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    props.onChange(name, e.target.value);

  return (
    <form
      className="filing-form"
      // `noValidate` on purpose. The browser's own validation on
      // `type="email"` silently refuses to submit and shows a bubble that
      // cannot be styled, cannot be associated with the field for a screen
      // reader, and does not say what this app actually needs the address for.
      // Our validation reports every field at once and explains each one.
      noValidate
      onSubmit={(e) => {
        e.preventDefault();
        props.onFind();
      }}
    >
      <div className="form-intro">
        <span className="eyebrow">SEC disclosure explorer</span>
        <h1>Extract an SEC disclosure table</h1>
        <p>
          Choose a company, filing year and table. This server downloads the filing;
          Python extracts and normalizes it in your browser.
        </p>
      </div>

      <div className="form-divider" />

      <Field
        id="email"
        label="Contact email"
        error={errors.email}
        hint={
          // Said before the field, not in a policy page. The address goes to a
          // third party, which is the kind of thing a person should know before
          // typing rather than after.
          //
          // The wording is careful on two points. SEC asks the *requester* to
          // identify itself with a monitored contact; it does not ask a website
          // to collect every visitor's address. Passing yours through instead
          // of shipping one shared address is this project's choice, and it is
          // described as one. And the privacy claim covers what this
          // application does — it cannot speak for hosting, intermediaries or
          // whatever else is between a browser and a server.
          <>
            Sent to SEC in the <code>User-Agent</code> header. Asking for your address is
            this app's design choice, not an SEC rule. The application does not
            intentionally store or log it; hosting providers, intermediaries and browser
            extensions are outside that promise.
          </>
        }
      >
        <input
          id="email"
          name="email"
          type="email"
          autoComplete="email"
          value={values.email}
          onChange={set("email")}
          placeholder="you@example.com"
        />
      </Field>

      <div className="row">
        <Field id="ticker" label="Ticker" error={errors.ticker}>
          <input
            id="ticker"
            name="ticker"
            value={values.ticker}
            onChange={set("ticker")}
            placeholder="DAL"
            autoCapitalize="characters"
            spellCheck={false}
          />
        </Field>

        <Field id="year" label="Filing year" error={errors.year}>
          <input
            id="year"
            name="year"
            inputMode="numeric"
            value={values.year}
            onChange={set("year")}
            placeholder="1997"
          />
        </Field>

        <Field id="form" label="Form" error={errors.form}>
          <select id="form" name="form" value={values.form} onChange={set("form")}>
            {FORMS.map((f) => (
              <option key={f} value={f}>
                {f}
              </option>
            ))}
          </select>
        </Field>
      </div>

      <Field
        id="table"
        label="Table"
        error={errors.table}
        hint="Three disclosure tables are supported today."
      >
        <select id="table" name="table" value={values.table} onChange={set("table")}>
          {TABLES.map((t) => (
            <option key={t.value} value={t.value}>
              {t.label} — {t.item}
            </option>
          ))}
        </select>
      </Field>

      {props.filings.length > 0 ? (
        <Field
          id="filing"
          label={props.filings.length === 1 ? "Filing" : `Filing (${props.filings.length} match this year)`}
          hint={
            props.filings.length > 1
              ? "More than one filing matched. A company that files twice in a year is usually correcting the first, so the latest is selected."
              : undefined
          }
        >
          <select
            id="filing"
            name="filing"
            value={props.selectedFilingId ?? ""}
            onChange={(e) => props.onSelectFiling(e.target.value)}
          >
            {props.filings.map((f) => (
              <option key={f.id} value={f.id}>
                {f.filingDate} · {f.form}
                {f.route === "complete_submission" ? " · complete submission" : ""}
              </option>
            ))}
          </select>
        </Field>
      ) : null}

      <div className="actions">
        <button type="submit" className="primary-action" disabled={props.busy}>
          <span aria-hidden="true">⌕</span> Find filing
        </button>
        <button type="button" className="extract-action" onClick={props.onExtract} disabled={!props.canExtract}>
          <span aria-hidden="true">▤</span> Extract table
        </button>
        <button type="button" className="secondary" onClick={props.onCancel} disabled={!props.busy}>
          Cancel
        </button>
      </div>

      <div className="supported" id="supported">
        <span>Supported disclosure tables</span>
        <div className="supported-tags">
          <span>Executive compensation</span>
          <span>Director compensation</span>
          <span>Beneficial ownership</span>
        </div>
      </div>
    </form>
  );
}
