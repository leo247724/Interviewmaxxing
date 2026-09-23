"use client";

import { useRef, type FormEvent } from "react";
import type { CandidateProfileInput, ResumeDocumentView } from "@/lib/service/types";
import type { FieldErrors } from "@/lib/validation";
import { formatBytes, formatDate } from "@/lib/format";
import { ErrorSummary } from "./ErrorSummary";
import { TextField } from "./fields";

const FIELD_ORDER = [
  "applicationUrl",
  "firstName",
  "lastName",
  "email",
  "phone",
  "location",
  "linkedinUrl",
  "websiteUrl",
  "resumeId",
  "resumeFile",
] as const;

const FIELD_TARGET: Record<string, string> = { resumeId: "resume-choices", resumeFile: "resume-file" };

export interface ComposeFormProps {
  applicationUrl: string;
  onApplicationUrl: (value: string) => void;
  profile: CandidateProfileInput;
  onProfile: (profile: CandidateProfileInput) => void;
  resumes: ResumeDocumentView[];
  resumeId: string | null;
  onResumeId: (id: string) => void;
  onUpload: (file: File) => Promise<boolean>;
  uploading: boolean;
  errors: FieldErrors;
  submitCount: number;
  alert: string | null;
  starting: boolean;
  candidateLoaded: boolean;
  onSubmit: () => void;
}

