import { createFileRoute } from "@tanstack/react-router";
import { MyTokensPage } from "@/features/admin/TokensPages";

export const Route = createFileRoute("/_app/account/tokens")({
  component: MyTokensPage,
});
