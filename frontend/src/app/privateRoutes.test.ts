import { QueryClient } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { accountApi, accessKeyApi, historyApi, usageApi } from "../api/domain";
import { prefetchPrivateDestination } from "./privateRoutes";

vi.mock("../api/domain", () => ({
  accountApi: { profile: vi.fn(), sessions: vi.fn() },
  accessKeyApi: { list: vi.fn() },
  historyApi: { list: vi.fn() },
  usageApi: {
    summary: vi.fn(),
    volume: vi.fn(),
    latency: vi.fn(),
    records: vi.fn(),
  },
}));

describe("private destination prefetch", () => {
  let client: QueryClient;

  beforeEach(() => {
    client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    vi.mocked(usageApi.summary).mockResolvedValue({} as never);
    vi.mocked(usageApi.volume).mockResolvedValue({} as never);
    vi.mocked(usageApi.latency).mockResolvedValue({} as never);
    vi.mocked(usageApi.records).mockResolvedValue({ items: [], next_cursor: null });
    vi.mocked(accessKeyApi.list).mockResolvedValue([]);
    vi.mocked(historyApi.list).mockResolvedValue({ items: [], total: 0 } as never);
    vi.mocked(accountApi.profile).mockResolvedValue({} as never);
    vi.mocked(accountApi.sessions).mockResolvedValue([]);
  });

  it("warms every independent usage section", async () => {
    await prefetchPrivateDestination("/usage", client);

    expect(usageApi.summary).toHaveBeenCalledOnce();
    expect(usageApi.volume).toHaveBeenCalledOnce();
    expect(usageApi.latency).toHaveBeenCalledOnce();
    expect(usageApi.records).toHaveBeenCalledOnce();
  });

  it("reuses fresh prefetched access-key data", async () => {
    await prefetchPrivateDestination("/access-keys", client);
    await prefetchPrivateDestination("/access-keys", client);

    expect(accessKeyApi.list).toHaveBeenCalledOnce();
  });

  it("prefetches the default history page", async () => {
    await prefetchPrivateDestination("/history", client);

    expect(historyApi.list).toHaveBeenCalledWith(10, 0);
  });
});
