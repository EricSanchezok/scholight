export const productConfig = {
  search: {
    maxQueryLength: 500,
    resultLimit: 10,
    cacheTimeMs: 5 * 60_000,
  },
  history: {
    pageSize: 10,
  },
  usage: {
    rangeDays: 30,
    recordsPageSize: 10,
  },
  accessKeys: {
    maxActive: 10,
  },
  navigation: {
    intentPrefetchDelayMs: 100,
  },
} as const;
