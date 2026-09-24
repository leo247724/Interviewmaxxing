import type { AnswerValue, AttestationView, CandidateProfileInput, RequiredQuestionView } from "./service/types";

export type FieldErrors = Record<string, string>;

/** Profile fields the desk needs before it can start any application. */
export const REQUIRED_PROFILE_FIELDS = ["firstName", "lastName", "email"] as const;

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function validateApplicationUrl(raw: string): string | null {
  const value = raw.trim();
  if (!value) return "Paste the link to the application page.";
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return "That isn't a complete web address. It should start with https://";
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") {
    return "Use the application page's web address (https://…).";
  }
  if (!url.hostname.includes(".") && url.hostname !== "localhost") {
    return "That address is missing a domain, like jobs.example.com.";
  }
  return null;
}

function validateOptionalUrl(raw: string, label: string): string | null {
  const value = raw.trim();
  if (!value) return null;
  try {
    const url = new URL(value);
    if (url.protocol === "https:" || url.protocol === "http:") return null;
  } catch {
    // fall through
  }
  return `${label} should be a full web address starting with https://`;
}

export function validateProfile(profile: CandidateProfileInput): FieldErrors {
  const errors: FieldErrors = {};
  if (!profile.firstName.trim()) errors.firstName = "Enter your first name.";
  if (!profile.lastName.trim()) errors.lastName = "Enter your last name.";
  if (!profile.email.trim()) {
    errors.email = "Enter the email address employers should use to reach you.";
  } else if (!EMAIL_PATTERN.test(profile.email.trim())) {
    errors.email = "That email address looks incomplete, for example name@example.com.";
  }
  if (profile.phone.trim() && profile.phone.replace(/\D/g, "").length < 7) {
    errors.phone = "That phone number looks too short. Include the area code.";
  }
  const linkedin = validateOptionalUrl(profile.linkedinUrl, "LinkedIn");
  if (linkedin) errors.linkedinUrl = linkedin;
  const website = validateOptionalUrl(profile.websiteUrl, "Website");
  if (website) errors.websiteUrl = website;
  return errors;
}

export interface ApplyFormInput {
  applicationUrl: string;
  profile: CandidateProfileInput;
  resumeId: string | null;
}

export function validateApplyForm(input: ApplyFormInput): FieldErrors {
  const errors: FieldErrors = {};
  const urlError = validateApplicationUrl(input.applicationUrl);
  if (urlError) errors.applicationUrl = urlError;
  Object.assign(errors, validateProfile(input.profile));
  if (!input.resumeId) errors.resumeId = "Choose a saved resume or upload one. Applications are sent with it.";
  return errors;
}

export function isEmptyAnswer(value: AnswerValue | undefined): boolean {
  if (value === null || value === undefined) return true;
  if (typeof value === "string") return value.trim() === "";
  if (Array.isArray(value)) return value.length === 0;
  return false;
}

/** Required-answer message for a site lookup (location, school or company search box). */
function missingLookupMessage(question: RequiredQuestionView, enteringOther: boolean): string {
  if (enteringOther) {
    return "Type the value to enter in the site's search box, or choose one of the site's suggestions.";
  }
  return question.options && question.options.length > 0
    ? "Choose one of the site's suggestions, or enter a different value."
    : "Type what the site should look up to continue.";
}

/**
 * Validate answers to the questions the service asked.
 * `requireComplete` is true when continuing the application and false when
 * only saving a draft, which may leave required questions blank.
 * `lookupOther` marks lookup questions whose "Enter a different value…" text
 * box is in use. Any non-blank text answers a lookup: it is typed into the
 * site's search box, so it never has to be one of the suggestions.
 */
export function validateAnswers(
  questions: RequiredQuestionView[],
  attestations: AttestationView[],
  answers: Record<string, AnswerValue>,
  accepted: Record<string, boolean>,
  requireComplete: boolean,
  lookupOther: Readonly<Record<string, boolean>> = {},
): FieldErrors {
  const errors: FieldErrors = {};
  for (const question of questions) {
    const value = answers[question.id];
    if (isEmptyAnswer(value)) {
      if (requireComplete && question.required) {
        errors[question.id] = question.lookup
          ? missingLookupMessage(question, lookupOther[question.id] === true)
          : question.control === "single_select" || question.control === "boolean"
            ? "Choose an answer to continue."
            : question.control === "multi_select"
              ? "Choose at least one option to continue."
              : "Answer this question to continue.";
      }
      continue;
    }
    if (typeof value === "string") {
      if (question.maxLength && value.length > question.maxLength) {
        errors[question.id] = `Keep this under ${question.maxLength} characters (currently ${value.length}).`;
      } else if (question.control === "number" && !/^\d[\d,]*(\.\d+)?$/.test(value.trim())) {
        errors[question.id] = "Enter a number using digits only.";
      } else if (question.control === "email" && !EMAIL_PATTERN.test(value.trim())) {
        errors[question.id] = "Enter a complete email address.";
      } else if (question.control === "url" && validateOptionalUrl(value, "This answer")) {
        errors[question.id] = "Enter a full web address starting with https://";
      }
    }
  }
  if (requireComplete) {
    for (const attestation of attestations) {
      if (attestation.required && !accepted[attestation.id]) {
        errors[attestation.id] =
          "The site requires this statement. Check it only if it is true for you; otherwise the application stays paused.";
      }
    }
  }
  return errors;
}
