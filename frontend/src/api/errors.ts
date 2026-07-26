export interface FieldError {
  field: string;
  message: string;
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
    public readonly code?: string,
    public readonly retryable = false,
    public readonly retryAfter?: number,
    public readonly fieldErrors?: FieldError[],
  ) {
    super(message);
    this.name = "ApiError";
  }
}

interface ErrorDetail {
  code?: string;
  message?: string;
  retryable?: boolean;
}

export async function toApiError(response: Response, body?: unknown): Promise<ApiError> {
  let payload = body;
  if (payload === undefined) {
    try {
      payload = await response.clone().json();
    } catch {
      payload = undefined;
    }
  }

  const record = isRecord(payload) ? payload : {};
  const detail = record.detail;
  const structured = isRecord(detail) ? (detail as ErrorDetail) : undefined;
  const validation = Array.isArray(detail)
    ? detail.flatMap((item): FieldError[] => {
        if (!isRecord(item) || typeof item.msg !== "string") return [];
        const location = Array.isArray(item.loc) ? item.loc.slice(1).join(".") : "form";
        return [{ field: location || "form", message: item.msg }];
      })
    : undefined;
  const retryAfterHeader = response.headers.get("Retry-After");
  const retryAfter = retryAfterHeader ? Number(retryAfterHeader) : undefined;
  const fallback =
    response.status === 429
      ? "You have reached the current search limit. Please try again later."
      : response.status === 503
        ? "Scholight is temporarily unavailable. Please try again shortly."
        : "Something went wrong. Please try again.";

  return new ApiError(
    response.status,
    structured?.message ?? (typeof detail === "string" ? detail : fallback),
    structured?.code,
    structured?.retryable ?? response.status >= 500,
    Number.isFinite(retryAfter) ? retryAfter : undefined,
    validation,
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
