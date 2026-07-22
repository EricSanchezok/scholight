export function safeReturnTo(value: string | null): string {
  return value && /^\/(?!\/)/.test(value) ? value : "/";
}
