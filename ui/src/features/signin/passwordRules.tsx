// The sign-in policy the browser may read before anyone is signed in (quick 261120-0ye, S1 and
// S5): which sign-in methods are on and the rules a new password must meet. `GET /auth/policy` is
// public and carries no secret. The screens that set a password list the rules and pre-check them;
// the server stays the judge (its answer is still shown when it refuses). When the call fails the
// screens fall back to today's defaults, so a policy outage never blocks sign-in.
import { useQuery } from "@tanstack/react-query";
import { useRouteContext } from "@tanstack/react-router";
import type { AuthPolicy, PasswordRules } from "@/api/types";
import styles from "./passwordRules.module.css";

export const DEFAULT_PASSWORD_RULES: PasswordRules = {
  min_length: 12,
  require_upper: false,
  require_lower: false,
  require_digit: false,
  require_symbol: false,
};

export const authPolicyKey = ["auth-policy"] as const;

/** The public sign-in policy. Never retried: a failure means "use the configured defaults". */
export function useAuthPolicy() {
  const { api } = useRouteContext({ from: "__root__" });
  return useQuery({
    queryKey: authPolicyKey,
    retry: false,
    staleTime: 30_000,
    queryFn: async (): Promise<AuthPolicy> => {
      const { data } = await api.GET("/api/v1/identity/auth/policy");
      if (data === undefined) throw new Error("GET /auth/policy had no body");
      return data;
    },
  });
}

/** The rules in force, or today's defaults while loading or when the call failed. */
export function usePasswordRules(): PasswordRules {
  return useAuthPolicy().data?.password_rules ?? DEFAULT_PASSWORD_RULES;
}

/** The rules as short sentences, in the API's fixed order ("At least 14 characters"). */
export function describeRules(rules: PasswordRules): string[] {
  const lines = [`At least ${String(rules.min_length)} characters`];
  if (rules.require_upper) lines.push("An upper-case letter");
  if (rules.require_lower) lines.push("A lower-case letter");
  if (rules.require_digit) lines.push("A digit");
  if (rules.require_symbol) lines.push("A symbol");
  return lines;
}

/**
 * The first rule `password` does not meet, as an instruction, or null. A pre-check only: it
 * normalises like the server (NFKC) and uses Unicode classes, but the server decides.
 */
export function unmetRule(
  rules: PasswordRules,
  password: string,
): string | null {
  const value = password.normalize("NFKC");
  if (Array.from(value).length < rules.min_length)
    return `Use at least ${String(rules.min_length)} characters.`;
  if (rules.require_upper && !/\p{Lu}/u.test(value))
    return "Add an upper-case letter.";
  if (rules.require_lower && !/\p{Ll}/u.test(value))
    return "Add a lower-case letter.";
  if (rules.require_digit && !/\p{Nd}/u.test(value)) return "Add a digit.";
  if (rules.require_symbol && !/[^\p{L}\p{N}\s]/u.test(value))
    return "Add a symbol (any character that is not a letter, digit or space).";
  return null;
}

/** The rules a new password must meet, as a list under the field. */
export function PasswordRulesList({ rules }: { rules: PasswordRules }) {
  return (
    <ul className={styles.rules} aria-label="Password rules">
      {describeRules(rules).map((line) => (
        <li key={line}>{line}</li>
      ))}
    </ul>
  );
}
