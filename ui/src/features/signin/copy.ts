import type { Notice } from "@/auth/notices";

// Sign-in strings in one module (Copywriting Contract), so a later i18n pass has one place to look.
export const signInCopy = {
  documentTitle: "Sign in · Contractum",
  brandMark: "Contractum",
  brandTagline: "Data contract governance",
  brandLead:
    "Author, version and publish data contracts that pass every governance rule.",
  heading: "Sign in",
  username: "Username",
  password: "Password",
  submit: "Sign in",
  usernameRequired: "Enter your username.",
  passwordRequired: "Enter your password.",
  divider: "or",
  microsoft: "Sign in with Microsoft",
  noMethod:
    "Sign-in is not available. Ask your administrator to enable a sign-in method.",
} as const;

// UI-SPEC 7.3: the forced bootstrap rotation screen.
export const rotateCopy = {
  documentTitle: "Set a new password · Contractum",
  brandMark: "Contractum",
  heading: "Set a new password",
  lead: "Your account was created with a one-time password. Choose a new password to continue.",
  newPassword: "New password",
  confirmPassword: "Confirm new password",
  helper:
    "Spaces and any characters are allowed, and a long passphrase works well.",
  mismatch: "Passwords do not match.",
  submit: "Set new password",
  busy: "Saving…",
  back: "Back to sign in",
  success: "Password set. You are signed in.",
} as const;

// UI-SPEC 7.2: the /auth/callback screen.
export const callbackCopy = {
  documentTitle: "Signing in · Contractum",
  progress: "Signing you in…",
  slow: "This is taking longer than expected.",
  failedTitle: "Microsoft sign-in did not complete",
  failedDetail:
    "The sign-in was cancelled or refused. Try again, or sign in another way.",
  retry: "Try loading again",
  back: "Back to sign in",
} as const;

// UI-SPEC Copywriting Contract: one fixed line per notice, never text from the URL.
export const noticeCopy: Record<Notice, string> = {
  "password-changed": "Password changed. Sign in again.",
  "session-expired": "Your session expired. Sign in again.",
  "signed-out-elsewhere": "You were signed out in another tab.",
  "rotation-restart":
    "Sign in with your one-time password again to set a new password.",
};
