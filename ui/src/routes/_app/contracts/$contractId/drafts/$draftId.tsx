import { createFileRoute } from "@tanstack/react-router";
import { DraftEditor } from "@/features/contracts/DraftEditor";

export const Route = createFileRoute(
  "/_app/contracts/$contractId/drafts/$draftId",
)({
  component: function DraftEditorRoute() {
    const { contractId, draftId } = Route.useParams();
    return (
      <DraftEditor key={draftId} contractId={contractId} draftId={draftId} />
    );
  },
});
