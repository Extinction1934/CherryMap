import { createFileRoute } from "@tanstack/react-router";
import { publishedApi } from "@/cherrymap/publishedBridge.server";

// Live preview sends /api to the source worker through the dev proxy, so this
// route only runs on the published host. That host has no local worker. Calling
// it used to answer HTTP 503 and stop the map from launching.
async function handle(request: Request): Promise<Response> {
  const response = await publishedApi(request);
  if (response.status >= 500) {
    return publishedApi(new Request(new URL("/api/bootstrap", request.url), { method: "GET" }));
  }
  return response;
}

export const Route = createFileRoute("/api/$")({
  server: {
    handlers: {
      GET: ({ request }) => handle(request),
      POST: ({ request }) => handle(request),
    },
  },
});
