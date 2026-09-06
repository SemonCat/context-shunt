import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
    environment: "node",
    // A gate with zero collected cases must fail, never silently pass.
    passWithNoTests: false,
  },
});
