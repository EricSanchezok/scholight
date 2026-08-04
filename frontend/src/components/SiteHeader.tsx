import { useState } from "react";
import { AnimatePresence } from "motion/react";
import * as m from "motion/react-m";
import { Link, NavLink, useLocation } from "react-router-dom";

import { useAuth } from "../auth/context";
import { routes, visibleAccountRoutes, withQuery } from "../app/routes";
import { mobileMenuMotion } from "../app/motion";
import { useI18n } from "../i18n/I18nProvider";
import { styles } from "../styles/classes";
import { usePublicCapabilities } from "../features/capabilities/usePublicCapabilities";
import { AccountMenu } from "./AccountMenu";
import { CloseIcon, MenuIcon } from "./icons";

export function SiteHeader() {
  const { messages } = useI18n();
  const { status, adminCapabilities, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const location = useLocation();
  const capabilities = usePublicCapabilities();

  const nav = (
    <>
      <NavLink to={routes.home.path} end onClick={() => setOpen(false)}>
        {messages.navigation.home}
      </NavLink>
      {capabilities.data?.survey === "all" ? (
        <NavLink to={routes.survey.path} onClick={() => setOpen(false)}>
          {messages.navigation.survey}
        </NavLink>
      ) : null}
      <NavLink to={routes.docs.path} onClick={() => setOpen(false)}>
        {messages.navigation.docs}
      </NavLink>
    </>
  );

  return (
    <header className={styles.header}>
      <div className={styles.headerInner}>
        <Link className="wordmark" to={routes.home.path} aria-label={messages.brand.homeLabel}>
          {messages.brand.name}
        </Link>
        <div className={styles.headerLinks}>
          <nav className={styles.desktopNav} aria-label={messages.navigation.mainLabel}>
            {nav}
          </nav>
          <span className={styles.navDivider} aria-hidden="true" />
          <div className={styles.headerActions}>
            {status === "authenticated" ? (
              <AccountMenu />
            ) : (
              <Link
                className={styles.signInLink}
                to={withQuery(routes.login.path, {
                  returnTo: location.pathname + location.search,
                })}
              >
                {messages.navigation.signIn}
              </Link>
            )}
          </div>
        </div>
        <div className={styles.mobileActions}>
          <button
            className={styles.mobileMenuButton}
            type="button"
            onClick={() => setOpen((value) => !value)}
            aria-expanded={open}
            aria-controls="mobile-menu"
          >
            {open ? <CloseIcon /> : <MenuIcon />}
            <span>{open ? messages.navigation.close : messages.navigation.menu}</span>
          </button>
        </div>
      </div>
      <AnimatePresence>
        {open && (
          <m.nav
            id="mobile-menu"
            className={styles.mobileNav}
            aria-label={messages.navigation.mobileLabel}
            {...mobileMenuMotion}
          >
            {nav}
            {status === "authenticated" ? (
              <>
                {visibleAccountRoutes(adminCapabilities).map((route) => (
                  <Link to={route.path} onClick={() => setOpen(false)} key={route.id}>
                    {messages.navigation[route.id]}
                  </Link>
                ))}
                <button type="button" onClick={() => void logout()}>
                  {messages.navigation.signOut}
                </button>
              </>
            ) : (
              <Link to={routes.login.path} onClick={() => setOpen(false)}>
                {messages.navigation.signIn}
              </Link>
            )}
          </m.nav>
        )}
      </AnimatePresence>
    </header>
  );
}
