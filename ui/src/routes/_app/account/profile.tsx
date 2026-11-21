import { createFileRoute } from "@tanstack/react-router";
import { ProfilePage } from "@/features/account/ProfilePage";

export const Route = createFileRoute("/_app/account/profile")({
  component: ProfilePage,
});
