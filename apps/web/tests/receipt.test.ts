import { describe, expect, it } from "vitest";
import { receiptAuthority } from "@/lib/receipt";
import { PreviewApplicationService } from "@/lib/service/preview";
import type { EvidenceView, SubmissionReceiptView } from "@/lib/service/types";

const siteShot: EvidenceView = {
  kind: "screenshot",
  label: "Page after pressing Submit",
  value: null,
  href: "/api/imx/applications/app_1/evidence/ev_1",
  observedAt: "2026-09-22T20:00:00Z",
  source: "site",
};
const userNote: EvidenceView = {
  kind: "user_report",
  label: "You reported a confirmation in a confirmation email",
  value: null,
  href: null,
  observedAt: "2026-09-22T20:05:00Z",
  source: "user",
};
const base: SubmissionReceiptView = {
  receiptId: "rcpt_1",
  submittedAt: "2026-09-22T20:05:00Z",
  confirmationReference: null,
  evidence: [],
};

describe("receipt authority", () => {
  it("treats USER_CONFIRMED with older site screenshots as user-reported", () => {
    const authority = receiptAuthority({
      ...base,
      evidence: [siteShot, userNote],
      confirmationMethod: "USER_CONFIRMED",
      confirmationAuthority: "user",
    });
    expect(authority.byUser).toBe(true);
    expect(authority.description).toMatch(/You reported/);
  });

  it("trusts an explicit site authority", () => {
    const authority = receiptAuthority({
      ...base,
      evidence: [siteShot],
      confirmationMethod: "ATS_CANDIDATE_PORTAL",
      confirmationAuthority: "site",
    });
    expect(authority).toMatchObject({ byUser: false, method: "ATS_CANDIDATE_PORTAL" });
  });

  it("never shows a USER_CONFIRMED method as site-confirmed, even if authority disagrees", () => {
    expect(
      receiptAuthority({ ...base, confirmationMethod: "USER_CONFIRMED", confirmationAuthority: "site" }).byUser,
    ).toBe(true);
  });

  it("falls back conservatively for services that don't send the fields yet", () => {
    expect(receiptAuthority({ ...base, evidence: [siteShot, userNote] }).byUser).toBe(true);
    expect(receiptAuthority({ ...base, evidence: [userNote] }).byUser).toBe(true);
    expect(receiptAuthority({ ...base, evidence: [siteShot] }).byUser).toBe(false);
  });
});

describe("preview reconciliation keeps who confirmed it", () => {
  async function unknown(service: PreviewApplicationService) {
    let view = await service.start({
      applicationUrl: "https://jobs.example.test/juniper-vale/apply",
      profile: {
        firstName: "Robin",
        lastName: "Vale",
        email: "robin.vale@example.test",
        phone: "",
        location: "Austin, TX",
        linkedinUrl: "",
        websiteUrl: "",
      },
      resumeId: "res_pv_lifecycle",
    });
    for (let i = 0; i < 30 && view.state !== "SUBMISSION_UNKNOWN"; i++) view = await service.status(view.id);
    return view;
  }

  it("a user report keeps the earlier site artifacts but is attributed to the user", async () => {
    const service = new PreviewApplicationService({ scenario: "uncertain", stepDelayMs: 0 });
    const view = await unknown(service);
    const settled = await service.reconcile(view.id, {
      kind: "user_found_confirmation",
      foundIn: "email",
      reference: null,
      note: null,
    });
    const receipt = settled.receipt!;
    expect(receipt.evidence.map((item) => item.source)).toEqual(["site", "user"]);
    expect(receipt).toMatchObject({ confirmationMethod: "USER_CONFIRMED", confirmationAuthority: "user" });
    expect(receiptAuthority(receipt).byUser).toBe(true);
  });

  it("a site recheck is attributed to the site", async () => {
    const service = new PreviewApplicationService({ scenario: "uncertain", stepDelayMs: 0 });
    const view = await unknown(service);
    await service.reconcile(view.id, { kind: "recheck" });
    const settled = await service.reconcile(view.id, { kind: "recheck" });
    expect(settled.receipt).toMatchObject({
      confirmationMethod: "ATS_CANDIDATE_PORTAL",
      confirmationAuthority: "site",
    });
  });
});
