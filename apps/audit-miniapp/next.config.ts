import type { NextConfig } from 'next';

// The Mini App is reverse-proxied below https://dashboard.example/audit/.
// Nginx removes that prefix before forwarding to this root-mounted app, while
// browser-facing static asset URLs retain it.
const nextConfig: NextConfig = {
  assetPrefix: '/audit',
};

export default nextConfig;
