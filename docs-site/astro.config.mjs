// InterLock documentation site. The sidebar lives in sidebar.json so tests can
// check that every page is reachable from it without evaluating this file.
import { readFileSync } from 'node:fs';
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import starlightLinksValidator from 'starlight-links-validator';

const sidebar = JSON.parse(readFileSync(new URL('./sidebar.json', import.meta.url), 'utf8'));

// The public address, used for canonical URLs and the sitemap. A fork or a
// preview can build for another address with DOCS_SITE_URL.
const site = process.env.DOCS_SITE_URL || 'https://interlock.contextdata.dev';

export default defineConfig({
  site,
  telemetry: false,
  integrations: [
    starlight({
      title: 'InterLock',
      favicon: '/favicon.png',
      description: 'Governed access from AI agents to your data sources.',
      logo: {
        light: './src/assets/logo-light.svg',
        dark: './src/assets/logo-dark.svg',
        replacesTitle: false,
      },
      social: [
        { icon: 'github', label: 'GitHub', href: 'https://github.com/ContextData/interlock' },
      ],
      editLink: {
        baseUrl: 'https://github.com/ContextData/interlock/edit/main/docs-site/',
      },
      lastUpdated: false,
      pagination: true,
      sidebar,
      plugins: [starlightLinksValidator({ errorOnLocalLinks: false })],
      customCss: ['./src/styles/custom.css'],
    }),
  ],
});
