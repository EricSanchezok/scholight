import { routes } from "../app/routes";

export function safeReturnTo(value: string | null): string {
  return value && /^\/(?!\/)/.test(value) ? value : routes.home.path;
}
