import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { useQueryClient } from "@tanstack/react-query";
import { AnimatePresence } from "motion/react";
import * as m from "motion/react-m";
import { useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import accountIcon from "../assets/icons/menu-account.svg";
import accessKeysIcon from "../assets/icons/menu-access-keys.svg";
import historyIcon from "../assets/icons/menu-history.svg";
import signOutIcon from "../assets/icons/menu-sign-out.svg";
import usageIcon from "../assets/icons/menu-usage.svg";
import { popoverMotion } from "../app/motion";
import {
  type AccountDestination,
  prefetchPrivateDestination,
  preloadPrivateRoutes,
} from "../app/privateRoutes";
import { useAuth } from "../auth/context";
import { avatarInitials } from "../lib/format";
import styles from "../styles/app.module.css";
import { ChevronDownIcon } from "./icons";

export function AccountMenu() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const intentTimer = useRef<number | undefined>(undefined);
  if (!user) return null;
  const name = user.display_name?.trim() || user.email.split("@")[0];
  const destinations: { to: AccountDestination; label: string; icon: string }[] = [
    { to: "/usage", label: "Usage & quota", icon: usageIcon },
    { to: "/access-keys", label: "Access Keys", icon: accessKeysIcon },
    { to: "/history", label: "Search history", icon: historyIcon },
    { to: "/account", label: "Account settings", icon: accountIcon },
  ];
  const clearIntent = () => window.clearTimeout(intentTimer.current);
  const warmDestination = (destination: AccountDestination, immediate = false) => {
    clearIntent();
    const warm = () => void prefetchPrivateDestination(destination, queryClient);
    if (immediate) warm();
    else intentTimer.current = window.setTimeout(warm, 100);
  };

  return (
    <DropdownMenu.Root
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (next) preloadPrivateRoutes();
        else clearIntent();
      }}
    >
      <DropdownMenu.Trigger className={styles.accountTrigger} aria-label="Open account menu">
        <span className={styles.avatar}>{avatarInitials(user.display_name, user.email)}</span>
        <span className={styles.accountName}>{name}</span>
        <m.span animate={{ rotate: open ? 180 : 0 }} transition={{ duration: 0.14 }}>
          <ChevronDownIcon />
        </m.span>
      </DropdownMenu.Trigger>
      <AnimatePresence>
        {open && (
          <DropdownMenu.Portal forceMount>
            <DropdownMenu.Content asChild forceMount align="end" sideOffset={10}>
              <m.div className={styles.dropdown} {...popoverMotion}>
                <div className={styles.dropdownIdentity}>
                  <strong>{name}</strong>
                  <span>{user.email}</span>
                </div>
                <DropdownMenu.Separator className={styles.menuSeparator} />
                {destinations.map((destination) => (
                  <DropdownMenu.Item asChild className={styles.menuItem} key={destination.to}>
                    <Link
                      to={destination.to}
                      aria-current={location.pathname === destination.to ? "page" : undefined}
                      onPointerEnter={() => warmDestination(destination.to)}
                      onPointerLeave={clearIntent}
                      onFocus={() => warmDestination(destination.to, true)}
                    >
                      <span className={styles.menuActiveMark} aria-hidden="true" />
                      <img src={destination.icon} alt="" /> {destination.label}
                    </Link>
                  </DropdownMenu.Item>
                ))}
                <DropdownMenu.Separator className={styles.menuSeparator} />
                <DropdownMenu.Item
                  className={styles.menuItem}
                  onSelect={() => void logout().then(() => navigate("/"))}
                >
                  <img src={signOutIcon} alt="" /> Sign out
                </DropdownMenu.Item>
              </m.div>
            </DropdownMenu.Content>
          </DropdownMenu.Portal>
        )}
      </AnimatePresence>
    </DropdownMenu.Root>
  );
}
