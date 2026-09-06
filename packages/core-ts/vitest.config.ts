import { defineConfig } from "vitest/config";

/**
 * `node:sqlite` is newer than the Node builtin list this Vite version knows, so Vite strips
 * the `node:` prefix and then fails to resolve a package called `sqlite`. Marking it
 * external by hand keeps the store loadable under the test runner; nothing about the module
 * needs transforming.
 */
const externalNodeSqlite = {
  name: "external-node-sqlite",
  enforce: "pre" as const,
  resolveId(source: string) {
    // Vite strips the `node:` prefix for modules it does not recognise as builtins, so the
    // bare form has to be caught too.
    if (source === "node:sqlite" || source === "sqlite") {
      return { id: "node:sqlite", external: true as const };
    }
    return null;
  },
};

export default defineConfig({
  plugins: [externalNodeSqlite],
  test: {
    include: ["test/**/*.test.ts"],
    environment: "node",
    // A gate with zero collected cases must fail, never silently pass.
    passWithNoTests: false,
  },
});
