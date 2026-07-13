/** @type {import('next').NextConfig} */
const nextConfig = {
  // Sprint 46B: enable the standalone output so the production
  // Docker image can run `node server.js` (no `npm start` shell,
  // smaller image, faster cold start). The Dockerfile copies
  // .next/standalone into the final image.
  output: 'standalone',

  // Sprint 46Z (audit M13): stop hiding lint and TypeScript errors
  // during the build. Pre-46Z the dashboard's CI deploy could
  // ship with type errors (ignoreBuildErrors: true) and lint
  // warnings (ignoreDuringBuilds: true) because we didn't want a
  // "stray unused var" to block the deploy. But the audit's M13
  // was right: silent type regressions in the dashboard cost
  // debug time the next time we touch that file, and the
  // deploy-safety margin was an illusion (we'd find the bug in
  // production logs, not in CI). Now the build is allowed to
  // fail and we fix what's flagged.
};

export default nextConfig;
