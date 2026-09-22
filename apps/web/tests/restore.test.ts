import { describe, expect, it } from "vitest";
import { ServiceError } from "@/lib/service/errors";
import { ACTIVE_ID_KEY, restoreActiveApplication } from "@/lib/restore";
import type { ApplicationView } from "@/lib/service/types";

function memoryStorage(initial: Record<string, string> = {}) {
  const data = new Map(Object.entries(initial));
  return {
    getItem: (key: string) => data.get(key) ?? null,
    removeItem: (key: string) => void data.delete(key),
    data,
  };
}

const view = { id: "app_1", state: "SUBMITTING" } as ApplicationView;

describe("restoreActiveApplication", () => {
  it("does nothing without a saved application", async () => {
    const storage = memoryStorage();
    const outcome = await restoreActiveApplication({ status: async () => view }, storage);
    expect(outcome).toEqual({ kind: "none" });
  });

  it("keeps the id after a transient failure, then restores the same id", async () => {
    const storage = memoryStorage({ [ACTIVE_ID_KEY]: "app_1" });
    const requested: string[] = [];
    let calls = 0;
    const service = {
      status: async (id: string) => {
        requested.push(id);
        calls += 1;
        if (calls === 1) throw new ServiceError("unavailable", "The application service isn't running.");
        return view;
      },
    };

    const first = await restoreActiveApplication(service, storage);
    expect(first).toEqual({
      kind: "pending",
      applicationId: "app_1",
      code: "unavailable",
      message: "The application service isn't running.",
    });
    expect(storage.data.get(ACTIVE_ID_KEY)).toBe("app_1");

    const second = await restoreActiveApplication(service, storage);
    expect(second).toEqual({ kind: "restored", view });
    expect(requested).toEqual(["app_1", "app_1"]);
    expect(storage.data.get(ACTIVE_ID_KEY)).toBe("app_1");
  });

  it.each([
    ["network failure", new TypeError("fetch failed")],
    ["server error", new ServiceError("unknown", "HTTP 500")],
    ["conflict", new ServiceError("conflict", "locked")],
  ])("keeps the id on a %s", async (_label, error) => {
    const storage = memoryStorage({ [ACTIVE_ID_KEY]: "app_1" });
    const outcome = await restoreActiveApplication(
      {
        status: async () => {
          throw error;
        },
      },
      storage,
    );
    expect(outcome.kind).toBe("pending");
    expect(storage.data.get(ACTIVE_ID_KEY)).toBe("app_1");
  });

  it("clears the id only when the service definitively has no such application", async () => {
    const storage = memoryStorage({ [ACTIVE_ID_KEY]: "app_gone" });
    const outcome = await restoreActiveApplication(
      {
        status: async () => {
          throw new ServiceError("not_found", "No such application.");
        },
      },
      storage,
    );
    expect(outcome).toEqual({ kind: "gone", applicationId: "app_gone" });
    expect(storage.data.has(ACTIVE_ID_KEY)).toBe(false);
  });
});
