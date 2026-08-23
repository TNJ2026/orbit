import { defineConfig } from 'tsdown'

const pluginId = '@orbit-runtime/dsh-orbit'

export default defineConfig({
  name: `${pluginId}/client`,
  entry: { client: 'lib/client.js' },
  outDir: 'lib',
  format: 'cjs',
  platform: 'browser',
  target: 'es2022',
  dts: false,
  sourcemap: true,
  clean: false,
  deps: {
    neverBundle: specifier => specifier === 'react',
    alwaysBundle: specifier => specifier !== 'react',
  },
  outputOptions: {
    entryFileNames: 'client.js',
    banner: `window.__ModuleLoader__.load({ id: ${JSON.stringify(pluginId)}, factory: (require) => {`,
    footer: 'return module.exports; } });',
    intro: 'var module = { exports: {} }; var exports = module.exports;',
  },
})
