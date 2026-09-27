import { defineRailway, github, preserve, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  const hermesAgentVolume = volume("hermes-agent-volume", { alerts: { usage: { "100": {}, "80": {}, "95": {} } }, allowOnlineResize: true, region: "asia-southeast1-eqsg3a", sizeMB: 5000 });
  const hermesAgent = service("hermes-agent", {
    source: github("waynelian/hermes-agent-railway-template", { branch: "main", checkSuites: false, upstreamUrl: "https://github.com/arjunkomath/hermes-agent-railway-template" }),
    // Migrated from railway.toml. The service's dashboard builder is RAILPACK,
    // so the Dockerfile builder must be declared here.
    build: { builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    start: "tini -- /app/start.sh",
    healthcheck: "/api/health",
    deploy: { restartPolicyType: "ON_FAILURE", restartPolicyMaxRetries: 10 },
    replicas: { "asia-southeast1-eqsg3a": 1 },
    volumeMounts: { "/data": hermesAgentVolume },
    env: { ADMIN_PASSWORD: preserve(), ADMIN_USERNAME: preserve(), ELEVENLABS_API_KEY: preserve(), GITHUB_TOKEN: preserve(), GOG_KEYRING_BACKEND: preserve(), GOG_KEYRING_PASSWORD: preserve(), GOOGLE_SAFE_BROWSING_KEY: preserve(), PORT: preserve(), UV_LINK_MODE: preserve() },
  });

  return project("hermes-agent", {
    resources: [hermesAgent, hermesAgentVolume],
  });
});