export function ComposeForm(props: ComposeFormProps) {
  const { profile, errors } = props;
  const fileRef = useRef<HTMLInputElement>(null);

  const summary = FIELD_ORDER.filter((key) => errors[key]).map((key) => ({
    fieldId: FIELD_TARGET[key] ?? key,
    message: errors[key],
  }));

  function setField(key: keyof CandidateProfileInput) {
    return (value: string) => props.onProfile({ ...profile, [key]: value });
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    props.onSubmit();
  }

  return (
    <div className="compose-layout">
      <form className="compose" method="post" noValidate onSubmit={handleSubmit} aria-labelledby="compose-title">
        <header className="compose__intro">
          <h1 id="compose-title" className="display">
            Apply to a job you&rsquo;ve chosen.
          </h1>
          <p className="lede">
            Paste the application page. The desk reads the form, fills it with your details and resume, submits it, and
            keeps the site&rsquo;s confirmation as your receipt.
          </p>
        </header>

        <ErrorSummary
          title={`Fix ${summary.length === 1 ? "this" : `these ${summary.length} things`} before applying`}
          items={summary}
          attempt={props.submitCount}
        />

        <section className="sheet" aria-labelledby="sheet-url">
          <SheetHeading number="01" id="sheet-url" title="Application page" />
          <TextField
            id="applicationUrl"
            label="Application link"
            value={props.applicationUrl}
            onChange={props.onApplicationUrl}
            error={errors.applicationUrl}
            required
            type="url"
            inputMode="url"
            autoComplete="off"
            spellCheck={false}
            placeholder="https://jobs.example.com/company/role/apply"
            hint="The page where the application form starts, copied from your browser."
            variant="hero"
          />
        </section>

        <section className="sheet" aria-labelledby="sheet-you">
          <SheetHeading
            number="02"
            id="sheet-you"
            title="Your details"
            note={
              props.candidateLoaded
                ? "Loaded from your saved profile where available. Edits apply to this application."
                : "Loading your saved profile…"
            }
          />
          <div className="field-grid">
            <TextField
              id="firstName"
              label="First name"
              value={profile.firstName}
              onChange={setField("firstName")}
              error={errors.firstName}
              required
              autoComplete="given-name"
            />
            <TextField
              id="lastName"
              label="Last name"
              value={profile.lastName}
              onChange={setField("lastName")}
              error={errors.lastName}
              required
              autoComplete="family-name"
            />
            <TextField
              id="email"
              label="Email"
              value={profile.email}
              onChange={setField("email")}
              error={errors.email}
              required
              type="email"
              inputMode="email"
              autoComplete="email"
              spellCheck={false}
            />
            <TextField
              id="phone"
              label="Phone"
              value={profile.phone}
              onChange={setField("phone")}
              error={errors.phone}
              type="tel"
              inputMode="tel"
              autoComplete="tel"
              hint="If the form requires a phone and this is blank, you'll be asked."
            />
            <TextField
              id="location"
              label="City and region"
              value={profile.location}
              onChange={setField("location")}
              error={errors.location}
              autoComplete="address-level2"
              hint="City, then state or region, e.g. Austin, TX. Add a country only if you want one on the form."
            />
            <TextField
              id="linkedinUrl"
              label="LinkedIn"
              value={profile.linkedinUrl}
              onChange={setField("linkedinUrl")}
              error={errors.linkedinUrl}
              type="url"
              inputMode="url"
              autoComplete="url"
              spellCheck={false}
            />
            <TextField
              id="websiteUrl"
              label="Website or portfolio"
              value={profile.websiteUrl}
              onChange={setField("websiteUrl")}
              error={errors.websiteUrl}
              type="url"
              inputMode="url"
              spellCheck={false}
            />
          </div>
        </section>

        <section className="sheet" aria-labelledby="sheet-resume">
          <SheetHeading
            number="03"
            id="sheet-resume"
            title="Resume"
            note="Sent with this application exactly as uploaded."
          />
          <fieldset
            id="resume-choices"
            className={`resume-choices${errors.resumeId ? " is-invalid" : ""}`}
            aria-describedby={errors.resumeId ? "resume-choices-error" : undefined}
            tabIndex={-1}
          >
            <legend className="visually-hidden">Resume to send</legend>
            {props.resumes.length === 0 ? (
              <p className="resume-choices__empty">
                No saved resumes yet. Upload the one to send with this application.
              </p>
            ) : (
              props.resumes.map((resume) => (
                <label key={resume.id} className="resume-option">
                  <input
                    type="radio"
                    name="resume"
                    value={resume.id}
                    checked={props.resumeId === resume.id}
                    onChange={() => props.onResumeId(resume.id)}
                  />
                  <span className="resume-option__body">
                    <span className="resume-option__name">{resume.fileName}</span>
                    <span className="resume-option__meta">
                      {fileKind(resume.fileName)} · {formatBytes(resume.sizeBytes)} · added{" "}
                      {formatDate(resume.uploadedAt)}
                    </span>
                  </span>
                </label>
              ))
            )}
            {errors.resumeId && (
              <p id="resume-choices-error" className="field__error">
                {errors.resumeId}
              </p>
            )}
          </fieldset>

          <div className="upload">
            <input
              ref={fileRef}
              id="resume-file"
              className="upload__input"
              type="file"
              accept=".pdf,.doc,.docx,application/pdf,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
              aria-describedby={`resume-file-hint${errors.resumeFile ? " resume-file-error" : ""}`}
              aria-invalid={errors.resumeFile ? true : undefined}
              disabled={props.uploading}
              onChange={async (event) => {
                const file = event.target.files?.[0];
                if (!file) return;
                await props.onUpload(file);
                if (fileRef.current) fileRef.current.value = "";
              }}
            />
            <label htmlFor="resume-file" className="button button--secondary upload__button">
              {props.uploading ? "Uploading…" : "Upload a resume"}
            </label>
            <p id="resume-file-hint" className="field__hint">
              PDF or Word, up to 10 MB.
            </p>
            {errors.resumeFile && (
              <p id="resume-file-error" className="field__error">
                {errors.resumeFile}
              </p>
            )}
          </div>
        </section>

        <section className="dispatch" aria-labelledby="dispatch-title">
          <h2 id="dispatch-title" className="visually-hidden">
            Apply
          </h2>
          <p className="dispatch__terms">
            Applying submits this application to the employer with the details above. You&rsquo;ll be stopped only if
            the site asks something your profile doesn&rsquo;t answer, needs a statement only you can make, or needs you
            to sign in.
          </p>
          {props.alert && (
            <p className="form-alert" role="alert">
              {props.alert}
            </p>
          )}
          <button type="submit" className="button button--primary button--apply" disabled={props.starting}>
            {props.starting ? "Starting…" : "Apply and submit"}
            <span aria-hidden="true" className="button__arrow">
              →
            </span>
          </button>
        </section>
      </form>
      <aside className="margin-notes" aria-label="How the desk handles your application">
        <div className="margin-note">
          <h2 className="margin-note__title">When it stops</h2>
          <p>
            Only for a question your profile can&rsquo;t answer, a statement only you can make, or a sign-in or CAPTCHA.
          </p>
        </div>
        <div className="margin-note">
          <h2 className="margin-note__title">What it never guesses</h2>
          <p>Salary, work authorization, sponsorship, demographic questions and consent are always answered by you.</p>
        </div>
        <div className="margin-note">
          <h2 className="margin-note__title">What &ldquo;submitted&rdquo; means</h2>
          <p>
            The site confirmed it. If the site doesn&rsquo;t confirm, you&rsquo;ll see &ldquo;not confirmed&rdquo; and
            the desk won&rsquo;t retry on its own.
          </p>
        </div>
      </aside>
    </div>
  );
}

function SheetHeading({ number, id, title, note }: { number: string; id: string; title: string; note?: string }) {
  return (
    <div className="sheet__heading">
      <span className="sheet__number" aria-hidden="true">
        {number}
      </span>
      <h2 id={id} className="sheet__title">
        {title}
      </h2>
      {note && <p className="sheet__note">{note}</p>}
    </div>
  );
}

function fileKind(name: string) {
  const extension = name.split(".").pop()?.toUpperCase();
  return extension && extension.length <= 4 ? extension : "File";
}
