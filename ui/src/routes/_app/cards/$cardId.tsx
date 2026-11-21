import { createFileRoute } from "@tanstack/react-router";
import { CARD_TABS, CardPage, type CardTab } from "@/features/cards/CardPage";

// One subscriber card with in-page tabs; the open tab is a typed search param (deep-linkable).
export const Route = createFileRoute("/_app/cards/$cardId")({
  validateSearch: (search: Record<string, unknown>): { tab?: CardTab } =>
    CARD_TABS.includes(search.tab as CardTab) && search.tab !== "overview"
      ? { tab: search.tab as CardTab }
      : {},
  component: function CardRoute() {
    const { cardId } = Route.useParams();
    const { tab } = Route.useSearch();
    const navigate = Route.useNavigate();
    return (
      <CardPage
        cardId={cardId}
        tab={tab ?? "overview"}
        onTabChange={(next) => {
          void navigate({
            search: next === "overview" ? {} : { tab: next },
            replace: true,
          });
        }}
      />
    );
  },
});
