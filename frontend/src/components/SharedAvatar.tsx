import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { accountApi } from "../api/domain";
import { queryKeys } from "../app/queryKeys";
import { avatarInitials } from "../lib/format";
import { styles } from "../styles/classes";
import { nextAvatarRefreshInterval } from "../lib/query/avatar-refresh";

type SharedAvatarProps = {
  displayName: string | null | undefined;
  email: string;
  size?: "compact" | "profile";
};

export function SharedAvatar({ displayName, email, size = "compact" }: SharedAvatarProps) {
  const avatar = useQuery({
    queryKey: queryKeys.avatar,
    queryFn: accountApi.avatar,
    retry: false,
    refetchInterval: (query) => nextAvatarRefreshInterval([query.state.data]),
    staleTime: 10 * 60_000,
  });
  const [failedVersion, setFailedVersion] = useState<string | null>(null);
  const version = avatar.data?.version ?? null;
  const showImage = Boolean(avatar.data?.url && version !== failedVersion);

  return (
    <span
      className={`${styles.sharedAvatar} ${size === "profile" ? styles.sharedAvatarProfile : ""}`}
      aria-hidden="true"
    >
      {showImage ? (
        <img
          src={avatar.data?.url}
          alt=""
          referrerPolicy="no-referrer"
          onError={() => version && setFailedVersion(version)}
        />
      ) : (
        avatarInitials(displayName, email)
      )}
    </span>
  );
}
