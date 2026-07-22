import { zodResolver } from "@hookform/resolvers/zod";
import { useEffect, useRef, useState } from "react";
import { useForm } from "react-hook-form";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { z } from "zod";

import { authApi } from "../api/domain";
import { ApiError } from "../api/errors";
import { routes, withQuery } from "../app/routes";
import { useAuth } from "../auth/context";
import { safeReturnTo } from "../auth/redirect";
import { styles } from "../styles/classes";

const emailSchema = z.object({ email: z.email("Enter a valid email address.") });
const credentialsSchema = emailSchema.extend({
  password: z.string().min(12, "Password must be at least 12 characters."),
});
const loginSchema = emailSchema.extend({ password: z.string().min(1, "Enter your password.") });
const resetSchema = z
  .object({
    newPassword: z.string().min(12, "Password must be at least 12 characters."),
    confirmPassword: z.string(),
  })
  .refine((value) => value.newPassword === value.confirmPassword, {
    message: "Passwords do not match.",
    path: ["confirmPassword"],
  });

function AuthShell({
  title,
  intro,
  children,
}: {
  title: string;
  intro?: string;
  children: React.ReactNode;
}) {
  return (
    <main className={styles.authPage}>
      <div className={styles.authShell}>
        <Link className="wordmark" to={routes.home.path}>
          scholight
        </Link>
        <div className={styles.authIntroBlock}>
          <h1>{title}</h1>
          {intro && <p className={styles.authIntro}>{intro}</p>}
        </div>
        {children}
      </div>
    </main>
  );
}

function FormMessage({ error, success }: { error?: string; success?: string }) {
  if (error)
    return (
      <p className={styles.formMessageError} role="alert">
        {error}
      </p>
    );
  if (success)
    return (
      <p className={styles.formMessageSuccess} role="status">
        {success}
      </p>
    );
  return null;
}

export function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [serverError, setServerError] = useState("");
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<z.infer<typeof loginSchema>>({ resolver: zodResolver(loginSchema) });
  const submit = handleSubmit(async (values) => {
    try {
      setServerError("");
      await login(values);
      navigate(safeReturnTo(params.get("returnTo")), { replace: true });
    } catch (error) {
      setServerError(
        error instanceof ApiError ? error.message : "Unable to sign in. Please try again.",
      );
    }
  });
  return (
    <AuthShell title="Sign in" intro="Save searches and revisit your research history.">
      <form className={styles.authForm} onSubmit={submit} noValidate>
        <div className={styles.authFields}>
          <label>
            Email
            <input
              type="email"
              autoComplete="email"
              placeholder="name@example.com"
              {...register("email")}
            />
            {errors.email && <span role="alert">{errors.email.message}</span>}
          </label>
          <label>
            Password
            <input
              type="password"
              autoComplete="current-password"
              placeholder="Enter your password"
              {...register("password")}
            />
            {errors.password && <span role="alert">{errors.password.message}</span>}
          </label>
        </div>
        <div className={styles.formAside}>
          <span>Search is open to everyone</span>
          <Link to={routes.forgotPassword.path}>Forgot password?</Link>
        </div>
        <FormMessage error={serverError} />
        <button className={styles.authSubmit} disabled={isSubmitting}>
          {isSubmitting ? "Signing in…" : "Sign in"}
        </button>
      </form>
      <p className={styles.authFoot}>
        New to Scholight? <Link to={routes.register.path}>Create an account</Link>
      </p>
    </AuthShell>
  );
}

export function RegisterPage() {
  const navigate = useNavigate();
  const [serverError, setServerError] = useState("");
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<z.infer<typeof credentialsSchema>>({ resolver: zodResolver(credentialsSchema) });
  const submit = handleSubmit(async (values) => {
    try {
      setServerError("");
      await authApi.register(values);
      navigate(withQuery(routes.checkEmail.path, { email: values.email }));
    } catch (error) {
      setServerError(
        error instanceof ApiError
          ? error.message
          : "Unable to create your account. Please try again.",
      );
    }
  });
  return (
    <AuthShell title="Create an account" intro="Keep your searches and research history in sync.">
      <form className={styles.authForm} onSubmit={submit} noValidate>
        <div className={styles.authFields}>
          <label>
            Email
            <input
              type="email"
              autoComplete="email"
              placeholder="name@example.com"
              {...register("email")}
            />
            {errors.email && <span role="alert">{errors.email.message}</span>}
          </label>
          <label>
            Password
            <input
              type="password"
              autoComplete="new-password"
              placeholder="Create a password"
              {...register("password")}
            />
            {errors.password && <span role="alert">{errors.password.message}</span>}
            <small>Use 12 or more characters.</small>
          </label>
        </div>
        <FormMessage error={serverError} />
        <button className={styles.authSubmit} disabled={isSubmitting}>
          {isSubmitting ? "Creating account…" : "Create account"}
        </button>
      </form>
      <p className={styles.authFoot}>
        Already have an account? <Link to={routes.login.path}>Sign in</Link>
      </p>
    </AuthShell>
  );
}

