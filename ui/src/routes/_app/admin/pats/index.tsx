import { createFileRoute } from "@tanstack/react-router";
import { AllPatsPage } from "@/features/admin/TokensPages";

export const Route = createFileRoute("/_app/admin/pats/")({
  component: AllPatsPage,
});
