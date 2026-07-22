import { useState } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";
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
      {status === "authenticated" && (
        <NavLink to="/history" onClick={() => setOpen(false)}>
          History
        </NavLink>
      )}
    </>
  );

  return (
    <header className={styles.header}>
      <div className={styles.headerInner}>
        <Link className="wordmark" to="/" aria-label="Scholight home">
          scholight
        </Link>
        <nav className={styles.desktopNav} aria-label="Main navigation">
          {nav}
        </nav>
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
      {open && (
        <nav id="mobile-menu" className={styles.mobileNav} aria-label="Mobile navigation">
          {nav}
          {status === "authenticated" ? (
            <>
              <Link to="/account" onClick={() => setOpen(false)}>
                Account
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
        </nav>
      )}
    </header>
  );
}
