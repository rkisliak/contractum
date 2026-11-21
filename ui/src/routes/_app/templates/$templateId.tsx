import { createFileRoute } from "@tanstack/react-router";
import { TemplatePage } from "@/features/templates/TemplatePage";

export const Route = createFileRoute("/_app/templates/$templateId")({
  component: function TemplateRoute() {
    const { templateId } = Route.useParams();
    return <TemplatePage key={templateId} templateId={templateId} />;
  },
});
