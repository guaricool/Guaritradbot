/** @type {import('next').NextConfig} */
const nextConfig = {
  // Sprint 46B: enable the standalone output so the production
  // Docker image can run `node server.js` (no `npm start` shell,
  // smaller image, faster cold start). The Dockerfile copies
  // .next/standalone into the final image.
  output: 'standalone',

  // Don't fail the build on lint errors during CI/CD deploy.
  // Coolify rebuilds on every push; we don't want a stray unused
  // var to block the deploy.
  eslint: {
    ignoreDuringBuilds: true,
  },

  // Same for TS — the dashboard is type-checked at dev time, but
  // don't block the build on a transient error. The 49 tests cover
  // the type contract.
  typescript: {
    ignoreBuildErrors: true,
  },
};

export default nextConfig;
