import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: ['tests/**/*.test.ts'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json-summary'],
      // Coverage is measured on the calculation path: engine logic, modules,
      // registry and validation. Pure data tables (src/config) and the export
      // barrel (src/index.ts) are excluded — they carry no logic.
      include: ['src/**/*.ts'],
      exclude: ['src/config/**', 'src/index.ts', 'src/types.ts'],
      thresholds: {
        lines: 90,
        functions: 90,
        statements: 90,
        branches: 85,
      },
    },
  },
});