export function CheckEmailPage() {
  const [params] = useSearchParams();
  const email = params.get("email") ?? "your email address";
  const [status, setStatus] = useState("");
  const resend = async () => {
    try {
      await authApi.resendVerification(email);
      setStatus("A new verification email has been sent.");
    } catch {
      setStatus("If the address can receive verification mail, a new message will arrive shortly.");
    }
  };
  return (
    <AuthShell
      title="Verify your address"
      intro="We sent a verification link. Open it to finish creating your Scholight account."
    >
      <div className={styles.authInfo}>
        <p>
          The link was sent to <strong>{email}</strong>.
        </p>
        <button className={styles.secondaryButton} type="button" onClick={() => void resend()}>
          Resend verification
        </button>
        <FormMessage success={status} />
      </div>
      <p className={styles.authFoot}>
        <Link to={routes.login.path}>Back to sign in</Link>
      </p>
    </AuthShell>
  );
}

export function VerifyEmailPage() {
  const [params] = useSearchParams();
  const [state, setState] = useState<"working" | "done" | "error">("working");
  const token = params.get("token");
  const started = useRef(false);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    window.history.replaceState({}, "", routes.verifyEmail.path);
    if (!token) {
      setState("error");
      return;
    }
    void authApi
      .verifyEmail(token)
      .then(() => setState("done"))
      .catch(() => setState("error"));
  }, [token]);
  return (
    <AuthShell
      title={
        state === "working"
          ? "Verifying your email…"
          : state === "done"
            ? "Your email is verified"
            : "This link could not be verified"
      }
      intro={
        state === "working"
          ? "This will only take a moment."
          : state === "done"
            ? "You can now sign in and keep a private search history."
            : "The link may have expired or already been used."
      }
    >
      {state !== "working" && (
        <Link
          className={styles.authSubmitLink}
          to={state === "done" ? routes.login.path : routes.register.path}
        >
          {state === "done" ? "Continue to sign in" : "Create an account"}
        </Link>
      )}
    </AuthShell>
  );
}

export function ForgotPasswordPage() {
  const [sent, setSent] = useState(false);
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<z.infer<typeof emailSchema>>({ resolver: zodResolver(emailSchema) });
  const submit = handleSubmit(async ({ email }) => {
    try {
      await authApi.forgotPassword(email);
    } finally {
      setSent(true);
    }
  });
  return (
    <AuthShell
      title="Reset your password"
      intro="Enter your email and we’ll send reset instructions if an account is eligible."
    >
      {sent ? (
        <div className={styles.authInfo}>
          <FormMessage success="If an account matches that address, reset instructions are on the way." />
          <Link to={routes.login.path}>Back to sign in</Link>
        </div>
      ) : (
        <form className={styles.authForm} onSubmit={submit} noValidate>
          <label>
            Email
            <input type="email" autoComplete="email" {...register("email")} />
            {errors.email && <span role="alert">{errors.email.message}</span>}
          </label>
          <button className={styles.authSubmit} disabled={isSubmitting}>
            {isSubmitting ? "Sending…" : "Send reset instructions"}
          </button>
        </form>
      )}
    </AuthShell>
  );
}

export function ResetPasswordPage() {
  const [params] = useSearchParams();
  const token = params.get("token");
  const location = useLocation();
  const navigate = useNavigate();
  const [serverError, setServerError] = useState("");
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<z.infer<typeof resetSchema>>({ resolver: zodResolver(resetSchema) });
  useEffect(() => {
    if (location.search) window.history.replaceState({}, "", routes.resetPassword.path);
  }, [location.search]);
  const submit = handleSubmit(async ({ newPassword }) => {
    if (!token) return setServerError("This reset link is invalid or has expired.");
    try {
      await authApi.resetPassword(token, newPassword);
      navigate(withQuery(routes.login.path, { reset: "complete" }), { replace: true });
    } catch (error) {
      setServerError(error instanceof ApiError ? error.message : "Unable to reset your password.");
    }
  });
  return (
    <AuthShell title="Reset password" intro="Choose a new password for your account.">
      <form className={styles.authForm} onSubmit={submit} noValidate>
        <label>
          New password
          <input type="password" autoComplete="new-password" {...register("newPassword")} />
          {errors.newPassword && <span role="alert">{errors.newPassword.message}</span>}
        </label>
        <label>
          Confirm new password
          <input type="password" autoComplete="new-password" {...register("confirmPassword")} />
          {errors.confirmPassword && <span role="alert">{errors.confirmPassword.message}</span>}
        </label>
        <FormMessage error={serverError} />
        <button className={styles.authSubmit} disabled={isSubmitting}>
          Reset password
        </button>
      </form>
    </AuthShell>
  );
}
