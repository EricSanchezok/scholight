import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Link, useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";
import { avatarInitials } from "../lib/format";
import styles from "../styles/app.module.css";
import { ChevronDownIcon } from "./icons";

export function AccountMenu() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  if (!user) return null;
  const name = user.display_name?.trim() || user.email.split("@")[0];

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger className={styles.accountTrigger} aria-label="Open account menu">
        <span className={styles.avatar}>{avatarInitials(user.display_name, user.email)}</span>
        <span className={styles.accountName}>{name}</span>
        <ChevronDownIcon />
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content className={styles.dropdown} align="end" sideOffset={10}>
          <div className={styles.dropdownIdentity}>
            <strong>{name}</strong>
            <span>{user.email}</span>
          </div>
          <DropdownMenu.Separator className={styles.menuSeparator} />
          <DropdownMenu.Item asChild className={styles.menuItem}>
            <Link to="/history">Search history</Link>
          </DropdownMenu.Item>
          <DropdownMenu.Item asChild className={styles.menuItem}>
            <Link to="/account">Account settings</Link>
          </DropdownMenu.Item>
          <DropdownMenu.Separator className={styles.menuSeparator} />
          <DropdownMenu.Item
            className={styles.menuItem}
            onSelect={() => void logout().then(() => navigate("/"))}
          >
            Sign out
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
