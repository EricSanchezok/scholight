import { useState } from "react";
import { AnimatePresence } from "motion/react";
import * as m from "motion/react-m";
import { Link, NavLink, useLocation } from "react-router-dom";

import { useAuth } from "../auth/context";
import styles from "../styles/app.module.css";
import { AccountMenu } from "./AccountMenu";
import { CloseIcon, MenuIcon } from "./icons";

export function SiteHeader() {
  const { status, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const location = useLocation();

  const nav = (
    <>
      <NavLink to="/" end onClick={() => setOpen(false)}>
        Home
      </NavLink>
      <NavLink to="/docs" onClick={() => setOpen(false)}>
        Docs
      </NavLink>
    </>
  );

  return (
    <header className={styles.header}>
      <div className={styles.headerInner}>
        <Link className="wordmark" to="/" aria-label="Scholight home">
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
                to={`/login?returnTo=${encodeURIComponent(location.pathname + location.search)}`}
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
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1, transition: { duration: 0.18 } }}
            exit={{ height: 0, opacity: 0, transition: { duration: 0.1 } }}
          >
            {nav}
            {status === "authenticated" ? (
              <>
                <Link to="/usage" onClick={() => setOpen(false)}>
                  Usage &amp; quota
                </Link>
                <Link to="/access-keys" onClick={() => setOpen(false)}>
                  Access Keys
                </Link>
                <Link to="/history" onClick={() => setOpen(false)}>
                  Search history
                </Link>
                <Link to="/account" onClick={() => setOpen(false)}>
                  Account settings
                </Link>
                <button type="button" onClick={() => void logout()}>
                  Sign out
                </button>
              </>
            ) : (
              <Link to="/login" onClick={() => setOpen(false)}>
                Sign in
              </Link>
            )}
          </m.nav>
        )}
      </AnimatePresence>
    </header>
  );
}
