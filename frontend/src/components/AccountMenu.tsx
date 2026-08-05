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
import { chevronMotion, popoverMotion } from "../app/motion";
import { prefetchPrivateDestination, preloadPrivateRoutes } from "../app/privateRoutes";
import { type AccountDestination, routes, visibleAccountRoutes } from "../app/routes";
import { useAuth } from "../auth/context";
import { productConfig } from "../config/product";
import { useI18n } from "../i18n/I18nProvider";
import { styles } from "../styles/classes";
import { ChevronDownIcon } from "./icons";
import { SharedAvatar } from "./SharedAvatar";

export function AccountMenu() {
  const { messages } = useI18n();
  const { user, adminCapabilities, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const intentTimer = useRef<number | undefined>(undefined);
  if (!user) return null;
  const name = user.display_name?.trim() || user.email.split("@")[0];
  const destinationDetails: Record<AccountDestination, { label: string; icon: string }> = {
    [routes.usage.path]: { label: messages.navigation.usage, icon: usageIcon },
    [routes.accessKeys.path]: { label: messages.navigation.accessKeys, icon: accessKeysIcon },
    [routes.history.path]: { label: messages.navigation.history, icon: historyIcon },
    [routes.account.path]: { label: messages.navigation.account, icon: accountIcon },
    [routes.adminOverview.path]: {
      label: messages.navigation.adminOverview,
      icon: usageIcon,
    },
    [routes.quotaAdmin.path]: { label: messages.navigation.quotaAdmin, icon: usageIcon },
    [routes.adminOperations.path]: {
      label: messages.navigation.adminOperations,
      icon: usageIcon,
    },
  };
  const clearIntent = () => window.clearTimeout(intentTimer.current);
  const warmDestination = (destination: AccountDestination, immediate = false) => {
    clearIntent();
    const warm = () => void prefetchPrivateDestination(destination, queryClient);
    if (immediate) warm();
    else
      intentTimer.current = window.setTimeout(warm, productConfig.navigation.intentPrefetchDelayMs);
  };

  return (
    <DropdownMenu.Root
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (next) preloadPrivateRoutes(adminCapabilities);
        else clearIntent();
      }}
    >
      <DropdownMenu.Trigger
        className={styles.accountTrigger}
        aria-label={messages.navigation.accountMenuLabel}
      >
        <SharedAvatar displayName={user.display_name} email={user.email} />
        <span className={styles.accountName}>{name}</span>
        <m.span {...chevronMotion(open)}>
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
                {visibleAccountRoutes(adminCapabilities).map((destination) => {
                  const details = destinationDetails[destination.path];
                  return (
                    <DropdownMenu.Item asChild className={styles.menuItem} key={destination.path}>
                      <Link
                        to={destination.path}
                        aria-current={location.pathname === destination.path ? "page" : undefined}
                        onPointerEnter={() => warmDestination(destination.path)}
                        onPointerLeave={clearIntent}
                        onFocus={() => warmDestination(destination.path, true)}
                      >
                        <span className={styles.menuActiveMark} aria-hidden="true" />
                        <img src={details.icon} alt="" /> {details.label}
                      </Link>
                    </DropdownMenu.Item>
                  );
                })}
                <DropdownMenu.Separator className={styles.menuSeparator} />
                <DropdownMenu.Item
                  className={styles.menuItem}
                  onSelect={() => void logout().then(() => navigate(routes.home.path))}
                >
                  <img src={signOutIcon} alt="" /> {messages.navigation.signOut}
                </DropdownMenu.Item>
              </m.div>
            </DropdownMenu.Content>
          </DropdownMenu.Portal>
        )}
      </AnimatePresence>
    </DropdownMenu.Root>
  );
}
