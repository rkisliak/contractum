// The frame the Profile and Change password pages share (quick 261121-0sh): the page header with
// the account tabs, and the stand-in shown until `/me` has loaded (a problem with Retry when it
// failed, a skeleton while it loads).
import type { useMe } from "@/features/access/useMe";
import { PageHeader } from "@/components/PageHeader";
import { ProblemAlert } from "@/components/ProblemAlert";
import { Skeleton } from "@/components/states";
import { AccountTabs } from "@/features/admin/TokensPages";
import { problemOf } from "@/lib/unexpected";

export function AccountHeader({ title }: { title: string }) {
  return (
    <>
      <PageHeader title={title} breadcrumbs={[{ label: "Account settings" }]} />
      <AccountTabs />
    </>
  );
}

/** The page while `/me` is not available: the header over the failure or a skeleton. */
export function AccountPending({
  title,
  me,
}: {
  title: string;
  me: ReturnType<typeof useMe>;
}) {
  return (
    <>
      <AccountHeader title={title} />
      {me.isError ? (
        <ProblemAlert
          problem={problemOf(me.error)}
          onRetry={() => void me.refetch()}
        />
      ) : (
        <Skeleton label="your account" rows={3} />
      )}
    </>
  );
}
