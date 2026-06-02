import type { SidebarsConfig } from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  docsSidebar: [
    'index',
    {
      type: 'category',
      label: 'Install',
      items: ['install'],
    },
    {
      type: 'category',
      label: 'Usage',
      items: ['usage'],
    },
    {
      type: 'category',
      label: 'Guides',
      items: ['quickstart', 'roadmap'],
    },
    {
      type: 'category',
      label: 'Reference',
      items: ['architecture', 'open-source-product-plan', 'versioning'],
    },
  ],
};

export default sidebars;
