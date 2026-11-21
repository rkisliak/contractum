// Swagger UI configuration for the gateway's API docs (PLAT-06, D-177, D-178). Replaces the
// tarball's default initializer. Same-origin relative URLs only: the merged document first,
// then each service's own document (the D-177 fallback dropdown). validatorUrl "none" keeps the
// page from calling the public validator; queryConfigEnabled stays at its default (off), so a
// ?url= parameter cannot point the page at another document.
window.onload = function () {
  window.ui = SwaggerUIBundle({
    urls: [
      { url: "/api/v1/openapi.json", name: "All services" },
      { url: "/api/v1/identity/openapi.json", name: "identity" },
      { url: "/api/v1/contracts/openapi.json", name: "contracts" },
      { url: "/api/v1/validation/openapi.json", name: "validation" },
      { url: "/api/v1/audit/openapi.json", name: "audit" },
    ],
    "urls.primaryName": "All services",
    validatorUrl: "none",
    dom_id: "#swagger-ui",
    deepLinking: true,
    presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset],
    plugins: [SwaggerUIBundle.plugins.DownloadUrl],
    layout: "StandaloneLayout",
  });
};
