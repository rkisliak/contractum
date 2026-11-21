import { createFileRoute } from "@tanstack/react-router";
import { RuleSetPage, type RuleSetTab } from "@/features/rule-sets/RuleSetPage";

type RuleSetSearch = { via?: string; tab?: RuleSetTab };

const TABS: RuleSetTab[] = ["draft", "versions", "rollout", "impact"];

export const Route = createFileRoute("/_app/rule-sets/$ruleSetId")({
  validateSearch: (search: Record<string, unknown>): RuleSetSearch => ({
    ...(typeof search.via === "string" && search.via !== ""
      ? { via: search.via }
      : {}),
    ...(TABS.includes(search.tab as RuleSetTab)
      ? { tab: search.tab as RuleSetTab }
      : {}),
  }),
  component: function RuleSetRoute() {
    const { ruleSetId } = Route.useParams();
    const search = Route.useSearch();
    const navigate = Route.useNavigate();
    return (
      <RuleSetPage
        key={ruleSetId}
        ruleSetId={ruleSetId}
        via={search.via}
        tab={search.tab}
        onTabChange={(tab) => {
          void navigate({ search: { ...search, tab }, replace: true });
        }}
      />
    );
  },
});
