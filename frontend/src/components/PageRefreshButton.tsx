import { RefreshIcon } from "./icons";
import { styles } from "../styles/classes";

export function PageRefreshButton({
  label,
  refreshing,
  onRefresh,
}: {
  label: string;
  refreshing: boolean;
  onRefresh: () => void | Promise<unknown>;
}) {
  const action = refreshing ? "Refreshing" : "Refresh";

  return (
    <button
      className={styles.pageRefreshButton}
      type="button"
      disabled={refreshing}
      aria-busy={refreshing}
      aria-label={`${action} ${label}`}
      onClick={() => void onRefresh()}
    >
      <RefreshIcon />
      <span>{refreshing ? "Refreshing…" : "Refresh"}</span>
    </button>
  );
}
