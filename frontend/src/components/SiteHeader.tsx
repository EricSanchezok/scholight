import { useState } from "react";
import { AnimatePresence } from "motion/react";
import * as m from "motion/react-m";
import { Link, NavLink, useLocation } from "react-router-dom";

import { useAuth } from "../auth/context";
import { accountRoutes, routes, withQuery } from "../app/routes";
import { mobileMenuMotion } from "../app/motion";
import styles from "../styles/app.module.css";
import { AccountMenu } from "./AccountMenu";
import { CloseIcon, MenuIcon } from "./icons";

export function SiteHeader() {
  const { status, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const location = useLocation();

  const nav = (
    <>
      <NavLink to={routes.home.path} end onClick={() => setOpen(false)}>
        Home
      </NavLink>
      <NavLink to={routes.docs.path} onClick={() => setOpen(false)}>
        Docs
      </NavLink>
    </>
  );

  return (
    <header className={styles.header}>
      <div className={styles.headerInner}>
        <Link className="wordmark" to={routes.home.path} aria-label="Scholight home">
          scholight
        </Link>
        <div className={styles.headerLinks}>
          <nav className={styles.desktopNav} aria-label="Main navigation">
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
                Sign in
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
            <span>{open ? "Close" : "Menu"}</span>
          </button>
        </div>
      </div>
      <AnimatePresence>
        {open && (
          <m.nav
            id="mobile-menu"
            className={styles.mobileNav}
            aria-label="Mobile navigation"
            {...mobileMenuMotion}
          >
            {nav}
            {status === "authenticated" ? (
              <>
                {accountRoutes.map((route) => (
                  <Link to={route.path} onClick={() => setOpen(false)} key={route.id}>
                    {route.id === "usage"
                      ? "Usage & quota"
                      : route.id === "accessKeys"
                        ? "Access Keys"
                        : route.id === "history"
                          ? "Search history"
                          : "Account settings"}
                  </Link>
                ))}
                <button type="button" onClick={() => void logout()}>
                  Sign out
                </button>
              </>
            ) : (
              <Link to={routes.login.path} onClick={() => setOpen(false)}>
                Sign in
              </Link>
            )}
          </m.nav>
        )}
      </AnimatePresence>
    </header>
  );
}
