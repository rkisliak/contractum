// A temporary page for a navigation area whose screens are still being built. It keeps the shell
// complete (every sidebar entry lands on a real page) and is replaced by the area's own screens.
import { Hammer } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { EmptyState } from "@/components/states";

export function BeingBuilt({
  title,
  description,
}: {
  title: string;
  description: string;
}) {
  return (
    <>
      <PageHeader
        title={title}
        description={description}
        breadcrumbs={[{ label: "Home", to: "/" }, { label: title }]}
      />
      <EmptyState
        icon={Hammer}
        heading="This area is being built"
        body="Its screens are on the way. Everything here is already available through the API."
      />
    </>
  );
}
