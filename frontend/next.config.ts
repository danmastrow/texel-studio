import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "export",
  distDir: "../static",
  // Emit route/index.html so FastAPI StaticFiles can resolve clean URLs.
  trailingSlash: true,
};

export default nextConfig;
