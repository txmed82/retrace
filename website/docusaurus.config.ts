import type * as Preset from '@docusaurus/preset-classic';
import type { Config } from '@docusaurus/types';

const LightCode = '#1d4ed8';
const DarkCode = '#60a5fa';

const config: Config = {
  title: 'Retrace',
  tagline: 'Your real users are your QA team.',
  favicon: 'img/favicon.ico',
  url: 'https://txmed82.github.io',
  baseUrl: '/retrace/',
  organizationName: 'txmed82',
  projectName: 'retrace',
  deploymentBranch: 'gh-pages',
  trailingSlash: false,
  onBrokenLinks: 'throw',
  onBrokenMarkdownLinks: 'warn',
  i18n: { defaultLocale: 'en', locales: ['en'] },
  presets: [
    [
      'classic',
      {
        docs: {
          routeBasePath: '/',
          sidebarPath: './sidebars.ts',
          editUrl: 'https://github.com/txmed82/retrace/tree/master/website/',
          exclude: ['**/study-notes/**', '**/superpowers/**'],
        },
        blog: false,
        theme: { customCss: './src/css/custom.css' },
      } satisfies Preset.Options,
    ],
  ],
  themeConfig: {
    navbar: {
      title: 'Retrace',
      logo: { alt: 'Retrace', src: 'img/logo.svg' },
      items: [
        { to: '/', label: 'Docs', position: 'left' },
        { to: '/install', label: 'Install', position: 'left' },
        { to: '/usage', label: 'Usage', position: 'left' },
        { href: 'https://github.com/txmed82/retrace', label: 'GitHub', position: 'right' },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Docs',
          items: [
            { label: 'Install', to: '/install' },
            { label: 'Usage', to: '/usage' },
            { label: 'Quickstart', to: '/quickstart' },
          ],
        },
        {
          title: 'Community',
          items: [
            { label: 'GitHub Issues', href: 'https://github.com/txmed82/retrace/issues' },
            { label: 'GitHub Discussions', href: 'https://github.com/txmed82/retrace/discussions' },
          ],
        },
      ],
      copyright: `Copyright \u00a9 ${new Date().getFullYear()} Retrace contributors. Built with Docusaurus.`,
    },
    colorMode: { defaultMode: 'dark', disableSwitch: false, respectPrefersColorScheme: true },
    prism: { theme: { plain: { color: '#1d4ed8' }, styles: [] }, darkTheme: { plain: { color: '#60a5fa' }, styles: [] }, additionalLanguages: ['bash', 'python', 'yaml', 'json'] },
    algolia: {
      appId: 'placeholder',
      apiKey: 'placeholder',
      indexName: 'retrace',
    },
  } satisfies Preset.ThemeConfig,
  plugins: [],
};

export default config;
